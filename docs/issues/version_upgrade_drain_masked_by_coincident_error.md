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

**Status:** investigated 2026-07-07 from two failed Better-Resavio loop iterations on the main cluster. **Root cause confirmed by elimination against the DB state + code paths; fix plan written (see "Fix plan"), not yet implemented.** The exact `error_message` string on the two jobs was not retrieved (see "Open questions"). Symbols/line numbers current as of this date.
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

## Fix plan

Two small changes — an orchestrator-side precedence guard (the real fix) and an agent-side hygiene line (defense in depth) — plus tests and a k3d drill. `determine_job_status` has exactly **one** production call-site (`orchestrator/main.py:11131`, the `/complete` handler), so the blast radius is the status-decision matrix itself; the existing test files already pin most of that matrix.

### Design decision: guard the error short-circuit, don't blanket-pause

The naive reorder — `if freeze_type in AUTO_REDISPATCH_FREEZE_TYPES: return ("paused", None)` before the error check — is **wrong**: it would bypass the `memory_unavailable`/`kb_unavailable` retry-cap logic (which must return `failed` at `MEMORY_RETRY_CAP`) and the `llm_unavailable` 24 h ceiling. Instead, keep every freeze branch exactly where it is and make the `error` short-circuit **step aside** when a freeze with its own pause-vs-fail branch is present. The freeze's branch then decides — pause under cap, fail past it — precisely as today.

Scope: the immune set is `AUTO_REDISPATCH_FREEZE_TYPES ∪ {llm_unavailable}`. `llm_unavailable` was not in the observed incident but is the same failure class (freeze whose branch owns pause-vs-fail, with the outage sweeper doing re-dispatch), and including it is strictly safer than the status quo — its branch still enforces the ceiling. `delegation` is deliberately **not** included (an errored delegation parent failing is current behavior, and its children/unblock lifecycle is a separate can of worms — see `critic_failure_leaves_parent_job_stuck_reviewing.md`). Critic subjobs (`parent_job_id` set) keep their existing routing: the guard only prevents the error-fail; a *drained critic* landing `pending_review` instead of `paused` is a pre-existing gap, out of scope here.

### Step 0 — forensics confirmation (optional, does not gate the fix)

Grab the actual `error_message`/`error_details` for `e55ff79a`/`87a8cbe7` (SQL in "Open questions") to confirm the interrupt-under-SIGTERM inference and check `error.type` (`job_error` vs `workspace_unavailable`). The precedence bug holds regardless of the string, so the fix does not wait on this.

### Step 1 — orchestrator: guard the error short-circuit (`orchestrator/services/completion.py`)

New constant next to `AUTO_REDISPATCH_FREEZE_TYPES` (~line 268):

```python
# Freeze types whose dedicated branch in determine_job_status must own the
# pause-vs-fail decision even when a coincident ``error`` rides the same
# completion report — a drain/outage freeze taken at a clean phase boundary
# beats a transient interrupt error (the work is checkpointed; re-dispatch
# resumes it). docs/issues/version_upgrade_drain_masked_by_coincident_error.md
ERROR_IMMUNE_FREEZE_TYPES: frozenset[str] = AUTO_REDISPATCH_FREEZE_TYPES | frozenset(
    {"llm_unavailable"}
)
```

In `determine_job_status` (~481-499): hoist the freeze-data resolution above the error check (it's pure parsing; today it happens after the `should_stop` early-return), then guard:

```python
    error = result.get("error")
    should_stop = result.get("should_stop", False)
    goal_achieved = result.get("goal_achieved", False)

    # Resolve freeze_data early (DB row preferred, request-body fallback) so
    # the error short-circuit below can see an auto-redispatch freeze.
    fd = _parse_freeze_data(job)
    if not fd:
        fd = result.get("freeze_data")
        if not isinstance(fd, dict):
            fd = {}
    freeze_type = fd.get("freeze_type")

    if error:
        error_msg = (
            error.get("message", str(error)) if isinstance(error, dict) else str(error)
        )
        # A coincident error must not mask a clean-boundary freeze whose
        # branch below owns the pause-vs-fail decision (drain Continue-as-New,
        # memory/KB retry caps, LLM-outage ceiling). Otherwise a deploy drain
        # racing a transient interrupt hard-fails a re-dispatchable job.
        if not (should_stop and freeze_type in ERROR_IMMUNE_FREEZE_TYPES):
            return ("failed", error_msg)
        logger.warning(
            "Job %s: '%s' freeze accompanied by coincident error — routing the "
            "freeze, not the error: %s",
            job.get("id"),
            freeze_type,
            error_msg[:200],
        )

    if not should_stop:
        return (None, None)  # Still running — leave as processing
```

…and delete the now-duplicate fd-resolution block (old ~494-499) and the later `freeze_type = fd.get("freeze_type")` (old ~520). Everything downstream is unchanged: `version_upgrade`/`workspace_upgrade_required` → `paused` (then the `/complete` paused-branch clears the agent + stashes the freeze, and the 1·mem counter bump at `main.py:~11133` runs for `memory_unavailable` exactly as designed); `memory_unavailable` at cap / `llm_unavailable` over ceiling still → `failed`.

Note the guard requires `should_stop` — an error with a freeze but `should_stop=False` still fails (unchanged), so a stale row-level freeze can't shield a genuinely crashed run. (Belt-and-suspenders against stale row freezes already exists: the paused-path stash clears them, per "Why the DB state pins the root cause" §1.)

The coincident error is observable via the `logger.warning` (and the freeze itself lands in `context.last_freeze_data` on the stash). Don't write it to `error_message` — a paused row with an `error_message` reads as failed in the cockpit.

### Step 2 — agent: don't ship a stale `error` with the drain freeze (`src/graph.py:3235-3256`)

In the drain-intent branch of `handle_transition`, clear the state's `error` channel alongside setting the freeze — the phase boundary is clean and the resume continues from the checkpoint, so whatever a mid-phase node left in `error` is moot:

```python
            updates["freeze_data"] = upgrade_freeze
            updates["should_stop"] = True
            updates["error"] = None  # clean boundary — a stale mid-phase error must not
                                     # mask the freeze at the orchestrator (see issue doc)
```

(`{"error": None}` is an established node-return pattern — e.g. `src/graph.py:1883`, `2120`.) With this, the observed payload shape can't be produced by the drain path at all; Step 1 remains as the orchestrator-side guarantee for old agents mid-rollout and any other producer of freeze+error payloads.

### Step 3 — tests

`tests/test_drain_intent.py::TestVersionUpgradeFreeze` (already pins version_upgrade → paused, lines 385-429) — add:

- `test_version_upgrade_with_coincident_error_still_pauses` — the incident shape: `result={"should_stop": True, "error": {"message": "…interrupted…", "type": "job_error"}, "freeze_data": {version_upgrade}}` → `("paused", None)`. Also the DB-side variant (freeze on the job row, error in result) — that's the exact shape `/complete` produces after its early freeze write.
- `test_error_immune_freezes_route_their_own_branch` — parametrized: `workspace_upgrade_required`+error → `paused`; `memory_unavailable`+error under cap → `paused`; **`memory_unavailable`+error at `MEMORY_RETRY_CAP` → `failed`** (cap survives the guard); `llm_unavailable`+error under ceiling → `paused`.
- `test_bare_error_still_fails` — error with no freeze, and error with a non-immune freeze (`delegation`) → `("failed", msg)` unchanged.
- `test_error_without_should_stop_still_fails` — error + version_upgrade freeze but `should_stop=False` → `failed` (guard requires the stop signal).

Graph side: extend the drain-boundary freeze test (or add one alongside `TestDualDrainHandler`) asserting `handle_transition`'s updates include `error: None` when the drain freeze fires.

Run: `pytest tests/test_drain_intent.py tests/test_complete_job_endpoint.py tests/test_completion_endpoint.py tests/test_llm_outage_resilience.py tests/test_delegation.py -x -q` + `ruff check src/ orchestrator/ tests/`.

### Step 4 — verify on k3d (Tilt inner loop)

1. **Endpoint simulation** (deterministic reproduction of the incident payload): create a throwaway job, let it reach `processing`, then post the double payload from inside the cluster:

   ```bash
   kubectl --context=k3d-srw -n srw exec deploy/srw-orchestrator -c orchestrator -- \
     curl -sf -X POST http://localhost:8085/api/jobs/<id>/complete \
     -H "X-Internal-Key: $KEY" -H 'Content-Type: application/json' \
     -d '{"should_stop": true,
          "error": {"message": "simulated SIGTERM interrupt", "type": "job_error"},
          "freeze_data": {"freeze_type": "version_upgrade", "phase": "tactical",
                          "phase_number": 5, "reason": "orchestrator drain intent at phase boundary"}}'
   ```

   Assert on the row: `status='paused'`, `assigned_agent_id IS NULL`, `freeze_data IS NULL`, `context.last_freeze_data.freeze_type='version_upgrade'`; the warning line in orchestrator logs; then the dispatcher re-dispatches and the job resumes (flips back to `processing` on a fresh pod).

2. **Full drain drill** (end-to-end, covers Step 2): start a k3d loop or single worker job, then set the drain intent on its busy agent row (`UPDATE agents SET intents = intents || '{"should_drain": true, "drain_reason": "stale_image"}'::jsonb WHERE id = …`). Watch: phase-boundary freeze → `paused` → re-dispatch → job continues and completes. This re-exercises the whole Continue-as-New path (including the livelock-fix resume-clear) with the new code.

### Step 5 — rollout + post-deploy watch

Commit both changes together on `develop` (orchestrator guard + agent hygiene + tests), normal pipeline to the main cluster. The two sides are independently safe in either rollout order (Step 1 protects against old-agent payloads; Step 2 stops producing them).

Post-deploy signal: the next agent-image deploy that lands while the Better-Resavio loop is mid-iteration should produce `paused` → re-dispatch → the **same job id** resuming and completing, not a failed iteration — grep orchestrator logs for the new "freeze accompanied by coincident error" warning and for `status set to 'paused'`. The existing `auto_continue_drains` backstop counter (progress-aware, alerts at `AUTO_CONTINUE_DRAIN_ALERT_CAP`) already guards the downside risk of this change — a job that re-pauses without progressing gets alerted on, not silently churned.

When shipped, update **Status** above and the memory note (`project_version_upgrade_drain_masked_by_error`).

### What must NOT change (pinned by the tests above + existing suites)

| input shape | status (unchanged) |
|---|---|
| `error`, no freeze / non-immune freeze | `failed` |
| `error` + immune freeze, `should_stop=False` | `failed` |
| `memory_unavailable` at `MEMORY_RETRY_CAP` (error or not) | `failed` |
| `llm_unavailable` over 24 h ceiling (error or not) | `failed` |
| `delegation` freeze (no error) | `waiting` |
| critic subjob with `freeze_data.status` | that status |
| genuine completion (`job_complete`/`goal_achieved`) | `completed`/`reviewing`/`pending_review` |

## Open questions

- **Exact `error_message`** on `e55ff79a` / `87a8cbe7` — not retrievable through the MCP tools (the job formatters omit `error_message`/`error_details` and `query_table` can't filter to a row). Get it directly:
  `SELECT id, status, error_message, error_details, freeze_data FROM jobs WHERE id IN ('e55ff79a-2d64-4abb-85e6-82a7afe3259e','87a8cbe7-9f1e-4bee-8db5-15ff5e39b1a0');`
  That confirms whether the error is a workspace/SSH interrupt, a checkpoint-write failure, an `asyncio` cancellation surfaced as an exception, or something else — and whether `error.type` is `job_error` vs `workspace_unavailable`.
- **The ~2.5-min stall**: confirm it is `terminationGracePeriodSeconds` on the agent pod (compare the grace value to ~148 s) vs a fixed SFTP/checkpoint timeout. If it's the grace period, consider whether the busy-worker drain should also *pre-empt* the phase-transition I/O rather than letting it block to timeout under `SIGTERM`.
- **Whether to merge/preserve** the failed iteration's unmerged branch on a drain-masked failure once it's correctly re-dispatched (the re-dispatch resumes from the checkpoint, so this may become moot).
