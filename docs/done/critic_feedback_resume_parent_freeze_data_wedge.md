---
tags:
  - issue
  - jobs
  - critic
  - dispatcher
  - lifecycle
---

# Critic "returned with feedback" wedges the parent: `_internal_resume_job` keeps `freeze_data`, so the paused parent is invisible to the dispatcher forever

**Status:** FIXED — resolved incidentally by `4dba9836` (2026-07-22), the same
commit that fixed the blocking-message sibling
(`docs/done/blocking_message_reply_keeps_freeze_data.md`): it replaced
`_internal_resume_job`'s inline UPDATE with `PostgresDB.queue_job_for_resume`,
which sets `freeze_data = NULL` (and stashes the blob to
`context.last_freeze_data`). The critic-"returned" arm reaches it via
`_internal_resume_job`, so it got the fix transitively.
**Audit 2026-08-06 (batch fix session):** all sibling resume paths the doc
lists verified clean at HEAD — blocking-message/urgent/LLM-triage resume
(via the same wrapper), explicit-Resume `_queue_for_dispatch`
(`queue_job_for_resume`), sudo VM-upgrade approve (`main.py` inline
`freeze_data = NULL`), sudo-denial resume (inline `freeze_data = NULL`),
deliverable-gate bounce (`deliverable_gate.queue_resume`). Regression tests
added the same day: DB-level critic-flavored test in
`tests/test_queue_job_for_resume.py`
(`test_critic_returned_resume_clears_freeze_so_dispatcher_sees_job`) and a
seam pin in `tests/test_verification_flow.py`
(`test_returned_resume_routes_through_freeze_clearing_write`) — the existing
wiring test stubbed `_internal_resume_job` wholesale, so nothing pinned the
critic path to the freeze-clearing write.
**Residual (separate docs):** (1) the two live victims below predate the fix —
if never manually unwedged, they still need the one-off
`UPDATE jobs SET freeze_data = NULL`; the code fix does not retroactively
clear rows. (2) The same defect class survives in `PostgresDB.pause_job`
(no freeze clear) — tracked in
`docs/issues/recovery_pause_repersists_stale_freeze_invisible_job.md`.
**Severity:** high — every review round-trip on a non-`full`-autonomy job
silently dead-ends; the job looks "Paused" in the cockpit and nothing ever
picks it up.
**Component:** `orchestrator/main.py` (`_internal_resume_job`),
`orchestrator/database/postgres.py` (`get_dispatchable_jobs`)
**Sister issue (same defect class, delegation path, already fixed):**
`delegation_freeze_lifecycle_gaps.md` Gap 1.

## Symptom

Parent job completes → goes to `reviewing` → critic reviews and returns it
with feedback → parent flips to `paused` with `context.queued_feedback` set
and `assigned_agent_id = NULL` → **and then nothing happens, ever**. The
critic sits in `waiting` for the next round that never starts.

Live victims (2026-07-18):

- `ba887943-489a-41bb-8ac4-ce1bb5cab91d` (nightly ProtonMail automation job,
  developer). Completed 2026-07-17 23:33Z (`job_complete` freeze written to
  `jobs.freeze_data`), critic `14fbbdc8` returned it with feedback 23:57Z
  (all five ACs were stubs). Parent paused with `queued_feedback` since —
  never re-dispatched. (Its `updated_at` drifts from sweeper touches; the
  audit trail shows a single 347-iteration round, no second round.)
- `18174320-b529-4306-aa44-001493326467` ("stab", kai's project). Completed
  10:44Z 07-17, critic `8ed9979d` returned feedback 11:18Z, parent paused
  and undispatchable since (~20 h at time of filing).
- `b988e3f0-d5cf-4406-bc9a-5e567d4d2adb` ("netzteil") hit the same wedge on
  07-16, **escaped it only because the user clicked cockpit Resume** — which
  routed it into the resume-credential-injection bug
  (`job_resume_direct_path_skips_credential_injection.md`) and killed it
  terminally. The two bugs compose: the only path out of this wedge is the
  one path that strips credentials.

## Root cause

1. When the parent completes, the orchestrator persists the agent's
   `job_complete` freeze blob in the `jobs.freeze_data` column (that is what
   `get_frozen_job` serves during review).
2. The critic's "returned" verdict branch
   (`_handle_critic_verdict_on_complete`, `orchestrator/main.py:11613`)
   calls `_internal_resume_job(target_job_id, feedback)`
   (`orchestrator/main.py:10716`), whose single UPDATE sets
   `context || {queued_feedback}`, `status='paused'`,
   `assigned_agent_id=NULL` — **and never touches `freeze_data`**.
3. The dispatcher's `get_dispatchable_jobs`
   (`orchestrator/database/postgres.py` ~4571) hard-requires
   **`freeze_data IS NULL`** (partial-index contract, migration 0046).
   The parent fails the predicate on every tick → invisible forever.

The delegation path had this exact bug and fixed it locally:
`claim_delegation_resume` (`postgres.py:4413`) explicitly sets
`freeze_data = NULL` on its `waiting → paused` requeue, with a comment citing
Gap 1. The critic-feedback path (and the other `_internal_resume_job`
callers: `send_message_to_job` blocking-message resume ~8941-8980, sudo
denial ~11702, ~13007) never got the equivalent.

Verified live: both victims' rows have `status='paused'`,
`assigned_agent_id=NULL`, `context.queued_feedback` set, and a non-NULL
`job_complete` freeze blob → excluded by the dispatchable predicate.

## Why it wasn't noticed earlier

- Autonomy `full` jobs never enter review, loop jobs auto-approve; only
  review-autonomy jobs with a critic that actually *returns* feedback hit it.
- The cockpit shows "Paused · Resume" — indistinguishable from a benign
  pause, so users click Resume, which sometimes works (warm agent) and
  masked the pattern as flakiness.

## Fix direction

Primary: clear the stale blob in `_internal_resume_job`'s UPDATE
(`freeze_data = NULL`, mirroring `claim_delegation_resume`) — the freeze has
served its purpose once a verdict exists; the review artifacts already live
in the critic's context/frozen file. Audit the other `_internal_resume_job`
call sites for the same expectation. Add a regression test:
complete → critic returned → parent must appear in `get_dispatchable_jobs`.

Remediation for the two live victims once fixed (or manually):
`UPDATE jobs SET freeze_data = NULL WHERE id IN ('ba887943…','18174320…')`
and let the dispatcher pick them up (their `queued_feedback` is intact).

## Related

- `delegation_freeze_lifecycle_gaps.md` — same wedge, delegation flavor.
- `stale_critic_waiting_status_escapes_reaper.md` — the critic-side leak
  discovered in the same sweep.
- `critic_failure_leaves_parent_job_stuck_reviewing.md` — inverse direction
  (critic dies, parent stuck in `reviewing`).
