# Deleting a job orphans its workspace pod forever (no-bound-row = never reapable)

**Status:** RESOLVED — investigated 2026-07-05 from a live orphan on the main cluster; both fixes implemented + k3d-verified 2026-07-05 (see "Fix directions" — teardown-in-delete verified end-to-end via API delete of a pod-backed job row; reaper verified via planted orphan pod: spared while under grace, reaped after); shipped in `af5cb4af`. **Confirmed on the main cluster 2026-07-09:** the original orphan pod and its ConfigMap are gone post-deploy, and every remaining workspace pod is bound to a live job/thread row (paused/processing/reviewing) — zero orphans. VM backstop parity built 2026-07-09 (see section at the bottom), uncommitted; its live verification rides the next VM-controller rollout.
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

## Fix directions (both implemented 2026-07-05)

1. **Teardown in `delete_job`** (primary) — IMPLEMENTED: `delete_job` now
   first 409s when the job has child rows (`postgres_db.has_child_jobs`, a new
   method mirroring the `parent_job_id` FK — the row delete would fail anyway,
   and it must fail BEFORE teardown or the job would survive with its pod
   gone; previously this case was an opaque FK 500). It then calls
   `_archive_and_cleanup_workspace(job_id)` (the same centralized helper the
   cancel/completion paths use — handles k8s pod + seed ConfigMap + PVC +
   Service, docker pool release, and VM release uniformly) before deleting the
   row, then `snapshot_service.delete_snapshot(job_id)` since a deleted job's
   S3 snapshots (including the one the release just captured) can never be
   restored. Both best-effort: failures warn, the delete proceeds (matches the
   vector-DB cleanup stance).
2. **Age-gated missing-row reap in the reconciler** (backstop, covers any other
   row-deletion path and crash windows) — IMPLEMENTED in
   `workspace_manager.py`: `_fetch_job`/`_fetch_thread` now return a
   `_FETCH_FAILED` sentinel on lookup errors so `list_instances` can set
   `bound_row_missing=True` only when the query succeeded and found no row
   (plus `pod_age_s` from `creationTimestamp`). `is_reapable` treats a
   missing-row pod as reapable once older than
   `WORKSPACE_ORPHAN_GRACE_SECONDS` (default 900 — the provisioning
   pod-before-row window is seconds). `is_dirty` returns False for orphans
   (no entity can restore a snapshot; `record_attempt` would merge into the
   deleted row as a silent no-op and retry forever), so the reap goes straight
   to `delete(grace_s=0)`; `_is_terminal` returns True so PVC + Service are
   reclaimed and `give_up` never recreates.

Verification (k3d, 2026-07-05): unit — 106 tests in
`test_lifecycle_workspace_manager.py`/`test_lifecycle_reconciler_reap.py`
incl. new `TestMissingRowOrphan` (marking, fetch-failure distinction, age
gate, clean+terminal, reconciler-tick end-to-end) + a `delete_job` ordering
test in `test_job_access.py` (teardown before row delete). Live — inserted a
`paused` job row bound to a planted pod, `DELETE /api/jobs/{id}` as the test
user → `{"status":"deleted"}`, row gone, pod Terminating inline; planted a
second pod with a nonexistent job id → reconciler listed it every tick,
spared it under the grace age, reaped it after.

## VM backstop parity (built 2026-07-09, uncommitted)

The VM analogue of the gap is worse than "never reapable": the VM manager
enumerates instances FROM the jobs/threads rows (`_fetch_vm_rows`), so a VM
whose row was deleted never surfaces as an `Instance` at all — invisible, not
just protected. Fix (1) covers deleted jobs' VMs (release via the shared
helper); the reconciler backstop needed a backend inventory and is now built:

- **Controller** (`vm/controller/controller.py`): `_do_list()` enumerates the
  managed KubeVirt VMs (`agent-vm-<uuid>` names encode the owning job/thread;
  goldens excluded), exposed as NATS `vm.lifecycle.list.{ORCHESTRATOR_ID}`
  (request/reply) and HTTP `GET /vms`.
- **Provisioner** (`vm_provisioner.list_vms()` + `nats_bridge.request_vm_list()`):
  NATS > HTTP > direct-KubeVirt dispatch. Returns None for "unknown" (docker
  pool; an OLD controller without the op times out / 404s) — distinct from
  `[]`, so the sweep never treats a mute inventory as an empty cluster.
- **Sweep** (`VMInstanceManager.reap_orphans()`, picked up by the reconciler's
  existing optional once-per-tick hook): reaps a listed VM only when its name
  parses as a UUID, it is older than the shared orphan grace
  (`WORKSPACE_ORPHAN_GRACE_SECONDS`, extracted to
  `workspace_manager.orphan_grace_seconds()`), and the id has NO row in jobs
  NOR threads (VM names don't encode the entity type, so both tables are
  checked; a row of ANY status leaves the VM to the instance path/dispatcher).
  DB error → skip. Deletes via the id-keyed controller path, which also
  removes the Headscale mesh node.

Tests: `TestHandleList`/`TestHttpList` (controller), `TestRequestVmList`
(bridge), `TestListVms` (provisioner), `TestReapOrphans` incl. a
reconciler-tick end-to-end (manager). Live verification requires a controller
rollout (CI builds `vm/controller/` on change); until the new controller is
deployed, NATS list requests time out and the sweep no-ops by design.

## Cleanup for the live orphan

No longer needed — after the fix deployed, the pod and its
`code-server-config-*` ConfigMap were confirmed gone from the main cluster
(2026-07-09), which is exactly the pair `delete_workspace` removes. There was
nothing to preserve: emptyDir volume, owning job deleted, zero agent activity
ever.
