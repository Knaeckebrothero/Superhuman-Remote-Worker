# Session job-management toolset needs a rework (truncated job IDs + missing automation control + gating)

**Date:** 2026-06-28
**Status:** Partially fixed. Problem 1 was fixed on 2026-07-08: session job
lists now expose full UUIDs, action tools tolerate visible UUID prefixes, job
summaries include more supervision context, and pause/cancel use the backend's
`PUT` routes. Problem 2 (automation tools) and the broader Problem 3 toolset
rework remain open. **No decision has been made yet** on the automation approach
or the gating policy.
**Component:**
- `src/tools/orchestrator/jobs.py` — the session orchestrator tools and their
  formatters (`_format_job_summary` :146, list formatter :264).
- `src/tools/orchestrator/__init__.py` — `get_orchestrator_metadata()`.
- `config/persistent_defaults.yaml` + `config/interactive.yaml` — the
  `orchestrator: [ ]` tool group (empty in every shipped session config).
- `orchestrator/main.py` — `get_job` (`:5942`), `POST /api/jobs` list/create.
- `orchestrator/routers/automations.py` — the would-be target for automation
  tools (already gated by `require_approved_user`).
- `orchestrator/mcp/server.py` — the external-Claude-Code MCP surface (also
  lacks automation tools).

## Background / how this surfaced

While scoping "can a session create or manage automations for the user?" we
established the current capability boundary (Problem 2). Separately, a session
agent monitoring a project self-improvement loop ("create better resavio" / ERP
loop — scholar → critic → developer chain) reported it could not act on the
loop's jobs. Verbatim:

> The most recent developer job I can see for the "create better resavio" / ERP
> loop is `19707fa1…` — paused … From the orchestrator listing it follows two
> completed loop steps (`7cc5b31f…` scholar completed, `40cf0d50…` critic
> completed). **I could not retrieve the full job detail because the available
> job list only exposes the shortened ID, and `get_worker_job("19707fa1")` does
> not resolve to the full UUID.** I also checked likely workspace files
> (`output/job_frozen.json`, `plan.md`, `output/completion.json`) but they were
> not accessible with the shortened ID. … it is paused, but the exposed tools do
> not show the specific pause reason for that job.

That report is fully explained by the code (Problem 1). It also motivates the
broader rework (Problem 3): the agent could not see the pause *reason* either,
because the summary formatter omits freeze/pause data.

---

## Problem 1 — Truncated job IDs make every session action tool unusable (BUG)

**This is a real bug, not a design gap.** The session can *list* jobs but cannot
*act* on any of them.

Chain of causation:

1. **The list tool throws away the full UUID.** `list_worker_jobs` formats each
   row as
   ```python
   lines.append(f"--- {job.get('id', '?')[:8]}... ---")   # jobs.py:264
   ```
   The full UUID is present in the JSON response (`job["id"]`) but is truncated
   to its first 8 chars with a literal `...` appended. So the LLM never sees a
   usable identifier — only `19707fa1...`.
2. **Every action tool interpolates the ID verbatim.** `get_worker_job` (`:280`),
   `get_job_workspace_file` (`:303`), `approve_worker_job` (`:333`),
   `resume_worker_job` (`:353`), `cancel_worker_job` (`:387`), and
   `pause_worker_job` (`:407`) all do
   `client.get/post(f"{base_url}/api/jobs/{job_id}...")` with no normalization.
3. **The orchestrator resolves by exact UUID.** `GET /api/jobs/{job_id}`
   (`main.py:5942`, `get_job` → `require_job_access`) uses the path param as-is
   for an exact match. A truncated `19707fa1` → 404 → the tool returns
   `"Job '19707fa1' not found."`

**Net effect:** the only ID the agent can obtain (from `list_worker_jobs`) is
the one form the action tools cannot consume. Get-detail, read-workspace-file,
resume, pause, cancel, and approve are all dead-on-arrival from a session unless
the agent already knows a full UUID from some other channel (e.g. the user pasted
one). This silently neuters the entire monitor/manage half of the toolset.

### Options (Problem 1)

Ordered by increasing cost; (c) recommended.

a. **Stop truncating.** Print the full UUID in `list_worker_jobs` output
   (optionally `full-uuid (short)` for readability). One-line change at
   `jobs.py:264`. Removes the proximate cause; the agent can then copy a full ID
   into the action tools.
b. **Tolerate prefixes.** Add a shared resolver so the action tools accept an
   8+ char prefix and expand it to a full UUID before calling the API — either
   client-side (list + match) or a server-side `?id_prefix=` lookup on
   `/api/jobs`. Handles the case where a short ID arrives from anywhere (logs,
   user paste, the loop UI). Must reject/flag ambiguous prefixes.
c. **Both (recommended).** Surface full IDs *and* tolerate prefixes. Defensive on
   both ends; small total cost.

### Related defect in the same file: the summary hides *why* a job paused

`_format_job_summary` (`jobs.py:146`) emits only `id / status / description /
config / assigned_agent / created_at / error_message`. It does **not** surface
`freeze_data` (pause/freeze reason + type: `job_complete`, `phase_boundary`,
`budget_exceeded`, `blocking_message`, VM/sudo approval), autonomy level, current
phase, project, or parent/loop lineage. That is exactly why the reporter could
say *that* the job was paused but not *why*. Enriching this formatter (and the
list formatter) belongs to the same fix.

---

## Problem 2 — Sessions cannot create or manage automations (GAP)

(Investigated 2026-06-27; see also the automations feature surface in
`orchestrator/routers/automations.py` + `docs/features/automations_v0.md`.)

Findings:

- **"Automations" are cron-scheduled job templates** owned by a user, created and
  managed only through `POST/PATCH/.../api/automations` (+ the cockpit
  `views/automations/` UI). Every endpoint is gated by `require_approved_user`.
- **A session is NOT an MCP client.** The "MCP" in the session path is the
  *auth-header* mechanism: native orchestrator tools forward
  `X-MCP-User-Id = ToolContext.user_id` + `X-Internal-Key`, which the
  orchestrator's `_get_user_from_mcp_headers` resolves so calls are accepted as
  the owning user (`security/auth.py:114,575,608`). The session calls the
  regular REST API directly via `jobs.py`'s `_get_client`; it does not connect to
  the MCP server.
- **Neither agent-facing surface exposes automations.** The native orchestrator
  toolset (`jobs.py`) has `create_worker_job` etc. but no automation tools; the
  MCP server (`orchestrator/mcp/server.py`) has `create_job` etc. but no
  `create_automation`. So "the MCP can't do it either" is correct.
- **The auth model would already allow it.** Because `create_automation` is gated
  by `require_approved_user` and the session forwards the owner's identity, a
  session tool that POSTs to `/api/automations` would be accepted *as the user*,
  and the endpoint's own ACL guarantees the agent can only act for its own owner
  (and only on projects where that owner is editor+). This is a **missing-tool**
  gap, not an auth-architecture blocker.

### Implementation sketch (Problem 2) — undecided, captured for later

Mirror exactly how `create_worker_job` already works (thin REST wrappers; reuse
all existing validation/ACL/dispatcher-seeding in the router — no logic
duplication):

1. New module `src/tools/orchestrator/automations.py` (sibling of `jobs.py`):
   `create_automation`, `list_automations`, `update_automation`,
   `pause_automation`, `resume_automation`, `run_automation_now`,
   `delete_automation`, `list_automation_runs`. Each ≈10 lines:
   `_get_client(user_id=context.user_id)` → REST call to `/api/automations…`.
   Auto-inject `context.project_id` so session-created automations can scope to
   the session's project (parity with `create_worker_job`, `jobs.py:210`).
2. Register them in `get_orchestrator_metadata()`
   (`src/tools/orchestrator/__init__.py`).
3. Enable in config: add the names to the `orchestrator:` group in
   `config/persistent_defaults.yaml` (and/or a specific expert such as
   `assistant`) — it is `[ ]` today, and `config/interactive.yaml` zeroes it too.
4. (Optional) If external Claude Code should also manage automations, add
   `@mcp.tool` wrappers in `orchestrator/mcp/server.py` + matching methods in
   `orchestrator/mcp/client.py`. Independent of the session path.

### Open decision (Problem 2) — gating policy

Letting an agent create automations = letting it schedule **recurring,
budget-consuming jobs on the user's behalf** — a prompt-injection target. The
endpoint ACL stops cross-user abuse but not "the agent schedules things for its
own owner." Candidate policy (recommended default, **not yet decided**):

- **Propose-don't-activate:** the agent's `create_automation` forces
  `enabled=false`; a human flips it on in the cockpit. Defuses the injection risk
  while staying useful.
- **Capability grant:** gate the automation tools behind a new
  `manage_automations` key in the capability-grant system (the same mechanism as
  user-defined experts), default-off.
- **Distinct audit** line for agent-created automations.

---

## Problem 3 — The session orchestrator toolset deserves a holistic rework (DESIGN)

The original job-control toolset (`jobs.py`, 8 job tools) grew piecemeal and
has accumulated rough edges beyond the two specific items above. Pull these into
one rework rather than patching individually:

1. **ID + display handling** (Problem 1) — full IDs, prefix tolerance, richer
   summaries incl. pause/freeze reason.
2. **Stringly-typed returns.** Every tool returns a hand-formatted string
   (`_format_job_summary`, the list loop). The LLM has to re-parse IDs/status out
   of prose — fragile, and the proximate cause of the truncation bug living
   unnoticed. Consider structured returns (or at least a single consistent
   formatter that always carries the full ID).
3. **Incomplete surface.** No automations (Problem 2). No project-/loop-scoped
   listing — the reporter had to reconstruct a scholar→critic→developer loop
   chain by eye from a flat `list_worker_jobs`. A "jobs in this project/loop,
   with lineage" view would have answered the original question directly.
4. **Safety / gating.** Creating jobs (and future automations) on the user's
   behalf is a prompt-injection surface, yet the only control today is the
   YAML on/off of the whole `orchestrator` group. Tie tool exposure to capability
   grants + autonomy level rather than a binary group toggle.
5. **Empty-by-default confusion.** `orchestrator: [ ]` in every shipped session
   config means most sessions cannot delegate or monitor jobs at all, despite the
   tools existing and the auth being wired (`ToolContext.user_id`,
   `context.py:131`). Decide the intended default surface per expert.

## Scope / open questions

- **Problem 1 fix shape** — (a) full IDs only, (b) prefix resolution, or (c)
  both? If prefix resolution: client-side match vs. a server-side `?id_prefix=`
  param; how to handle ambiguous prefixes.
- **How much of the summary to surface** — pause/freeze reason and type,
  autonomy, phase, project, parent/loop lineage. Bound the size for the LLM.
- **Structured vs. string returns** — worth the churn, or is "always print the
  full UUID + a consistent formatter" enough?
- **Automation gating** — propose-disabled + `manage_automations` grant (rec.),
  or full create-and-enable, or something else? (Undecided.)
- **Surfaces** — session native tools only, or also the external MCP server?
- **Default tool surface per expert** — which sessions get the orchestrator group
  at all, and which subset?
- **Loop ergonomics** — should there be a first-class "list/monitor the jobs of
  this project loop" tool, given self-improvement loops are a primary consumer?

## References

- **The original 8 session job tools + the truncation bug:**
  `src/tools/orchestrator/jobs.py` — list formatter truncation at `:264`,
  thin summary at `:146`, action tools `:280`/`:303`/`:333`/`:353`/`:387`/`:407`,
  the `X-MCP-User-Id`/`X-Internal-Key` client at `:121`.
- **Tool metadata + registration:** `src/tools/orchestrator/__init__.py`
  (`get_orchestrator_metadata`), `src/tools/registry.py` (`TOOL_REGISTRY`).
- **Why the group is dark in sessions:** `config/persistent_defaults.yaml` and
  `config/interactive.yaml` (`orchestrator: [ ]`); identity plumbing in
  `src/tools/context.py:131` (`ToolContext.user_id` / `thread_id`).
- **Orchestrator exact-match job lookup:** `orchestrator/main.py:5942`
  (`get_job` → `require_job_access`); job list/create at `POST /api/jobs`
  (`:5958`).
- **Automations surface:** `orchestrator/routers/automations.py` (CRUD +
  run-now/pause/resume/runs, all `require_approved_user`),
  `orchestrator/services/automations.py`, `orchestrator/services/cron_dispatcher.py`,
  `docs/features/automations_v0.md`, cockpit `views/automations/`.
- **Auth header path:** `orchestrator/security/auth.py:114,575,608`
  (`require_approved_user` → `_get_user_from_mcp_headers`).
- **MCP server (no automation tools):** `orchestrator/mcp/server.py`,
  `orchestrator/mcp/client.py`.
- **Related issues:** `docs/issues/delegation_light_mode_missing.md` (the other
  half of the agent-orchestration toolset),
  `docs/issues/mcp_created_jobs_ownerless_capability_grant_denied.md`
  (ownership/gating of agent-created jobs — same family as Problem 2's gating),
  `docs/issues/scholar_delegation_not_exercised.md` (whether the model *chooses*
  to use these tools — orthogonal).
