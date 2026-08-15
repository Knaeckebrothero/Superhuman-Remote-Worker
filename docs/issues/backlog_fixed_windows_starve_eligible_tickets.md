---
tags:
  - issue
  - officers
  - backlog
  - liveness
  - database
status: open
priority: P0
created: 2026-08-15
aliases:
  - BP-06
  - first-ten backlog starvation
related:
  - "[[officer_control_plane_post_implementation_audit]]"
  - "[[officer_backlog_pools]]"
---

# Fixed pre-filter windows can starve eligible backlog tickets forever

**Status:** OPEN — machine-liveness blocker. Audit finding **BP-06**.

## Problem

Several queries apply `LIMIT` before the semantic filtering that defines their answer:

- The tick fetches 10 ranked ready/category rows, then removes already-claimed, ambiguous,
  and invalid-expert tickets in Python. Ten permanently claimed high-ranked tickets can
  hide eligible ticket 11 forever.
- Cockpit ready depth uses a capped candidate set before claim eligibility, so it can report
  false low/zero depth while valid work exists in the tail.
- Breaker history fetches 10 jobs before selecting terminal outcomes on distinct tickets;
  recent non-terminals or repeated same-ticket outcomes can hide the relevant two results.
- Stale-claim detection fetches the newest 50 open claims although its decision needs the
  oldest overdue claim.

This is permanent starvation, not a one-tick delay: the head rows are intentionally stable
until officer disposition.

## Required direction

No correctness decision may use a fixed window applied before its eligibility predicate.
Prefer SQL/CTE predicates where the stores allow it. Where claim data and KB ranking cannot
be joined, page deterministically until enough eligible rows are found or source exhaustion
is proven. Preserve stable priority/age/ID cursors so paging cannot skip or duplicate rows.

Use dedicated queries for distinct terminal-ticket breaker outcomes and oldest stale
claims; those are not backlog-list operations.

## Acceptance

- More than 10 claimed, malformed, or invalid-expert tickets ahead of a valid ticket cannot
  prevent its dispatch.
- Ready depth remains exact (or explicitly lower-bounded/partial) beyond 25 candidates.
- More than 10 mixed jobs, including repeats for one ticket, still returns the two most
  recent terminal outcomes on distinct tickets.
- More than 50 open claims still surfaces the oldest threshold breach.
- Pagination is bounded operationally without reintroducing a silent correctness ceiling;
  unavailable/exhausted/partial states are distinguishable.
- Query plans and latency are measured at the supported maximum backlog size.

## Dependencies

Close before any auto-pull live fire. The performance follow-up is
[[officer_ready_depth_poll_multiplies_backlog_queries]]; correctness comes first.
