# Workspace snapshot/restore is structurally dead for jobs — every reaped/crashed job re-dispatches blank

**Status:** Filed from a 5-agent code audit (2026-06-29) prompted by job `19707fa1`. **Confirmed at code level, not fixed.** This is the systemic sibling of `loop_job_workspace_lost_wedged_in_recovery.md` (that doc covers the *handler* wedge; this one covers the *restore feature being non-functional for jobs even on the happy path*). The Tier-2 fork in that doc and the fix here are **the same decision**. **Disposition note 2026-07-02:** the companion wedge doc has since chosen **PVC reattach** as the recovery path (S3 snapshot-restore is *not* being revived), which most likely makes restore-for-jobs **superseded / won't-fix** rather than a build TODO — but snapshot **capture** still runs (write-only CPU/SSH/S3 cost on every reap), so the concrete open action is to *confirm the supersession and disable the now-pointless capture*, not to implement restore.
**Found:** 2026-06-29. Surfaced while auditing the workspace-loss recovery of job `19707fa1` (which had a healthy 99 MB pod snapshot `available` that was never consumed).
**Severity:** **Medium-High.** Snapshots are captured for jobs (CPU/SSH/S3 cost on every reap) but **never restored** — they are write-only and GC'd after `SNAPSHOT_RETENTION_DAYS=90`. Every reaped or crashed job silently re-dispatches onto a **blank** workspace, losing all on-disk work (repo, documents, outputs, archive) and relying solely on the LangGraph checkpoint for graph state. For loop jobs this may be acceptable-by-design (work is pushed to git `main`); for others it is silent data loss. Either way the system pays to capture snapshots it can never use, and the manager's documented design is unfulfilled.
**Component:** `orchestrator/services/workspace_suspension.py` (`restore_workspace`, `suspend_workspace`, `check_idle_all`) · `orchestrator/services/container_provisioner.py:324` (`delete_workspace` writes `status="deleted"`) · `orchestrator/services/workspace_lifecycle.py:94-117` (`ensure_workspace` restore-on-`"suspended"` gate) · `orchestrator/services/lifecycle/{reconciler,workspace_manager,vm_manager}.py` (reap path + dead `restore()` methods) · `orchestrator/main.py:787-799` (idle sweeper went reconcile-only)
**Related:** `loop_job_workspace_lost_wedged_in_recovery.md` (the crash-path handler that *also* never calls restore) · memory `project_cross_pod_checkpointer_d3` (the checkpoint that makes "blank is OK for jobs" plausible) · `project_loop_repo_compounding` (git-push cadence that bounds the loss)

---

## Symptom

Jobs capture workspace snapshots to S3 (e.g. job `19707fa1`'s 99 MB `source_type:"pod"` snapshot, `context.snapshot.status="available"`) but **no job ever restores one**. On the next dispatch the job gets a fresh, empty workspace; the snapshot is never read and is eventually garbage-collected. This contradicts the `WorkspaceInstanceManager` docstring (`workspace_manager.py:5-9`): *"on next dispatch `WorkspaceSuspensionService.restore_*` rehydrates a fresh-version pod from the same S3 reference."* That handshake was scaffolded and never completed.

## Root cause — a `status`-string mismatch breaks the reap→restore handshake

**Restore IS implemented** (refuting the earlier "pod restore unimplemented" guess): `restore_workspace` (`workspace_suspension.py:252-388`) creates a fresh pod and SSH-extracts the S3 tarball end-to-end (K8s `else` branch `:323-358`, `_extract_snapshot` `:389-434`). The bug is that **nothing triggers it for jobs**:

1. **Reap writes the wrong status.** The reconciler reaps every idle/paused job (`reconciler.py:251-261`): `snapshot()` then `delete()`. `delete_workspace` sets `workspace_container.status = "deleted"` (`container_provisioner.py:324`).
2. **Restore only fires on `"suspended"`.** The job dispatcher's `ensure_workspace` restores only when `current_status == "suspended"` (`workspace_lifecycle.py:101-103`); it treats `"deleted"` as **blank `_create`** (`:94-96`).
3. **Nothing writes `"suspended"` for a job.** The only writer is `suspend_workspace` (`workspace_suspension.py:230`), reachable **only** via `check_idle_all` — which has **zero live callers** (the idle sweeper went reconcile-only, `main.py:787-799`; `check_idle_all`/`check_idle_threads` confirmed callerless by grep).

**Net: no job ever reaches `status="suspended"` → `restore_workspace` for jobs is unreachable in production.** It runs only for **sessions/threads** (`main.py:16527`, `:17512`, gated on `"suspended"`, which the session path *does* set).

### Supporting facts
- The crash path doesn't call restore either: the `workspace_unavailable` handler (`main.py:10118-10161`) sets `vm.requested` + pauses, never `restore_workspace` (see the sibling doc).
- `give_up` is **not** an S3 restore: it deletes the dead pod, and for **PVC** volumes recreates the pod to reattach the volume (local-disk reattach, no S3) — but the default volume is **emptyDir** (`workspace_manager.py:66-80 _pod_volume_is_ephemeral` defaults True; `container_provisioner.py` "storage dies with the pod"), so `give_up` just deletes. Its comment "full restore-by-reattach lands with the migration spec" refers to the PVC arm, **not** the S3 path.
- `WorkspaceInstanceManager.restore()` (`workspace_manager.py:403`) and `VMInstanceManager.restore()` (`vm_manager.py:305`) are **dead code** — registered with the reconciler but never called by `tick()`/`_reap()`.
- Snapshot keying is by `job_id` only (`s3://<bucket>/jobs/<uuid>/env.tar.zst` + `manifest.json`, `snapshot_service.py:151-194`), latest-wins; no producer in the job path passes `phase_number`, so only the top-level latest exists.

## What's built vs what's missing

- **Built & working:** snapshot capture (`snapshot_service.capture_vm_snapshot`, multiple producers); `restore_workspace` K8s/Docker/VM branches; the `ensure_workspace` restore-on-`"suspended"` wiring (proven by the session path).
- **Missing:** any code that puts a *job* into `"suspended"` after a successful snapshot — i.e. the one status transition that would make the existing restore fire.

## Fix — the same fork as the sibling doc's Tier-2 (DECISION DEFERRED)

- **Option A — make restore work for jobs.** When a reap successfully snapshots a job, write `workspace_container.status = "suspended"` (not `"deleted"`) — distinguish snapshotted-reap from terminal-delete (keep `"deleted"` for `completed/failed/cancelled`). The already-wired `ensure_workspace` path then rehydrates on the next dispatch automatically, for both normal loop-advance and (once the sibling doc's routing poison is removed) the crash path. Also delete or wire the dead `restore()` manager methods. Revives the feature the manager docstring promises.
- **Option B — accept blank-for-jobs and stop pretending.** Treat the workspace as disposable for worker/loop jobs (state lives in checkpoint + git `main` + KB), make blank+checkpoint explicit and guardrailed (force re-clone, rewind to last pushed phase boundary, execution-roles only — see the sibling doc's Tier-2 Option B), **stop capturing job snapshots** (remove the write-only cost), and keep snapshot/restore for **sessions** only.

**Coupling:** whichever way `loop_job_workspace_lost_wedged_in_recovery.md` Tier-2 is decided, this doc must match — Option A here pairs with Option A there; Option B here pairs with Option B there.

## Acceptance criteria

- A decision is recorded (A or B) and applied consistently across both docs.
- If A: a job reaped *with a successful snapshot* re-dispatches with its files restored (verify a loop job's `repo/` survives a reap→re-dispatch); the dead `restore()` methods are wired or removed.
- If B: job snapshots are no longer captured (no write-only S3 cost), blank+checkpoint resume is guardrailed, and session restore is unaffected.
