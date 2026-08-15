---
tags:
  - issue
  - officers
  - backlog
  - jobs
  - data-integrity
status: open
priority: P1
created: 2026-08-15
aliases:
  - BP-05
  - claim deletion re-arms ticket
related:
  - "[[officer_control_plane_post_implementation_audit]]"
  - "[[officer_backlog_pools]]"
---

# Deleting a job silently releases its one-shot backlog claim

**Status:** OPEN — claim/data-retention decision required. Audit finding **BP-05**.

## Problem

`PostgresDB.newest_ticket_claims` reconstructs the claim ledger solely from current `jobs`
rows. The authorized job DELETE path physically removes those rows. If the ticket remains
`ready`, deleting its newest claim makes it eligible again. After a re-ready cycle, an
older surviving claim predates `ready_at`, so deletion can also re-arm without a new officer
timestamp.

This contradicts the design statement that only explicit officer re-ready re-arms a
one-shot ticket.

## Decision required

Choose one explicit semantic:

1. **Recommended: durable claim ledger/tombstone.** Claim events outlive job-row retention;
   deletion removes the operational job but not dispatch history.
2. **Explicit release operation.** Deletion does not release by default; a separate
   owner/officer action releases with audit, reason, and warning that the ready ticket may
   dispatch again.

Making every hard delete implicitly release is possible, but it must be a deliberate,
audited product decision and the UI must say so before deletion.

## Acceptance

- Delete terminal and non-terminal claimed jobs and assert the chosen claim behavior.
- Ordinary retention/cleanup cannot re-arm a ticket.
- Re-ready generations remain monotonic and select the newest relevant claim event.
- Claim history remains project-scoped and survives recommission.
- UI/API deletion explains whether the claim remains, blocks unsafe non-terminal deletion
  as appropriate, and records actor/reason.
- Migration/backfill preserves claims for extant historical jobs without dispatching old
  ready tickets during rollout.

## Dependencies

Coordinate schema/admission writes with
[[officer_admission_does_not_lock_the_durable_post]].
