---
tags:
  - issue
  - fix-spec
  - jobs
  - workspace-lifecycle
  - sudo-gate
  - vm-upgrade
  - remote-backend
  - reaper
---

# A job paused for VM-upgrade approval gets its workspace reaped before the operator can approve


**Closed by the 2026-08-06 doc-truth sweep (batch #3):** Shipped `e3751793` — all six fixes + the sudo-gate bonus at HEAD; tests/test_sudo_vm_upgrade_decisions.py 26/26 green. Carried caveat: the approve→restore-into-fresh-VM arm and the 30-min grace reap were never live-smoked.

**Status:** investigated 2026-07-08 from a live incident on the main cluster
(`superhuman-remote-worker`); **research-refined 2026-07-09** (4 codebase lanes + 2
verified web-prior-art lanes); **IMPLEMENTED 2026-07-09** — all six fixes landed
(see "Implementation status" below for what shipped vs. what was found already
done vs. the one explicitly deferred piece). Unit-tested
(`tests/test_sudo_vm_upgrade_decisions.py` + updated lifecycle tests; full
suite 8281 green) and **k3d-smoked live**: migration 0049 applied, jobs list
returns `pending_approval` + request id, and a planted frozen job was denied
end-to-end through `POST /deny` — request row `denied` with decider identity,
job unwedged (freeze cleared, sticky `sudo_denial` + reasoned
`queued_feedback` in context), repeat-deny a visible no-op, conflicting
approve a 409. Not live-smoked (needs real agents/VM provisioner): the
approve→VM arm, the /complete auto-deny + freeze-capture, and the 30-min
grace-reap timing. Uncommitted.
**Severity:** high — the job is unrecoverable and the loss is silent. Any job that
hits the sudo/VM-upgrade gate and isn't decided within the reaper's window (next
tick, ~60 s) dies on decision/resume, even though the approval request itself is
valid for **24 h**. For VM-tier and emptyDir workspaces the agent's live state is
destroyed (only phase-boundary git commits survive). A second, independent defect
found during research: **approving via the generic endpoints (incl. MCP) wedges the
job forever** even when the workspace is still alive.
**Component:**
- `orchestrator/services/lifecycle/workspace_manager.py`, `vm_manager.py`,
  `reconciler.py` (reap predicates, no grace for `paused`)
- `orchestrator/services/completion.py` (`vm_upgrade_required` → `paused`)
- `orchestrator/services/sudo_gate.py` + `orchestrator/main.py` approve/deny
  endpoints (decision paths)
- `orchestrator/services/snapshot_service.py`, `workspace_suspension.py`
  (capture/restore substrate)
- `src/core/capability_grants.py` + grant PEPs in `main.py` (auto-deny / admin)
- `cockpit/src/app/views/jobs/job-list.component.ts` + jobs-list SQL (UX)
**Observed on:** main cluster, job `b5b0c0d0-e7b4-403b-a143-dd8d9f98ae51`
("project management software", `default` config, `gemma-4-moe`), sudo request
`98d88547`, workspace `workspace-b5b0c0d0-e7b` / VM `agent-vm-b5b0c0d0-e7b`.

---

## TL;DR

The sudo gate freezes a job as `vm_upgrade_required` and pauses it so a human can
decide whether to upgrade it to a VM. That approval request is deliberately given a
**24 h TTL** — "operator decides in their own time" (`sudo_gate.py:588`). But the
lifecycle reaper classifies **`paused` as an idle, reapable state**
(`workspace_manager.py:53`) with **no grace window** — a paused job is reapable on
the very next reconciler tick — and nothing links the reap decision to the open
approval. The workspace is destroyed minutes into a 24 h decision window.

By the time anyone decides (or hits Resume), the workspace is gone. Resume
reconnects to the recorded endpoint, gets NXDOMAIN, exhausts
`WORKSPACE_RECOVERY_MAX_ATTEMPTS`, and fails the job as `workspace_unavailable`.

**The correct model is reap-and-restore, not hold-alive** (decided 2026-07-09, and
confirmed as the industry-consensus shape — no surveyed system holds compute for a
pending approval): durably capture workspace state at the pause, keep the container
warm only for a short idle grace, reap it, and make the approve/deny decision
re-provision + restore + continue. What makes today's behaviour a bug is not that
it reaps — it's that the reap is **destructive** and the decision path has **no
restore step** (and, for deny and all generic approve paths, no job action at all).

## Timeline (observed, job `b5b0c0d0`)

| Time (UTC, 2026-07-08) | Event |
|---|---|
| 08:24:01 | Job created (`default`, `gemma-4-moe`). Its scholar research subjob `f8326e32` runs and completes; output exported to OpenCloud (`sessions/job-f8326e32a3ea`). |
| 09:39–09:54 | Agent works phases 4–8. Phase boundaries committed to Gitea repo `job-b5b0c0d0` — 20 commits, last `04b82c19` "[Phase 8 Strategic] Complete" at **09:54:30**. |
| 09:55:58 | Agent runs `sudo docker --version` in its (non-VM) workspace. |
| 09:56:08 | **Sudo gate fires** → freeze `vm_upgrade_required` → job `paused`; `sudo_approval_requests` row `98d88547` inserted (`vm_upgrade`, 24 h TTL). Last audit entry: *"paused while the operator decides whether to upgrade this job to a VM environment."* |
| ~09:56 → ~15:47 | **Zero job activity for ~6 h.** Workspace reaped during this window; `context.vm.status` → `deleted`, DNS for `workspace-b5b0c0d0-e7b…svc.cluster.local` no longer resolves. |
| ~15:47:42 | Operator hits **Resume**. Orchestrator tries to reconnect → `Failed to connect to workspace … [gone]: [Errno -2] Name or service not known`; recovery exhausted after 3 attempts → job `failed` (`freeze_type: workspace_unavailable`, `recovery_attempts: 4`). |

The sudo request `98d88547` remained `PENDING` throughout. An approval email *was*
received and (reportedly) acted on by an operator — but no approval ever registered
on the request row, consistent with the operator landing on the job's Resume button
rather than the inbox Approve controls, or an approve click failing silently
against the dead workspace.

## Root cause (confirmed in code)

**A TTL mismatch between the approval window and the reaper's window, with no
durable capture underneath.**

### 1. The pause is legitimately long-lived
`completion.py:577` maps the gate freeze to a paused job. Deliberately, `vm_upgrade_required`
is **NOT** in `AUTO_REDISPATCH_FREEZE_TYPES` (`completion.py:268-275`), so `/complete`
leaves `freeze_data` on the row — which parks the job invisible to the auto-dispatcher
(`get_dispatchable_jobs` requires `freeze_data IS NULL`) until a human decides.
`sudo_gate.insert_vm_upgrade_request` (`sudo_gate.py:577-636`) records the decision
with a **24 h** TTL, by design.

### 2. The reaper treats that same pause as immediately disposable
`workspace_manager.py:53`:
```python
_IDLE_JOB_STATUSES = frozenset(
    {"paused", "pending_review", "reviewing", "waiting_for_reply"}
)
```
`is_idle()`/`is_reapable()` return `True` for `paused` **with no grace window** —
`is_idle` explicitly documents that it does *not* gate on `last_activity` ("react as
soon as the bound work is in a quiescent state, regardless of how long",
`workspace_manager.py:232-250`), and `Instance.metadata` built in `list_instances`
carries no `paused_at`/`last_activity` at all. The only reap guards are
`has_live_shared_child` and `bound_row_missing` — **nothing checks for an open
sudo/VM approval.** Same in `vm_manager.py:196` for VMs.

### 3. The reap is destructive for exactly the workspace classes that matter
The reconciler's snapshot-before-delete lives in `_reap` (`reconciler.py:254`,
snapshot at `:271-275`), delegating to `SnapshotService.capture_vm_snapshot`
(SSH-tar → S3/MinIO, `snapshot_service.py:300`). Per workspace class:
- **PVC-backed pods** — already safe for *same-tier* pause-reap: `delete()` retains
  the PVC for non-terminal jobs (`workspace_manager.py:551`) and `give_up()`
  recreates the pod against it (`:437`). But the **upgrade-to-VM path abandons the
  PVC** (fresh VM, different backend) — cross-tier approve is lossy even here.
- **emptyDir pods** — snapshot works while the pod is reachable, but nothing forces
  it at pause time; if capture is skipped/exhausted, `give_up()` force-deletes with
  no recreate (`:413-439`) and S3 was the only copy.
- **VMs** — capture is a **structural no-op**: the orchestrator is not a tailnet
  member, so `orchestrator_can_reach()` (`ssh_helpers.py:49`, `_TAILNET_NET
  100.64.0.0/10:33`) rejects the VM's ssh_host and `capture_vm_snapshot` returns
  `capture_skipped` (`snapshot_service.py:331-354`). `give_up()` is an explicit
  force-delete with *"no volume-reattach (delete is destructive)"*
  (`vm_manager.py:315-322`). A paused VM job is reapable + dirty + unreachable →
  bounded attempts → destroyed. **This is what killed `b5b0c0d0`.**
- **Graph checkpoint** — default `CHECKPOINTER_BACKEND=sqlite` (`db_url.py:54`)
  stores the LangGraph checkpoint **inside the workspace**
  (`workspace/checkpoints/job_<id>.db`, `agent.py:1077,3375-3384`) — so losing the
  workspace loses the reasoning state too. The cross-pod `AsyncPostgresSaver`
  backend exists (`agent.py:1073`) but is not yet the chart default.

### 4. The decision paths can't recover — and two of three wedge the job outright
There are **three** approve surfaces (found 2026-07-09):
- `POST /api/sudo/requests/{id}/approve` (generic; **also what the MCP
  `approve_sudo_request` tool calls**, `mcp/server.py:2596-2618`) →
  `sudo_gate.approve_request()` **only flips the row**; the NULL `nats_reply_subject`
  makes the reply a no-op (`sudo_gate.py:682-688`). `freeze_data` stays set → job
  invisible to the dispatcher → **wedged forever, silently.** Same for `/deny`.
- `POST /api/sudo/requests/{id}/approve-upgrade` (`main.py:8129-8148`) → the only
  path that acts: `upgrade_job_to_vm()` (`main.py:8691-8817`) does the correct
  Continue-as-New skeleton — clears `freeze_data`, `assigned_agent_id=NULL`,
  `status='paused'`, `context.vm.requested=true`, `_trigger_dispatch()` — but
  performs **no restore** of any snapshot into the fresh VM; it silently relies on
  a checkpoint that the reap may have destroyed.
- `POST /api/sudo/requests/{id}/resume-without-vm` (`main.py:8151-8170`) →
  `approve_job()`, which sets `status='processing'` **without unassigning or
  re-provisioning** — it assumes the original workspace is still live (exactly what
  the reap destroys) and injects **no denied tool result**.

**A real deny path does not exist in any form.**

## Additional defects surfaced by the research

- **D2 — generic approve/deny (and MCP) silently wedge the job** (see §4 above).
  This is a live bug independent of the reaper: even with the workspace alive, an
  operator approving from the MCP tools or any generic client parks the job forever.
- **D3 — `upgrade_job_to_vm` is itself ungated** (`main.py:8691` checks only
  `require_job_access`); VM-grant enforcement is deferred to the dispatcher
  pre-flight (`_check_vm_permission`, `main.py:3892/3906`), which fails ungrantable
  jobs with a generic 403 instead of a graceful outcome.
- **D4 — admin premise of the incident corrected.** The job creator (`kai`,
  `082c4027…`) has `users.is_admin = TRUE` (verified in the main-cluster DB
  2026-07-09), and **admin already bypasses every grant check**: the PDP takes
  `is_admin` (`capability_grants.py:134`), and the PEPs short-circuit
  (`main.py:3095, 3133, 3196-3197, 3245, 3261, 3926`) — commits `25283b38`
  (2026-04-23) and `a7ad2be0` (2026-06-18), both predating the incident. So the VM
  check should never have blocked him, and the post-incident "give him the VM grant"
  was very likely unnecessary. What misled everyone: the **Grants UI shows an admin
  with zero grant rows**, which reads as "no VM permission" — an admin-looks-
  unentitled UX gap, not a missing bypass. (Caveat: the *deployed* main-cluster
  build wasn't diffed against these commits — verify before closing fix #5's
  option A as already-done.) One real footgun remains:
  `postgres_db.user_can_use_vm` (`postgres.py:9203-9224`) is **admin-agnostic**;
  it's safe today only because every caller is `_check_vm_permission`, which checks
  `is_admin` first — any new direct caller (e.g. fix #4's auto-deny) would
  misclassify an admin.
- **D5 — the job list hides the wait.** STATUS shows a bare `Paused`/`Created` chip
  and a green **Resume** button (`job-list.component.ts:305` desktop, `:223` mobile
  kebab) for a job that is actually blocked on an approval; the list has zero sudo
  awareness (`JobSummary` has no approval field). The **email path is already
  correct** — it deep-links to `/inbox?sudo=<requestId>` (`email.py:269-275`) where
  Approve/Deny/Upgrade controls exist (`inbox-page.component.ts:327-357`,
  query-param select at `:1567-1580`).
- **D6 — `PENDING`-forever display.** The 24 h TTL keeps `list_sudo_requests`
  showing `PENDING` long after the workspace it depends on is gone.

## Implementation plan (research-refined 2026-07-09)

The model: **reap-and-restore.** The workspace-relative tar archive in S3/MinIO is
the durable contract; the runtime (pod or VM) is disposable; the approve/deny
decision — not Resume — triggers re-provision + restore + continue. This is the
Gitpod-Classic production pattern (tar → S3 → restore into a fresh container) and
the Airflow-deferrable shape (free the compute, persist a resume point, re-queue on
signal). CSI VolumeSnapshots are a dead end on our stack (k3s local-path-provisioner
has no snapshot support), and VMM-level snapshots (savevm/Firecracker-style) solve
process resume we don't need — LangGraph checkpoint already externalizes it.

### Fix 1 — durable capture at the gate pause (the precondition)

*Exists:* `SnapshotService.capture_vm_snapshot` (SSH-tar → S3, includes
`/home/agent-host/` + `/usr/local/`, excludes `repos/`+`node_modules/`, 10 GB cap,
`snapshot_service.py:300-416`); `workspace_suspension` suspend/restore
(`workspace_suspension.py:110/252/436`); phase snapshots (`src/core/phase_snapshot.py`).

*Add:*
1. **Force a snapshot at the `vm_upgrade_required` freeze**, before the job becomes
   reapable. Hook points: the agent-side freeze consumer (`src/graph.py:4286-4305`,
   alongside the existing `job_frozen.json` write + git commit — the agent can tar
   from *inside*, sidestepping orchestrator reachability) and/or the `/complete`
   vm_upgrade branch (`main.py:11009`) for pod workspaces the orchestrator can reach.
2. **Flip `CHECKPOINTER_BACKEND` to `postgres` in the chart** (the saver exists,
   `agent.py:1073`; cross-pod checkpointer is live-verified) so reasoning state
   never lives only inside the workspace. Treat "checkpoint persisted" as part of
   the freeze contract.
3. **VM capture:** until the persistent-rootdisk feature lands
   ([`../features/vm_persistent_rootdisk.md`](../features/vm_persistent_rootdisk.md)
   — the proper substrate; disk survives VM deletion, reattached by name), either
   snapshot from inside the guest (agent-side tar-to-S3 at freeze, as in (1)) or
   accept `ORCHESTRATOR_HAS_TAILNET_ROUTE`. Do **not** build on
   `orchestrator_can_reach` for VM tier — it is `False` by construction
   (`ssh_helpers.py:49`).
4. **Archive is the cross-tier contract:** workspace-root-relative tar, one
   canonical uid across workspace image and VM golden image, extract with
   `--numeric-owner` by the provisioner (init-container for pods; cloud-init /
   VM-controller first-boot pull-from-MinIO for VMs) *before* the agent attaches.
   Container→VM restore then falls out for free. Avoid virtiofs as a restore
   transport (uid-mapping traps). No fsfreeze needed — the workload is stopped;
   `sync` in-guest before a VM tar.
5. Everything between freeze and snapshot must be **idempotent** — on restore the
   graph re-enters at the checkpoint and the gated tool call is replayed
   (LangGraph re-executes pre-interrupt code; this is our exact failure shape if
   restore replays a partial turn).

### Fix 2 — warm grace, then reap; TTL decoupled from workspace lifetime

*Exists:* the suspension sweep already implements snapshot-then-free after
**`WORKSPACE_IDLE_TIMEOUT` (default 30 min)** for `paused`/`pending_review`/
`waiting_for_reply` jobs (`workspace_suspension.py:781,102-104,828-843`); the
orphan age-gate shows the predicate pattern (`_orphan_grace_s`,
`workspace_manager.py:378`).

*Add:* a grace gate for `paused` in **both** managers' `is_idle`/`is_reapable`
(`workspace_manager.py:232/252`, `vm_manager.py:196`): plumb a
`paused_at`/`last_activity` timestamp into `Instance.metadata` in `list_instances`
(currently absent) and treat `paused` as reapable only after the grace window
(reuse/align with `WORKSPACE_IDLE_TIMEOUT`; 30–60 min matches the
Codespaces/Gitpod/Coder consensus). Fast approver → instant lossless resume on the
warm workspace; slow approver → pays a restore. The `sudo_approval_requests` 24 h
TTL is untouched and fully decoupled. Retain the S3 archive for as long as the job
is paused, plus ~30 days past terminal (extend when uncommitted git changes exist —
Gitpod's 14-vs-28-day distinction).

### Fix 3 — restore-and-decide; a real deny path; unwedge the generic endpoints

*Exists:* the approve skeleton `upgrade_job_to_vm` (`main.py:8691-8817`) +
dispatcher Continue-as-New contract (`get_dispatchable_jobs` `postgres.py:3099-3161`,
CAS claim `:2950-2979`, `pause_job` `:973-1002`) + fresh-agent resume
(`restore_todo_state`, `graph.py:3532-3586`). The tier-upgrade design doc names this
the template (`docs/features/workspace_tier_upgrade.md` §2.1; its W3 worker-VM arm
is explicitly deferred — this fix is effectively W3).

*Add:*
1. **Approve:** extend `upgrade_job_to_vm` (or the fresh-agent resume path,
   `_resume_job_on_agent` `main.py:2155`) to **restore the archive + checkpoint into
   the newly provisioned VM** before the graph re-enters; the gated command then
   re-runs under `sudo_action="allow"`. Keep approval and resume as **two steps**
   (approval never auto-guarantees the run — re-provisioning can fail and must
   surface, not silently wedge; GitLab's model). Bound the upgraded VM's elevated
   lifetime rather than leaving open-ended escalation (AWS-TEAM lesson).
2. **Deny (new):** like `upgrade_job_to_vm` but without `vm.requested`: clear
   `freeze_data`, unassign, re-provision the **original tier**, restore, and
   **inject a denied tool result** into the checkpointed history so the gated
   `run_command` returns *who denied it, why, and what to do instead* (e.g. "sudo
   denied by operator — continue without elevated privileges; do not re-attempt
   sudo"). Reason-less denials demonstrably cause agent retry loops. Make the
   denial **sticky** for the job so a re-request of the same escalation auto-denies
   without re-freezing.
3. **Unwedge the generic surface (D2):** `approve_request`/`deny_request` must
   branch on `request_type='vm_upgrade'` (uniquely identified by
   `nats_reply_subject IS NULL`) and dispatch to (1)/(2) — so the generic REST
   endpoints (`main.py:8092-8126`) and the MCP tools (`server.py:2596-2643`) drive
   the job instead of only flipping the row.
4. **Decision hygiene:** decisions idempotent and bound to the immutable request id
   (first-decider-wins, second decision = visible no-op — PIM; never "the currently
   pending item" — GitLab's stacking race); a TTL-expired request **rejects** late
   decisions (Step-Functions stale-token model).
5. **Expiry:** when the 24 h TTL lapses undecided, fail the job loudly with an
   explicit **`vm_upgrade_expired`** reason ("approval window closed; re-run") —
   never route it through the generic `workspace_unavailable` recovery-exhausted
   arm (`main.py:10588-10605`).

### Fix 4 — auto-deny an approval the requester can never satisfy

*Insertion point:* `main.py:11009` (the `ft == "vm_upgrade_required"` branch of the
`/complete` handler), **before** `insert_vm_upgrade_request` — the job row,
`postgres_db.get_user`, `user_can_use_vm`, `resolve_grants_for` + `evaluate` are
all in scope there. `SudoGateService` itself holds only a DB pool; the check does
not belong inside it.

```python
owner = await postgres_db.get_user(str(job["user_id"])) if job.get("user_id") else None
can_satisfy = owner and (owner.get("is_admin") or await postgres_db.user_can_use_vm(owner))
if not can_satisfy:
    # No decision a human can make → don't raise an unanswerable approval.
    # Route directly to the fix-3 deny arm (original tier + denied tool result).
else:
    sudo_request_id = await sudo_gate.insert_vm_upgrade_request(...)
```

This is standard JIT-access practice, not an optimization (Teleport rejects
unsatisfiable requests at creation by construction; GCP PAM only lets entitled
principals request). **Still write the auto-denied request row** (status
`auto_denied` — the enum already exists in `list_sudo_requests`) for audit parity.
Ensure the deny arm prevents the job from ever reaching dispatch with
`context.vm.requested=true` for an ungrantable user — otherwise the dispatcher's
`_check_vm_permission` (`main.py:3906`) fails it with a generic 403 (D3).

### Fix 5 — admin semantics: harden + make the data/UI tell the truth

Option A (short-circuit) is **already implemented** at every choke point (see D4).
Remaining work:
1. **Harden `postgres_db.user_can_use_vm`** (`postgres.py:9203`) with an
   `is_admin` guard — the one admin-agnostic primitive, and a direct dependency of
   fix #4's predicate.
2. **Option B for robustness (recommended):** seed admin grants at user creation
   (`upsert_user_from_oidc`, `postgres.py:6991/7079` — currently seeds **no** grant
   rows for anyone) + a one-time backfill migration
   (`0049_backfill_admin_grants.sql`, following 0030's idempotent
   `INSERT … SELECT … ON CONFLICT DO NOTHING` precedent, lines 51-66, filtered
   `WHERE is_admin = TRUE`: `vm_workspace=true`, optionally `shell_tools`,
   `delegation`, `autonomy_ceiling='full'`, `permission_mode='autonomous'`). This
   fixes the *data* so no future callsite needs to special-case admin — enforcement
   is currently spread across ≥6 PEP sites and only the PDP + `user_can_use_vm` are
   central.
3. **UI truth (the actual incident confusion):** the Grants page / `/api/users/me/
   capabilities` should render admins as "Admin — unrestricted" instead of an empty
   grant list that reads as "no permission". Mirror PIM/PAM: bypass = auto-approve
   *with a record*, never a silent absence.

### Fix 6 — job list tells the truth; Resume gated behind the decision

*Exists:* inbox deep-link + controls + email link (see D5) — no routing or email
work needed.

*Add (smallest honest version):*
1. **SQL:** in both jobs-list builders (`postgres.py` `get_jobs` :691-704,
   `get_visible_jobs` :767-780 — both already compute booleans via LEFT JOIN) add
   `EXISTS(SELECT 1 FROM sudo_approval_requests s WHERE s.job_id=j.id AND
   s.status='pending' AND s.expires_at>NOW()) AS pending_approval` plus the request
   id via `LEFT JOIN LATERAL … LIMIT 1` so the button can deep-link without a
   second fetch.
2. **Model:** add both fields to `JobSummary`
   (`cockpit/src/app/core/models/audit.model.ts:104-127`).
3. **Chip:** in the status cell (`job-list.component.ts:181-200`, tone map
   `jobStatusTone` :1108-1128) render `Waiting · approval` with a warning tone when
   `pending_approval` (today `paused`→neutral, `created`→info — the bland chip from
   the incident screenshot).
4. **Button:** guard the Resume `@if` (:305 desktop) and the mobile kebab item
   (:223) with `&& !row.job.pending_approval`; add an **"Approve request"**
   button/menu-item when `pending_approval`, navigating
   `router.navigate(['/inbox'], { queryParams: { sudo: pending_approval_request_id } })`
   (pattern: `goToReview()` :1276). Resume becomes unreachable for an
   approval-blocked job. GitHub-Actions precedent: an explicit "Waiting" state in
   run lists.
5. i18n strings (`jobs.status.waiting_approval`, `jobs.action.approveRequest`).

*Fuller version:* subscribe the list to the sudo SSE `request_decided` event
(`sudo.service.ts:180`) so the chip clears instantly; carry `request_type` +
`expires_at` for a countdown; a "Waiting · approval" filter chip; reword the email
button "Reply in Cockpit" → "Approve in Cockpit".

## Implementation status (2026-07-09)

All six fixes are implemented; unit suites green. What actually shipped:

- **Fix 1 (capture at pause):** `/complete`'s `vm_upgrade_required` branch now
  fires a background `_capture_workspace_snapshot_for_freeze` (main.py) —
  SSH-tar → S3 while the workspace is certainly alive; unreachable tailnet
  targets skip visibly inside the snapshot service. **Fix 1.2 was already
  done upstream:** the chart defaults `checkpointer.backend: "postgres"`
  (`helm/values.yaml:475`), so reasoning state no longer lives inside the
  workspace on chart deploys; `db_url.py`'s sqlite default only applies to
  bare-metal runs.
- **Fix 2 (warm grace):** `paused_grace_seconds()` / `paused_within_grace()`
  in `workspace_manager.py`, gating `is_idle`/`is_reapable` in BOTH managers;
  `job_updated_at` plumbed into `Instance.metadata` (pods via the bound row,
  VMs via `_fetch_vm_rows`). Env: `WORKSPACE_PAUSED_REAP_GRACE_S`, defaulting
  to `WORKSPACE_IDLE_TIMEOUT` (minutes, default 30) so the suspension sweep
  gets first claim. Unknown pause age = inside grace (never destroy on
  missing data).
- **Fix 3 (restore-and-decide):** the endpoint bodies were extracted into
  request-free internals — and in the process a **third live bug** surfaced:
  `/approve-upgrade` called `upgrade_job_to_vm(str(job_id))` and
  `/resume-without-vm` called `approve_job(str(job_id))` with the FastAPI
  `Request` parameter missing → TypeError→500 *after* flipping the row (and
  `approve_job` would 400 on a paused job anyway). Now:
  `_apply_vm_upgrade_decision()` is the single driver behind all four
  endpoints (and the MCP tools via REST): first-decider-wins on the row flip,
  expired-rejects-late-decisions, and an idempotent re-drive when the same
  decision is repeated while the job is still frozen (this also **recovers
  historically wedged jobs**). Deny/resume-without-vm →
  `_resume_job_without_vm_internal()`: sticky `context.sudo_denial` +
  reasoned `queued_feedback`, freeze cleared, unassigned, `paused`,
  dispatch triggered. Both dispatch paths apply `_apply_sticky_sudo_denial`,
  which flips the agent's sudo gate to `block` with a reasoned
  `shell.sudo_block_message` (new config key threaded through
  `RemoteBackend` + `ShellManager`) so the replayed command can't re-freeze.
  Same-tier restore-after-reap: `WorkspaceInstanceManager.delete()` now marks
  a reaped non-terminal emptyDir workspace with an available snapshot as
  `'suspended'` instead of `'deleted'`, so `ensure_workspace` routes the next
  dispatch through the S3 restore instead of a blank re-create.
  **Deferred:** extracting the S3 archive *into a fresh VM* on cross-tier
  approve — needs the presigned-pull/cloud-init transport or
  persistent-rootdisk (fix 1.3/1.4); until then the approve arm rides on the
  postgres checkpoint + Gitea phase-boundary seed, same as before, with the
  archive already captured for when the transport lands.
- **Fix 3.5 (expiry):** `_fail_expired_vm_upgrade_jobs()` runs in the sudo
  expiration sweeper: any paused vm_upgrade-frozen job whose requests are all
  expired (none pending) fails loudly with a `vm_upgrade_expired` message and
  cleared freeze (so Resume raises a FRESH request) — and it heals historical
  expired-wedges, not just new ones.
- **Fix 4 (auto-deny):** `/complete` pre-checks the owner via
  `_check_vm_permission` (kill-switch + admin + grant); an unsatisfiable
  request writes an `auto_denied` row (extended
  `insert_vm_upgrade_request(status=, decision_reason=)`, no SSE/no email)
  and routes straight to the deny arm. Infra failure during the pre-check →
  raise the approval normally (never guess). If the auto-deny resume fails,
  the job stays paused and the `auto_denied` row is re-drivable via the deny
  endpoint.
- **Fix 5 (admin):** `user_can_use_vm` now short-circuits on `is_admin`;
  migration `0049_backfill_admin_grants.sql` seeds max-level rows for
  existing admins (idempotent, 0030 pattern; demotion caveat documented in
  the header); `auth.py` seeds on first admin login / promotion
  (`seed_admin_grants`); the Admin→Grants page shows an "Admin —
  unrestricted" banner for admin users and marks them in the user picker
  (`/api/users/me/capabilities` already returned `grants: null` for admins).
- **Bonus fix (found during the k3d smoke):** the sudo gate was only
  DB-connected inside the NATS bridge (`nats_bridge.py:181`), so on any
  deployment without NATS every `/api/sudo/*` endpoint 404'd and the
  vm_upgrade freeze never even created its approval row. The gate now
  DB-connects unconditionally at startup (`main.py` lifespan); the NATS
  bridge re-connects it with the NATS handle when available (live
  sudo_command daemon requests still need that).
- **Fix 6 (job list):** both jobs-list builders carry
  `pending_approval` + `pending_approval_request_id` via a `LEFT JOIN
  LATERAL` on open, unexpired `sudo_approval_requests`; `JobSummary` gained
  both fields; the status cell shows a warning `Waiting · approval` chip in
  place of the bland status chip; Resume is unreachable while an approval is
  pending and an **Approve request** button/menu-item deep-links to
  `/inbox?sudo=<id>` (desktop + mobile kebab); en + de strings added.

### Suggested sequencing

1. **Fix 6 + D2-unwedge (fix 3.3) + fix 4 + fix 5.1** — small, independent, stop
   the silent wedges and the misleading UX immediately; no reaper/snapshot risk.
2. **Fix 1 (capture at pause) + fix 2 (grace)** — makes the reap non-destructive;
   pods first (infrastructure exists), VMs ride on persistent-rootdisk or
   agent-side capture.
3. **Fix 3.1/3.2 (restore-on-approve, deny path) + 3.4/3.5 (hygiene, expiry)** —
   completes restore-and-decide.
4. **Fix 5.2/5.3** — data seeding + UI truth, anytime.

## Prior art (verified 2026-07-09, primary sources)

- **No surveyed system holds compute for a pending approval** — Teleport/PIM/PAM/
  AWS-TEAM decouple access from workload; Temporal/Step Functions/Airflow release
  compute and resume on signal; GitHub's 30-day warm "Waiting" runs are the
  cautionary tale.
- **Auto-deny unsatisfiable requests at creation** is how Teleport
  (`allow.request.roles`) and GCP PAM (entitlement principals) work by construction.
- **Admin bypass = auto-approve with a record** (PIM/PAM), never a silent no-record
  path.
- **Denial must carry reason + alternative and be sticky** — OpenAI Agents SDK
  `reject({message})` / `alwaysReject`; bare denials produce documented retry loops.
- **Decision hygiene:** first-decider-wins + notify (PIM); decisions bound to
  immutable request ids (GitLab approval-stacking race); expired gates reject late
  decisions (Step Functions token regeneration).
- **Tar-to-object-storage is the production pattern for workspace pause** (Gitpod
  Classic); CSI snapshots unavailable on k3s local-path; VMM memory snapshots
  (savevm ~9× write amplification; Firecracker path/host-bound, disks excluded) are
  the wrong tool when process state is externally checkpointed.
- **Idle-grace consensus ~30 min** (Codespaces 30 min default; Gitpod 30 min;
  Coder activity-bumped), three-stage lifecycle (running → stopped-restorable →
  deleted after weeks), retention extended for uncommitted changes.

## Recovery for this incident

Not resumable — the workspace is gone and the failure is a destroyed VM workspace.
(The VM grant added afterwards was likely unnecessary — the creator is an admin and
bypasses the check; see D4.) The Phase-8 work **is** preserved in Gitea repo
`job-b5b0c0d0` (20 commits through `04b82c19`), and the research subjob output is in
OpenCloud (`sessions/job-f8326e32a3ea`). Re-run as a **fresh VM-backed job**,
optionally seeded from the `job-b5b0c0d0` repo. Clean up the orphaned `PENDING`
request `98d88547` (deny or let it expire).

**Operational caveat (2026-07-08):** VM jobs on the main cluster are currently
wedged independently by the orchestrator-vantage SSH-readiness gate (`96c33654`,
live via `sha-69a4da6`) — the orchestrator has no tailnet route so the probe can
never pass. A "re-run on VM" recovery will not dispatch until that is resolved.
Track separately (VM SSH-readiness saga). Note this is the *same* missing tailnet
route that makes VM snapshots a no-op (fix 1.3) — one route/rootdisk decision
serves both.

## Related

- [`deleted_job_orphans_workspace_pod.md`](deleted_job_orphans_workspace_pod.md) —
  the *inverse* failure (deleted-row pods never reaped); shares the
  `is_idle`/`is_reapable` predicates edited here.
- [`agent_fast_freeze_on_dead_workspace.md`](agent_fast_freeze_on_dead_workspace.md) —
  make workspace loss survivable/quick-to-freeze; the recovery consumer this
  incident lands in.
- [`loop_job_workspace_lost_wedged_in_recovery.md`](loop_job_workspace_lost_wedged_in_recovery.md)
  — the recovery arm that fails after `WORKSPACE_RECOVERY_MAX_ATTEMPTS`.
- [`reviewing_parent_pod_reaped_under_critic.md`](reviewing_parent_pod_reaped_under_critic.md)
  — origin of the `has_live_shared_child` reap guard.
- `docs/features/workspace_tier_upgrade.md` — the VM-upgrade design; its deferred
  **W3** (operator-gated worker `*→vm` re-dispatch) is effectively fix 3 here.
- [`../features/vm_persistent_rootdisk.md`](../features/vm_persistent_rootdisk.md) —
  the durable substrate for VM-tier capture (fix 1.3).
