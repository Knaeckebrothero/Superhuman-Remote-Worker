# Deleting a job orphans its workspace pod forever (no-bound-row = never reapable)

**Status:** investigated 2026-07-05 — root cause confirmed from a live orphan on the main cluster; fix not started
**Severity:** low urgency (leaks one pod's worth of node resources per occurrence) but unbounded — every orphan persists until someone hand-deletes it, and nothing surfaces it
**Component:** `orchestrator/main.py` `delete_job` (`DELETE /api/jobs/{job_id}`), `orchestrator/services/lifecycle/workspace_manager.py` (`is_reapable`/`is_idle`/`reap_orphans`)
**Observed on:** main cluster (`superhuman-remote-worker`), pod `workspace-a9ad385d-0ed` (job `a9ad385d-0edd-4bc3-8407-4e0523a0d35f`), alive 7d22h at investigation

---

## TL;DR

`DELETE /api/jobs/{job_id}` cleans up Gitea and the vector-DB rows, then deletes the
job row — **it never tears down the job's workspace pod** (or its seed ConfigMap).
Once the row is gone, the lifecycle reconciler can see the pod every tick but can
never act on it: `WorkspaceInstanceManager.is_idle` and `is_reapable` both return
`False` when the bound job/thread row is missing, because "no row" is deliberately
treated as "context may still be in flight" (a pod whose DB context hasn't been
persisted yet must not be reaped). A *deleted* job is indistinguishable from that
in-flight state, so the pod is healthy + not idle + not reapable → skipped on every
tick, forever. The `reap_orphans` backstop already implements the correct
"row present → use status; row gone → reap" policy — but only for **PVCs**, not pods,
and this pod is emptyDir-backed (no PVC), so no sweep ever touches it.

## Observed state (main cluster, 2026-07-05)

- Pod `workspace-a9ad385d-0ed`: Running, created 2026-06-27T11:51:06Z (7d22h old),
  node3, emptyDir `workspace-data` volume, no ownerReferences, annotated
  `srw.io/managed-by: lifecycle-reconciler`, label
  `srw/job-id=a9ad385d-0edd-4bc3-8407-4e0523a0d35f`, build-sha `bf50bb0`.
- ConfigMap `code-server-config-workspace-a9ad385d-0ed`: same age, also orphaned.
- No Service for the pod; no PVC (emptyDir).
- App DB: **no row** in `jobs` (nor `threads`) for `a9ad385d-…`.
- Audit store (`srw-auditdb`): **zero** rows in `agent_audit`, `chat_history`, and
  `llm_requests` for the job id — the job never executed a single agent turn.
  The workspace was provisioned at dispatch and the job died/was removed before
  the agent ever made an LLM call.
- `jobs` has no `ON DELETE CASCADE` FKs (checked `parent_job_id`, `project_id`),
  and `delete_project` only deletes the `projects` row — so the only path that
  removes a job row is the explicit `DELETE /api/jobs/{id}` endpoint (cockpit
  delete button or MCP `delete_job`). Someone deleted the job; the pod stayed.

Sanity check of the neighbouring resources: the 15 pod-less `pvc-workspace-*`
PVCs on the cluster all belong to `pending_review` jobs — non-terminal, retained
by design. The reconciler and the PVC orphan sweep are otherwise working; the
orphaned pod is the only anomaly.

## Root cause

Two halves, both necessary:

1. **Deletion path doesn't tear down the workspace.** `delete_job` in
   `orchestrator/main.py` (~line 6763) handles Gitea + vector-DB cleanup and then
   `postgres_db.delete_job(job_id)`. There is no
   `container_provisioner.delete_workspace(WorkspaceOwner.job(job_id))` call, no
   ConfigMap/Service/PVC cleanup. The row disappears while the pod lives.

2. **Reconciler conflates "row gone" with "row not yet written".**
   `workspace_manager.list_instances` fetches the bound job row; when
   `get_job` returns `None`, the instance metadata simply has no `job_status`.
   `is_idle` and `is_reapable` both end with `return False` for that case
   (`workspace_manager.py:222,244` — "A pod with no bound row is never reapable
   (context may be in flight)"). The conservatism is right for the seconds-old
   provisioning window but wrong as a permanent verdict — there is no age limit,
   so a pod whose row was deleted is protected indefinitely. Meanwhile
   `reap_orphans` (same file) makes exactly the missing distinction for PVCs:
   direct query, row present → check status, **no row → genuinely gone → reap**,
   query raised → skip. Pods never get that logic.

## Fix directions

1. **Teardown in `delete_job`** (primary): before deleting the row, best-effort
   `delete_workspace(WorkspaceOwner.job(job_id))` (which also removes the seed
   ConfigMap), plus `delete_workspace_pvc` + `_delete_service` when PVC-backed —
   the same trio the reconciler's terminal-delete runs. The job row (and its
   `workspace_container` context) is still available at that point, so the
   provisioner has everything it needs. Failures should warn, not block the
   delete (matches the vector-DB cleanup stance).
2. **Age-gated missing-row reap in the reconciler** (backstop, covers any other
   row-deletion path and crash windows): in `list_instances`, distinguish
   "fetch returned no row" (`bound_row_missing=True`) from "fetch raised"
   (skip, as today). In `is_reapable`, treat `bound_row_missing` as reapable
   once the pod is older than a grace period (e.g. 15–30 min via
   `metadata.creationTimestamp`, far beyond any provisioning window). The reap
   flow must then short-circuit `is_dirty`/snapshot for missing-row instances —
   there is no row to snapshot against (`capture_vm_snapshot` keys by job id,
   `record_attempt` merges into the deleted row and would silently no-op,
   leaving `attempts_exhausted` false forever) — and go straight to
   `delete(grace_s=0)`.

Either half alone closes the observed leak; (1) fixes the common path cheaply,
(2) makes the reconciler actually self-healing, which is what its
`srw.io/managed-by` annotation already promises.

## Cleanup for the live orphan

```bash
kubectl --context=main -n superhuman-remote-worker delete pod workspace-a9ad385d-0ed
kubectl --context=main -n superhuman-remote-worker delete configmap code-server-config-workspace-a9ad385d-0ed
```

Nothing to preserve: emptyDir volume, owning job deleted, zero agent activity ever.
