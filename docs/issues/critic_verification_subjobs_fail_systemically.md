# Critic (verification) subjobs fail systemically — three independent bugs (autonomy-ceiling GrantDenied, unroutable jobs-repo clone on inherited VMs, and an API that hides `error_message`)

**Status:** INVESTIGATION, root causes confirmed live on dev (`main` /
namespace `superhuman-remote-worker`) 2026-07-11. **No fix yet.** Three
distinct, independently-fixable defects surfaced while diagnosing two failed
critics the user flagged. Ranked below by 30-day volume. Findings #1 and #2
are the actual failure causes; finding #3 is why they were nearly invisible
(and why the first pass mis-diagnosed one of them).

**Motivating incidents:**
- Critic `42bbf782-bc39-491f-98f6-c421ed8431d0` (parent `7e45c299`,
  "Wissensdatein einspeisen", **container** backend) — failed 2026-07-10
  21:32:28.
- Critic `d8f09a2c-ff26-4051-9f3c-faff7dfd0c5d` (parent `2dbe6854`,
  "Hotel ERP UI Design Research", **VM** backend) — failed 2026-07-10
  20:59:05.

Both died at workspace init with the same recorded error:

```
Failed to clone project jobs repo 'project-<slug>-jobs' — refusing to fall
back to a disconnected git init (work would be lost on teardown). Check
jobs-repo URL reachability from this backend.
```

## TL;DR

Auto-spawned verification (critic) subjobs are the single largest source of
`failed` jobs on dev, and the failures are **not** the critic doing its job
badly — the critic never runs. Over 30 days:

| Class | Count | First → last | Root cause |
|---|---:|---|---|
| **autonomy_ceiling GrantDenied** | **19** | 06-19 → 07-10 | Finding #2 (dominant) |
| **VM workspace connection lost** | 12 | 06-16 → 07-04 | VM reachability (adjacent) |
| other | 4 | 06-12 → 06-18 | — |
| **jobs-repo clone failed (init)** | 2 | 07-10 | Finding #1 |
| reranker timeout | 2 | 07-05 | `reranker_timeout_kills_loop_job` |
| gpt-5.5 quota cooldown | 1 | 07-07 | model quota |
| LLM no-progress | 1 | 06-30 | — |

Query used:

```sql
SELECT
  CASE
    WHEN error_message LIKE 'Failed to clone project jobs repo%' THEN 'jobs-repo clone failed'
    WHEN error_message LIKE 'config exceeds your capability grants%' THEN 'autonomy_ceiling GrantDenied'
    WHEN error_message LIKE 'VM workspace connection lost%' THEN 'VM workspace connection lost'
    ... END AS error_class,
  count(*)
FROM jobs
WHERE config_name='critic' AND status='failed' AND created_at > now() - interval '30 days'
GROUP BY 1 ORDER BY 2 DESC;
```

When a critic dies, the parent is **not** lost: the `unstick_reviewing_parents`
watchdog (30-min grace, `stale_verification_sweeper`,
`orchestrator/database/postgres.py:2997`) flips the parent `reviewing` →
`pending_review` with *"Automated verification did not complete (critic
pipeline died); returned to manual review."* Confirmed live for `7e45c299` at
22:07:40. So the user-visible cost is: **automated verification silently never
happens, and every such job falls back to manual review.**

---

## Finding #1 — VM-inherited critics get an unroutable in-cluster jobs-repo URL, so the clone fails at init

**Severity:** medium (2 confirmed, plausibly feeds part of the 12 "VM workspace
connection lost" too). **Backend:** VM.

### Mechanism

A project critic clones the project **jobs repo as its workspace root on the
backend** (`src/core/workspace.py:406-429`,
`initialize_project_workspace`). If `GitManager.clone` returns falsy it fails
loud by design (F29 hardening — it refuses to silently `git init` and lose
every push on teardown):

```python
git_mgr = GitManager.clone(jobs_repo["repo_url"], self._workspace_path, backend=self._backend)
if not git_mgr:
    raise RuntimeError(f"Failed to clone project jobs repo '{repo_name}' — refusing to ...")
```

The jobs-repo URL is the cluster-internal Gitea host
(`http://…@srw-gitea:3000/srw/project-<slug>-jobs.git`). A VM backend is a
tailnet node that **cannot resolve/route `srw-gitea:3000`**. The dispatcher
already knows this and rewrites the URL to the ingress-routable host via
`externalize_gitea_url` — **but only inside the VM-injection branch, which is
gated on the VM being `ready`** (`orchestrator/main.py:1948-1965`):

```python
vm_ctx = _get_vm_context(job)
if vm_ctx.get("status") == "ready" and vm_ctx.get("ssh_host"):
    ...
    # F29: VMs are tailnet nodes and can't resolve srw-gitea:3000 ...
    git_remote_url = externalize_gitea_url(git_remote_url)
    for _repo in repositories_payload or []:
        if _repo.get("repo_url"):
            _repo["repo_url"] = externalize_gitea_url(_repo["repo_url"])
```

`_trigger_verification_on_complete` copies the parent's `context.vm` into the
critic **by value** at spawn time (`main.py:10520-10521`). When the parent VM
is no longer `ready` at critic dispatch — e.g. it was torn down / went
`workspace_unavailable` after the parent froze — the `status == "ready"` guard
is False, the whole VM branch is skipped, the URL is **never externalized**,
and the agent is handed `srw-gitea:3000`. The clone can't reach it → F29
RuntimeError → `failed`.

### Evidence (`d8f09a2c`, live)

Inherited VM context on the critic row:

| field | value |
|---|---|
| `context.vm.status` | `deleted` |
| `context.vm.previous_error` | `workspace_unavailable` |
| `context.vm.ssh_host` | `100.64.24.8` (tailnet) |
| `context.snapshot.error` | `unroutable tailnet target from orchestrator` |
| `context.git_remote_url` | `http://…@srw-gitea:3000/srw/project-68137e29-jobs.git` (NOT externalized) |
| `total_requests` | 0 |

So at dispatch the inherited VM was already `deleted`; the externalization
guard failed; the critic got the internal URL and failed the clone.

### Companion case (`42bbf782`, container) — same error, different cause

`42bbf782` ran on a **container** backend (`workspace_container` pod
`10.42.3.64`), which resolves `srw-gitea:3000` fine, and per
`workspace_intervals` the pod was alive `20:49:39 → 21:33:29` — still up when
the critic ran. Yet it hit the **same** F29 RuntimeError. The likely cause is
transient: the critic was dispatched to a warm-pool agent (`96726062`; its
sibling `2ad4568e` was born 21:18 and reaped together at 21:32:2x) that was
being **drained mid-clone**, so `GitManager.clone` was interrupted. This is an
interrupt, not a routing problem — but it shows the F29 fail-loud path also
turns a transient clone hiccup into a hard `failed` with no retry.

### Proposed fixes

1. **Externalize the jobs-repo URL for any VM-backend job, not only
   `status=="ready"`.** Decide "is this a VM job" from `vm.requested` /
   backend intent, not live readiness, so an inherited/not-yet-ready VM still
   gets a routable URL. (Alternatively externalize unconditionally when the
   resolved backend is `vm`.)
2. **Don't dispatch a critic onto an inherited VM that is not alive.**
   `_trigger_verification_on_complete` should resolve the parent's workspace
   at dispatch (re-provision or fail with a diagnosable message) rather than
   copying a stale `context.vm` by value — same class as
   `docs/issues/subjob_inherits_stale_workspace_container_snapshot.md`, which
   fixed the scholar/container path but left the critic/VM path.
3. **Make the F29 clone a bounded retry before fail-loud** so a transient
   interrupt (the `42bbf782` container case) doesn't burn the whole
   verification.

---

## Finding #2 — auto-spawned critics force `autonomy: "full"` but are admitted against the parent-owner's capability grants → GrantDenied every time (dominant, 19/30d)

**Severity:** high (19 failures, ~daily, one scheduled workload fully losing
verification). **Backend:** any.

### Mechanism

The critic factory hardcodes full autonomy
(`orchestrator/main.py` ~10525, inside `_trigger_verification_on_complete`):

```python
config_override = {
    "autonomy": "full",
    "tools": {"evaluation": ["approve_job", "return_job_with_feedback"]},
}
```

At dispatch, the subjob is admitted against the **parent-owner's** capability
grants. The grant check (`src/core/capability_grants.py:167-171`) defaults
`autonomy_ceiling` to `"review"`:

```python
grants.get("autonomy_ceiling", "review")
... f"autonomy_ceiling: autonomy '{fragment.get('autonomy')}' exceeds the ceiling"
```

and the dispatcher records
`error_message=_grant_violations_detail(gd.violations)`
(`orchestrator/main.py:2185`, `:2359`; message assembled at `main.py:3410`).
Any owner whose ceiling is below `full` gets a **dead-on-arrival critic** —
the config the system itself injected is rejected by the system's own gate.

### Evidence (live)

All 19 are the **same user** `b9878681`, `project_id` NULL, a **different
parent each day**, clustered ~09:xx (plus one 16:21) — i.e. a **daily
scheduled job** whose critic fails admission every run:

| parent | user | created |
|---|---|---|
| `82c38e0e` | `b9878681` | 07-10 09:44 |
| `927489a9` | `b9878681` | 07-09 09:32 |
| `30eab374` | `b9878681` | 07-08 09:40 |
| `0c30afa2` | `b9878681` | 07-06 09:47 |
| `de3e8554` | `b9878681` | 07-05 09:44 |
| … | … | (19 total, 06-19 → 07-10) |

### Proposed fix

Verification/curation subjobs are **system-spawned**, not user-authored — they
should not be held to the human owner's ceiling. Options:
- Admit system-spawned subjobs under a **service principal** (the same fix
  already proposed for ownerless MCP jobs —
  `docs/issues/mcp_jobs_ownerless_grant_denied.md` /
  `issue_mcp_jobs_ownerless_grant_denied`), or
- Exempt `parent_job_id IS NOT NULL` lifecycle subjobs (critic/curator/scholar)
  from the `autonomy_ceiling` check, or
- Stop hardcoding `autonomy: "full"` and instead clamp the critic's autonomy to
  the owner's ceiling (a `review`-autonomy critic still verifies; it just pauses
  instead of self-approving).

Related: `docs/issues/session_permission_mode_grant_denied.md`.

---

## Finding #3 — `GET /api/jobs/{id}` (and MCP `get_job`) never return `error_message` / `error_details`

**Severity:** medium (observability). This is why a failed job looks reasonless
in Cockpit and over the API, and why the first diagnosis of `42bbf782`
mis-attributed the cause: the API reported `error_message: null` while the DB
row held the real clone error the whole time (both written atomically at
failure, `updated_at == fail time`).

### Mechanism

`postgres_db.get_job` (`orchestrator/database/postgres.py:806-841`) selects an
explicit column list that **omits `error_message` and `error_details`**:

```sql
SELECT j.id, j.status, j.config_name, j.expert_id, j.config_override, j.resolved_config,
       j.assigned_agent_id, j.user_id, j.project_id, j.parent_job_id, j.priority,
       j.branch_name, j.repo_name, j.merge_status, j.repo_merge_statuses, j.freeze_data,
       j.cloud_diff_baseline_commit, j.diff_status, j.exported_folder_handle, j.exported_at,
       j.creation_order, j.worktree_path, j.delegation_context,
       j.created_at, j.updated_at, j.description, j.context, ...
FROM jobs j ...
-- no error_message, no error_details
```

The `/api/jobs/{id}` handler (`main.py:6618`) returns this dict verbatim, so
the fields are absent (serialize as null). MCP `get_job` uses the same read
path, so `get_job` / `get_job_summary` never surface a failure reason either.

### Evidence (live)

```
API  GET /api/jobs/42bbf782... → error_message: None
API  GET /api/jobs/d8f09a2c... → error_message: None
DB   both rows → error_message = "Failed to clone project jobs repo …"
```

### Proposed fix

Add `j.error_message, j.error_details` to the `get_job` SELECT and surface them
in the job-detail response + Cockpit failed-job view. Audit sibling read paths
(`list_jobs`, `get_job_summary`, MCP formatters) for the same omission.

---

## Appendix — dev-cluster forensics recipe

- MCP `main-dev-cluster` = the remote dev cluster, reachable by kube context
  `--context=main -n superhuman-remote-worker` (NOT local k3d, which is a
  separate DB).
- App DB: `srw-postgres-0`, `psql -U srw -d srw`. **Trust the DB, not the API,
  for `error_message`** (finding #3).
- Audit store: `srw-auditdb-0`, `psql -U srw -d srw_audit` (tables:
  `llm_requests`, `agent_audit`, `chat_history`, `usage_events`). A critic that
  fails at init has **0** rows in all of these.
- `workspace_intervals` (owner_id = job id) gives pod/VM start/end times.
- REST: `https://api.srw.works` + `Authorization: Bearer <MCP token from repo
  .mcp.json>`.
- Orchestrator pod logs retain only since the last restart — dispatch-time
  logs for a failure older than that are gone.
