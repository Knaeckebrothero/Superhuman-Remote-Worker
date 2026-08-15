---
tags:
  - issue
  - officers
  - backlog
  - notifications
  - liveness
status: open
priority: P1
created: 2026-08-15
aliases:
  - BP-10
  - false backlog floor wake
related:
  - "[[officer_control_plane_post_implementation_audit]]"
  - "[[officer_backlog_pools]]"
---

# A failed backlog-floor wake consumes the six-hour debounce

**Status:** OPEN. Audit finding **BP-10**.

## Problem

When a pool is below its ready floor, `tick_officer()` records
`backlog_floor_wakes[pool]=now` even when the notifier is absent or raises. A notifier that
returns false without raising also increments the wake metric and consumes the debounce.

A transient notification outage can therefore suppress the required officer wake for six
hours while state and metrics claim it was sent.

## Required direction

- Define success as a durable wake-outbox insert, not “the function was called.”
- Record last attempted, last durably queued, last delivered (if available), failure class,
  and next retry separately.
- Start the policy debounce only from the durable queued/success event.
- Use an idempotent dedup key per project/post incarnation/pool/floor episode.
- Keep retry backoff separate from the six-hour “do not nag a healthy officer” debounce.

## Acceptance

- Missing notifier, exception, false result, and outbox failure do not consume the policy
  debounce or increment successful-wake metrics.
- A durable outbox write consumes it exactly once across tick replicas.
- Retry succeeds after transient failure without duplicate officer messages.
- Hold/decommission routes the durable wake according to post lifecycle policy.
- SITREP/Cockpit show attempted vs queued/delivered truthfully.

## Dependencies

May reuse the normalized outcome concepts from
[[message_route_delivery_failure_is_stamped_delivered]], but has a distinct debounce and
acceptance contract.
