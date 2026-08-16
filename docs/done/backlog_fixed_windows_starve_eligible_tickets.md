---
tags:
  - issue
  - officers
  - backlog
  - liveness
  - database
status: resolved
priority: P0
created: 2026-08-15
resolved: 2026-08-16
aliases:
  - BP-06
  - first-ten backlog starvation
related:
  - "[[officer_control_plane_post_implementation_audit]]"
  - "[[officer_backlog_pools]]"
---

# Fixed pre-filter windows can starve eligible backlog tickets forever

**Status:** RESOLVED locally 2026-08-16. `auto_pull` remains off; this is not a live-fire
claim.

## Resulting architecture

### Cross-store backlog eligibility

KB rank and durable app-database claims cannot be joined. `BacklogCursor` and
`fetch_backlog(after=…, include_counts=False)` therefore expose a total keyset order:
`priority ASC, created_at ASC NULLS LAST, note_id ASC`. Migration
`0021_kb_backlog_keyset_index.notx.sql` adds the matching partial index without changing
checkpointed app migration 0162.

`officer_backlog._scan_eligible_tickets` reads 100 rows per transport page, obtains claim
state for exactly that page, applies the existing BP-05 eligibility law, and advances by
the last complete order key. The page size is not a decision ceiling:

- tick admission continues until it has enough eligible rows for dispatch/floor logic or
  proves source exhaustion; a sufficient early result is explicitly `lower_bound=true`;
- ready depth requests exhaustion and publishes only an exact value;
- KB or claim-database failures return `unavailable`, never exact zero;
- a non-advancing cursor is also explicit `unavailable` rather than an infinite loop.

There is no page-count or time cutoff. BP-05's project scope, `legacy_unversioned` cutover
barrier, non-terminal blocker, generation comparison, and one-shot final transaction are
unchanged.

### Database-native questions

- `list_officer_distinct_terminal_outcomes` filters terminal outcomes first, uses
  `DISTINCT ON (ticket_note_id)` to retain the newest outcome per ticket, then returns the
  newest two distinct tickets. Live rows and repeated outcomes cannot occupy a pre-window.
- `list_stale_officer_claims` applies the open-claim and movement-threshold predicates in
  SQL and returns all breaches oldest first. `get_oldest_open_officer_claim` gives the
  sitrep its exact oldest claim directly.
- Executor singleton/disposition reads now apply `work_category='executor'` and terminal
  predicates in SQL before `LIMIT 1`.
- Optional per-slot spend obtains every job id for that already-scoped slot (`limit=None`)
  rather than pricing only the newest 100.

These changes deliberately do not implement BP-12's broader Cockpit polling optimization.
The only shared seam is suppressing the unrelated counts query on scan pages.

## Acceptance and performance evidence

Unit coverage proves 11 claimed head tickets followed by a valid tail, exact empty
exhaustion versus app/KB unavailability, a 100/101 equal-key page boundary without gaps or
duplicates, and exact ready depth of 30. The existing real transaction suite continues to
prove manual/tick convergence on one claim/job.

Real PostgreSQL/pgvector tests use the production migrations and indexes:

- 10,000 target backlog rows plus 10,000 other-project rows: exhaustive 100-row keyset
  pagination returned all 10,000 ordered rows in **183.85 ms**;
  `EXPLAIN ANALYZE` used `idx_knowledge_backlog_page` and reported **0.03 ms** execution
  for the first-page plan.
- 10,000 unrelated claim-ledger rows plus mixed target history: distinct breaker, 60 stale
  claims, exact oldest-open, executor semantic-limit, and 101-row complete slot-spend
  queries completed in **22.71 ms** total;
  `EXPLAIN ANALYZE` used `idx_officer_ticket_claims_lineage_slot_claimed` and reported
  **0.08 ms** for the measured terminal candidate plan.

Local verification on 2026-08-16:

```text
Officer backlog/surfacing/sitrep/slot/project suites: 216 passed in 0.84s
complete Officer Post real-PostgreSQL suite:          67 passed in 140.62s
real-pgvector BP-06 pagination/plan suite:              1 passed in 10.99s
focused 10k app-query/plan rerun:                       1 passed in 16.27s
```
