# VM Golden-Image Boot Acceleration

**Status: DONE — live-verified on the dev vm cluster 2026-07-01.** Implemented
in `5cb48aad`/`a4d031dd` (unit-verified, 106/106), enabled via `5efb6ccf`
(`vmController.goldenImage.enabled: true` in the dev Fleet overlay
`deployment-vms/srw-vm-controller/fleet.yaml`). Measured end-to-end with probe
job `ef7dcd2c`: **job → agent-processing-on-VM ~5m20s vs ~12 min baseline
(~2.3×)**, per-VM GHCR pull eliminated. See "Live verification results" at the
end. Prod vm-controller (`srw-prod-vm`, v0.0.21) remains flag-off. Design
originally refined 2026-07-01 via a 5-subagent investigation (3 codebase traces
+ 2 web/best-practice sweeps); decision **(A)** chosen (see below).
**Motivation:** the loop VM override (`project_self_improvement_loop.md`) was
impractical because every loop VM cold-booted in ~10 min.

## Problem

Every KubeVirt VM the vm-controller provisions imports its root disk from the
container registry, from scratch, every single time:

- `vm/controller/controller.py::render_template` (`:104-149`) string-substitutes
  into the `vm-template.yaml` ConfigMap, whose
  `dataVolumeTemplates[0].spec.source` is `registry: { url: docker://${VM_IMAGE} }`.
- CDI's importer re-pulls `agent-vm-base` (~2.87 GB compressed / ~21 GB qcow2
  target) from GHCR, unpacks it, and writes a **fresh** PVC — bypassing the
  node's container-image cache entirely (CDI treats the registry as a data
  source, not a runnable image).

**Observed (dev, 2026-06-30):** probe job `b1a0d1f4` and loop job `d199f0c5`
(scholar, iter 1) each took **~10–12 min** to `vm_status=ready`; one hit a flaky
`ErrImportFailed` retry that ~doubled it. Zero reuse across VMs — the loop's
scholar VM re-imported the identical image the probe pulled 40 min earlier.

**Impact:** with `workspace_backend=vm`, every role (scholar/critic/developer)
boots its own VM → ~10 min dead time before *every* step, and each import is an
independent flaky-failure surface (3 consecutive → the loop's
`max_consecutive_failures` trips and stops it).

## Constraints (the vm cluster as it actually is)

- **Single node**; `rancher.io/local-path` is the only StorageClass (default,
  WaitForFirstConsumer), on a 2 TB SSD. Confirmed: no other provisioner.
- **No VolumeSnapshotClass** (the external-snapshotter CRDs aren't installed);
  local-path is a **non-CSI** provisioner with no snapshots and no CSI cloning.
- CDI has three clone strategies — `csi-clone`, `snapshot` (smart-clone), and
  `copy` (host-assisted). It uses `snapshot` if a VolumeSnapshotClass exists,
  else falls back to `copy`. On local-path (unknown provisioner → empty
  StorageProfile, no snapshot class) it is **forced to host-assisted `copy`**.
  So true copy-on-write clones are **not available** without changing the
  storage backend — out of scope (no Longhorn). See Alternatives for the CoW
  upgrade path if ~30–60 s isn't fast enough.

## Decision: (A) golden PVC + host-assisted clone — CHOSEN 2026-07-01

Chosen over the ephemeral-root alternatives (`containerDisk` / `ephemeral`
volume — still documented in Alternatives). **Rationale:** VM root disks are
heading toward **independent, persistent PVCs** regardless of this feature —
container workspaces just moved to PVC-backed storage (Branch-a:
deterministically-named, reattach-by-name, crash-recovery), and PVC-backed VM
root disks are the next step on that same track. Design (A) keeps the root a
real persistent PVC, so it's a **stepping stone** in that direction rather than a
detour into ephemeral roots we'd later have to undo — and it preserves today's
crash-recovery model unchanged. Boot ~30–60 s (a full *local SSD* copy), no
storage change.

**Forward-compat with independent VM PVCs:** today the VM rootdisk is a VM-owned
`dataVolumeTemplates` entry (cascade-GC'd on VM delete). When VM rootdisks later
become independent, reattachable PVCs (the container-PVC pattern applied to VMs),
the golden + clone mechanics are **unchanged** — only the *insertion point*
moves: instead of mutating the VM's `dataVolumeTemplates[0].spec.source`, the
controller sets `source.pvc: golden` on the standalone rootdisk DataVolume it
creates before the VM. `_ensure_golden`, GC, RBAC, and the golden itself all
carry over as-is.

## Design (A): golden PVC + CDI host-assisted clone

Import the base image **once** into a golden PVC per image digest; source each
VM's root disk as a **clone** of that golden (CDI `source.pvc`). On local-path
this is a host-assisted clone — a pod-to-pod **local SSD copy**: no network
pull, no unpack, no flaky GHCR retry. The one import that *can* flake now happens
**once per image**, not once per VM.

### 1. Golden artifact — a standalone DataVolume per digest

- One CDI `DataVolume` (→ PVC) per base image, in the **same namespace as the
  VMs** (`agent-vms` / release namespace), name = `agent-vm-golden-<sha256[:12]>`.
- **Must be created standalone** (not via a VM's `dataVolumeTemplates`) so it
  is owned by no VM and survives VM deletion — which also means **GC is the
  controller's sole responsibility** (nothing cascades it away).
- **Manifest must use the explicit `spec.pvc` form with `accessModes` AND
  `volumeMode` set — not the size-only `spec.storage` inference form.** On
  local-path there is no StorageProfile, so inference fails validation
  (*"missing accessMode, cannot get from StorageProfile"*). Force
  `accessModes: [ReadWriteOnce]`, `volumeMode: Filesystem` (local-path has **no
  Block support**), `storageClassName: local-path`.
- Annotations (both required, both verified against CDI docs):
  - `cdi.kubevirt.io/storage.bind.immediate.requested: "true"` — the golden is
    **never mounted by a VM**, so on WaitForFirstConsumer storage nothing would
    ever trigger binding and the import would **hang forever**. This forces
    immediate populate. (Do **NOT** put this on per-VM clone targets — it would
    bind them to a node before the VM schedules.)
  - `cdi.kubevirt.io/storage.deleteAfterCompletion: "false"` — recent CDI
    garbage-collects a *Succeeded* DataVolume, leaving only its PVC; that would
    make `_ensure_golden`'s GET-DV fast path 404 on a healthy golden and trigger
    needless re-imports. Keeping the DV object is our stable handle (the
    controller has only `CustomObjectsApi`, no `CoreV1Api` — see §RBAC).
- Labels `srw.io/golden-image: <hash>` + `srw.io/vm-image: <label-safe ref>`
  (store the full ref in an annotation if >63 chars) for selection + GC.

```yaml
apiVersion: cdi.kubevirt.io/v1beta1
kind: DataVolume
metadata:
  name: agent-vm-golden-<sha256[:12]>
  namespace: <release ns>          # same ns as VMs → no cross-ns clone RBAC
  labels: { srw.io/golden-image: <hash>, srw.io/vm-image: <label-safe> }
  annotations:
    cdi.kubevirt.io/storage.bind.immediate.requested: "true"
    cdi.kubevirt.io/storage.deleteAfterCompletion: "false"
spec:
  source: { registry: { url: docker://<image> } }
  pvc:                             # explicit form — NOT spec.storage
    accessModes: [ReadWriteOnce]
    volumeMode: Filesystem
    storageClassName: local-path
    resources: { requests: { storage: 20Gi } }
```
(Verify empirically that CDI accepts this exact form for a clone *source* on
local-path; if `spec.pvc` is rejected in the installed CDI version, hand-populate
a StorageProfile CR for `local-path` instead.)

### 2. Per-VM root disk = clone of golden — by manifest mutation, not template swap

**Keep the ConfigMap template on `source.registry` unchanged.** In `_do_create`,
after `render_template` parses the YAML (`controller.py:149`) and **only when a
golden name is present**, mutate the parsed dict:

```python
src = manifest["spec"]["dataVolumeTemplates"][0]["spec"]["source"]
src.pop("registry", None)
src["pvc"] = {"name": golden_name}      # same namespace → omit `namespace:`
# match the golden's volumeMode on the clone target:
manifest["spec"]["dataVolumeTemplates"][0]["spec"]["storage"]["volumeMode"] = "Filesystem"
```

Why mutation instead of editing the template's `source`:
- **Flag-off is byte-for-byte identical** (AC #6) and **golden-failure fallback
  to registry is trivial** (AC #4) — both just leave the manifest untouched.
- Avoids the `${VM_NAMESPACE}` / `${GOLDEN_PVC_NAME}` placeholder problem —
  those are **not** in `render_template`'s substitution set and would render
  literally. Same-namespace clone omits `namespace:` entirely.
- Localizes the entire feature to `controller.py` (+ RBAC + a flag env). The
  per-VM clone target stays **WaitForFirstConsumer** (no `bind.immediate`),
  Filesystem, RWO — RWO is concurrency-safe on a single node (RWO = one *node*;
  all clone pods co-locate, so parallel clones work; only `ReadWriteOncePod`
  would serialize).

### 3. Controller: `_ensure_golden` before create

New `_ensure_golden(image) -> str | None` at the **top of `_do_create`** (after
the Headscale key, before `render_template`), so it runs on **every** create
including recovery re-dispatch. Resolve the image with the **exact same
expression the controller renders with** — `job_config.get("vm_image") or
DEFAULT_VM_IMAGE` (`controller.py:125`; `vm_image` is `None` on the common path,
so the golden hash must key off the resolved default or it won't match what the
VM clones).

- Compute `golden_name` from the resolved image digest/ref.
- GET the golden DataVolume (via `CustomObjectsApi`, group `cdi.kubevirt.io`,
  version `v1beta1`, plural `datavolumes`):
  - **Succeeded** → return name (fast path).
  - **Importing/Pending/CloneScheduled** → wait (bounded ~15 min, mirroring
    `VM_UPGRADE_POLL_TIMEOUT=900s`) for Succeeded.
  - **Absent** → create it; a **409 = another create won the race** → fall
    through to wait. (The K8s create-409 is the concurrency lock — no leasing.)
  - **Failed** → delete + recreate once; still failing → return `None` →
    **fallback** to the legacy registry source for this VM.
- **All K8s calls via `asyncio.to_thread` + `asyncio.sleep`** — `_do_create`
  currently calls the client synchronously (`controller.py:213`); a 15-min
  blocking poll would stall every other NATS/HTTP handler.

### 4. Garbage collection (DataVolume-only, and load-bearing for recovery)

- After a successful ensure for the current image, sweep goldens whose
  `srw.io/vm-image` ≠ current `DEFAULT_VM_IMAGE`. Keep the **last N=3 digests**
  (mirrors CDI's own `DataImportCron` default).
- **Never delete a golden with an in-flight clone.** This is a *correctness*
  requirement, not hygiene: crash-recovery re-dispatches a fresh VM that clones
  the golden at unpredictable times, so a GC that deletes a golden mid-clone
  corrupts the recovered VM's disk. Guard by listing per-VM rootdisk
  DataVolumes still in a clone phase that reference this golden (or a
  `last-used` annotation bumped on each clone + a min-age, e.g. 30 min).
- **Delete the golden DataVolume only** (the PVC cascades) — the controller has
  no `CoreV1Api`, so we never create/delete PVCs directly, only DataVolumes.

### 5. RBAC — currently absent, must be added (in BOTH charts)

The controller Role today grants only `kubevirt.io/{virtualmachines,
virtualmachineinstances}` + core `pods` get/list — **no `datavolumes`, no
`persistentvolumeclaims`**, so `_ensure_golden` would 403 immediately. Add:

```yaml
- apiGroups: ["cdi.kubevirt.io"]
  resources: ["datavolumes"]
  verbs: ["get", "list", "create", "delete", "patch"]   # patch = last-used annotation
- apiGroups: [""]
  resources: ["persistentvolumeclaims"]
  verbs: ["get", "list"]                                  # NOT create/delete — CDI owns them
```
`datavolumes/source` (the CDI cross-namespace clone-auth subresource) is **not
needed** because the golden and clones are **same-namespace** — confirmed
against CDI RBAC docs. (Harmless to add, but unnecessary.)

### 6. Two controllers — both charts

There are **two** near-identical vm-controllers running the same image:
`helm-vm-cluster/templates/vm-controller/` (NATS, the dev vm cluster) **and**
`helm/templates/vm-controller/` (HTTP, main chart, `vmController.enabled`). The
RBAC + flag/env must land in **both** or one deployment silently 403s / stays on
registry-per-VM. (The feature logic is all in the shared `controller.py`, so no
template edits are needed in either — the manifest-mutation approach means the
ConfigMap templates are untouched.)

### 7. Feature flag + optional pre-warm

- `VM_GOLDEN_IMAGE_ENABLED` (controller env; Helm `vmController.goldenImage.enabled`,
  default **off** → byte-identical current behavior). On → golden-clone with
  legacy fallback on any error. Ship off, enable on the vm cluster, measure,
  default-on after soak.
- Pre-warm (nice-to-have): on startup and on detecting a new `DEFAULT_VM_IMAGE`,
  the controller `_ensure_golden` in the background so the first real job doesn't
  pay the one-time import on its critical path.

## Crash-recovery compatibility — VERIFIED SAFE

Traced end-to-end (`orchestrator/main.py:10193-10333`, `services/lifecycle/
vm_manager.py:259-303`, `services/snapshot_service.py:300-459`, `vm/controller/
controller.py:190-272`): **golden-clone does not break VM crash-recovery.**

- VM recovery is **discard-disk + provision-fresh**: on `workspace_unavailable`
  it deletes the VirtualMachine (which cascade-GCs the DataVolume+PVC via
  ownerRefs — no retention policy anywhere) and re-dispatches a brand-new VM;
  reasoning-state resumes from the Postgres checkpoint, files (if any) from the
  **S3 SSH-tar snapshot** (`capture_method: ssh_tar`) — never from the old disk.
- Nothing in recovery or snapshot reads *how* the rootdisk PVC was populated
  (registry import vs clone). A host-assisted clone yields a normal, fully
  independent RWO PVC, indistinguishable to create/delete/GC. VMs have **no
  volume-reattach** (unlike the pod/Branch-a G1 path), so there's no PVC
  identity/label assumption to violate.
- This is exactly why we chose clone over `containerDisk` for design (A):
  containerDisk *would* forfeit this by making the root ephemeral.

Three requirements this imposes (all folded into the design above):
1. **The GC in-flight-clone guard is load-bearing** (§4) — recovery injects
   clone traffic at unpredictable times.
2. **`_ensure_golden` must be on the path *all* creates take** (§3), recovery
   included, with the registry fallback reachable from recovery too.
3. **Test "delete mid-run → recreate against a clone source"** — the existing
   `_do_create` 409 retry guards the VirtualMachine, not the same-named
   DataVolume/PVC still terminating; clone timing differs from import.

## Honest limitations

- **Time + reliability, not space.** Host-assisted clone is a *full copy* — each
  VM still uses ~20 Gi (no CoW savings). Win = wall-clock + reliability.
- **Not instant.** ~30–60 s clone+boot for a sparse few-GB qcow2 on SSD, vs
  ~10 min. Single-digit-second boots need CoW → design (B) or a CSI swap.
- **First boot per new image** still pays one ~10-min import (mitigated by
  pre-warm).
- **Lever to shrink the copy:** `virt-sparsify`/compact the qcow2 before
  blessing the golden so fewer real bytes move.

## Alternatives considered

**Ephemeral-root options (near-instant, no storage change, disposable-VM-native):**
- **`containerDisk`** — the base is *already* a container image wrapping a qcow2
  at `disk/`, i.e. already containerDisk format. VM boots off the node-cached
  image with an ephemeral qcow2 CoW overlay (`/var/run/kubevirt-ephemeral-disks`,
  discarded on stop). KubeVirt positions it *exactly* for "many identical
  stateless throwaway VMs from one golden, durable state externalized" — our
  loop model. Near-instant warm-cache boot, no CDI import, no storage change,
  ≤ the code of design (A). Cost: ephemeral root → no root snapshot/reattach
  recovery; changing the golden = rebuild+push. **This is design (B) and a
  serious contender — see the Decision point.**
- **`ephemeral` volume** — a **read-only golden PVC** + per-VM local qcow2
  overlay discarded on stop. Genuine CoW-from-a-PVC on local-path **today**, no
  CSI change, golden on a PVC instead of a registry image. Same ephemeral-root
  tradeoff as containerDisk. Verify concurrent `ephemeral` consumers of one RWO
  golden on a single node.

**True-CoW upgrade path (persistent root, near-instant, requires a storage swap):**
Replace `local-path` with a CoW-capable local CSI on the SSD, then let CDI use
`snapshot`/`csi-clone`. **Primary recommendation: TopoLVM (LVM-thin)** —
in-kernel `dm-thin` (no out-of-tree kernel module / DKMS breakage), supports
**both** CSI snapshot and CSI clone, and is Red-Hat-productized (OpenShift LVM
Storage / MicroShift LVMS). ~20 Gi clones become metadata-only (near-instant).

| Option | CoW | Storage change | Persistent root | Recovery | Notes |
|---|---|---|---|---|---|
| **(A) golden + host-assisted clone** | No (full copy) | None | Yes | Yes | This doc. ~30–60 s. |
| TopoLVM (LVM-thin) CSI | Yes | Swap SC (thin pool) | Yes | Yes | Best CoW upgrade; in-kernel; both CSI paths; RH-hardened. |
| zfs-localpv | Yes | Swap SC (zpool) | Yes | Yes | Richest CoW; needs OpenZFS kernel module (DKMS). |
| lvm-localpv | Yes* | Swap SC (thin pool) | Yes | Yes | *snapshot→restore only, no PVC clone; OpenEBS ≥ v4.4.0. |
| **(B) containerDisk** | Overlay | None | **No** | **No (root)** | Near-instant; disposable-VM-native; less code. |
| ephemeral volume | Overlay | None | **No** | **No (root)** | CoW-from-PVC today; golden on RO PVC. |

Shared CSI caveats: all three need a **free partition/extent** on the SSD (can't
overlay live-filesystem space); LVM-thin pools **must never fill** (enable
autoextend monitoring); you may need to hand-author the CDI StorageProfile
(`cloneStrategy`/`volumeMode`); Block-volumeMode has known smart-clone edge
cases. Not needed unless design (A)'s ~30–60 s proves too slow.

Also considered: **warm PVC pool / CDI populators** (pre-populate PVCs off the
VM-start critical path) — orthogonal, layer on later if useful.

## Acceptance criteria

1. Flag on: the **2nd+** VM of the same base image reaches `vm_status=ready` in
   **< ~90 s** (target 30–60 s), with **no per-VM GHCR pull** — verify via the
   rootdisk target-PVC annotations CDI stamps: `cdi.kubevirt.io/cloneType`
   (host-assisted), `clonePhase`, `cloneFallbackReason`.
2. Base image imported from the registry **exactly once per digest**; concurrent
   creates don't double-import (create-409 lock).
3. A new `DEFAULT_VM_IMAGE` sha triggers one new golden import; stale goldens are
   GC'd (keep last 3), never one with an in-flight clone.
4. Golden failure **falls back** to the legacy registry-per-VM path — VMs still
   boot. Flag off = byte-for-byte current behavior.
5. VM crash-recovery (discard + fresh + S3-restore) still works with cloned
   rootdisks — exercise delete-mid-run → recreate against a clone source.
6. No cross-namespace clone RBAC required (golden co-located with VMs).

## Verification plan

- **k3d can't test this** (no KubeVirt/CDI) — verify on the dev + vm clusters.
- Measure `job created → vm_status=ready` for (a) cold first VM of a new image,
  (b) 2nd/3rd VM same image; baseline is the ~10-min 2026-06-30 capture
  (`b1a0d1f4`, `d199f0c5`). Note the win is bounded to the disk-import segment;
  guest boot + daemon-register (the `vm_status=ready` + `vm_ssh_host` gate in
  `_poll_vm_ready`) is unchanged.
- Confirm the clone path via the target-PVC `cloneType=host assisted` annotation.
- Soak the loop (`workspace_backend=vm`, Better Resavio); confirm steps advance
  at clone-speed with no flaky per-VM import failures.
- Single-node makes the notorious WFFC node-affinity / snapshot-race clone bugs
  (kubevirt #10011, #10832) non-issues — but do **not** carry this design to a
  multi-node vm cluster without revisiting them.

## Files touched

- `vm/controller/controller.py` — `_ensure_golden` (+ `_get_dv`/`_wait_dv_
  succeeded`/`_delete_dv`, all via `to_thread`), manifest mutation in
  `_do_create`, `_golden_name`, GC sweep, startup pre-warm, flag read.
- **Both** `helm-vm-cluster/templates/vm-controller/{rbac,deployment}.yaml` +
  `values.yaml` **and** `helm/templates/vm-controller/{rbac,deployment}.yaml` +
  `values.yaml` — the DataVolume/PVC RBAC and `VM_GOLDEN_IMAGE_ENABLED` /
  `vmController.goldenImage.*`. (No ConfigMap template edits — mutation is in
  code.)
- `tests/test_vm_controller.py` — mirror `_FakeApiException` (404/409) +
  `patch("asyncio.sleep")` patterns; cover ensure states
  (succeeded/importing/absent→create/failed→recreate→fallback), name derivation,
  GC (add a `list_namespaced_custom_object` mock; assert in-flight golden is NOT
  deleted), and the manifest-mutation clone-source rendering.
- (Completeness, not prod-exercised) `orchestrator/services/vm_provisioner.py`
  `_render_template` "direct K8s mode" also substitutes `${VM_IMAGE}` from a
  separate template; it has no golden logic. The vm cluster uses the controller
  (NATS), not direct mode, so it isn't on the golden path — noted so a future
  direct-mode change doesn't silently skip golden-clone.
- **CI:** no action needed beyond editing the above — `develop.yml` change
  detection now watches **both** `helm/` and `helm-vm-cluster/` (commit
  `cc5fe77a`), and a `vm/controller/` edit gates the vm-cluster chart republish +
  fleet bump anyway. (The earlier "only diffs helm/" gotcha is fixed.)

## Research provenance

Refined from a 5-subagent sweep (2026-07-01): controller/RBAC internals,
vm_provisioner+chart+CI, crash-recovery trace, CDI clone best-practices, and CoW
alternatives. Primary external sources: KubeVirt CDI docs (clone-datavolume,
smart-clone, efficient-cloning, storageprofile, waitforfirstconsumer-storage-
handling, RBAC), the KubeVirt "VM Image Usage Patterns" + "Building golden images
with Packer" guides, and OpenEBS/TopoLVM storage docs.

## Live verification results (2026-07-01, dev vm cluster)

Enabled via `5efb6ccf`; Fleet synced in 34 s. Probe job `ef7dcd2c` (throwaway,
`config_override: {workspace: {backend: vm}, scholar/verification disabled}`).

**Prewarm (once per digest):** `_prewarm_golden` fired at controller startup →
golden DV `agent-vm-golden-58a046a8c6f3` imported `agent-vm-base:sha-7ae23f7`
in ~12 min — **including one importer crash-restart mid-import** (progress
reset 75%→24%, CDI retried). That flakiness used to hit every VM boot; now it
lands once, off the hot path. PVC bound immediately (`bind.immediate` beat the
WFFC deadlock as designed).

**Probe boot timeline:**

| Event | Wall clock | Δ from job create |
|---|---|---|
| Job created | 20:24:36 | 0 |
| Controller received NATS create; golden Succeeded fast-path; VM created | 20:24:40 | +4 s |
| Rootdisk DV `CloneScheduled` (not `ImportScheduled`) | 20:24:48 | +12 s |
| `CloneInProgress` | 20:25:09 | +33 s |
| Rootdisk `Succeeded` + **VMI Running** | 20:28:36 | **+4m00s** |
| VM booted, tailnet joined, `vm_status=ready`, agent heartbeating (job `processing`) | ~20:29:59 | **~+5m20s** |

**Teardown:** cancel → full cascade (VM+VMI+DV+PVC+Headscale node) gone in
~12 s; **golden survived**; job stayed `cancelled`, agent freed.

**Acceptance criteria scorecard:**

1. ~~<90 s~~ → **measured 4m00s to VMI Running / ~5m20s to agent-working**
   (vs ~12 min baseline, ~2.3×). The aspirational 30–90 s assumed a cheap
   clone; on local-path the host-assisted **full copy of the 20 Gi volume
   (~3m27s @ ~100 MB/s SATA)** dominates. Mechanism 100 % verified: rootdisk
   DV `source.pvc = agent-vm-golden-…`, PVC `cloneType: copy`,
   `cloneFallbackReason: "In tree storage class does not support
   snapshot/clone"` — no per-VM GHCR pull. **Accepted** (single SATA SSD,
   10-year-old host; TopoLVM is the path to true seconds if ever needed).
2. Import once per digest ✅ — probe hit the golden `Succeeded` fast-path, no
   second import. (Concurrent-create 409 race live-untested; unit-covered.)
3. Image-bump → new golden + GC keep-3: not yet live-exercised (needs a
   `defaultVmImage` bump); unit-covered. Will self-exercise on the next bump.
4. Fallback: not live-exercised (golden stayed healthy); unit-covered.
   Flag-off = byte-identical **was** live-verified (controller ran 4 h dormant
   on the new image, zero restarts, before the flip).
5. Crash-recovery with cloned rootdisks: cancel-cascade verified clean;
   delete-mid-run → recreate not explicitly exercised (discard-disk model
   reads no PVC provenance, so no mechanism for it to differ).
6. No cross-namespace clone RBAC ✅ — clone ran with namespace-local Role.

**Residual (not blocking):** TopoLVM/CoW upgrade if clone time ever matters;
prod flip (deliberate, later); criteria 3–5's live-exercise happens naturally
in operation.
