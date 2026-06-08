# Workspace PVC-Backed Storage Migration

## Status

Design brief — 2026-06-05. **Not yet designed in full**; this document frames
the problem and enumerates the decisions to make next session. It is the
deferred follow-up carved out of the workspace-reaper work
([`docs/superpowers/specs/2026-06-04-workspace-reaper-lifecycle-design.md`](../superpowers/specs/2026-06-04-workspace-reaper-lifecycle-design.md))
and builds on the ephemeral-workspace snapshot model
([`ephemeral_workspaces.md`](ephemeral_workspaces.md)).

## Problem

Container workspace pods currently mount `/home/agent-host` from an
**`emptyDir`** volume — storage dies with the pod. State survives a teardown
only via the **S3 snapshot → restore** cycle (SnapshotService +
WorkspaceSuspensionService). That cycle works, but it has two costs the reaper
work made concrete:

1. **State loss when snapshot is impossible.** If a pod is dirty but
   unreachable over SSH (port migration, stale IP, network blip), no snapshot
   can be taken, and the reaper's escape hatch force-deletes it — losing the
   work. With a persistent volume, the disk would survive the pod and a fresh
   pod could re-mount it, making "force-delete" non-destructive.
2. **Snapshot/restore latency and SSH fragility.** Every resume is a download +
   `zstd`-extract over SSH. A reattachable PVC makes resume a pod re-create
   against the same volume — faster, and independent of SSH reachability at
   teardown.

The reaper already **anticipates** this: `WorkspaceInstanceManager` has an
`is_state_ephemeral` predicate and a `give_up` branch that does
*recreate-pod-keep-PVC* for non-ephemeral workspaces — but that branch is
**minimally activated** (designed-for, not exercised), because emptyDir is the
only mode in production today. This migration is what turns that branch on.

## Why this is its own project (not part of the reaper)

The reaper made cleanup *correct* for both volume modes. Actually *running*
PVC-backed is a storage-lifecycle change with its own blast radius:
per-workspace PVC create/bind/GC, access modes, reclaim policy, capacity
planning, and a data-safety story. It deserves its own spec → plan → rollout.

## Current state (verified 2026-06-05)

- **Default volume:** `emptyDir` (`sizeLimit: 10Gi`) at
  `container_provisioner.py` `_build_pod_manifest` (the `pvc_name`-gated
  branch: PVC if `pvc_name` given, else emptyDir).
- **PVC code already exists but is dormant/legacy:** `_create_pvc`
  (`ReadWriteOnce`, `storageClassName=self._storage_class`),
  `delete_workspace_pvc` (names `pvc-workspace-<id[:12]>` /
  `pvc-ws-thread-<id[:12]>`). `delete_workspace_pvc`'s own docstring calls
  itself backward-compat for "workspaces created before the emptyDir switch."
  So PVC-backed workspaces existed once and were deliberately moved off.
  **Next session: find out *why* they were removed** (cost? RWO scheduling?
  leak? git history on the emptyDir switch) before re-introducing them.
- **Storage classes on the cluster:**
  - `longhorn` — reclaim **Retain**, Immediate binding
  - `longhorn-ephemeral` — reclaim **Delete** (current `WORKSPACE_STORAGE_CLASS`)
  - `longhorn-static` — reclaim Delete
  - A shared `srw-workspace` PVC (20Gi, **RWX**) exists, mounted by the
    orchestrator — distinct from per-workspace volumes; clarify its role.
- **Reaper hooks ready:** `is_state_ephemeral` (reads pod volume spec; defaults
  True), `give_up` PVC arm (delete pod, recreate against owner — PVC untouched).
  Helm: `workspace.storageClass`/`storageSize` values exist but default empty.

## Open decisions (the agenda for next session)

1. **Why was emptyDir chosen over PVC originally?** Recover the rationale first
   — this migration may be re-litigating a settled trade-off. Check the commit
   that introduced the emptyDir default and any related issue doc.
2. **Scope: which workspaces?** Jobs, threads (sessions), or both? Sessions are
   the stronger case (long-lived, resumable); jobs are shorter and already
   snapshot at completion.
3. **Access mode + scheduling:** `ReadWriteOnce` (current `_create_pvc`) ties a
   volume to one node — a recreated pod must land on the same node or the PVC
   won't attach. Options: RWO + node affinity, RWX (Longhorn supports it but
   heavier), or accept rescheduling constraints. This is the crux.
4. **Reclaim / GC policy:** which storage class? `longhorn` (Retain) protects
   data but leaks PVs if not GC'd; `longhorn-ephemeral` (Delete) auto-cleans but
   defeats the purpose. Likely a new/!ephemeral class + an explicit PVC reaper
   for terminal-bound work (job completed / thread deleted, *not* merely ended).
5. **PVC lifecycle ownership:** who deletes the PVC, and when? Pod teardown must
   NOT delete it (that's the whole point); only terminal bound-work should. This
   is the VM-reaper "DB-row persists after delete" lesson applied to PVCs —
   define the terminal-cleanup path up front. (See the reaper's
   `_TORN_DOWN_VM_STATUSES` skip pattern and the PVC-orphan risk.)
6. **Snapshot coexistence:** does S3 snapshot stay as a backup/portability layer
   (cross-node, DR) alongside PVC, or does PVC replace it for the same-cluster
   resume path? Recommend: keep snapshot for DR/portability, use PVC for the
   fast same-node resume — but decide explicitly.
7. **Capacity:** N concurrent workspaces × storageSize on Longhorn — model the
   ceiling and set quotas so a workspace fleet can't exhaust cluster storage.
8. **Migration/rollback:** flag-gated rollout (`WORKSPACE_STORAGE_CLASS` +
   helm value already exist) so emptyDir stays the default until proven; how do
   in-flight emptyDir workspaces coexist during cutover?

## Success criteria (draft)

- A workspace pod that crashes or is force-deleted while dirty **does not lose
  state**: a fresh pod re-mounts the same PVC and resumes.
- The reaper's `give_up` PVC arm is exercised end-to-end (recreate-keep-PVC),
  not just unit-tested.
- No PVC/PV leaks: terminal-bound work reliably releases its volume; a reaper
  or owner-ref backstop catches orphans (mirror the pod-leak fix that started
  all this).
- emptyDir remains a supported fallback behind the flag.

## Out of scope

- **VM volume-reattach** — separate, controller-side (VM disks live behind NATS
  in the unmanaged VM controller). Tracked separately.
- **Automated DB-orphan enumeration** in the reconciler (`list_instances`
  surfacing rows whose pod/PVC is gone) — its own follow-up.

## References

- [`docs/superpowers/specs/2026-06-04-workspace-reaper-lifecycle-design.md`](../superpowers/specs/2026-06-04-workspace-reaper-lifecycle-design.md)
  — the reaper; `is_state_ephemeral` + `give_up` PVC arm
- [`ephemeral_workspaces.md`](ephemeral_workspaces.md) — S3 snapshot/restore model
- `orchestrator/services/container_provisioner.py` — `_build_pod_manifest`
  (emptyDir vs PVC), `_create_pvc`, `delete_workspace_pvc`, `release_workspace`
- `orchestrator/services/lifecycle/workspace_manager.py` — `is_state_ephemeral`,
  `_pod_volume_is_ephemeral`, `give_up`
- `helm/values.yaml` — `workspace.storageClass` / `storageSize` (currently empty)
