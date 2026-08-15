---
tags:
  - issue
  - officers
  - lifecycle
  - database
  - liveness
status: open
priority: P0
created: 2026-08-15
aliases:
  - OC-03
  - split officer decommission
related:
  - "[[officer_control_plane_post_implementation_audit]]"
  - "[[officer_post]]"
  - "[[officer_message_routing]]"
---

# Officer decommission is a chain of independent transactions

**Status:** OPEN — lifecycle/data-integrity blocker. Audit finding **OC-03**.

**Read-surface half FIXED 2026-08-15** (pre-live-fire): `get_project_officer_summary`
now derives `commissioned` from the live post join (`get_officer_thread_for_project`,
which already filters ended threads) instead of link non-nullness, so a stale
ended-thread link renders as ordinary vacancy — never `commissioned: true` over an
empty officer block. Regression pinned in
`tests/test_officer_conference.py::test_stale_ended_thread_link_reads_as_vacant`.
The commission endpoint already used the live join for its already-commissioned
guard, so a stale link does not block recommissioning. The atomicity half —
one locked transaction for harvest/wake-fold/route-drain/unlink/incarnation —
remains open and is this issue's remaining scope.

## Problem

`orchestrator/main.py::_decommission_officer_post` performs state harvest, wake-queue fold,
blocking-route drain, post unlink, and incarnation append through separate database calls.
`_stand_down` catches and swallows failure of that hygiene chain. A direct thread end and
the explicit decommission endpoint therefore share a funnel, but not one atomic state
transition.

The read surface has a related truth bug: `get_project_officer_summary` derives
`commissioned` from a non-null post link before proving that the linked thread is live. A
stale ended-thread link can return `commissioned: true` and an empty officer block.

## Impact

A crash can vacate the post while losing harvested state or incarnation history, leave
pending routes only partly drained, or delete wake intent without folding it into the
vacant ledger. The next commission then starts from a history that claims a cleaner handoff
than actually occurred.

## Required direction

- Add one Postgres lifecycle method that locks the `project_officers` row and validates the
  expected current thread/incarnation.
- In the same transaction: harvest state, fold/clear wake rows, transition pending routes
  to their durable fallback/outbox state, append exactly one incarnation, and clear the
  post link.
- Keep external notification delivery outside the transaction, driven by durable outbox
  intent written inside it.
- Make thread end/decommission report an incomplete authoritative transition; do not swallow
  it as non-fatal after ending the thread.
- Compute `commissioned` from a valid live post join, not link non-nullness.

## Acceptance

- Inject failure after every decommission substep. The transaction either rolls back with
  the same commissioned officer or commits one complete vacant-post handoff.
- Repeating decommission is idempotent: one incarnation entry, one route fallback, no lost
  or duplicated wake entries.
- Concurrent decommission/recommission cannot install the new incarnation before the old
  handoff commits.
- Direct DELETE/end and the explicit endpoint have identical authoritative behavior.
- A stale ended-thread link is repaired/read as vacant and never returns
  `commissioned: true`.

## Dependencies

Use the same stable post/project lock chosen by
[[officer_admission_does_not_lock_the_durable_post]].
