# Per-job persistent VM rootdisks — reattach-on-recreate ("Branch a for VMs")

**Status: IMPLEMENTED 2026-07-29 (Phases 0-2), flag-gated OFF, live gate
owed.** Scope widened from the original proposal — see "Two false premises,
corrected" below. Commits: `51c0bc71` (Phase 0, controller), `f4d05e07`
(Phase 1, orchestrator), `dc3d3f8d` (session extension), `3de31025` (Phase 2,
GC). Originally written 2026-07-02 as PROPOSED.

Companion to:
- [`workspace_pvc_branch_a_implementation.md`](workspace_pvc_branch_a_implementation.md) — the
  container-pod equivalent this mirrors (PVC named by job UUID, reattach on
  recreate, terminal GC, capacity guard). VMs were an explicit non-goal there.
- [`../done/vm_golden_image_boot_acceleration.md`](../done/vm_golden_image_boot_acceleration.md) —
  the prerequisite that shipped 2026-07-01. Design (A) was chosen over
  containerDisk *because* "VM roots are heading to independent persistent
  PVCs"; this doc is that step.

**One-line model:** the VM rootdisk becomes a standalone, deterministically
named DataVolume (`agent-vm-<jobid>-rootdisk` — the name the VM template
already uses) that is created once (clone-from-golden), **survives VM
deletion**, and is reattached by name on any recreate. The Postgres LangGraph
checkpoint carries reasoning state; the rootdisk carries the files (and the
whole environment: apt installs, repos, caches); on a crash the new VM boots
on the old disk and resumes the checkpoint — coherent by construction, no
snapshot/restore dance. Purge happens when the job goes terminal.

---

## Problem

Workspace **pods** just got crash-safe storage (Branch a): a mid-job pod crash
deletes the pod but keeps the PVC, the re-dispatched pod reattaches it by
name, and the agent resumes from its Postgres checkpoint on intact files.

**VM jobs have the weakest recovery story in the system**, and with the
all-VM loop ([loop VM override](../done/) + golden image) VM jobs are about to
become the *common* case:

| | Pod job (Branch a, live) | VM job (today) |
|---|---|---|
| Disk | standalone PVC `pvc-workspace-<id[:12]>` | DV via `dataVolumeTemplates` → **owned by the VM object** |
| VM/pod deleted | PVC retained | DV + PVC **cascade-deleted** (live-verified: cancel → all gone in ~12 s) |
| Crash re-dispatch | reattach by name, files intact | fresh VM = **pristine golden clone**, files gone |
| File restore on recovery | not needed | **none** — `_resume_job_on_agent` (`main.py:2460`) has no restore step |
| What survives a mid-job crash | everything | only graph state (Postgres checkpointer, D3) |

Code anchors:

- The crash-recovery split: `orchestrator/main.py:10329` (`workspace_unavailable`
  handler). Pod arm (G1) reattaches; the VM arm (`main.py:10423`, "legacy
  path, unchanged") calls `delete_vm` — destroying the disk — then re-queues.
  It also *replaces* `context.vm` with `{requested, recovering}`
  (`main.py:10444`), wiping the SSH coordinates.
- The gap is named in the code:
  `orchestrator/services/lifecycle/vm_manager.py:262` — *"VMs have no
  volume-reattach (delete is destructive; disks live behind the external VM
  controller) — so the terminal action is force-delete. Volume-reattach for
  VMs is a separate, controller-side design."* This doc is that design.
- **The S3 snapshot net does not cover crashes.** `capture_vm_snapshot`
  (SSH-tar → S3) fires only at graceful points: pause (`main.py:3566/3579`),
  release/complete (`vm_provisioner.py:365`), idle suspension
  (`workspace_suspension.py`), and the reconciler reaping a
  dirty-but-*reachable* VM (`vm_manager.py:280`). A hard crash means the VM
  is unreachable → no snapshot possible at that moment; a job that ran
  straight through has **no snapshot at all**. The one restore-on-resume path
  (`main.py:8498`) runs only on the manual `/resume` endpoint and only when
  `context.vm` still has an `ssh_host` — which the crash arm just wiped.

So a mid-job VM crash is *worse* than "back to the backup": it is back to an
empty workspace with a checkpoint that believes it is mid-phase-N.
Loop jobs are partially masked (repo compounding pushes state to git/KB), but
anything uncommitted is lost, and non-loop VM jobs have no mask at all.

Note `runStrategy: RerunOnFailure` already handles *guest* crashes (KubeVirt
restarts the VMI on the same disk in-place). The unprotected case is VM
*object* deletion — exactly what orchestrator crash recovery, the reconciler
(`drain`/`give_up`), and node-level failures do.

## Why the fix is small

The golden-image work built almost everything this needs:

1. **The rootdisk already has a deterministic per-job name.** The VM template
   (`helm*/templates/vm-controller/configmap.yaml`) declares
   `dataVolumeTemplates[0].metadata.name: agent-vm-${JOB_ID}-rootdisk` and
   references it via `volumes[].dataVolume.name` — *by name*. Creating the DV
   standalone (not templated) keeps the `volumes` section byte-identical.
2. **The controller already speaks CDI**: `_get_dv` / `_delete_dv` /
   `_wait_dv_succeeded` / `_golden_dv_manifest` / clone mutation
   (`vm/controller/controller.py`), all `asyncio.to_thread`-wrapped.
3. **RBAC is already done**: both charts' vm-controller Roles have
   `datavolumes` get/list/create/delete/patch + `persistentvolumeclaims`
   get/list (added for the golden feature).
4. **Free speed win**: a recovery boot *skips the ~3m27s host-assisted clone
   entirely* (the disk already exists). Crashed VM jobs come back in roughly
   VMI-boot time (~90 s observed VMI-start→SSH-ready), i.e. **recovery becomes
   faster than a fresh start** (~5m20s job→processing).

---

## Design

### D1 — standalone rootdisk DV, flag-gated manifest mutation (controller)

New flag `VM_PERSISTENT_ROOTDISK` (helm: `vmController.persistentRootdisk.enabled`,
default **false**). In `_do_create` (`vm/controller/controller.py:220`), when ON:

1. Compute `rootdisk_name = f"agent-vm-{job_id}-rootdisk"` (unchanged name).
2. `_get_dv(rootdisk_name)`:
   - **Succeeded** → *reattach*: skip creation AND skip the golden clone
     entirely. Log `rootdisk reattach: <name>` (observability for acceptance).
   - **Failed** → delete + recreate (mirror `_ensure_golden`'s Failed
     handling).
   - **in-progress phase** → adopt it (another create raced; KubeVirt gates
     VMI start on DV readiness anyway).
   - **absent** → create a standalone DV: same `spec` the template renders
     today (`spec.storage` + registry source), then apply the existing
     `_apply_clone_source`-equivalent mutation when a golden is available
     (source → `pvc: {name: golden}`, `volumeMode: Filesystem`). NO
     `bind.immediate` annotation (clone target must stay WFFC so it binds on
     the VM's node — same rule as the golden work). Label it
     `srw.io/rootdisk: "true"` + `job-id: <jobid>` for GC listing.
3. Mutate the rendered VM manifest: **pop `spec.dataVolumeTemplates`**. The
   `volumes[].dataVolume.name` reference is untouched and now points at the
   standalone DV. No ownerRef → the disk survives VM deletion.

Flag OFF → today's rendering byte-identical (same pattern that made the
golden flag safe to merge dark; live-verified there via a 4-hour dormant run).

Composition with the golden flag: persistent-rootdisk ON + golden ON is the
intended pair (first create clones, recreate reattaches). Persistent ON +
golden OFF still works — first create pays the registry import into the
standalone DV, recreates reattach it.

### D2 — delete gains a purge intent (controller + NATS + orchestrator)

`_do_delete` (`controller.py:302`) gets `purge_disk: bool` from the payload,
**default `True`** — an orchestrator that never sends the field gets today's
semantics exactly (delete VM → disk goes too, now via explicit `_delete_dv`
instead of the ownerRef cascade). `handle_delete` (`controller.py:599`)
passes the whole payload dict through instead of just `data["job_id"]`.

Orchestrator side — `vm_provisioner.delete_vm` / `nats_bridge.request_vm_delete`
grow a `purge_disk` kwarg (HTTP transport: body field). Call-site intents:

| Call site | Intent | Why |
|---|---|---|
| Crash-recovery VM arm (`main.py:10453`) | **keep** | the whole point — files survive to the recreate |
| Reconciler `give_up` (`vm_manager.py:259`) | **keep** | dirty + unreachable + snapshot-exhausted = exactly the data we must not destroy; the kept disk is the recovery artifact. Update the docstring that today declares this impossible |
| `release_vm` / `release_thread_vm` (complete, cancel, archive) | purge | job is terminal; S3 archive snapshot already taken |
| Pause path (`main.py:3566/3579`) | purge (v1) | pause/resume already works via snapshot+restore; keeping disks for every paused job costs 20 Gi each (see Deferred) |
| Reconciler `drain` (drift) | purge | version drift wants a fresh image anyway |

When the orchestrator sends keep, it records it:
`merge_vm_context(job_id, {"rootdisk": "kept"})` — powering the GC sweep and
making kept disks visible in the job context. The recovery arm's ctx reset
(`main.py:10444`) must preserve/set this key.

### D3 — tailnet identity on a reused disk (the one real wrinkle)

The controller deletes the Headscale node in `_do_delete`
(`controller.py:323`). A reused disk still holds `/var/lib/tailscale` state
for that (now-deleted) node → the recovered VM's `tailscaled` would reconnect
as a dead node and never come up.

**Decision: keep-disk delete also keeps the Headscale node.** The recovered
VM boots, `tailscaled` reuses on-disk state, reconnects as the *same* tailnet
node with the *same* IP. Bonus: the stored `ssh_host` stays valid; the
management daemon (systemd, config persisted on disk) re-registers with the
orchestrator on boot and refreshes `context.vm` regardless. Purge-delete
keeps deleting the node (today's behavior).

Cloud-init caveat — **resolved by the live probe 2026-07-29: the instance-id
changes per VMI.** A session suspend/resume cycle showed the recreated VM
re-running `tailscale up` with the fresh auth key and joining as a NEW node
(tailnet IP changed). Functionally fine — the management daemon re-registers
and refreshes the stored coordinates, which is the fallback this paragraph
predicted — but each keep-disk recreate strands the previous Headscale node.
Follow-up (filed, not built): pin the NoCloud instance-id so per-instance
modules don't re-run and on-disk tailscale state rejoins as the kept node;
verify first whether KubeVirt's `cloudInitNoCloud` exposes the instance-id at
all. Until then, keep-disk recreates cost one stranded Headscale node each —
same leak class as [[srw_agent_headscale_ephemeral_leak]]'s non-ephemeral
keys.

### D4 — GC discipline (the leak guard, mirror of Branch a Phase 1)

A kept disk that never gets purged is a 20 Gi leak on the VM cluster's single
SATA SSD. Three layers, same shape as Branch a:

1. **Terminal purge (primary)**: every terminal path already calls
   `release_vm`/`delete_vm` → sends `purge_disk=True` (D2). Covers the normal
   lifecycle including a recovered job later completing: its rootdisk dies
   with the *final* VM delete.
2. **Orchestrator sweep (kept-disk reconciliation)**: periodic (piggyback the
   lifecycle reconciler tick): jobs with `context.vm.rootdisk == "kept"` AND
   terminal status → publish delete with `purge_disk=True` (idempotent: VM
   404 is already tolerated) → clear the ctx key. Catches jobs that were
   crash-recovered but then cancelled/failed *without* a live VM.
3. **Controller age backstop (orphan net)**: on the existing golden-GC hook
   (fire-and-forget after create), also list `srw.io/rootdisk` DVs with no
   corresponding VirtualMachine and age > `VM_ROOTDISK_ORPHAN_HOURS`
   (default 72 h, helm knob) → delete. Catches disks whose job row the
   orchestrator no longer knows (DB reset on dev, deleted rows). Generous
   default because a kept disk is *supposed* to outlive its VM briefly during
   recovery; 72 h with no VM means nobody is coming back for it.

Capacity guard: reuse the Branch a 3a pattern — a namespaced `ResourceQuota`
on `agent-vms` (requests.storage + PVC count), helm-configurable, default
off. Fail-closed is inherent: DV create fails → `_ensure` returns None-path →
create fails loudly (no silent fallback to templated disk, which would
silently reintroduce cascade-delete).

### D5 — what stays the same

- **S3 SSH-tar snapshots remain** for terminal archive and IDE-on-old-jobs —
  what a disk cannot do, because terminal purge deletes it by design.

  > **Corrected 2026-07-29.** This bullet originally also claimed snapshots
  > work at the graceful points (pause, idle suspension, snapshot-before-reap)
  > and that this feature "removes their never-working role as a *crash* net,
  > nothing else". For **VM** targets they have never worked at *any* point:
  > capture SSHes from the orchestrator, a VM workspace is only reachable over
  > the tailnet, and the orchestrator is not a tailnet node. So this feature
  > covers materially more than the original text claims — every graceful VM
  > point too. See
  > `../issues/vm_workspace_snapshot_unreachable_from_orchestrator.md`.
- Graph state continuity is already handled (D3 cross-pod Postgres
  checkpointer); this is its file-system counterpart.
- Golden lifecycle, image-bump GC, fallback-to-registry: untouched.
- Prod vm-controller: flag stays off (manual v0.0.21 release —
  `HomeLab/deployments_unmanaged/`, LEAVE ALONE until its chart catches up).

---

## Phases

### Phase 0 — the flip (controller-only, dark)

D1 + D2's controller half (accept `purge_disk`, default True). No
orchestrator changes. Behavior with flag ON is *externally identical* to
today (every existing delete purges) — but the disk is now structurally
independent. Flag OFF byte-identical.

- Unit: manifest mutation (dataVolumeTemplates popped, volumes untouched);
  reattach path skips clone; Failed-DV recreate; purge-default delete removes
  DV; keep delete leaves DV + Headscale node.
- Acceptance: `helm template` clean on **both** charts (`helm/` +
  `helm-vm-cluster/` — two controllers, same image; the golden work's
  checklist applies); existing VM boot E2E unchanged on dev.

### Phase 1 — keep-disk intent + recovery reattach (orchestrator)

D2's orchestrator half + D3. Crash-recovery arm and `give_up` send keep;
`context.vm.rootdisk` tracking; docstring updates in `vm_manager.py`.

- Acceptance (live, dev vm cluster — the golden probe methodology):
  1. Boot a VM probe job, write a sentinel file, **hard-kill the VM** (delete
     the VM object from the vm cluster, mimicking the crash arm) → job
     auto-recovers → new VM boots **without a clone** (controller logs
     `rootdisk reattach`) → sentinel present → job completes. No S3 restore
     involved.
  2. Recovery job→agent-processing measurably faster than fresh (~2 min vs
     ~5m20s; the clone is off the path).
  3. Tailnet: recovered VM reachable over SSH; no duplicate Headscale node
     (V3 verified, instance-id pinned if needed).
  4. Terminal purge: cancel the recovered job → VM + rootdisk DV + PVC all
     gone; golden untouched.

### Phase 2 — GC discipline + capacity

D4 (sweep + backstop + quota knob).

- Acceptance: seed a kept-disk for a terminal job → sweep purges it; orphan
  DV older than knob with no VM → controller backstop deletes it; quota knob
  renders and (when set low) fails DV create loudly.

### Deferred (explicit non-goals for v1)

- **Pause keeps the disk** (skip the S3 round-trip on pause/resume entirely).
  Valuable — LLM-outage backoff pauses loop jobs — but every paused job then
  holds 20 Gi; needs the capacity story proven first.
- **Suspend = VM stop/start** (`runStrategy` flip instead of
  snapshot+delete+restore). The natural endgame once the disk is independent;
  bigger blast radius (suspension service, reconciler `is_dirty` semantics).
- ~~**Session/thread VMs**~~ — **promoted into scope and IMPLEMENTED**
  (`dc3d3f8d`). The premise that "sessions already have working
  suspend/restore" was false: VM session suspend was blocked first by a tier
  misread (fixed, `6d66f7c4`) and then by the unreachable capture above, so no
  VM session had ever suspended. Suspend now deletes the VM with
  `purge_disk=False` and the disk carries the workspace to the resume; a
  failed capture no longer blocks it (VM tier only — a pod has no disk to
  keep, so it stays fail-closed). Restore skips the snapshot extract when the
  disk was reattached: the disk is the live state at teardown, a snapshot is
  the same moment at best and stale at worst.
- **TopoLVM CoW**: orthogonal (makes the *first* clone near-instant);
  reattach already makes recovery clone-free.

---

## Risks / wrinkles

| Risk | Assessment |
|---|---|
| Dirty ext4 after a hard crash | Guest journal replay on boot — same exposure as any physical machine losing power. The golden-clone fallback (delete Failed rootdisk → fresh clone) remains for an unbootable disk; recovery attempt cap (`WORKSPACE_RECOVERY_MAX_ATTEMPTS`, exists) bounds the loop. |
| cloud-init re-runs on reused disk | V3 live probe; mitigation is pinning `instance-id` in the template (one line). |
| Tailscale/Headscale identity | D3: keep node on keep-disk delete. Worst case (state corrupt) → bounded recovery attempts → golden-clone fresh start = today's behavior. |
| Disk leak | Three-layer GC (D4), mirroring the pattern that's holding on the container side. |
| 20 Gi × concurrent VM jobs on one SATA SSD | Full copies (no CoW on local-path) — capacity knob + the loop runs few concurrent VMs. TopoLVM later if it bites. |
| Old orchestrator ↔ new controller skew | `purge_disk` defaults True in the controller → un-upgraded orchestrator keeps exact current semantics. Fleet deploys controller + orchestrator from the same repo anyway. |
| WFFC on standalone DV | Clone target stays WFFC (no bind.immediate) and the VM is created immediately after — binding proceeds exactly as the templated DV does today (live-verified timeline in the golden doc). |

## Files to touch

```
vm/controller/controller.py                       D1 mutation + reattach + purge_disk in _do_delete + orphan backstop
orchestrator/services/vm_provisioner.py           delete_vm(purge_disk=), release paths send purge
orchestrator/services/nats_bridge.py              request_vm_delete payload field
orchestrator/main.py                              crash-recovery arm sends keep + ctx tracking (10423-10464)
orchestrator/services/lifecycle/vm_manager.py     give_up keeps disk; docstring; kept-disk sweep hook
helm/templates/vm-controller/*                    flag env + values (+ optional quota template)
helm-vm-cluster/templates/vm-controller/*         same (BOTH charts — two controllers, one image)
deployment-vms/srw-vm-controller/fleet.yaml       dev enable (after Phase 1 verified, mirroring golden rollout)
tests/test_vm_controller.py                       Phase 0 unit coverage
tests/test_lifecycle_*.py                         give_up/sweep coverage
```

## Verification plan (dev vm cluster)

Reuse the golden-image live-test harness: throwaway probe job via in-pod curl
(`X-Internal-Key`, attributed `user_id`, bare config), watch
`kubectl --context=vm -n agent-vms get dv,pvc,vm,vmi`, job status via
`psql -U srw -d srw` on the main cluster. The crash is induced by deleting
the VM object (equivalently: the VMI's node-kill), which drives the agent's
`workspace_unavailable` completion → the recovery arm under test. Flag
enable ships via `deployment-vms/srw-vm-controller/fleet.yaml` (Fleet syncs
in ~60 s, no CI round-trip).
