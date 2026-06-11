---
tags:
  - agent
  - bug
  - llm-routing
  - retry
  - resilience
  - resolved
related:
  - "[[orchestrator_phase_override_credentials_not_injected]]"
  - "[[agent_audit_collection_missing_indexes]]"
  - "[[orchestrator_mongodb_cascading_failure_resilience]]"
---

# Agent Loops Forever on Non-Retriable LLM Errors

**Reported**: 2026-05-12
**Status**: **Resolved in `ca7bb28`** (2026-05-12). Verified in
production on 2026-05-12 against job
`10af3438-f843-431c-8ed9-9935aba4a250` pinned to the deliberately
non-existent model `gpt-99-nonexistent`: the agent received a 404, the
classifier flagged it `permanent`, and the job flipped to `failed`
within ~15 seconds with the underlying 404 surfaced as
`error_message`. No iteration loop, no audit write storm. See the
[Resolution](#resolution) section below for full verification.

Original report kept below for historical context — the analysis is
how we got to the fix.

## Summary

`src/graph.py`'s `execute` node has two retry layers:

1. An inner attempt loop (`max_retries` from the retry manager,
   default 3) with exponential backoff.
2. An outer iteration loop that bumps `iteration` and continues even
   after the inner loop returns an error.

The inner loop catches `Exception` and retries indiscriminately; the
outer loop continues unconditionally because `error.recoverable` is
**hardcoded to `True`** at the exhaustion path. A model-not-found
404 — which will never succeed regardless of how many times we ask —
gets the same treatment as a transient timeout. The result is an
infinite loop that:

- Burns CPU on the agent (~10 LLM calls / minute against a guaranteed
  failure).
- Writes an `error`-type audit row on every retry-exhaustion
  (`graph.py:1684-1702`).
- Never advances `Todo state: total=N, completed=0` past zero.
- Eventually trips a higher-level timeout / manual cancellation, but
  not before producing the write storm that triggered the 2026-05-12
  cluster outage.

## Observed Behavior

Same incident as the phase-override bug. Sample agent log
(`srw-agent-j-97cec126` → container `agent`), abridged:

```
10:49:16  ERROR  [418d6f58-...] LLM error after 4 attempts: 404 Model
          'gpt-5.3-codex-spark' not found
10:49:16  INFO   [Iteration 57] job=418d6f58-...
10:49:16  INFO   Todo state: total=5, completed=0, in_progress=0, pending=5
10:49:17  WARNING [418d6f58-...] LLM error (attempt 1/3), retrying in 1.0s
10:49:18  WARNING [418d6f58-...] LLM error (attempt 2/3), retrying in 2.1s
10:49:20  WARNING [418d6f58-...] LLM error (attempt 3/3), retrying in 4.0s
10:49:24  ERROR  [418d6f58-...] LLM error after 4 attempts: 404 ...
10:49:24  INFO   [Iteration 58] ...
```

Each iteration: ~7 seconds (3 retries with exponential backoff), one
audit row written at the failure path, zero progress. The job ran for
~9 minutes before being cancelled — that's ~70+ iterations against an
endpoint that was guaranteed to keep returning 404.

The orchestrator log corroborates this from the receiving side:

```
GET /api/jobs 200 (34055ms)
GET /api/jobs 200 (33248ms)
```

`/api/jobs` enrichment counts audit rows per job
(`orchestrator/database/mongodb.py:286-287`), so each enrichment fans
out one Mongo aggregation per audit-emitting job; with a stuck job
emitting rows continuously, those aggregations get slower with every
tick. See [[agent_audit_collection_missing_indexes]] for the Mongo
side.

## Expected Behavior

Permanent / non-retriable LLM errors should fail the job fast with a
clear status (`failed` + `error_message` naming the offending model /
endpoint), not loop. Concretely:

- `404` "model not found" → fail; the model id is invalid for that
  endpoint and won't become valid by retrying.
- `401` / `403` auth → fail; credentials are wrong.
- `400` with body `"invalid_request_error"` and a non-rate-limit code
  → fail; schema problem we can't paper over.
- `429` / `503` / `httpx.ConnectError` / `asyncio.TimeoutError` →
  retry as today; these *are* transient.

The outer iteration loop should treat a job that has produced no
useful output across N consecutive iterations as stuck regardless of
the inner classification — a defense-in-depth circuit breaker.

## Root Cause

`create_execute_node` in `src/graph.py` catches every exception from
the LLM invoke under a single `except Exception` and unconditionally
flags `recoverable: True`:

```python
# src/graph.py:1656-1711 (abridged)
retry_manager.record_failure("llm_invoke")

if retry_manager.should_retry("llm_invoke", attempt):
    delay = retry_manager.get_retry_delay(attempt)
    rate_limit_delay = _extract_rate_limit_delay(e)
    ...
    retry_manager.record_retry()
    await asyncio.sleep(delay)
    attempt += 1
    continue

# Max retries exceeded
logger.error(f"[{job_id}] LLM error after {attempt + 1} attempts: {e}")

if auditor:
    auditor.audit_step(
        ...
        data={
            "error": {
                "type": "llm_error",
                "message": str(e)[:500],
                "recoverable": True,        # ← always
                "attempts": attempt + 1,
            }
        },
        ...
    )

return {
    "error": {
        "message": str(e),
        "type": "llm_error",
        "recoverable": True,                 # ← always
    },
    "iteration": iteration + 1,
}
```

The only error-class branching that exists today is for *rate limit*
extraction (`_extract_rate_limit_delay`) — used purely to extend the
backoff window, not to change retry semantics. There is no path that
sets `recoverable: False`, and there is no consumer of `recoverable`
in the outer graph that would halt iteration if it were ever `False`.

Combined with the dispatch-time bug
[[orchestrator_phase_override_credentials_not_injected]] (which
produces a stable, permanent 404), this is what turned one misrouted
Scholar job into a write storm.

## Code References

| File | Lines | Role |
|---|---|---|
| `src/graph.py` | ~1550-1711 | `create_execute_node.execute` — inner attempt loop + outer return |
| `src/graph.py` | 1656-1678 | Retry decision — no error-class branching |
| `src/graph.py` | 1684-1702 | Audit step on retry-exhaustion (one row per cycle) |
| `src/graph.py` | 1704-1711 | Returns `error.recoverable=True` unconditionally |
| `src/core/context.py` | 1661-1707 | `RetryManager.should_retry` — count-based only, no error-type input |
| `src/graph.py` | (rate-limit helper) | `_extract_rate_limit_delay` — the only existing error classifier |

## Reproduction

1. Configure any job with a model name that doesn't exist at the
   target endpoint (easiest: pin `gpt-5.3-codex-spark` while leaving
   `base_url` at `https://ai.h4ll.app/v1` — see
   [[orchestrator_phase_override_credentials_not_injected]] for a way
   to reach this state without manual DB edits).
2. Start the job; tail the agent pod logs.
3. Observe iteration counter increasing while `Todo state:
   completed=0` is unchanged.
4. The job will never reach `failed` on its own — only cancellation
   stops the loop.

## Resolution

Fixed in commit `ca7bb28` ("Add `_classify_llm_error` for robust LLM
error classification and retry logic", 2026-05-12). The commit:

- Adds `_classify_llm_error` to `src/graph.py`, categorising LLM
  exceptions into `permanent`, `rate_limit`, or `transient` based on
  HTTP status, exception type, and OpenAI/Anthropic SDK error bodies.
- Drives the retry loop with the classification — `permanent` errors
  return `recoverable=False` immediately without consuming further
  attempts, and the outer iteration loop honours the flag by routing
  to a hard-failure path instead of incrementing `iteration`.
- Audits the permanent failure with the resolved error type and
  message so the job's `error_message` carries the underlying cause to
  the cockpit.
- Adds `tests/test_graph_helpers.py` with 113 lines covering the
  classifier across OpenAI SDK errors, network failures, ambiguous
  conditions, and rate-limit edge cases.

### Production verification (2026-05-12, post-deploy)

Negative test: created a job pinned to `gpt-99-nonexistent` (a model
intentionally absent from the registry) on the new orchestrator image
(`sha-8e92c81`):

| Aspect | Pre-fix (morning of 2026-05-12) | Post-fix |
|---|---|---|
| Initial 404 received | iteration 1 | iteration 1 |
| Behaviour after 4 retries | next iteration, retry again, forever | classifier flags `permanent`, exit retry loop |
| Audit rows written before stop | ~1 per iteration × N iterations | ≤ 1 (single failure row) |
| Final job status | `cancelled` only via manual intervention | `failed` automatically within ~15s |
| `error_message` on the row | empty | `Error code: 404 - {'error': {'message': "Model 'gpt-99-nonexistent' not found", 'type': 'invalid_request_error'}}` |

Verified directly:

```
 status |          assigned_agent_id           |                                                   error_message
--------+--------------------------------------+-------------------------------------------------------------------------------------------------------------------
 failed | a561d4f4-1c4f-4305-8849-25f72c81ba45 | Error code: 404 - {'error': {'message': "Model 'gpt-99-nonexistent' not found", ... } }
```

### Open follow-ups (deferred — not blocking)

- **Fix 3 from below** (iteration-level circuit breaker) — still
  worth doing as defence in depth for failure modes the classifier
  doesn't cover (e.g. a model that 200s with empty content forever).
  Not urgent now that the most common cause is handled.
- **Fix 4 from below** (rate-limiting per-iteration audit writes) —
  still relevant; rolls into [[orchestrator_mongodb_cascading_failure_resilience]]
  rather than belonging here.

## Proposed Fixes

### Fix 1 — Classify LLM exceptions into retriable vs permanent (Required)

Add a small classifier (or extend `_extract_rate_limit_delay` into
`_classify_llm_error`) that returns one of `retriable_transient`,
`retriable_rate_limit`, `permanent`. Drive the inner loop with it:

```python
classification = _classify_llm_error(e)
if classification == "permanent":
    # Skip retry; surface as a hard failure.
    return {
        "error": {
            "message": str(e),
            "type": "llm_error",
            "recoverable": False,
        },
        ...
    }
```

Minimum set to classify as `permanent`:

- `openai.NotFoundError` with body `"Model '..." not found"`.
- `openai.AuthenticationError` (`401`).
- `openai.PermissionDeniedError` (`403`).
- `openai.BadRequestError` (`400`) where the body's `type` is
  `invalid_request_error` and the code is **not** a rate-limit /
  context-window code (those we already special-case elsewhere).

`httpx.ConnectError`, `httpx.ReadTimeout`, `asyncio.TimeoutError`, 429,
5xx stay retriable.

### Fix 2 — Have the outer graph honour `recoverable=False` (Required)

The outer iteration loop needs a halt condition. Either:

- Route any `error.recoverable=False` straight to a `fail_job` node
  that flips the job to `status='failed'` and writes a final audit
  row, or
- Continue using the existing finalization path but skip the
  `iteration += 1` increment and short-circuit out.

Today `error` is read by downstream nodes (the LangGraph router picks
the next node based on state), but there is no node that interprets
`recoverable=False` as terminal — so this fix is mostly graph wiring,
not state shape.

### Fix 3 — Iteration-level circuit breaker (Defensive)

Independent of error classification: if N consecutive iterations
return without advancing any todo and without producing tool output,
fail the job. Catches future failure modes that we haven't classified
(e.g. a model that 200s with empty content forever). Concretely: track
`(iteration_count_since_progress, todo_state_hash)` in state; if
`iteration_count_since_progress > 10` and `todo_state_hash` unchanged,
emit a hard failure.

### Fix 4 — Don't audit every retry-exhaustion (Adjacent fix)

The audit row at `graph.py:1684-1702` fires on every iteration's
retry-exhaustion event. Once Fix 2 is in place this becomes a
single-write-per-job (job is failed and the loop ends), but during
transient classes the existing per-iteration writes are still useful.
Worth considering rate-limiting or deduplicating "same error, same
job, last N seconds" — keeps `agent_audit` bounded during partial
outages.

## Priority

1. **Fix 1 + Fix 2** — together they eliminate the infinite-loop class.
   Should land as one PR; either alone is incomplete.
2. **Fix 3** — defence in depth; protects against future failure modes
   not covered by the classifier.
3. **Fix 4** — write-storm mitigation; complements
   [[agent_audit_collection_missing_indexes]] and the broader
   [[orchestrator_mongodb_cascading_failure_resilience]] work.

## Open Questions

- LangChain wraps provider exceptions; the classifier needs to look at
  both the LangChain envelope (`langchain_core.exceptions.OutputParserException`
  etc.) and the underlying `openai.*Error` / `anthropic.*` / provider-
  native types. Worth auditing what actually surfaces here in practice
  — the incident showed a raw `Error code: 404 - {'error': ...}`
  string, suggesting the OpenAI client's `APIStatusError` makes it
  through unmodified.
- Should `recoverable=False` propagate up to the orchestrator so the
  cockpit can show a structured "Model 'X' not available on endpoint
  'Y'" message rather than a generic failure? Probably yes; ties into
  the existing `error_message` / `error_details` columns on `jobs`.
- Some "non-retriable" errors *do* become retriable if the user fixes
  config mid-job. Fix 1/2 should produce a `failed` state that a
  cockpit-side "Retry" button could reset; not a worse experience than
  today.
