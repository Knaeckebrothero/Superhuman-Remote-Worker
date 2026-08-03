---
tags:
  - issue
  - jobs
  - critic
  - verification
  - orchestrator
  - agent-resilience
---

# A job rescued by hand silently skips its configured critic, and nothing can spawn one afterwards

**Filed:** 2026-08-03, from Better Resavio jobs `e1192a9d` ("Job 1A — Heritage
Green") and `c6dd288d` ("Job 1B — Modern Kurort Natural") on dev.
**Status:** **DIAGNOSED, UNFIXED.** No code written.
**Severity:** **medium-high** — not a wedge and not data loss, but it is a
*silent* loss of the review gate. Both jobs are now `completed` with
`verification.enabled: true` and zero critic subjobs. Nothing in the DB, the
UI, or the logs distinguishes them from jobs a critic actually approved.
**Component:** `orchestrator/main.py`
(`_trigger_verification_on_complete`, `complete_job`, `approve_job`),
`orchestrator/services/completion.py` (`is_job_completion_freeze`).

## Symptom

A job that was wedged by an infrastructure failure and moved to
`pending_review` by hand shows no critic subjob in the cockpit, no expand
chevron, and no `context.verification_rounds` ledger — despite carrying
`verification: {enabled: true, max_rounds: 5}`. It can then be approved
straight to `completed` through the normal Review button, and at no point does
anything mention that the configured review round never happened.

The cockpit is reporting the truth. `groupJobsHierarchically`
(`cockpit/src/app/views/jobs/job-list.component.ts:1087`) nests purely on
`parent_job_id`, and renders an orphaned child standalone when the parent is
absent from the list, so it cannot hide a subjob that exists. There simply
isn't one.

## Evidence

Queried against dev via `/api/jobs?limit=500` (window `2026-06-25` →
`2026-08-03`, 500 jobs), plus per-job `/api/jobs/{id}`:

| Job | id | status (08-03) | `freeze_data` | `context.last_freeze_data.freeze_type` | children |
|---|---|---|---|---|---|
| 1A | `e1192a9d` | `completed` | NULL | `version_upgrade` | **0** |
| 1B | `c6dd288d` | `completed` | NULL | `version_upgrade` | **0** |
| 1C | `4435994d` | `pending_review` | SET | `job_complete` | 2 (`cdbbc10d` failed, `37c418d2` completed) |

All three are `config_name: designer` with an identical override:

```json
{"verification": {"enabled": true, "max_rounds": 5}, "autonomy": "review", ...}
```

and for 1A/1B the resolved config confirms it landed —
`{"enabled": true, "max_rounds": 5, "critic_config": "critic"}`. So
`is_verification_enabled()` returns `True` for both. **Config is not the
difference**; 1C proves the same config spawns critics normally.

1A and 1B additionally carry stale `context.llm_outage` blobs with
`next_retry_at` of 2026-07-27 / 2026-07-26 and `started_at`/`completed_at` both
NULL — they never ran to a real completion. Their upstream cause is the Jul 27
incident already recorded in
[`transient_db_error_hard_fails_job_and_destroys_vm.md`](transient_db_error_hard_fails_job_and_destroys_vm.md);
`c6dd288d` is named there and in both
`docs/superpowers/specs/2026-07-28-*` specs. **Neither that incident doc nor
either spec contains the word "critic" or "verification"** — this consequence
is unrecorded.

## Root cause

Critic subjobs have exactly one creation site in the entire system:
`_trigger_verification_on_complete` (`orchestrator/main.py:13005`), called from
one place, the `POST /api/jobs/{job_id}/complete` handler
(`orchestrator/main.py:15597`). That endpoint is `require_internal` — a
mandatory, fail-closed `X-Internal-Key` check
(`orchestrator/security/access.py:1120`) that no cockpit, MCP, or PAT caller
can satisfy. There is no other route in. Confirmed by enumerating every
mutating `/api/jobs/{job_id}/*` route across `main.py` and `routers/`: no
`verify`, no `re-verify`, no admin spawn.

So a hand-written `UPDATE jobs SET status='pending_review'` produces a job that
*looks* reviewable and is missing the only side effect that matters.

The second half is that the state such a rescue leaves behind can no longer
satisfy the trigger's own guard (`orchestrator/main.py:13054`):

```python
if not is_job_completion_freeze(job) and job.get("status") != "reviewing":
    return
```

`is_job_completion_freeze` (`orchestrator/services/completion.py:238`) reads the
job row's `freeze_data`. For 1A/1B that column is NULL — **correctly** so: the
auto-redispatch pause path deliberately moves `freeze_data` into
`context.last_freeze_data` because the dispatcher's partial index requires
`freeze_data IS NULL` (`0046_jobs_dispatchable_partial_idx.notx.sql`), and a
kept freeze blob would make the job permanently undispatchable. So every job
parked by a drain / `version_upgrade` / LLM-outage pause has NULL `freeze_data`
by design, and a hand-flip to `pending_review` fails both halves of the guard.
Setting `reviewing` by hand instead doesn't help either: nothing sweeps for
targets in `reviewing` without a critic.

Finally, `approve_job` never consults verification state — it approves a
`pending_review` job whether or not a round ever ran. That's what turns a
recoverable gap into a permanent, invisible one.

## Why the existing fixes don't cover it

Fix 2 of `2026-07-28-transient-infra-failure-handling-design.md` shipped
(`orchestrator/main.py:14833`) and is the closest thing: a late `job_complete`
freeze arriving on a **`failed`** job is now let through the terminal gate,
`error_message` is cleared, and the report re-resolves the job — which does
reach the verification trigger, because the handler persists the body's
`freeze_data` to the row *before* calling it.

That closes the `failed` case only, and only for an agent still alive enough to
retry. It does nothing for a job already sitting at `pending_review` (or
`completed`) with NULL `freeze_data`, which is precisely the state a human
rescue produces. The parked
`2026-07-28-job-termination-negotiated-transition-design.md` would dissolve the
upstream cause but is explicitly not scheduled.

## Fix options

Ranked; not yet decided.

1. **Make the skip visible (smallest, do this regardless).** When
   `approve_job` resolves a job whose config has `verification.enabled` but
   whose `context.verification_rounds` is absent/empty, log at WARNING and
   stamp something durable on the job (`context.verification_skipped =
   {"reason": ..., "at": ...}`) so the cockpit can badge it. This does not
   restore the review round, but it stops the system from presenting an
   unreviewed job as an approved one. It is also the only option that helps
   the two jobs already `completed`.

2. **An explicit admin re-verify action.** `POST /api/jobs/{job_id}/verify`,
   admin-gated, callable on `pending_review` / `completed`: synthesize a
   `job_complete` freeze from `context.last_freeze_data` when the row's own
   `freeze_data` is NULL, then call `_trigger_verification_on_complete`
   directly. The gate decision is already safe for this — with an empty ledger
   `_verification_gate_decision` (`orchestrator/main.py:12906`) returns
   `("spawn", "")`, and `has_live_verification_critic`
   (`orchestrator/database/postgres.py:4979`) prevents a double spawn. Cost is
   a thin brief: `content_tree` is absent, so the no-progress guard abstains on
   round 2 (documented as safe — the round cap bounds it).

3. **A sweeper.** Periodically find jobs in `pending_review` with verification
   enabled and an empty ledger, and spawn. Rejected on first read: it would
   also fire on the many legitimate ways a job reaches `pending_review` without
   a critic (autonomy pauses, `scholar_pending_review_silent_success.md`), and
   guessing intent is worse than an explicit action.

Option 1 + 2 together are the coherent pair: 1 makes the gap legible forever,
2 gives an operator a way to close it.

## Testing

| Option | Test | Assertion |
|---|---|---|
| 1 | `test_completion_endpoint.py` | approving a verification-enabled job with an empty ledger stamps `verification_skipped` + logs WARNING; a job with a ledger does not |
| 2 | `test_completion_endpoint.py` | `POST /verify` on `pending_review` + NULL `freeze_data` + non-empty `last_freeze_data` → critic created with `parent_job_id` set |
| 2 | `test_completion_endpoint.py` | second `POST /verify` while the first critic is live → no second spawn (`has_live_verification_critic`) |
| 2 | — | non-admin caller → 403; job with `verification.enabled: false` → 400 |

## Notes

- **Supersedes [`subjob_trigger.md`](subjob_trigger.md)** (2026-03-07) as the
  live record of "job in `pending_review`, no critic spawned". That doc's
  diagnosis predates the orchestrator cutover: it blames agent-side
  `_maybe_trigger_verification` / `_is_verification_enabled`, neither of which
  exists in `src/` anymore — the only surviving trace is the docstring at
  `orchestrator/main.py:14792` recording the move. Anyone grepping the symptom
  lands there and is sent down a dead path.
- The `complete_job` docstring claims "Ingress strips this path". It doesn't:
  `helm/templates/ingress.yaml` blocks only the `/api/agents` and
  `/api/internal` prefixes, and `ingress.internalPathBlock.enabled` defaults to
  `false` (`helm/values.yaml:689`). The real and sufficient gate is the
  app-layer `require_internal`. Worth correcting so nobody reasons about
  reachability from a false premise.
