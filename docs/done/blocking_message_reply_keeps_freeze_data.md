---
tags:
  - issue
  - jobs
  - dispatcher
  - messaging
---

# Replying to a blocking agent message wedges the job: `paused` but dispatcher-invisible

**Status:** FIXED — shipped and live on dev. Committed 2026-07-22 in
`4dba9836` (bundled into "feat(core): …improve job resume handling"), pushed to
`origin/develop`, and deployed in orchestrator image `sha-dc58820` (also carried
forward in the later `sha-f131079`). Verified against the running pod: its
`/app/database/postgres.py` defines `queue_job_for_resume` and `/app/main.py`
calls it from both resume sites. Same bug class as
[`delegation_freeze_lifecycle_gaps`](../issues/delegation_freeze_lifecycle_gaps.md)
Gap 1, one door further along: the human-reply resume paths were the last
frozen→paused transitions that did not shed the row-level freeze.
**Severity:** high — every reply to a blocking agent message silently wedged
its job; 13 jobs were found stuck this way on dev (oldest 2026-06-10).
**Component:** `orchestrator/main.py` `_internal_resume_job` (~10668) and
`resume_job`'s `_queue_for_dispatch` (~10077); `orchestrator/database/postgres.py`
`queue_job_for_resume`.

**Filed:** 2026-07-22, found live on the dev cluster while diagnosing job
`9b760af1` (project-loop iter 3 developer, blocked since 2026-07-18 on a stuck
managed shell tab). Line numbers are develop @ 2026-07-22.

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
`tests/test_delegation_resume_claim.py`. `testcontainers` is not installed in
the local dev env (Py3.14), so the test runs in CI; the SQL was additionally
validated pre-ship against real dev Postgres on a `CREATE TEMP TABLE` probe
(frozen row → paused + agent/freeze cleared + feedback merged + siblings kept;
unfrozen row → no phantom `last_freeze_data` key).

## Recovery for already-wedged jobs (executed 2026-07-22)

The deployed code only prevents *new* wedges; the 13 jobs already stuck were
cleared by hand. The recovery mirrored the code fix exactly — stash the blob,
then drop it — so the audit trail survived:

```sql
UPDATE jobs
   SET context = COALESCE(context, '{}'::jsonb)
                 || jsonb_build_object('last_freeze_data', freeze_data),
       freeze_data = NULL, updated_at = CURRENT_TIMESTAMP
 WHERE status = 'paused' AND freeze_data IS NOT NULL
   AND context ? 'queued_feedback';   -- matched all 13
```

`context.queued_feedback` was already stamped, so each job received its human
reply on resume. Loop job `9b760af1` was additionally bumped to `priority = 20`:
the dispatch queue is `priority DESC, created_at ASC`, and at the default 5 the
07-18 job sat behind the whole June backlog with only one ready agent, so it
would have been starved. It then re-provisioned its workspace, dispatched, and
**ran to completion** (`queued_feedback` consumed, confirming the reply reached
the agent's context on resume); the rest of the batch drained too.

Note the clear only revives the *job*. Its project loop had been stopped in the
interim (2026-07-22 10:40, `POST /loop/stop`) and is terminal
(`current_job_id = NULL`, advance hook is a no-op), so the revived job ran as an
orphan iteration and the loop did **not** advance to iter 4.

## Related

- [`delegation_freeze_lifecycle_gaps`](../issues/delegation_freeze_lifecycle_gaps.md) —
  Gap 1, same contract violated by the delegation resume claim.
- `services/completion.py` `AUTO_REDISPATCH_FREEZE_TYPES` — the `/complete`
  stash-and-clear backstop, and why it does not cover `blocking_message`.
