---
tags:
  - issue
  - fix-spec
  - jobs
  - orchestrator
  - dispatch
---

# Issue — boot-failed completion re-persists a stale freeze; recovery pause leaves the job paused-but-invisible

**Status:** **FIXED 2026-08-06 (batch #2)** — both layers, exactly as proposed
below:
1. Root cause: `complete_job` now gates the freeze persist on
   `should_persist_completion_freeze(result)` (orchestrator/services/
   completion.py) — a `workspace_unavailable` completion can only ECHO the
   previous freeze, so it is not persisted (logged instead). Deliberately
   narrow: errored completions with a genuinely new freeze (llm_outage
   backoff) still persist.
2. Invariant: `handle_pod_workspace_recovery` pauses via the new
   `PostgresDB.pause_job_shed_freeze` — `pause_job`'s `status='processing'`
   CAS (the double-dispatch gate) plus `queue_job_for_resume`'s
   stash-and-clear shape (freeze → `context.last_freeze_data`, column
   NULLed), so the paused-implies-dispatchable invariant holds regardless of
   what earlier steps persisted. Manual/user pauses keep the plain
   `pause_job` (mid-run pauses legitimately keep freeze_data).
Tests: `TestShouldPersistCompletionFreeze` +
`test_recovery_pause_sheds_freeze` (tests/test_workspace_recovery_probe.py).
Not live-exercised (would need a boot-failed agent echoing a stale freeze);
the DB shape is the same stash-and-clear already live-proven by the
critic-freeze wedge fix (batch #1, `d3a16617`).

**Originally:** Observed 2026-07-26 on dev (job `52949749`), manually unwedged.
Not yet fixed. Work on `develop`.

**One line:** an agent that dies before its graph runs can echo the job's
*previous* `freeze_data` in its completion report; `complete_job` persists that
blob **before** routing into the workspace-recovery arm, whose `pause_job`
does not shed it — leaving the job `paused` with `freeze_data` set, which the
dispatcher can never see again.

## The invariant that breaks

`get_dispatchable_jobs` selects `status IN ('created','paused') AND
freeze_data IS NULL` (partial-index contract, migration 0046 —
`orchestrator/database/postgres.py`, see the docstring on
`queue_job_for_resume`). Therefore: **any transition into `paused` that keeps
a row-level freeze creates a job nothing will ever schedule again.** The
blocking-message-reply wedge
(`docs/issues/blocking_message_reply_keeps_freeze_data.md`) was the first
instance of this class; this is the second, via a new route.

## Incident sequence (2026-07-26, job `52949749`)

1. **23:28** — verdict-resume parks the job correctly:
   `queue_job_for_resume` stashes the round-2 freeze into
   `context.last_freeze_data` and clears the column. Job is dispatchable.
2. **05:31:27** — agent `srw-agent-j-f11194b9` is dispatched, loads the frozen
   config, and dies **pre-boot**: `Failed to create remote backend … [gone]:
   Name or service not known` (the workspace pod had been torn down;
   `src/agent.py:1935`). It reports a completion with
   `error.type=workspace_unavailable`.
3. That completion's payload carried the stale `job_complete` freeze blob
   (timestamp `00:19:48` — the *previous* freeze). `complete_job` persists
   `result.freeze_data` unconditionally, **before** the error-routing arm
   (`orchestrator/main.py`, the `UPDATE jobs SET freeze_data = …` block at the
   top of the handler).
4. The recovery arm (`handle_pod_workspace_recovery`) probes the workspace
   (dead), marks the container context, calls `db.pause_job` — which flips
   status and unassigns but does **not** touch `freeze_data` — and triggers
   dispatch.
5. Net row state: `status='paused'`, `freeze_data` = stale `job_complete`
   blob → invisible to `get_dispatchable_jobs`. The job sat 7.5 h until a
   human noticed "it seems to be paused for some reason".

Forensic fingerprint for future triage: `context.last_freeze_data` holds a
*properly stashed* freeze with an earlier timestamp than the poison blob in
`freeze_data`; the job's audit trail is silent after the last real freeze; no
assigned agent.

## Fix proposal (both layers, cheap)

1. **Don't persist an echoed freeze on error completions (root cause).** In
   `complete_job`, skip the `freeze_data` persist when the report carries
   `error.type == "workspace_unavailable"` (or more broadly: when
   `result.error` is present and the agent never executed graph nodes). A
   boot-failed agent has nothing new to freeze — whatever it echoes is by
   definition stale.
2. **Recovery pause sheds the freeze (invariant enforcement).** In
   `handle_pod_workspace_recovery`, replace the bare `db.pause_job` with the
   stash-and-clear shape (`queue_job_for_resume` semantics): stash any
   row-level freeze into `context.last_freeze_data`, NULL the column, then
   pause. This makes the paused-implies-dispatchable invariant locally true
   regardless of what earlier steps persisted.

Unit tests belong next to the existing recovery-arm suite
(`tests/test_workspace_recovery_probe.py`): completion-with-error does not
overwrite `freeze_data`; recovery pause leaves `freeze_data IS NULL` even when
the row had a blob.

## Manual unwedge (until fixed)

```sql
UPDATE jobs SET
  context = jsonb_set(COALESCE(context,'{}'::jsonb), '{last_freeze_data}', freeze_data),
  freeze_data = NULL
WHERE id = '<job>' AND status = 'paused' AND freeze_data IS NOT NULL;
```

## Related

- `docs/issues/blocking_message_reply_keeps_freeze_data.md` — same wedge
  class, different entry route; its fix inventory of frozen→paused transitions
  should be extended with the recovery arm.
- `docs/issues/maxsessions_parallel_tools_false_workspace_death.md` — the
  incident chain this fell out of.
