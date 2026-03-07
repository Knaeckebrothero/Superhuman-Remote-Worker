# Verification Subjob Not Spawned After Job Completion

**Date:** 2026-03-07
**Job:** `34f93b9d-e410-4e36-b1f2-b73a5f38890e` ("Redeploy everything as quadlets")
**Status:** `pending_review` (should have been `reviewing` with critic spawned)

## Symptoms

1. Job completed phase 6, froze for review with `freeze_type: "job_complete"`
2. `freeze_data` was written correctly to DB (819 bytes, verified via psql)
3. Job status set to `pending_review` instead of `reviewing`
4. No critic subjob was spawned
5. No log output from `_maybe_trigger_verification` at all — no warnings, no errors, nothing

## Expected Behavior

1. `_update_job_status_from_result` should detect `freeze_type: "job_complete"` and `verification.enabled: true`, then override status to `reviewing`
2. `_maybe_trigger_verification` should read `freeze_data` from DB, confirm it's a job completion, and call `create_verification_job()` on the orchestrator

## What Was Verified (Static Analysis)

All of these checked out correctly:

- **Config loading:** `defaults.yaml` has `verification.enabled: true`, `critic_config: critic`, `max_rounds: 3`. Loading via `resolve_config_path("defaults")` -> `load_agent_config()` produces `config.extra["verification"] = {"enabled": True, ...}`. Tested with a standalone script.
- **`_is_verification_enabled()`:** Checks `_agent.config.extra.get("verification")` — returns True in tests.
- **`freeze_data` in DB:** Confirmed present with `freeze_type: "job_complete"` via direct psql query.
- **`_is_job_completion_freeze(row)`:** Parses `freeze_data` from row, checks for `freeze_type == "job_complete"` — should return True.
- **No config mutation:** No code path modifies `config.extra` in place during job processing. `dataclasses.asdict()` in `serialize_resolved_config` deep-copies. `_base_config` is never reassigned.
- **No connection issues:** `_agent.postgres_conn` is the system connection (not a datasource), stays alive after job completion. Same pool used by graph.py and app.py.
- **No exception swallowing:** `_maybe_trigger_verification` has its own `try/except` at line 567 that logs errors with `exc_info=True`. No errors in logs.
- **Code path confirmed:** Log shows `"Orchestrator job 34f93b9d completed: True"` (from `_process_orchestrator_job`, not resume path). Heartbeat at 15:43:48 confirms the early `_current_job_id` clear executed. Code should have continued to `_handle_critic_verdict` and `_maybe_trigger_verification`.

## What Could NOT Be Determined

The running agent (PID 1092901, started 14:45) had no debug logging in `_is_verification_enabled()` or `_maybe_trigger_verification`'s guard clauses. Both functions return silently when verification is not enabled. Without runtime logging, we cannot confirm:

- Whether `_agent.config.extra` actually contained `verification` at the exact moment `_is_verification_enabled()` was called
- Whether an exception occurred before the try block in `_maybe_trigger_verification` (lines 434-442 are outside the try/except)
- Whether `_update_job_status_from_result` took the "job completion" branch or the "phase boundary" branch (both log `'pending_review'` with identical messages)

## Likely Root Cause

`_is_verification_enabled()` returned `False` at runtime. This is the only explanation consistent with:
- No log output from inside `_maybe_trigger_verification`
- Status set to `pending_review` instead of `reviewing`
- No error logs

Why it returned False is unknown. All static analysis says `_agent.config.extra["verification"]` should exist.

## Fixes Applied

### Diagnostic logging (src/api/app.py)

1. **`_maybe_trigger_verification` entry:** Added INFO-level log on entry showing `should_stop`, `error` state
2. **`_is_verification_enabled()` guard:** Changed from silent return to WARNING-level log showing `_agent` state, `extra` keys, and `verification` value
3. **Verification enabled path:** Added INFO log confirming verification config when enabled
4. **`_update_job_status_from_result` branches:** Added DEBUG log showing `goal_achieved`, `is_job_completion_freeze`, `verification_enabled` when entering the completion branch. Changed the "not job completion" else branch to log freeze_data presence and freeze check result.

These changes require an agent restart to take effect.

## Resolution

Manual intervention needed:
- Set job status to `reviewing` and spawn a critic via orchestrator API, OR
- Resume the job directly if the deliverables are acceptable

## Prevention

The diagnostic logging will capture the exact runtime state next time this occurs. If `_is_verification_enabled()` returns False again, the WARNING log will show exactly what `config.extra` contains, making the root cause immediately identifiable.

## Architectural Problem: Verification Logic Lives in the Agent

The deeper issue is that the entire verification trigger chain is implemented agent-side (`src/api/app.py`), not in the orchestrator. This is a responsibility leak — the agent is doing job lifecycle management that belongs in the orchestrator.

### Current Flow (Agent-Driven)

```
Agent: _process_orchestrator_job()
  ├── _update_job_status_from_result()     ← agent decides DB status
  ├── _current_job_id = None               ← agent marks itself available
  ├── heartbeat(status="ready")            ← tells orchestrator it's free
  ├── trigger_subjob_merge()               ← if subjob, merge branch
  ├── _handle_critic_verdict()             ← if THIS is a critic, approve/return target
  └── _maybe_trigger_verification()        ← if main job, spawn critic
        ├── _is_verification_enabled()     ← reads _agent.config.extra
        ├── reads freeze_data from DB
        ├── formats critic instructions
        └── POST /api/jobs                 ← creates critic job on orchestrator
```

The orchestrator's only role is passive: store the job and dispatch it to an available agent. It has zero knowledge of verification logic.

### What's Wrong With This

1. **Fragile post-completion chain.** The agent must stay alive and healthy *after* job completion to trigger verification. If it crashes between `_update_job_status_from_result` and `_maybe_trigger_verification`, the critic never spawns and nobody notices — exactly the class of failure we saw with job `34f93b9d`.

2. **Silent failures with no recovery.** `_is_verification_enabled()` returned False for an unknown reason. The orchestrator never knew verification was expected, so it couldn't recover. The job just sat in `pending_review` indefinitely.

3. **Config trust problem.** The decision depends on `_agent.config.extra["verification"]` in the agent's memory. But the orchestrator already has `config_name` and `config_override` in the database. The agent's in-memory config might drift from what was stored — and there's no way to detect this from outside.

4. **Duplicated orchestration.** `_handle_critic_verdict` (approve/return with feedback, count rounds, auto-accept after max) is also agent-side. The agent manages the entire verification loop: creating jobs, resuming jobs, counting rounds. All orchestrator responsibilities.

### Correct Flow (Orchestrator-Driven)

The agent should only report completion. The orchestrator should handle everything after:

```
Agent:
  └── Reports result (status, freeze_data, goal_achieved) → done

Orchestrator (on status change to pending_review/completed):
  ├── Reads job config (config_name, config_override, or resolved_config)
  ├── Checks verification.enabled
  ├── Reads freeze_data, confirms freeze_type == "job_complete"
  ├── Overrides status to "reviewing" if verification enabled
  ├── Creates critic job (formats instructions, sets parent_job_id)
  └── Dispatches critic to available agent
```

The natural trigger point is the job status update — either via the heartbeat endpoint, a dedicated completion callback, or the existing `update_job_status` DB call. The orchestrator already has `_trigger_dispatch()` wired into status changes; verification would be another post-status-change hook.

### Benefits

- **Crash-safe:** If the agent dies after reporting, the orchestrator still sees `pending_review` + `freeze_type: "job_complete"` + `verification.enabled` in the stored config. A background reconciliation loop could catch orphaned jobs.
- **Observable:** The orchestrator can log every verification decision centrally, not scattered across agent pods.
- **Single source of truth:** Config is read from the database, not from agent memory.
- **Simpler agent:** The agent's job is to run the graph and report results. Period.

## Refactor Roadmap

### What Moves to the Orchestrator

Three chunks of logic currently in the agent (`src/api/app.py`):

| Function | What it does | Current location |
|----------|-------------|-----------------|
| `_update_job_status_from_result` | Decides DB status from graph result (failed, reviewing, pending_review, completed) | `app.py:766` |
| `_maybe_trigger_verification` | Guards, reads freeze_data, formats instructions, creates critic job | `app.py:412` |
| `_handle_critic_verdict` | Approve/return target job, count rounds, auto-accept after max | `app.py:900+` |

Supporting code that also moves or gets removed:

| Function / Method | Location |
|-------------------|----------|
| `_is_verification_enabled()` | `app.py:369` |
| `_get_verification_config()` | `app.py:357` |
| `_is_job_completion_freeze()` | `app.py:391` |
| `create_verification_job()` | `orchestrator_client.py:483` |
| `_format_verification_instructions()` | `orchestrator_client.py:591` |

### Trigger Point: Dedicated Completion Endpoint

Add `POST /api/jobs/{job_id}/complete` to the orchestrator. The agent calls it with the graph result after finishing:

```json
{
  "should_stop": true,
  "goal_achieved": false,
  "error": null
}
```

The orchestrator handles everything from there: status determination, verification trigger, critic verdict handling, dispatch.

**Why not heartbeat or DB polling?**
- Heartbeat is indirect — the orchestrator would have to infer which job finished and what happened.
- DB polling adds latency and complexity.
- A dedicated endpoint is explicit, carries the result payload, and is easy to reason about.

### Verification Config Resolution

The orchestrator reads verification settings from `resolved_config` JSONB (already stored on the jobs table). This is the exact config the agent used — no drift possible.

```sql
SELECT resolved_config->'extra'->'verification' FROM jobs WHERE id = $1
```

### Verification Instruction Template

Currently lives on the agent's filesystem, formatted by `orchestrator_client.py::_format_verification_instructions()`. Options for the orchestrator:

1. **Bundle the template in the orchestrator codebase** — simplest, template rarely changes
2. **Store in `resolved_config`** — already captured at job start, but bloats the JSONB
3. **Shared config directory** — both agent and orchestrator mount `config/`

Option 1 is the pragmatic choice. The template is small and specific to orchestration logic.

### Migration Phases

**Phase 1: Add orchestrator endpoint, agent calls it**

Orchestrator (`orchestrator/main.py`):
- Add `POST /api/jobs/{job_id}/complete` endpoint
- Port `_update_job_status_from_result` logic (status determination)
- Port `_maybe_trigger_verification` logic (guards, freeze_data check, critic creation)
- Port `_handle_critic_verdict` logic (approve/return, round counting, auto-accept)
- Move `_format_verification_instructions` template + formatting here
- Read verification config from `resolved_config` JSONB instead of agent memory

Agent (`src/api/app.py`):
- `_process_orchestrator_job` simplifies to:
  1. Run graph → get result
  2. `POST /api/jobs/{job_id}/complete` with result
  3. `_current_job_id = None`
  4. `heartbeat(status="ready")`
- Keep old functions as dead code initially (easy rollback)

Agent (`src/api/orchestrator_client.py`):
- Add `report_job_complete(job_id, result)` method

**Phase 2: Clean up agent**

Remove from `src/api/app.py`:
- `_maybe_trigger_verification()`
- `_handle_critic_verdict()`
- `_update_job_status_from_result()`
- `_is_verification_enabled()`, `_get_verification_config()`
- `_is_job_completion_freeze()`
- Curation trigger logic (same pattern, same problem)

Remove from `src/api/orchestrator_client.py`:
- `create_verification_job()`
- `_format_verification_instructions()`

**Phase 3: Resilience**

Add orchestrator background reconciliation:
- Scan for jobs in `pending_review` with `freeze_type: "job_complete"` and `verification.enabled` in `resolved_config` but no critic subjob → auto-recover by creating the critic
- Scan for critic jobs stuck in `created` with no agent assigned → re-trigger dispatch
- Log all verification decisions centrally for observability
