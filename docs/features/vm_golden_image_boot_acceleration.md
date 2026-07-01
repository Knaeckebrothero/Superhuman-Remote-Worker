# VM Golden-Image Boot Acceleration

**Status:** Proposed 2026-07-01. Not built.
**Motivation:** the loop VM override (`project_self_improvement_loop.md`) is unusable in practice because every loop VM cold-boots in ~10 min.

## Problem

Every KubeVirt VM the vm-controller provisions imports its root disk from the
container registry, from scratch, every single time:

- `vm/controller/controller.py::render_template` substitutes into
  `helm-vm-cluster/templates/vm-controller/configmap.yaml` (`vm-template.yaml`),
  whose `dataVolumeTemplates[0].spec.source` is
  `registry: { url: docker://${VM_IMAGE} }`.
- CDI's importer pod re-pulls `agent-vm-base` (~2.87 GB compressed / ~21 GB
  qcow2 target) from GHCR, unpacks it, and writes a **fresh** PVC — this path
  bypasses the node's container-image cache entirely (CDI treats the registry
  as a data source, not a runnable image).

**Observed (dev, 2026-06-30):** probe job `b1a0d1f4` and loop job `d199f0c5`
(scholar, iter 1) each took **~10–12 min** to reach `vm_status=ready`, and one
hit a flaky `ErrImportFailed` retry that roughly doubled the time. There is
**zero reuse** across VMs — the loop's scholar VM re-imported the identical
image the probe had pulled 40 min earlier.

**Impact on the loop:** with `workspace_backend=vm`, every role
(scholar/critic/developer) boots its own VM → ~10 min of dead time before
*every* step, and every import is an independent flaky-failure surface (3
consecutive failures trips the loop's `max_consecutive_failures` and stops it).
A 33-iteration loop would spend multiple hours just importing disks.

## Constraints (the vm cluster as it actually is)

- **Single node**; `rancher.io/local-path` is the only StorageClass (default),
  backed by a 2 TB SSD.
- **No VolumeSnapshotClass** — the external-snapshotter CRDs aren't installed;
  local-path supports neither CSI snapshots nor CSI volume cloning.
- Therefore **copy-on-write clones (the "seconds, space-free" path) are not
  available** without changing the storage backend — explicitly out of scope
  (no Longhorn).

## Design: golden PVC + CDI host-assisted clone

Import the base image **once** into a golden PVC per image digest, then source
each VM's root disk as a **clone** of that golden PVC instead of a registry
import. On local-path (no snapshot/CSI-clone), CDI falls back to a
**host-assisted clone** — a pod-to-pod copy, entirely **local SSD-to-SSD**: no
network pull, no unpack, no flaky GHCR retry.

### 1. Golden artifact

- One CDI `DataVolume` (→ PVC) per base image, in `agent-vms` (same namespace as
  the VMs → no cross-namespace clone RBAC), named by a DNS-safe hash of the
  image ref, e.g. `agent-vm-golden-<12hex>`.
- `source: registry: { url: docker://<image> }`, `storageClassName: local-path`,
  `storage: <VM_DISK_SIZE>`, plus annotation
  `cdi.kubevirt.io/storage.bind.immediate.requested: "true"` so it provisions
  and imports immediately (nothing "consumes" a golden except clone reads; with
  WaitForFirstConsumer it would otherwise stall).
- Labels `srw.io/golden-image: <hash>` + `srw.io/vm-image: <full ref>` for
  selection + GC.

### 2. Per-VM root disk = clone of golden

Template change in `vm-template.yaml` — replace:
```yaml
source:
  registry:
    url: docker://${VM_IMAGE}
```
with:
```yaml
source:
  pvc:
    namespace: ${VM_NAMESPACE}
    name: ${GOLDEN_PVC_NAME}
```
`${GOLDEN_PVC_NAME}` is a new placeholder the controller computes from
`${VM_IMAGE}`.

### 3. Controller: ensure-golden before create

New `_ensure_golden(image) -> golden_name` in `controller.py`, called at the top
of `_do_create` before `render_template`:

- Compute `golden_name` from the image ref.
- `GET` the golden DataVolume:
  - **Succeeded** → return name (fast path; clone proceeds).
  - **Importing/Pending** → wait (bounded, e.g. 15 min) for Succeeded — a
    concurrent create already kicked the import.
  - **Absent** → `create` the golden DataVolume (a 409 means another create won
    the race → fall through to wait), then wait for Succeeded.
  - **Failed** → delete + recreate once; if still failing → **fall back** to the
    legacy registry source for this VM (feature stays non-breaking).
- The Kubernetes API's create-409 is the concurrency lock — no extra leasing
  (single node anyway).
- Uses the existing `CustomObjectsApi` (CDI group `cdi.kubevirt.io`, version
  `v1beta1`, plural `datavolumes`).

### 4. Garbage collection

- After a successful `_ensure_golden` for the current image, sweep goldens whose
  `srw.io/vm-image` ≠ current `DEFAULT_VM_IMAGE` **and** that have no VM
  referencing them; delete those PVCs to reclaim SSD.
- **Never** delete a golden with an in-flight clone (host-assisted clone reads
  the source; deleting mid-clone corrupts the target). Guard with a
  "last-used" annotation bumped on each clone + a min-age.
- The 2 TB SSD comfortably holds the golden (~20 Gi) + N in-flight full-copy
  clones (~20 Gi each); GC is hygiene, not a hard requirement.

### 5. Optional pre-warm (nice-to-have)

The first VM after a new base-image deploy still pays the one-time golden import
on its critical path. To remove even that: the controller, on startup and on
detecting a new `DEFAULT_VM_IMAGE`, proactively `_ensure_golden` in the
background so the golden is warm before the first real job. (A CDI
`DataImportCron` is the k8s-native equivalent but assumes a moving tag; SRW pins
by sha, so controller-driven ensure fits better.)

### 6. Feature flag + rollout

- `VM_GOLDEN_IMAGE_ENABLED` (vm-controller env; Helm value
  `vmController.goldenImage.enabled`), default **off** → current
  registry-per-VM behavior. On → golden-clone path with legacy fallback on any
  golden error.
- Ship off, enable on the vm cluster, measure, then default-on after a soak.

## Honest limitations (what this buys and what it doesn't)

- **Time + reliability, not space.** Host-assisted clone on local-path is a
  *full copy* — each VM still consumes ~20 Gi (no CoW savings). The win is
  wall-clock (local SSD copy vs network import) and reliability (the flaky
  import happens once per image, not once per VM).
- **Not "instant."** Expect **~30–60 s** clone+boot for a sparse few-GB qcow2 on
  SSD (copy + pod scheduling), vs ~10 min today. Single-digit-second boots need
  CoW → a snapshot-capable local CSI (LVM-thin / ZFS-localpv) or `containerDisk`
  (see Alternatives).
- **First boot per new image** still pays one ~10-min import (mitigated by
  pre-warm).
- **Crash-recovery preserved.** The rootdisk stays a normal independent PVC, so
  the existing VM snapshot/reattach recovery path is unaffected (unlike
  containerDisk, which would forfeit it).

## Alternatives considered

- **containerDisk (ephemeral, node-cached).** Boots off the node's cached
  container image with an ephemeral overlay → near-instant, and it's the mode
  that matches the "k8s caches it" intuition. Rejected as primary: it makes the
  root disk ephemeral, forfeiting the PVC-based crash-recovery the VM path
  relies on. Revisit if we decide loop VMs should be fully disposable (state
  already lives in git + KB).
- **LVM-thin / ZFS-localpv CSI for real CoW.** True seconds-fast, space-efficient
  clones on the single node without Longhorn. Deferred: it's a storage-backend
  change (repurpose the 2 TB SSD as a thin pool / zpool). This is the natural
  upgrade if host-assisted copy time proves too slow — **the golden-image design
  above is unchanged; only the StorageClass swaps and clones become CoW.**
- **qcow2 backing-file overlay (hand-rolled CoW via hostDisk).** Instant +
  space-efficient, but bypasses CDI/PVC, needs privileged hostPath disk
  management, and breaks the clean model + recovery. Not worth it vs LVM/ZFS.
- **Warm VM pool.** Pre-boot N idle VMs, hand out instantly. Orthogonal; costs
  idle compute; can layer on later.
- **Shrink the ~21 GB base image.** Complementary — speeds the one-time golden
  import and the per-VM copy proportionally. Cheap follow-up.

## Acceptance criteria

1. Flag on: the **2nd and subsequent** VMs for the same base image reach
   `vm_status=ready` in **< ~90 s** (target ~30–60 s), with **no per-VM GHCR
   pull** (rootdisk DV shows a `pvc`/clone source; no importer pod for it).
2. The base image is imported from the registry **exactly once per digest**;
   concurrent VM creates do not trigger duplicate imports.
3. A new `DEFAULT_VM_IMAGE` sha transparently triggers one new golden import,
   after which VMs clone from it; stale goldens are GC'd.
4. Golden import failure **falls back** to the legacy registry-per-VM path — VMs
   still boot (no hard regression).
5. VM crash-recovery (snapshot/reattach) still works with cloned rootdisks.
6. Flag off = byte-for-byte current behavior.

## Verification plan

- **k3d can't test this** (no KubeVirt/CDI) — verify on the dev + vm clusters.
- Measure `job created → vm_status=ready` for (a) cold first VM of a new image,
  (b) 2nd/3rd VM of the same image; compare to the ~10-min baseline captured
  2026-06-30 (`b1a0d1f4`, `d199f0c5`).
- Confirm per-VM rootdisks use a clone, not a registry import (CDI/importer logs,
  DV `.spec.source`).
- Soak the loop (`workspace_backend=vm`, Better Resavio); confirm steps advance
  at clone-speed with no flaky per-VM import failures.

## Files touched (estimate)

- `helm-vm-cluster/templates/vm-controller/configmap.yaml` — DV source
  `registry` → `pvc` (+ `${GOLDEN_PVC_NAME}` placeholder).
- `vm/controller/controller.py` — `_ensure_golden`, `${GOLDEN_PVC_NAME}`
  substitution, GC sweep, startup pre-warm, flag.
- `helm-vm-cluster/templates/vm-controller/{deployment.yaml,rbac.yaml}` +
  `values.yaml` — `VM_GOLDEN_IMAGE_ENABLED` / `vmController.goldenImage.*`; RBAC
  for `datavolumes` + `persistentvolumeclaims` (create/get/list/delete) if not
  already granted.
- `tests/test_vm_controller.py` — golden-ensure states (absent/importing/
  succeeded/failed→fallback), name derivation, GC guard, template clone-source
  rendering.
- **CI gotcha:** the vm-cluster chart change-detection only diffs `helm/`, not
  `helm-vm-cluster/` (`project_workspace_tier_upgrade` note) — touch `helm/` or
  the controller image to force a vm-cluster republish.
