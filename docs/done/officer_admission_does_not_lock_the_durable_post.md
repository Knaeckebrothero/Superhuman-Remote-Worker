---
tags:
  - issue
  - officers
  - backlog
  - concurrency
  - database
  - authorization
status: done
priority: P0
created: 2026-08-15
completed: 2026-08-15
aliases:
  - BP-02
  - BP-03
  - BP-04
  - split officer admission
related:
  - "[[officer_control_plane_post_implementation_audit]]"
  - "[[officer_post]]"
  - "[[officer_backlog_pools]]"
---

# Officer admission does not lock and revalidate the durable post

**Status:** DONE 2026-08-15 — audit findings **BP-02/BP-03/BP-04** closed.

These three findings are one issue because a partial fix leaves the same race through a
different entry point.

## Three manifestations

1. `officer_admission.admit()` releases its transaction-scoped lock before the REST
   create-job handler inserts the job. Concurrent manual creates can overfill different
   tickets; same-ticket contention falls into a database error instead of a clean 409.
2. `officer_backlog_tick_once()` enumerates `list_officer_threads()`, which trusts
   non-ended thread metadata `officer.enabled=true` rather than joining the one durable
   `project_officers` post. Orphan/legacy/duplicate threads can pull work.
3. The tick snapshots hold, auto-pull, roster, and lineage before admission. Its lock is
   keyed by the old thread ID and admission does not re-read the post. Hold, disable,
   decommission, or recommission can race a stale dispatch; old/new incarnations use
   different locks and can exceed shared capacity.

## Required invariant

There is one admission authority and one stable lock domain per project post:

```text
lock post/project
→ read project_officers JOIN current live thread FOR UPDATE
→ validate current incarnation, enabled, not held, auto-pull/manual authority
→ compute lineage and all-non-terminal capacity
→ validate ticket claim/re-ready generation when supplied
→ INSERT job with post/thread/ticket/slot provenance
→ commit
```

Manual officer `create_job` and automatic tick dispatch must call that same transaction
helper. Payload/provisioning preparation can occur before it, but no capacity decision can
escape the transaction. The partial unique ticket index remains the backstop, not the
primary control flow.

## Acceptance

- Two concurrent manual creates for different tickets cannot exceed the final free slot.
- Same-ticket manual/tick races produce one job and a deterministic conflict/skip, never 500.
- An enabled thread not registered on `project_officers` cannot dispatch.
- Interleave hold, disable, decommission, and recommission immediately before INSERT; the
  stale incarnation never dispatches.
- Old and new incarnations serialize on the same post lock and count the full lineage.
- Roster/capacity changes made while a request waits on the lock are re-read before INSERT.
- Ordinary non-officer job creation does not contend on this lock or acquire officer
  capacity accidentally.

## Dependencies

Use the same post lock for [[officer_decommission_is_not_atomic]]. Close this before
exposing [[officer_post_cannot_enable_auto_pull]].

## Resolution

Officer admission now has one preparation/finalization API in
`orchestrator/services/officer_admission.py`. Preparation performs the expensive
preflight work without a long transaction. Finalization owns one PostgreSQL transaction
and one connection, then:

1. locks `project_officers` by stable project identity;
2. locks and re-reads the linked thread;
3. validates the exact live enabled/unheld incarnation (and `auto_pull`/category for the
   tick), post config, runtime roster, owner, and complete incarnation lineage;
4. counts every non-terminal job across that lineage;
5. validates the current job-row ticket claim/re-ready generation;
6. stamps authoritative slot/category/config/incarnation provenance; and
7. calls `PostgresDB.create_job(conn=...)` before commit.

Manual `POST /api/jobs` and automatic backlog dispatch both call
`admit_and_create_job()`. Same-ticket contention is normalized to a retryable conflict,
while `uq_jobs_active_ticket_claim` remains the fail-closed backstop for legacy/direct
writers. The connection-aware inner helper is intentionally ready for BP-05 to add a
durable claim INSERT later without another admission-funnel rewrite; no BP-05 ledger was
added here.

The tick now starts from the dedicated
`list_commissioned_officer_posts_for_backlog()` query (`project_officers JOIN threads`),
while watchdog/session-wake callers retain the intentionally broader
`list_officer_threads()` runtime enumeration. Hold/release, post config/roster writes,
registration/recommission and decommission all share the stable post-row lock prefix.
Ordinary job creation continues to call `PostgresDB.create_job()` directly and never
locks an Officer Post.

## Acceptance evidence

Real-PostgreSQL tests in
`tests/test_officer_post_transactions_real_postgres.py` prove:

- different-ticket manual/manual contention cannot exceed one remaining slot;
- same-ticket manual/manual and manual/tick races produce exactly one job plus a normal
  conflict;
- an enabled orphan is absent from the commissioned-post tick query and fails final
  admission;
- hold, disable, roster change, decommission and recommission invalidate a prepared
  request before INSERT;
- predecessor work consumes successor capacity through the complete lineage; and
- an ordinary job INSERT completes while the post row is locked elsewhere.

Unit coverage additionally pins that manual REST creation and the tick call the same
final service, and that the scheduler never globally repurposes runtime officer-thread
enumeration. `auto_pull=true` appears only in isolated race fixtures. Production
`auto_pull` remains disabled and unexposed.

Verification at the completed checkpoint:

```bash
python -m pytest \
  tests/test_officer_post_transactions_real_postgres.py \
  tests/test_officer_message_routing_real_postgres.py -q --tb=short
# 48 passed in 115.55s

python -m pytest \
  tests/test_officer_lifecycle.py tests/test_officer_post.py \
  tests/test_officer_backlog_tick.py tests/test_officer_slots.py \
  tests/test_officer_message_routing.py \
  tests/test_officer_message_routing_real_postgres.py \
  tests/test_backlog_ticket_plumbing.py \
  tests/test_runtime_actor_authorization.py \
  tests/test_stateless_worker_control.py \
  tests/test_officer_post_transactions_real_postgres.py \
  tests/test_officer_conference.py tests/test_session_wake_linkage.py \
  -q --tb=short
# 461 passed in 199.47s

ruff check src/ orchestrator/ tests/
# All checks passed!
ruff format --check src/ orchestrator/ tests/
# 1201 files already formatted
git diff --check
# clean
```

The current `./scripts/pytest-fast.sh` attempt reached **14,772 passed and 123 skipped**
before its fail-fast boundary stopped on the system interpreter's missing declared
`arxiv` package. The import fails directly under `/usr/bin/python`; the complete arXiv
client file passes under the project virtualenv (**22 passed in 0.12s**).

O6 subsequently released the Resavio officer successfully with `auto_pull=false` on the
earlier deployed tranche, and that live-fire remains in progress. The final transaction
checkpoint represented by these expanded results is local and not deployed. It does not
permit unattended backlog release or resolve BP-01/BP-05/BP-06/BP-07/BP-08/BP-11.
