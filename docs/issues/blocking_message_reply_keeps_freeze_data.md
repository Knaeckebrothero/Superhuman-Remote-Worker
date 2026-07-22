---
tags:
  - issue
  - jobs
  - dispatcher
  - messaging
---

# Replying to a blocking agent message wedges the job: `paused` but dispatcher-invisible

**Filed:** 2026-07-22, found live on the dev cluster while diagnosing job
`9b760af1` (project-loop iter 3 developer, blocked since 2026-07-18 on a stuck
managed shell tab). Line numbers are develop @ 2026-07-22.

> **CONFIRMED and FIXED on develop 2026-07-22.** Same bug class as
> [`delegation_freeze_lifecycle_gaps`](delegation_freeze_lifecycle_gaps.md)
> Gap 1, one door further along: the human-reply resume paths were the last
> frozen→paused transitions that did not shed the row-level freeze.

## Symptom

A human replies to an `ASYNC`/`BLOCKED` agent message in the Cockpit Action
Center. The reply is accepted (`POST …/messages/{thread}/reply` → 200), the
orchestrator logs `Queued job … for auto-dispatch with feedback`, and the job
flips `waiting_for_reply` → `paused`. Then nothing. No agent is ever assigned,
no pod is created, and the job sits at `paused` indefinitely. Retrying the
reply reproduces the same non-event.

## Root cause

`_internal_resume_job` (`orchestrator/main.py`) re-queued the job in one fused
statement — merge `context.queued_feedback`, `status = 'paused'`,
`assigned_agent_id = NULL` — but never cleared `freeze_data`.

The dispatcher's candidate query, `get_dispatchable_jobs`
(`orchestrator/database/postgres.py`), requires:

```sql
WHERE j.status IN ('created', 'paused')
  AND j.assigned_agent_id IS NULL
  AND j.freeze_data IS NULL      -- partial-index contract, migration 0046
```

So the resumed job satisfied two of the three terms and failed the third
**forever**. `paused` with a freeze blob is a terminal wedge: the dispatcher
cannot see the job, and nothing else is scheduled to change its state.

For this path the freeze is not an edge case — it is a **precondition**.
`_route_inbound_reply` only takes the resume branch when

```python
is_blocking_reply = (
    job_status == "waiting_for_reply"
    and freeze_data
    and freeze_data.get("thread_id") == thread_id
)
```

i.e. the row is guaranteed to carry a `blocking_message` freeze. **Every** reply
to a blocking message wedged its job.

`blocking_message` is deliberately *not* in `AUTO_REDISPATCH_FREEZE_TYPES`
(`services/completion.py`) — correct for the freeze itself, which parks awaiting
human action — so the `/complete` stash-and-clear backstop never covered it
either.

### Second, identical hole

`resume_job`'s inner `_queue_for_dispatch` (`orchestrator/main.py`) — the
explicit Resume path used when no agent is ready or the picked agent declines —
had the same omission, and additionally split the context merge from the status
flip (a lost-update window). Any manual Resume of a frozen job wedged it the
same way.

## Live evidence (dev, 2026-07-22)

Job `9b760af1`, reply sent 09:57:04Z:

| field | value |
|---|---|
| `status` | `paused` |
| `assigned_agent_id` | `NULL` |
| `context.queued_feedback` | "I've fixed the issue you can resume. You now have a tool to clear the shell." |
| `freeze_data` | `{"freeze_type": "blocking_message", "thread_id": "d78144", "timestamp": "2026-07-18T23:24:06…"}` |

Fleet scan of the same wedge (`status='paused' AND freeze_data IS NOT NULL AND
context ? 'queued_feedback'`) found **13 jobs**, oldest `2026-06-10`; two of them
still held live workspace pods (3d and 25h old) that could never be released
because their jobs could never finish.

## Fix

New `PostgresDB.queue_job_for_resume(job_id, context_merge)` performs the whole
re-queue in one statement: merge context, `status = 'paused'`,
`assigned_agent_id = NULL`, stash the freeze into `context.last_freeze_data`
for observability, and `freeze_data = NULL`. Both `_internal_resume_job` and
`_queue_for_dispatch` call it.

Clearing also matters beyond dispatch: a stale row-level freeze poisons the
NEXT completion, because `_parse_freeze_data` prefers the DB copy over the
request body.

Regression test: `tests/test_queue_job_for_resume.py` (real Postgres via
testcontainers, asserts the `get_dispatchable_jobs` contract directly), mirroring
`tests/test_delegation_resume_claim.py`.

## Recovery for already-wedged jobs

`UPDATE jobs SET freeze_data = NULL WHERE id = …` makes the job dispatchable
again on the next dispatcher tick; `context.queued_feedback` is already stamped,
so the agent receives the human reply on resume. Note this only revives the job
— a project loop that was stopped in the meantime is terminal
(`current_job_id = NULL`, advance hook is a no-op), so the revived job runs as
an orphan iteration and the loop does not advance.

## Related

- [`delegation_freeze_lifecycle_gaps`](delegation_freeze_lifecycle_gaps.md) —
  Gap 1, same contract violated by the delegation resume claim.
- `services/completion.py` `AUTO_REDISPATCH_FREEZE_TYPES` — the `/complete`
  stash-and-clear backstop, and why it does not cover `blocking_message`.
