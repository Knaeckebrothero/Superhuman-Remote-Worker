# Transient infrastructure failures must not kill jobs

**Date:** 2026-07-28
**Status:** **IMPLEMENTED 2026-07-28** — all eight fixes in the working tree
with 91 new regression tests. NOT yet committed, NOT deployed; migration 0072
is unapplied and the live gate is OWED.

Two things changed during implementation, both recorded below:
* ENOSPC (`No space left on device`) was added to the transient class — job
  1A died to exactly that error, and excluding it would have left the incident's
  first casualty unfixed.
* `mark_complete` and the deliverable-read guard carried the same swallow as
  `job_complete`; all three sites were fixed (Fix 8).
**Incident / evidence record:** `docs/issues/transient_db_error_hard_fails_job_and_destroys_vm.md`
**Parked successor:** `docs/superpowers/specs/2026-07-28-job-termination-negotiated-transition-design.md`
— the lifecycle redesign that dissolves Defects 1/2/3 at the root. Every fix
here is deliberately a narrow case of one of its pillars.

## Problem

A Postgres connection blip on 2026-07-27 terminally failed three multi-day
jobs and destroyed two workspaces. Eight distinct defects turned one
infrastructure hiccup into permanent data loss; the incident doc is the
evidence record. This spec is the fix design.

## Design decisions

| # | Decision | Rationale |
|---|---|---|
| 1 | Transient infra errors become a **third error class**, not terminal | The taxonomy was binary: `WorkspaceUnavailableError` → recoverable, everything else → death |
| 2 | Classification is an **allow-list**, never a catch-all | A too-wide predicate would mask real bugs (constraint violations, undefined columns) as retryable |
| 3 | On transient failure, **keep the VM** and resume in place | Losing the workspace is what cost job `c6dd288d` its completion record |
| 4 | The `/complete` gate **re-resolves but never re-opens** | Re-dispatching an already-run job risks double execution and overwriting good deliverables |
| 5 | Agent liveness becomes **pull-backstopped**, push stays fast path | 13+ call sites can terminate a job; chasing each is fragile, one backstop catches all |
| 6 | `cancelled` is terminal against everything | Explicit human intent is not overridden by a late machine report |

---

## Fix 1 — A transient infrastructure error class

**Classification** (`src/core/workspace_backend.py`). Add
`is_transient_infra_error(exc) -> bool`, an **allow-list** of connection-state
failures:

- psycopg `OperationalError` / `InterfaceError` whose message indicates a
  closed or reset connection (`the connection is closed`, `connection already
  closed`, `server closed the connection unexpectedly`, `consuming input
  failed`)
- asyncpg `ConnectionDoesNotExistError`, `InterfaceError`,
  `AdminShutdownError`, `CannotConnectNowError`, `TooManyConnectionsError`
- socket-level `ConnectionResetError`, `BrokenPipeError`

**Explicitly excluded** — these stay terminal because they are real bugs:
`CheckViolationError`, `UniqueViolationError`, `UndefinedColumnError`,
`UndefinedTableError`, `DataError`, and any `ProgrammingError`.

Matching is by exception **type first**, message substring only for the
psycopg classes that overload `OperationalError`. Drivers are imported
defensively — the agent image and orchestrator image do not carry the same
set, so a missing driver must degrade to "not transient", never crash.

`completion_error_payload` gains a third branch, evaluated after the
workspace check:

```python
if isinstance(exc, WorkspaceUnavailableError):   type, recoverable = "workspace_unavailable", True
elif is_transient_infra_error(exc):              type, recoverable = "infra_transient", True
else:                                            type, recoverable = "job_error", False
```

**Routing** (`orchestrator/main.py`, beside the `workspace_unavailable` arm).
On `type == "infra_transient"`:

1. `pause_job(job_id)` — clears the agent, flips to `paused`.
2. Write `freeze_data = {"freeze_type": "infra_transient", "next_retry_at":
   <now + backoff>, "attempts": n+1, "last_error": <message>}`.
3. **No VM teardown. No checkpoint prune** — the prune only fires on terminal
   status, so pausing is already safe.

Backoff: 60s, 5m, 15m, 30m, 60s×… capped at 1h; ceiling **5 attempts**, after
which the job fails terminally with a message naming the infra cause. Mirrors
`docs/features/llm_outage_pause_and_backoff_redispatch.md`, whose sweeper
shape is reused: clear the freeze once `next_retry_at` passes, making the job
dispatchable (`paused` + `freeze_data IS NULL` + unassigned matches
`idx_jobs_dispatchable`).

**Resume** needs no new code. The re-dispatched agent reattaches the surviving
VM — the path is proven, `c6dd288d`'s own log shows `Reattached workspace
detected … preserving existing files (no clone, no re-init)`.

### Fix 1b — the reaper carve-out (without this, Fix 1 does nothing)

Nothing in the completion path tore down the incident's VM. The **reaper**
did: `_TERMINAL_JOB_STATUSES = {"completed", "failed", "cancelled"}` and
`failed` ∈ `_REAPABLE_JOB_STATUSES` (`vm_manager.py:61-63`). Critically, a
*paused-and-frozen* job is also idle → reapable once past
`paused_within_grace`, so **pausing alone does not keep the VM**.

`VMManager.is_reapable` / `is_idle` gain a carve-out: not reapable while all
three hold —

- `freeze_type == "infra_transient"`, **and**
- `next_retry_at` is in the future, **and**
- `attempts < ceiling`

Bounded on all three axes, so a VM cannot be held indefinitely; once the
ceiling terminally fails the job, normal reaping resumes. Follows the existing
`has_live_shared_child` / `paused_within_grace` carve-out pattern.

Scope note: an `llm_unavailable` pause today *does* lose its VM past the warm
grace. That is survivable (work is committed per-todo; resume reprovisions and
re-clones) and is deliberately left alone.

## Fix 2 — `/complete` accepts a late authoritative report

`orchestrator/main.py:14240`. Before the terminal guard, one narrow exception:
if `status == "failed"` **and** the reported `freeze_data.freeze_type ==
"job_complete"`, let it through, and clear `error_message` / `error_details`
in the same update so the UI does not show a stale failure on a
`pending_review` job.

Everything else terminal keeps the 400 — but logs the discarded report at
**WARNING** with its error type. That silence is why this hid through two
incidents.

- `cancelled` → always rejected.
- `recoverable: true` → never re-opens a terminal job.

## Fix 3 — Status-carrying heartbeat (pull backstop)

A cooperative stop already exists (`_request_stop`, reasons `cancel`/`pause`,
`src/api/app.py:64`) which the orchestrator pushes *before* setting status. It
did not fire for the out-of-band failure.

Rather than chase 13+ call sites, the heartbeat response
(`main.py:20704`) carries the assigned job's current status. The agent calls
`_request_stop("preempted")` when its current job is no longer `processing`.
Push remains the fast path; the heartbeat catches everything else within one
60s interval.

## Fix 4 — `failed_at`

Migration `0072_jobs_failed_at.sql` adds `failed_at timestamptz`;
`update_job_status` sets it on the transition into `failed`. Removes the
24-hour misdating caused by `gc_offline_agents`' FK cascade firing the
`update_jobs_updated_at` trigger.

## Fix 6 — Batched, visible checkpoint prune

`orchestrator/database/postgres.py:1203-1211`. Two changes:

1. Prune failure logs at **WARNING** with the thread id, not DEBUG. Under
   bloat this is the one signal that space reclamation has stopped working.
2. `delete_checkpoint_thread` deletes in bounded batches so row count cannot
   defeat it — the incident's prune was cancelled mid-statement.

## Fix 7 — Coerce `memory_type`

Allowed set is `factual, procedural, error_solution, vocabulary, relational`
(`vector_schema.sql:222`). Lift it into a Python constant so it cannot drift
from the SQL constraint, validate before insert, and fall back to `factual`
with a WARNING naming the rejected value.

Deliberately **not** fuzzy nearest-match: the observed value was `factial`
(an obvious typo), but nearest-match could silently mis-map a genuinely wrong
type. A default plus a loud log is safer.

## Fix 8 — `job_complete` must not swallow workspace death

`src/tools/core/job.py:253`. A bare `except Exception` stringifies everything,
including `WorkspaceUnavailableError` — which is exactly the signal the
fast-freeze path exists to catch. Re-raise it ahead of the generic handler.

## Testing

| Fix | Test | Assertion |
|---|---|---|
| 1 | `test_streaming_error_type.py` | psycopg closed-connection → `{"type": "infra_transient", "recoverable": true}`; `CheckViolationError` → still `job_error` |
| 1 | `test_completion_endpoint.py` | `infra_transient` report → `paused` + freeze written + **no** VM teardown call |
| 1 | `test_completion_endpoint.py` | attempts at ceiling → terminal fail naming the infra cause |
| 1b | `test_lifecycle_vm_manager.py` | frozen `infra_transient` job with future `next_retry_at` → `is_reapable` False; expired / over-ceiling → True |
| 2 | `test_completion_endpoint.py` | `job_complete` freeze on `failed` → 200, `pending_review`, error cleared; `workspace_unavailable` on `failed` → 400 + WARNING; `job_complete` on `cancelled` → 400 |
| 3 | agent-side test | heartbeat response with non-`processing` status → stop requested |
| 4 | `test_sweeps_real_postgres.py` | transition into `failed` sets `failed_at`; a later FK-cascade UPDATE does not change it |
| 6 | `test_checkpoint_retention.py` | N rows at batch size B → ⌈N/B⌉ statements; raising prune logs WARNING |
| 7 | auxiliary test | `type="factial"` → row inserted as `factual` + WARNING |
| 8 | `test_streaming_error_type.py` or tool test | `WorkspaceUnavailableError` propagates rather than returning a string |

## Rollout

Fixes 2, 6, 7, 8 are independent and land first — each converts a silent loss
into a recovery or a visible warning, and none depends on Fix 1. Fix 4 is a
standalone migration. Fixes 1 + 1b + 3 land together: 1 without 1b is a no-op
because the reaper undoes it.

Risk concentrates in Fix 1's classification predicate. It is an allow-list
precisely so that an unmatched error keeps today's behaviour (terminal) rather
than silently becoming retryable.
