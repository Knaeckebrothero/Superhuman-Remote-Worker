# A pre-job research scholar self-provisions its own workspace, but the inherit-workspace resolver misreads that self-provisioned `context.workspace_container` as an *inherited* one and blocks the scholar for 600s on the parent's never-provisioned workspace

**Status:** ROOT CAUSE CONFIRMED on live local k3d 2026-07-13, UNFIXED. Regression
introduced by commit `5a6f5a49` ("Introduce robust workspace inheritance handling
for subjobs", 2026-07-10) — the same change documented in
`subjob_inherits_stale_workspace_container_snapshot.md`. That fix hardened the
*inherit* path but did not distinguish an inheriting subjob from a
self-provisioning one, so it now traps every **root-job pre-research scholar**.

**Motivating incident:** root job **`981b1275-11ae-4646-8ded-082bcecda02f`**
("Calculator", *test's Project*, `runner_kind=user`), local k3d cluster
`k3d-srw` / namespace `srw`. Its pre-job research scholar
**`94e4d0e6-4795-4a33-bf33-23fcd23a43fd`** (`config_name=scholar`,
`runner_kind=lifecycle`) `failed` with:

```
Timed out after 600s waiting for parent job
981b1275-11ae-4646-8ded-082bcecda02f workspace to become ready
(container=None, vm=None).
```

The parent is still stranded in `waiting` with 1 child (the failed scholar) and
never provisioned a workspace of its own.

## TL;DR

For **root jobs**, `_spawn_scholar_subjob` (`orchestrator/main.py:10336`) runs
**synchronously inside `create_job`, before any workspace is provisioned**, and
immediately holds the parent at `status="waiting"` (`main.py:~10426`). A
`waiting` job is not dispatchable (`get_dispatchable_jobs` returns only
`status IN ('created','paused')`, `postgres.py:3385`), so **the parent never
provisions a workspace** while held — it is unblocked (`waiting → created`) only
later, by `_handle_scholar_completion` (`main.py:10503`), when the scholar
finishes.

Because the parent has no workspace at spawn, the scholar inherits nothing
(`parent_ctx.get("vm"/"workspace_container")` are both empty at
`main.py:~10405`), gets `worktree_path=NULL`, and **correctly self-provisions
its own pod** — this is the intended pre-research flow.

The regression: once the scholar's own pod is provisioned, the provisioner
writes it into the scholar's **`context.workspace_container`**. On the next
dispatch tick `_resolve_subjob_inherited_workspace` (`main.py:3465`, added by
`5a6f5a49`) decides "this is an inheriting subjob" **purely from the presence of
`ctx.get("workspace_container")`** (`main.py:~3505`):

```python
own_container = ctx.get("workspace_container") or {}
own_vm = ctx.get("vm") or {}
if not own_container and not own_vm:
    return ("proceed", None)   # <-- only reached BEFORE self-provisioning
```

That signal is **ambiguous** — the same key holds both an inherited *and* a
self-provisioned workspace. So the resolver re-reads the parent's live context
(empty), sees no ready parent workspace, and returns `("wait", …)` every tick
until the `WORKSPACE_INHERIT_MAX_WAIT_S` (default 600s) budget expires
(`main.py:~3589`), then `("fail", …)`. The scholar is failed even though **its
own workspace is `ready`**.

## Evidence (live, 2026-07-13, cluster `k3d-srw`)

### Timeline (orchestrator log, pod `srw-orchestrator-76678b89f9-29hgq`)

| Time (UTC) | Event |
|---|---|
| 07:12:27.99 | `Creating scholar subjob for job 981b1275 (scholar_config=scholar)` — parent set to `waiting` |
| 07:12:29.78 | `Scholar job 94e4d0e6 created for parent 981b1275` |
| 07:12:29.82–.85 | Provisioner: **PVC + Service + container `workspace-94e4d0e6-479` created for job 94e4d0e6** (the scholar's OWN pod) |
| 07:13:28.31 | `Workspace container ready: workspace-94e4d0e6-479 @ 10.42.0.2 (job 94e4d0e6)` |
| 07:13:28.31 | `Dispatcher: subjob 94e4d0e6 waiting for parent 981b1275 workspace to become ready` — **flip happens the same second the scholar's own pod goes ready** |
| 07:13:28 → 07:22:28 | same "waiting for parent workspace" line every 30s |
| 07:22:28.39 | `ERROR … subjob 94e4d0e6 cannot inherit parent workspace — failing: Timed out after 600s …` (age from `created_at`=07:12:28 is exactly 600s) |

The parent `981b1275` produces **zero** dispatch/provision log lines after
creation. `kubectl get pods -n srw | grep 981b1275` → **none**. No parent
workspace pod ever existed.

### DB rows

Parent `981b1275`: `status=waiting`, `assigned_agent_id=NULL`,
`updated_at=07:12:27` (frozen since creation),
`context = {"git_remote_url": …, "kickoff_message": "Please build a simple
calculator app in python."}` — **no `workspace_container`, no `vm`.**

Scholar `94e4d0e6`: `status=failed`, `parent_job_id=981b1275`,
`runner_kind=lifecycle`, `worktree_path=NULL`, and crucially its **own**
context points at its **own** pod:

```json
"workspace_container": {
  "host": "workspace-94e4d0e6-479.srw.svc.cluster.local",
  "port": 30022, "pod_ip": "10.42.0.2", "status": "ready",
  "pod_name": "workspace-94e4d0e6-479", "namespace": "srw",
  "snapshot_attempts": 12
}
```

Contrast with the sibling issue
`subjob_inherits_stale_workspace_container_snapshot.md`, where the scholar and
parent point at the **same** pod (`workspace-4b4b7127-99a`). Here the pod_name
is the **scholar's own** (`workspace-94e4d0e6-479`) — proof it self-provisioned
rather than inherited.

## Root cause

`_resolve_subjob_inherited_workspace` cannot tell a **self-provisioned**
workspace from an **inherited** one, because it keys the decision on the
presence of `context.workspace_container` / `context.vm` — the very keys the
provisioner writes for a workspace the subjob provisioned itself. For a root-job
pre-research scholar (which by design has nothing to inherit and self-provisions),
the classifier flips from `"proceed"` to `"wait"` the moment provisioning
completes, then times out.

A clean discriminator already exists: **`worktree_path` is set only for a true
inheriting subjob** (`main.py:~10486`, set only when
`parent_ctx.get("vm") or parent_ctx.get("workspace_container")`), and is `NULL`
for a self-provisioner. The scholar's `worktree_path` is `NULL` here.

## Secondary bug — failed scholar strands the parent forever

`_handle_scholar_completion` (`main.py:10503`) is designed to unblock the parent
on scholar **failure** too (`is_failure` branch → `merge_job_context({"scholar_failed": True})`
→ `update_job_status(parent, "created")`). But it is only invoked from
`complete_job` (`main.py:13213`) and `cancel_job` (`main.py:7914`). The
dispatch-loop inherit-timeout fails the scholar by calling
`postgres_db.update_job_status(job_id, status="failed", …)` **directly**
(`main.py:~4695`), bypassing `complete_job` — so the unblock never fires and the
parent is left in `waiting` indefinitely (observed: `updated_at` frozen at
07:12:27). Even after the primary bug is fixed, any dispatch-path subjob failure
should route through the parent-unblock handler.

## Affected scope

Every **root job with the scholar/research phase enabled**: the parent is always
held in `waiting` at creation (no workspace), so its scholar always
self-provisions and always trips the misclassification. This is a regression
window opened on **2026-07-10** by `5a6f5a49`; the `waiting`-hold itself predates
it (`181d21cf`) and is not the bug. Delegation/critic subjobs that genuinely
inherit a live parent workspace are unaffected (they legitimately have a parent
workspace to wait on).

## Fix options

1. **Discriminate self-provisioned vs inherited** in
   `_resolve_subjob_inherited_workspace`. Preferred: gate the inherit path on
   `job.get("worktree_path")` (or a dedicated `context.inherit_workspace=True`
   flag stamped at spawn in `_spawn_scholar_subjob`) rather than on the presence
   of `context.workspace_container` / `context.vm`. A self-provisioned subjob
   (`worktree_path is None`) must return `("proceed", …)` regardless of whether
   its own workspace snapshot is present.
2. **Route dispatch-path subjob failures through the unblock handler** so a
   failed scholar (for any reason) flips its parent `waiting → created` instead
   of stranding it. Call `_handle_scholar_completion` (or a shared unblock
   helper) from the dispatcher `fail` branch at `main.py:~4695`.

Both are needed: (1) stops the false failure; (2) is defense-in-depth so no
future subjob-failure path can strand a parent.

## Repro

Create any root job with scholar enabled on a cluster where the parent has no
pre-existing workspace (the normal case). The scholar self-provisions, then fails
~600s later with `container=None, vm=None`, and the parent stays in `waiting`.

## Key references

- `orchestrator/main.py:3465` — `_resolve_subjob_inherited_workspace` (classifier + 600s wait)
- `orchestrator/main.py:3462` — `_INHERIT_WORKSPACE_MAX_WAIT_S` (`WORKSPACE_INHERIT_MAX_WAIT_S`, default 600)
- `orchestrator/main.py:~4677` — dispatcher call site + `fail` branch (`~4695`)
- `orchestrator/main.py:10336` — `_spawn_scholar_subjob` (root-job pre-research spawn; `waiting` hold; inherit-copy; `worktree_path`)
- `orchestrator/main.py:10503` — `_handle_scholar_completion` (parent unblock, incl. `is_failure`)
- `orchestrator/database/postgres.py:3385` — `get_dispatchable_jobs` (`status IN ('created','paused')`)
- Sibling: `docs/issues/subjob_inherits_stale_workspace_container_snapshot.md`
- Sibling pattern (failed subjob strands parent): `docs/issues/critic_failure_leaves_parent_job_stuck_reviewing.md`
