---
tags:
  - issue
  - officers
  - backlog
  - performance
  - cockpit
  - database
status: open
priority: P2
created: 2026-08-15
aliases:
  - BP-12
  - officer card backlog query amplification
related:
  - "[[officer_control_plane_post_implementation_audit]]"
  - "[[officer_backlog_pools]]"
  - "[[backlog_fixed_windows_starve_eligible_tickets]]"
---

# Officer ready-depth polling multiplies unnecessary backlog queries

**Status:** OPEN PERFORMANCE ISSUE. Audit finding **BP-12**.

## Problem

`project_backlog.fetch_backlog()` always runs a ranked row query and a grouped count query.
The auto-pull tick and `ready_depth_by_pool()` discard those counts. The Officer Post card
polls every 15 seconds and computes depth serially per pool, multiplying vector/claim
queries by pools and open viewers.

Correctness must first be repaired by [[backlog_fixed_windows_starve_eligible_tickets]];
simply lowering limits or caching a truncated answer would make the liveness defect worse.

## Required direction

- Add a dedicated eligible-depth/batch query or `include_counts=false` path.
- Compute all configured pool depths in one bounded operation where possible, sharing claim
  lookup and observation timestamp.
- Cache only an explicitly versioned/short-lived truthful result; expose stale/partial/
  unavailable rather than false zero.
- Consider event-driven refresh or visibility-aware polling after measuring the batched path.
- Instrument query count, latency, rows scanned, cache age, and callers.

## Acceptance

- One officer-summary request has a fixed/bounded query count independent of pool count,
  rather than two-plus queries per pool.
- Tick does not execute grouped counts it never consumes.
- Multiple viewers and the maximum supported roster stay within an explicit latency/query
  budget on production-scale KB data.
- Ready-depth values use the exact eligibility semantics and observation time of the tick.
- KB/claim-source degradation is labeled and does not become cached zero.

## Dependencies

Implement after the paginated/exact semantics in
[[backlog_fixed_windows_starve_eligible_tickets]] are settled.
