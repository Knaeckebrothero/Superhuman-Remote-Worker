---
tags:
  - issue
  - jobs
  - agent-lifecycle
  - version-upgrade
  - drain
  - completion
  - self-improvement-loop
  - status-determination
---

# A `version_upgrade` drain freeze is masked by a coincident `error` and hard-fails the job

**Status:** investigated 2026-07-07 from two failed Better-Resavio loop iterations on the main cluster. **Root cause confirmed by elimination against the DB state + code paths; not yet fixed.** The exact `error_message` string on the two jobs was not retrieved (see "Open questions"). Symbols/line numbers current as of this date.
**Severity:** high for the RSI loop — every agent-image deploy that lands while a bug-hunter is mid-run can hard-fail that loop iteration instead of gracefully re-dispatching it. Self-healing at the loop level masks it, but each occurrence burns an iteration and bumps `consecutive_failures`; a cluster of deploys close together can trip `max_consecutive_failures` and stop the loop.
**Component:** `orchestrator/services/completion.py` (`determine_job_status`), `orchestrator/main.py` (`complete_job` `/api/jobs/{id}/complete`), `src/graph.py` (drain-intent freeze at phase boundary), `src/api/dual_app.py` (`_process_orchestrator_job` completion report)
**Observed on:** main cluster (homelab), loop `68137e29` "Better Resavio" (Hotel Rheinland ERP), MiniMax-M3. Jobs `e55ff79a-2d64-4abb-85e6-82a7afe3259e` (iter 10) and `87a8cbe7-9f1e-4bee-8db5-15ff5e39b1a0` (iter 2).
**Related:** `docs/issues/version_upgrade_drain_livelock.md` (a *different* drain bug — the resume side), `docs/issues/reranker_transient_fault_hard_fails_job.md` and `docs/issues/loop_advance_nonatomic_wedges_loop.md` (other members of the "one blip kills a loop iteration" family), `docs/features/project_self_improvement_loop.md`, `project_drift_drain_suspend_fix`

---

## TL;DR

When the orchestrator's lifecycle reconciler detects a busy worker on a stale image it sets `intents.should_drain`; the worker picks that up at its next phase boundary and freezes with `freeze_type="version_upgrade"` (`reason="orchestrator drain intent at phase boundary"`). This is **Continue-as-New**: the freeze is supposed to route to `paused`, shed its agent + freeze blob, and re-dispatch the same job onto a fresh-version pod.

`determine_job_status` (`orchestrator/services/completion.py`) checks `result["error"]` **first** and returns `("failed", …)` before it ever evaluates the `version_upgrade` branch further down. So if the completion report carries **both** a `version_upgrade` freeze **and** an `error`, the error wins and the job is hard-failed. The `/complete` handler persists the incoming `freeze_data` to the DB *before* status determination, which is why the two jobs end up in the contradictory-looking state `status=failed` **with** `freeze_data.freeze_type=version_upgrade` still on the row.

The drain decision is correct and there is no SHA skew problem. The bug is a **precedence** bug: a coincident transient/interrupt error masks an auto-redispatch drain freeze that should have paused the job.

## Symptom (observed)

Two loop jobs "failed during phase transition." Both show the identical fingerprint:

| job | iter | created (UTC) | failed at (UTC) | audit entries | last audit step |
|-----|------|---------------|-----------------|---------------|-----------------|
| `e55ff79a-…` | 10 | 05:07:49 | 05:16:08 | 96 | `phase_transition` |
| `87a8cbe7-…` | 2  | 23:25:12 (prev day) | 23:33:25 | 103 | `phase_transition` |

For both, `get_frozen_job` returns the row `freeze_data`:

```
freeze_type:  version_upgrade
phase:        tactical
phase_number: 5
reason:       orchestrator drain intent at phase boundary
```

…yet `status=failed`. The audit trail ends exactly at `phase_transition`, with **no** completion/error step after it, and there is a **~2.5-minute stall with zero LLM calls** immediately before that final `phase_transition`:

| job | last LLM request | `phase_complete` | `phase_transition` | gap |
|-----|------------------|------------------|--------------------|-----|
| `e55ff79a-…` | 05:13:17 (`todo_complete`, doc 27105) | 05:13:18 | 05:15:46 | 2m28s |
| `87a8cbe7-…` | 23:30:49 (`todo_complete`, doc 26322) | 23:30:50 | 23:33:16 | 2m26s |

Both jobs had just staged a valid next-phase todo set (`87a8cbe7` staged 7 todos at `next_phase_todos`, audit `[97]`), so the strategic→tactical transition was **not** rejected for a todo-count violation.

## Why the DB state pins the root cause

The row state `status=failed` **and** `freeze_data.freeze_type=version_upgrade` is only reachable one way. Work backwards:

1. A clean `version_upgrade` pause **stashes and clears** the freeze blob. `version_upgrade` is in `AUTO_REDISPATCH_FREEZE_TYPES` (`orchestrator/services/completion.py:268`), and the `/complete` paused-branch (`orchestrator/main.py:~11335`) moves `freeze_data` into `context.last_freeze_data` and nulls the column (also enforced by `recover_orphaned_jobs`, `orchestrator/database/postgres.py:~2734`, and required by the `freeze_data IS NULL` dispatch predicate). **The freeze is still on the row → the job never took the clean pause path.**

2. For a `version_upgrade` freeze, the **only** `determine_job_status` branch that returns `failed` is the top-of-function `if error:` short-circuit (the `memory_unavailable`/`llm_unavailable` cap-failures are different `freeze_type`s). So `result["error"]` was truthy.

3. The `/complete` handler writes `result["freeze_data"]` to the row **unconditionally, before** status determination (`orchestrator/main.py:10977-10987`). So the same report that failed the job also carried `freeze_data=version_upgrade`.

Conclusion: **one** completion report carried `{should_stop: true, freeze_data: version_upgrade, error: <truthy>}`. Handler persists the freeze, `determine_job_status` sees the error and returns `failed`, the `version_upgrade→paused` branch is never reached.

## Root cause (code)

`orchestrator/services/completion.py`, `determine_job_status`:

```python
# ~481
error = result.get("error")
should_stop = result.get("should_stop", False)
...
# ~485  ← short-circuits on ANY error, before freeze_type is even parsed
if error:
    error_msg = error.get("message", str(error)) if isinstance(error, dict) else str(error)
    return ("failed", error_msg)
...
# ~520  freeze_type resolved here
freeze_type = fd.get("freeze_type")
...
# ~550  ← never reached when error is set
if freeze_type == "version_upgrade":
    # Continue-as-New: pause + re-dispatch same context onto a fresh-version pod
    return ("paused", None)
```

`orchestrator/main.py`, `complete_job`:

```python
# 10977 — persist incoming freeze_data FIRST, unconditionally
if result.get("freeze_data"):
    await conn.execute(
        "UPDATE jobs SET freeze_data = $1::jsonb WHERE id = $2::uuid",
        json.dumps(result["freeze_data"]), job_id,
    )
...
# 11148 — then decide status (error beats version_upgrade)
new_status, error_message = determine_job_status(job, result)
```

The drain freeze itself is set correctly at the phase boundary (`src/graph.py:3235-3256`, `_is_drain_requested()` → `freeze_type="version_upgrade"`, `should_stop=True`), and the agent reports the graph's final state on the success path (`src/api/dual_app.py:502-537`, `result = final_state`; `report_completion(job_id, result)`). So the `error` and the `version_upgrade` freeze reach `/complete` **in the same payload** because both were present in `final_state`.

## Where the `error` came from — inference (not yet confirmed)

This is the one piece not pinned to a string. The signal is the **~2.5-minute stall with zero LLM activity** right before the final `phase_transition`. That is not compaction (LLM summarization would show a request) — it looks like the old-image pod sitting in its **termination grace period** after the rolling deploy `SIGTERM`'d it, while a workspace-SFTP / checkpoint write inside `archive_phase`/`handle_transition` blocked to timeout. An I/O interrupt there can leave an `error` in the accumulated graph state (the `error` channel is only reset to `None` by the execute node's success returns; downstream nodes that don't write the key leave the last value intact), which then rides out in `final_state` alongside the drain freeze the phase-boundary check had just set.

Two adjacent agent paths are worth noting when confirming this:
- `src/api/dual_app.py:551-558` (non-cancellation exception) reports `{"error": {...}}` with **no** `freeze_data` — that path alone cannot produce `failed + freeze_data=version_upgrade`, which is further evidence the failing report came from the **success** path with a `final_state` that carried both fields.
- `src/agent.py:1017-1046` builds its `error_state` **without** `freeze_data` for the same reason.

The trigger (a `version_upgrade` drain at all) means an **agent-image deploy landed while the loop was running** — the two failures are ~6 h apart and each corresponds to a rollout of the agent image (drift detected → `signal_drain_pending` on the busy worker). This is orthogonal to the in-flight `loop_parallel_stages` work; the main cluster runs the deployed image and `determine_job_status`'s ordering is long-standing.

## Impact — the loop self-heals, which hides this

`_advance_project_loop` treats `job.status == "failed"` as a failed iteration: it increments `consecutive_failures`, then rotates and spawns the next iteration anyway. Observed:

- `e55ff79a` (iter 10) failed 05:16:08 → next iteration `f79fcba5` spawned 05:15:51, ran (processing at time of investigation).
- `87a8cbe7` (iter 2) failed 23:33:25 → next iteration `6e5b5d72` spawned 23:33:21, completed.

So a single drain-masked failure costs one wasted iteration (its unmerged repo work is discarded; KB writes made before the freeze persist) and one `consecutive_failures` bump. The surrounding ~28 iterations completed cleanly, so the counter reset between the two. The latent danger is correlated deploys (a burst of pushes) failing several **consecutive** iterations and stopping the loop via the failure cap.

## Fix

Primary: in `determine_job_status`, honor the auto-redispatch freeze types **before** the generic `error` short-circuit. A clean drain freeze at a phase boundary should pause-and-redispatch even when the interrupted run also surfaced an error:

```python
error = result.get("error")
should_stop = result.get("should_stop", False)
goal_achieved = result.get("goal_achieved", False)

fd = _parse_freeze_data(job) or (result.get("freeze_data") if isinstance(result.get("freeze_data"), dict) else {})
freeze_type = (fd or {}).get("freeze_type")

# A drain / auto-redispatch freeze beats a coincident transient/interrupt error:
# the phase boundary is clean, the work is checkpointed, re-dispatch resumes it.
if should_stop and freeze_type in AUTO_REDISPATCH_FREEZE_TYPES:
    return ("paused", None)

if error:
    return ("failed", error_msg)
...
```

(Keep the memory/LLM cap logic that intentionally *fails* `memory_unavailable`/`llm_unavailable` after their retry ceilings — those are handled in their own branches with counters and are not part of this reorder. The reorder only guarantees the freeze type is consulted before a bare `error` hard-fails the job.)

Belt-and-suspenders (defense in depth): on the agent side, don't attach a transient interrupt `error` to a completion report that already carries a clean `version_upgrade` (or other auto-redispatch) freeze — prefer the freeze, since the phase boundary is the intended clean hand-off point (`src/api/dual_app.py` success-path report / `src/graph.py` final-state assembly).

## Regression test

Add to `tests/test_completion.py` (or wherever `determine_job_status` is unit-tested): a job whose `freeze_data.freeze_type="version_upgrade"` **and** `result={"should_stop": True, "error": {"message": "…interrupted…"}, "freeze_data": {...version_upgrade...}}` must resolve to `("paused", None)`, not `("failed", …)`. Cover the same for the other `AUTO_REDISPATCH_FREEZE_TYPES`, and a negative case confirming `memory_unavailable`/`llm_unavailable` past their caps still fail.

## Open questions

- **Exact `error_message`** on `e55ff79a` / `87a8cbe7` — not retrievable through the MCP tools (the job formatters omit `error_message`/`error_details` and `query_table` can't filter to a row). Get it directly:
  `SELECT id, status, error_message, error_details, freeze_data FROM jobs WHERE id IN ('e55ff79a-2d64-4abb-85e6-82a7afe3259e','87a8cbe7-9f1e-4bee-8db5-15ff5e39b1a0');`
  That confirms whether the error is a workspace/SSH interrupt, a checkpoint-write failure, an `asyncio` cancellation surfaced as an exception, or something else — and whether `error.type` is `job_error` vs `workspace_unavailable`.
- **The ~2.5-min stall**: confirm it is `terminationGracePeriodSeconds` on the agent pod (compare the grace value to ~148 s) vs a fixed SFTP/checkpoint timeout. If it's the grace period, consider whether the busy-worker drain should also *pre-empt* the phase-transition I/O rather than letting it block to timeout under `SIGTERM`.
- **Whether to merge/preserve** the failed iteration's unmerged branch on a drain-masked failure once it's correctly re-dispatched (the re-dispatch resumes from the checkpoint, so this may become moot).
