---
tags:
  - issue
  - officers
  - backlog
  - concurrency
  - database
  - authorization
status: open
priority: P0
created: 2026-08-15
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

**Status:** OPEN — central dispatch authority blocker. Audit findings **BP-02/BP-03/BP-04**.

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
