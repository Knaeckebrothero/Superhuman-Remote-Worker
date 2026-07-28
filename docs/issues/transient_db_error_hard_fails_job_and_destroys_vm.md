---
tags:
  - issue
  - fix-spec
  - agent
  - orchestrator
  - lifecycle
  - database
  - checkpointing
---

# A dropped Postgres connection terminally fails a job and destroys its VM — and every downstream signal misreports when and why

**Filed:** 2026-07-28, investigating dev-cluster job
`c6dd288d-25d0-41f0-a66e-79a8624f06ab` ("Job 1B — Modern Kurort Natural",
designer, project `68137e29`, alive ~46h, 4641 audit entries).
**Status:** CONFIRMED against live dev (`--context main`).
**All eight defects FIXED 2026-07-28** — implementation follows
`docs/superpowers/specs/2026-07-28-transient-infra-failure-handling-design.md`.
Committed to the working tree with 88 new regression tests; **not yet deployed
to dev**, and the live gate is OWED. Jobs `e1192a9d` and `c6dd288d` were
recovered by hand before the fixes landed (see Defect 2 for the procedure).
**Severity:** **high** — a sub-second infrastructure blip destroys multi-day
jobs *and* their workspaces, with no retry and no recoverable classification.
Fires during ordinary DB maintenance.
**Component:** `src/core/workspace_backend.py:24`,
`orchestrator/main.py:14240-14287`, `orchestrator/main.py:14559-14570`,
`orchestrator/database/postgres.py:1203-1211`, `:5979`.

## Summary

Three jobs in one project died to a single Postgres incident on 2026-07-27.
One died to the disk being full; **two died to the cleanup for it**. None of
them were recoverable, none were retried, and all three had their VMs torn
down. The job row then misdated its own failure by 24 hours.

The capacity incident itself — `checkpoint_blobs` bloat refilling the
Postgres PVC, first hit 2026-07-23 and recurred 2026-07-27 — is tracked in
`docs/superpowers/specs/2026-07-23-db-capacity-alerting-design.md`. This doc
is about the eight defects that incident *exposed* — every one of which is
independent of the disk and will fire again on the next connection blip.

**Defects 1, 2 and 3 share one root cause:** job termination is a unilateral
DB write, with no mechanism for the orchestrator and a live agent to disagree
about whether a job is still alive. Fixing that properly is a lifecycle
redesign, captured and deliberately parked in
`docs/superpowers/specs/2026-07-28-job-termination-negotiated-transition-design.md`.
**The fixes below are tactical and were chosen to be forward-compatible with
it** — each is a narrow case of one of that design's pillars, not throwaway
work. Read that doc before reworking any of the lifecycle paths.

## Forensic timeline (2026-07-27, UTC)

| Time | Event | Evidence |
|---|---|---|
| 05:26:43–05:26:52 | `ERROR: could not extend file "base/16384/701778.14": No space left on device` ×6 | `srw-postgres-0` log |
| 05:27:27 | `PANIC: could not create file "pg_logical/replorigin_checkpoint.tmp"` → crash + recovery | ” |
| 06:51:43 | `PANIC: could not write to file "pg_wal/xlogtemp.445438"` → crash + recovery | ” |
| ~07:00–09:20 | PVC 16→32Gi, keep-5 mass DELETE, `VACUUM FULL` (operator remediation) | capacity spec, *Reclaim* section |
| 09:28:02 / 09:28:18 | `checkpoints are occurring too frequently (20/16 seconds apart)`, distance 467 MB → 527 MB — the DELETE's WAL storm | `srw-postgres-0` log |
| **~09:27:46** | **Job 1B marked `failed`**, `error_message = the connection is closed` | `context.vm.last_heartbeat`, `vm.status = "deleted"` |
| 09:28:13 | Execution lease frozen — never renewed again | `jobs.lease_expires_at` |
| 09:28:28 | `ERROR: canceling statement due to user request` / `STATEMENT: DELETE FROM checkpoint_blobs WHERE thread_id = $1` — the terminal-status prune hook, cancelled | `srw-postgres-0` log |
| 09:28 → 09:49 | Agent `srw-agent-j-8cfeefe5` keeps streaming, unaware. 45 LLM events in the 09:00 hour | `agent_audit` |
| 09:49:32 | `VM workspace unavailable mid-stream: Failed to connect to workspace 100.64.0.106:22 after 2 attempt(s) [timeout]` | agent log (S3 archive) |
| 09:49:54 | `POST /api/jobs/{id}/complete` → `400 {"detail":"Job cannot be completed (status: failed)"}` | agent log |
| **2026-07-28 09:50:12** | `gc_offline_agents` deletes the agent row 24h later → FK cascade bumps `jobs.updated_at` | `jobs.updated_at` |

Direction of causality (job failed → VM collected, not the reverse) rests on
three points: the killing error is a **psycopg** error, which a dying VM
cannot produce; the DB was demonstrably thrashing at that instant; and
`context.vm.status` is `"deleted"`, which only the orchestrator's teardown
path writes — a VM that crashed on its own would not be marked deleted.

## Blast radius

| Job | `error_message` | Cause |
|---|---|---|
| `e1192a9d` "Job 1A — Heritage Green" | `could not extend file "base/16384/701778.14": No space left on device` + `HINT: Check free disk space.` | ENOSPC window |
| `c6dd288d` "Job 1B — Modern Kurort" | `the connection is closed` | reclaim (DELETE + `VACUUM FULL`) |
| "Loop iter 9 · DEVELOPER" | `the connection is closed` | reclaim |

All three carry `error_details = {"type": "job_error", "recoverable": false}`.
The incident record previously stated that only one job was affected and that
concurrent jobs survived the maintenance lock; both are wrong.

**1A was salvageable; 1B was not.** 1A had already written
`output/job_frozen.json` before it was failed, so the Face-A repair in
Defect 2 recovered it. 1B has **no frozen-job artifact** (`get_frozen_job` →
"No frozen job data found"), because the VM teardown at ~09:27:46 destroyed
the workspace the artifact is written to. Its model then called
`job_complete` **five times** between 09:32:51 and 09:45:18, and every call
returned `Error marking job as final: Failed to connect to workspace
100.64.0.106:22` — the job was trying to finish for 13 minutes against a
workspace that had already been collected.
**Roughly 46 hours of work is unrecoverable**, and the ordering is why: the
hard-fail deleted the VM, and deleting the VM permanently removed the only
path by which the job could have recorded its own completion.

---

## Defect 1 — the error taxonomy has no transient class, so infra blips are terminal

`completion_error_payload` is binary
(`src/core/workspace_backend.py:24-42`):

```python
is_ws = isinstance(exc, WorkspaceUnavailableError)
return {"error": {"message": str(exc),
                  "type": "workspace_unavailable" if is_ws else "job_error",
                  "recoverable": is_ws}}
```

`WorkspaceUnavailableError` is the *only* recoverable class. A psycopg
`OperationalError("the connection is closed")` — the checkpointer's
connection being reset — is not a workspace failure, so it is correctly
labelled `job_error`, and `job_error` means **terminal**. The orchestrator
writes `status='failed'` plus `error_details` in one UPDATE
(`orchestrator/main.py:14559-14570`) and the completion-side cleanup deletes
the VM.

This is the next gap in the same taxonomy as
`streaming_strips_workspace_unavailable_type.md` (FIXED). That doc closed the
case where a genuinely recoverable error was *genericized* into `job_error`.
This is the case where the error was never recoverable **to begin with**,
because no third class exists.

The project already has the right shape elsewhere: the `llm_unavailable`
freeze → backoff → re-dispatch path
(`docs/features/llm_outage_pause_and_backoff_redispatch.md`) treats a
transient upstream outage as a pause, not a death.

**Fix.** Add a transient/infra class that routes to pause-and-retry rather
than terminal:

1. Introduce `TransientInfrastructureError` (or a predicate over
   `psycopg`/`asyncpg` connection-state exceptions —
   `OperationalError`, `InterfaceError`, `ConnectionDoesNotExistError`,
   plus statement-cancellation on our own maintenance).
2. Emit `{"type": "infra_transient", "recoverable": true}` from
   `completion_error_payload`.
3. Route it in `/complete` alongside the `workspace_unavailable` arm: pause,
   **keep the VM**, re-dispatch with backoff, bounded by an attempt ceiling
   that terminally fails with a message naming the infra cause.
4. Keep the VM. Losing the workspace turns a resumable pause into a
   from-scratch re-run of a multi-day job.

## Defect 2 — the terminal-status guard sits upstream of the recovery arm

In `orchestrator/main.py`:

```python
# :14240 — guard
if job["status"] not in ("processing", "reviewing", "pending_review", "completed"):
    raise HTTPException(400, f"Job cannot be completed (status: {job['status']})")
...
# :14287 — recovery routing, unreachable once terminal
if isinstance(error, dict) and error.get("type") == "workspace_unavailable":
```

At 09:49:54 the agent filed a **correct, recoverable** `workspace_unavailable`
report. It never reached the recovery arm — the guard rejected it 47 lines
earlier. Once a job is terminal for *any* reason, every subsequent report is
discarded with a 400 before it is even inspected.

**This gate has two faces, and both were hit by this one incident:**

- **Face A — a finished job stays failed forever.** Job 1A (`e1192a9d`)
  reported a `freeze_type=job_complete` freeze *after* being failed
  out-of-band. The report 400'd, so it never reached `determine_job_status`
  (`orchestrator/services/completion.py:834`) and none of the Slice A/B/C
  carve-outs from
  `docs/done/coincident_infra_error_overrides_reported_job_outcome.md`
  applied — that doc's hardening is downstream of a gate that closes first.
  A fully successful job stays `failed` silently.
- **Face B — a recoverable job cannot be recovered.** Job 1B, above.

**Detecting Face A:** the freeze artifact is the truth, not the DB.
`jobs.freeze_data` may be NULL while `output/job_frozen.json` in the Gitea
jobs repo holds a `job_complete` freeze (`main.py:14919`, `:10974`). Check
`get_frozen_job`; `llm_unavailable` / `workspace_unavailable` freezes are
genuine failures and must be left alone.

**Repair for Face A** (verified 2026-07-28 on `e1192a9d`):

```sql
UPDATE jobs SET status='pending_review', error_message=NULL, error_details=NULL,
       updated_at=CURRENT_TIMESTAMP
 WHERE id='<job>' AND status='failed';
```

`pending_review` is not in the dispatchable set (`idx_jobs_dispatchable`
covers `created`/`paused`), so this does not re-run the job, and the approve
flow reads `output/job_frozen.json`. **Never "resume" such a job** — the VM
is long reaped and `context.vm` is stale, so resume hands an empty workspace
to an agent that already finished, risking overwrite of good deliverables.

**Fix.** Evaluate the report before the terminal guard: accept a
`job_complete` freeze on an already-failed job as an idempotent re-resolve
(Face A), and let a `recoverable: true` report re-open a job failed by a
non-deterministic cause within a bounded window (Face B). Alternatively stop
out-of-band error handlers writing terminal status onto a job that has a
freeze artifact. At minimum, log the discarded report at WARNING — today it
vanishes into an HTTP 400 that only the agent sees.

## Defect 3 — an agent keeps working a job that has already been terminated

`srw-agent-j-8cfeefe5` streamed for ~21 minutes after the orchestrator failed
its job (45 LLM events in the 09:00 hour). Nothing tells a mid-run agent that
its job's status changed. It discovered the truth only when the *side effect*
— its VM being collected — broke its SSH connection at 09:49:32, and
reasonably misread that as "my workspace died" rather than "my job was killed
and my workspace was collected as a consequence."

The lease already exists as the liveness channel
(`docs/features/job_execution_lease.md`) but is one-directional: the agent
renews it, and nothing surfaces a renewal *rejection* back into the run loop.

**Fix.** Make the existing heartbeat authoritative in both directions — have
the orchestrator's heartbeat response carry the job's current status, and
have the agent stop the stream when its job is no longer `processing`. No new
call, no new state: the heartbeat already runs on the right cadence.

## Defect 4 — `jobs.updated_at` misdates failures by up to 24 hours

Job 1B reads `updated_at = 2026-07-28 09:50:12` but failed on **07-27 at
~09:28**. The chain:

- `gc_offline_agents(retention_hours=24)`
  (`orchestrator/database/postgres.py:5979`, wired at `main.py:836`) deletes
  agent rows 24h after last heartbeat;
- `jobs.assigned_agent_id` is `ON DELETE SET NULL`, so the delete issues an
  UPDATE against every job the agent held;
- the `update_jobs_updated_at` BEFORE UPDATE trigger stamps
  `updated_at = CURRENT_TIMESTAMP`.

Result: every job held by a dead agent gets its `updated_at` rewritten to
exactly 24h after that agent's last heartbeat. Any incident review anchored
on `updated_at` lands a full day late and correlates against the wrong
window. This cost real time in this investigation.

**Fix.** Record terminal transitions explicitly rather than inferring them
from `updated_at`. `failed_at` alongside the existing `completed_at` is the
smallest change (`update_job_status` already special-cases terminal statuses
at `postgres.py:1203`). Until then, date failures from
`context.vm.last_heartbeat`, `lease_expires_at`, and the `agent_audit` tail
— **never** from `updated_at`.

## Defect 5 — checkpoint maintenance has no drain, so it kills live jobs

The keep-5 mass DELETE and `VACUUM FULL` ran against `checkpoint_blobs`,
`checkpoints`, and `checkpoint_writes` while jobs were mid-run. `VACUUM FULL`
takes ACCESS EXCLUSIVE; the DELETE generated a 467→543 MB WAL storm that
pushed Postgres into back-to-back checkpoints. Live checkpointer connections
did not survive it, and by Defect 1 that is fatal rather than transient.

**The periodic case is now handled.** `9bb24cea` is pushed and the sweeper is
live in dev — verified 2026-07-28: `Checkpoint retention sweeper started
(interval=600s, keep=3)` and `checkpoint retention: pruned 133 rows
(keep_last=3)` every 10 min on the leader replica, with `checkpoint_blobs`
stable at 1450 rows / 295 MB. Recurrence risk from steady-state growth is
largely closed.

**Fix (what remains).** The *emergency/manual* path still has no drain. Any
operator action touching the checkpoint tables — mass DELETE, `VACUUM FULL`,
PVC surgery — must first pause or drain in-flight jobs, or run behind
Defect 1's retry so a dropped connection is survivable. This is a runbook
change, not code, and it is what actually killed 1B and Loop iter 9.

## Defect 6 — the terminal prune is unbounded and self-defeating under bloat

`update_job_status` fires `delete_checkpoint_thread` on every terminal
transition (`postgres.py:1203-1211`), which runs
`DELETE FROM checkpoint_blobs WHERE thread_id = $1`. At 09:28:28 that
statement was **cancelled** — on a bloated table it is exactly the query that
cannot finish, and the failure is swallowed by design:

```python
try:
    await self.delete_checkpoint_thread(job_id)
except Exception as e:
    logger.debug("checkpoint prune skipped for %s: %s", job_id, e)
```

So under bloat the prune silently stops working, leaving the blobs that
caused the bloat. The one mechanism that reclaims space fails precisely when
space is scarce, and says so only at DEBUG.

**Fix.** Log prune failure at WARNING with the thread id, and batch the
delete (`DELETE ... WHERE ctid IN (SELECT ctid ... LIMIT n)` in a loop) so it
cannot be defeated by row count. Correctness does not depend on it completing
in one statement.

## Defect 7 (minor) — memory extraction drops rows on an invalid `memory_type`

```
2026-07-27T08:13:53Z WARNING src.services.auxiliary | Memory extraction:
failed to store memory: CheckViolationError: new row for relation "memories"
violates check constraint "valid_memory_type"
DETAIL: Failing row contains (..., factial, observer, ...)
```

The model emitted `factial` for `factual` and the whole extraction was
discarded. A free-text LLM field is being fed straight into a CHECK
constraint.

**Fix.** Validate/coerce `memory_type` against the allowed set before the
insert — nearest-match or fall back to a default type — and log at WARNING
when coercion happens, rather than losing the memory.

## Defect 8 — `job_complete` swallows workspace death into a soft tool error

The model called `job_complete` **five times** — 09:32:51, 09:35:59, 09:38:54,
09:41:56, 09:45:18 — and every call was recorded `Tool [ok] … success: true`
with the result string `Error marking job as final: Failed to connect to
workspace 100.64.0.106:22 after 2 attempt(s) [timeout]: timed out`.

The workspace was already gone, and `job_complete` knew it — but it caught
the `WorkspaceUnavailableError` and returned it as an ordinary tool-result
string. The agent therefore did not classify the workspace as dead until
09:49:32, when an unrelated `file_exists` call let the exception propagate:
**~16 minutes and five finalization attempts burned on a signal the finalizer
already held on the first try.**

Given Defect 2, the ordering also matters: had the first `job_complete`
propagated at 09:32, the report would still have 400'd, but the agent would
have stopped ~16 minutes earlier — and five identical failures in a row is
also the clearest available signal that a *retry* of the report was never
going to succeed.

**Fix.** `job_complete` must let `WorkspaceUnavailableError` propagate rather
than stringify it — it is exactly the error the fast-freeze path exists to
catch (`docs/issues/agent_fast_freeze_on_dead_workspace.md`). Broad
`except Exception → return f"Error …"` in a finalization tool converts a
lifecycle signal into model-visible prose.

---

## Open question

The `/complete` report that carried `the connection is closed` cannot be
attributed to a specific process. Agent `srw-agent-j-8cfeefe5` was mid-stream
at 09:28 and its S3 log archive contains no matching error line — both
app-layer handlers that build this payload log
`logger.error(f"... job {job_id} failed: {e}", exc_info=True)` first
(`src/api/app.py:591`, `:1143`), and neither line is present. `error_details`
is only ever written from an agent's `/complete` payload
(`orchestrator/main.py:14568` is the sole writer), so an agent process did
post it.

Most likely a second execution context for the same job — the job was
*resumed* onto `8cfeefe5` at 07-27 00:27, so an earlier pod ran it from 07-26
11:24 and may have survived as a zombie (cf.
`docs/issues/worker_pod_state_zombie_on_cancel.md`). Orchestrator logs for
that window have
rotated and the agent row has been GC'd, so this is not recoverable from
surviving evidence. **Fixing Defect 3 would make it moot and observable.**

## Suggested order

1. **Defect 1** — the actual bug. Everything else is amplification.
2. **Defect 2** — highest salvage value: it is the difference between 1A
   (recovered) and 1B (46h lost), and it has already bitten twice.
3. **Defect 3** — cheap (extend the heartbeat response), and it closes the
   open question above by making duplicate/zombie execution visible.
4. **Defect 6**, **Defect 8** — small, independent, each removes a silent
   failure.
5. **Defect 4** — `failed_at` column; pure diagnostics, but it will save the
   next investigation a day of misdirection.
6. **Defect 5** — operational runbook change plus pushing the retention
   sweeper.
7. **Defect 7** — unrelated to this incident, found in passing.

## Repro

Defect 1 has a clean unit-level repro: raise
`psycopg.OperationalError("the connection is closed")` from inside the graph
stream and assert the reported payload is recoverable. Today it asserts
`{"type": "job_error", "recoverable": false}`. Extend
`tests/test_streaming_error_type.py`, which already covers the
`workspace_unavailable` / `job_error` split.

Defect 4 is directly assertable: delete an `agents` row and assert that the
jobs it held did not have `updated_at` rewritten (or that `failed_at`
survives it).

## Related

- `docs/superpowers/specs/2026-07-28-job-termination-negotiated-transition-design.md`
  — **PARKED** lifecycle redesign that dissolves Defects 1/2/3 at the root
  (execution epoch, bidirectional liveness, idempotent resolution, deferred
  destructive side effects). The tactical fixes here are narrow cases of its
  pillars.
- `docs/superpowers/specs/2026-07-23-db-capacity-alerting-design.md` — the
  underlying capacity incident, its remediation, and the alerting decision
  (infra layer: Prometheus/Longhorn, app-level monitor dropped).
- `docs/issues/streaming_strips_workspace_unavailable_type.md` — FIXED; the
  previous gap in the same error taxonomy.
- `docs/done/coincident_infra_error_overrides_reported_job_outcome.md` —
  Slice C hardened `determine_job_status` against exactly this class of
  override, but sits downstream of the `/complete` gate in Defect 2.
- `docs/issues/agent_fast_freeze_on_dead_workspace.md`,
  `docs/issues/loop_job_workspace_lost_wedged_in_recovery.md` — the
  workspace-recovery arm this class of failure bypasses.
- `docs/features/llm_outage_pause_and_backoff_redispatch.md` — the
  transient-failure handling model Defect 1 should copy.
- `docs/features/job_execution_lease.md` — the liveness channel Defect 3
  should make bidirectional.
- `docs/superpowers/specs/2026-07-23-db-capacity-alerting-design.md` —
  capacity alerting decision (infra layer, Prometheus/Longhorn).
