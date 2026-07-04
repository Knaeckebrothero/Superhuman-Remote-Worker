---
tags:
  - issue
  - jobs
  - agent-lifecycle
  - version-upgrade
  - drain
  - langgraph-checkpoint
  - self-improvement-loop
  - livelock
---

# `version_upgrade` (and every auto-continue) resume re-freezes forever — a stuck LangGraph checkpoint, not eager draining

**Filed:** 2026-07-04, investigating the self-improvement loop's iter-6 DEVELOPER
job (`302e73af-552a-449d-b348-d803e282ab39`) that "kept going back to paused" on
dev (ns `superhuman-remote-worker`). **Root-caused via reproduction and fixed on
`develop` (uncommitted, pending review + deploy).** Symbols/line numbers current
as of this date.

## TL;DR

When a worker freezes for auto-redispatch (drain/`version_upgrade`, LLM outage,
memory/KB/workspace-upgrade), it sets `should_stop=True` in graph state and the
run reaches `END`. That terminal state is **persisted in the LangGraph
checkpoint**. On the plain auto-redispatch resume the agent calls
`ainvoke(None)` — and `ainvoke(None)` on a thread that already reached `END` with
`should_stop=True` **runs zero nodes and returns the terminal frozen state
verbatim**. So `restore_todo_state` (which *would* clear the stop flags on resume)
never runs, the agent re-reports the same freeze, and the job re-pauses →
re-dispatches → forever, doing no work. `processing` status + an advancing
`updated_at` make it invisible.

The drain *decision* is correct; there is no version-SHA skew. The bug is that
the plain graceful resume never clears the checkpoint's `should_stop` — only the
`feedback` and `delegation` resume paths do.

## Symptom

Flip-flopped `processing` ⇄ `paused` ~every 1–2 min for hours, then wedged.

| Fact | Value |
|---|---|
| Job | `302e73af-…` — Loop iter 6 · DEVELOPER (Hotel Rheinland ERP) |
| **Last real audit entry** | **#701 — `check` at 08:13:51Z** (frozen since; ~4.5 h zero work) |
| `version_upgrade` completes observed | ~30 between 11:35–12:31Z |
| `last_freeze_data` | `{"freeze_type":"version_upgrade","phase":"tactical","phase_number":3,…}` |
| row `freeze_data` | `NULL` (shed on each pause) |
| Stuck at | phase-3 tactical boundary — never executes phase-3 work |

## Mechanism (the full chain)

1. **Freeze.** A drain intent (or LLM/memory/workspace freeze) sets
   `should_stop=True` + `freeze_data` in graph state; e.g. `version_upgrade` at
   `src/graph.py:3213-3234`, `llm_unavailable` at `src/graph.py:2504-2544`. The
   run flows `execute → handle_transition → check_goal → END`
   (`check_goal` audits the "frozen" step — this is audit #701).
2. **Persist.** The graph compiles with a checkpointer
   (`src/graph.py:4617`), Postgres-backed for cross-pod resume
   (`checkpointer_backend()=="postgres"`, keyed by `thread_id=job_id`,
   `src/agent.py:_make_checkpointer`). The terminal `should_stop=True` +
   `freeze_data` are saved.
3. **Pause + shed.** `complete_job → determine_job_status` maps `version_upgrade`
   → `paused` (`orchestrator/services/completion.py:514-519`) and, because it's in
   `AUTO_REDISPATCH_FREEZE_TYPES`, sheds the row `freeze_data` into
   `context.last_freeze_data` (`orchestrator/main.py:10908-10936`) — hence
   `freeze_data IS NULL`.
4. **Resume that does nothing.** `paused` ∈ `GRACEFUL_STOP_STATUSES`, so
   `_resume_from_checkpoint` succeeds and `graph_input=None`
   (`src/agent.py:824-844`; non-None means *failure*). The `feedback` and
   `delegation` resume paths each `aupdate_state(..., should_stop=False, …)`
   (`src/agent.py:888-921`) — **but the plain auto-redispatch clears nothing.**
   `ainvoke(None)` (`src/agent.py`) on the ended `should_stop=True` thread runs
   **zero nodes** and returns the terminal state. `restore_todo_state`'s
   "Always clear stop flags on resume" (`src/graph.py:3528-3531`) is unreachable
   in exactly the state that needs it.
5. **Loop.** The agent re-reports `version_upgrade` → `paused` → re-dispatch →
   step 4 again. No `execute`, no audit, forever. **The loop is self-perpetuating
   via the checkpoint's `should_stop`, independent of whether the drain intent is
   still active** — the reconciler only has to fire once.

## Nailed via reproduction

A minimal LangGraph graph (`route_entry → restore → execute → handle_transition →
check_goal`, compiled with a checkpointer) run **inside a prod agent pod**
(langgraph 1.2.6 / checkpoint 4.1.1) reproduces it exactly:

- Fresh run with drain on → freezes: `work_done=1`, `should_stop=True`,
  `freeze=version_upgrade`.
- `invoke(None)` on that ended thread → **nodes run: `[]`**, returns terminal
  state unchanged (`work_done=1`). Zero progress. ← the bug.
- Clearing the stop flags via `update_state(as_node="__start__")` then
  `invoke(None)` → graph **re-enters** (`restore → execute → …`), `work_done 1→2`.
  ← the fix.

Repro: `scratchpad/drain_repro.py`; encoded as a regression test in
`tests/test_drain_intent.py::TestAutoContinueResumeClear`.

## Why it's invisible

`paused` only for the ~1 s between complete and re-dispatch; otherwise
`processing` with an agent attached. `freeze_data` is shed (row looks clean).
`updated_at` advances on heartbeats. The one true signal — a **flat audit count
while `processing`** — is surfaced nowhere.

## Ruled out

- **Not a version-SHA skew / broken drain comparison.** `expected` (the
  `PERSISTENT_AGENT_IMAGE` tag SHA) and the agent's reported `build_sha`
  (baked `BUILD_SHA`, `src/api/orchestrator_client.py:204`) both read `7ae56e6`
  at steady state, so `is_drift` is correctly `False` and matching agents are not
  drained. The drain fired legitimately during the day's image churn; the *loop*
  is the checkpoint bug, which persists after the drain stops.
- **`AGENT_IMAGE` unset is harmless** (provisioner falls back to
  `PERSISTENT_AGENT_IMAGE`, `agent_provisioner.py:85-88`).

## The fix (implemented on `develop`, uncommitted)

### Fix 1 — clear terminal stop flags on plain auto-continue resume *(the fix)*
`src/agent.py`: before `ainvoke`, for a graceful resume with `graph_input is None`
and no feedback/delegation, if the checkpoint's `should_stop` is set and its
`freeze_data.freeze_type` ∈ `_AUTO_CONTINUE_FREEZE_TYPES`, call
`aupdate_state({should_stop:False, goal_achieved:False, is_final_phase:False,
freeze_data:None}, as_node="__start__")` — the same clear the feedback/delegation
paths already perform. The graph then re-enters `route_entry → restore_todo_state
→ execute` and resumes from its checkpoint. Repro-validated on prod LangGraph
versions.

- **Scope = the whole class.** `_AUTO_CONTINUE_FREEZE_TYPES = {version_upgrade,
  llm_unavailable, memory_unavailable, kb_unavailable, workspace_upgrade_required}`
  — every in-graph freeze that reaches `END` with `should_stop=True` and is
  auto-redispatched shares this bug, so this fixes them all. Human-review /
  terminal stops (`budget_exceeded → pending_review`, genuine completions) are
  deliberately **not** in the set, so they stay stopped.
- **Under active drain it self-limits:** each re-dispatch now does exactly one
  phase, then re-freezes (new `phase_number`) — forward progress, one phase per
  upgrade — and runs to completion once the image settles.

### Fix 2 — progress-aware drain backstop *(defense-in-depth)*
`orchestrator/services/completion.py:auto_continue_drain_update` (pure) +
`orchestrator/main.py` (I/O). On each auto-redispatch pause, if the freeze
`phase_number` has **not** advanced since the last drain, increment
`context.auto_continue_drains`; a changed/absent phase resets it. At
`AUTO_CONTINUE_DRAIN_ALERT_CAP` (env, default 10) log ERROR + alert the operator
via `_notify_operator_freeze` — i.e. surface loudly if Fix 1 ever fails to make
progress, instead of spinning invisibly. No auto-fail (Fix 1 owns correctness).

### Tests
`tests/test_drain_intent.py`: `TestAutoContinueResumeClear` (LangGraph
bug + fix + settled-image completion, on the installed LangGraph),
`TestAutoContinueDrainBackstop` (the pure counter), `TestAutoContinueFreezeTypeScope`
(set covers the redispatch types, excludes human-review). 26 pass; ruff clean;
83 completion tests + 59-test resume/dispatch sweep green.

### Discarded approach
The original roadmap ("process-local *work-done* flag; skip the drain at the
boundary you resume into") was **wrong** — the graph never re-enters at all when
wedged at `END`, so a boundary-level guard would be a no-op. The reproduction
replaced that model.

## Related (separate issue) — the current wedge

When the drains stopped (~12:31Z), the job did not resume: its dual-mode agent
(`869f5a80` / `srw-agent-j-8e42aa12`) was repurposed to an unrelated persistent
session thread (`e496d293`, `current_job_id=NULL`, `status=session`) while the
job's `assigned_agent_id` still points at it — wedged in `processing`. This is a
distinct dual-agent-steal / session-zombie bug (clear `assigned_agent_id` when a
dual agent detaches; make a job-bound dual agent ineligible for session-attach)
and is **not** addressed here.

## Appendix — detect / reproduce

- **Detect:** `status=processing` **and** a flat audit count over minutes;
  `get_audit_trail(job, page=-1)` frozen at a `phase_transition`/`check` tail;
  `context.last_freeze_data.freeze_type` an auto-redispatch type.
- **Reproduce:** run `scratchpad/drain_repro.py` (or
  `tests/test_drain_intent.py::TestAutoContinueResumeClear`) — pipe it into a live
  agent pod to confirm on prod LangGraph versions:
  `kubectl --context main exec -i -n superhuman-remote-worker <agent-pod> -c agent -- python - < drain_repro.py`.
