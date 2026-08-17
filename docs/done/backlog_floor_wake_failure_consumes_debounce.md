---
tags:
  - issue
  - officers
  - backlog
  - notifications
  - liveness
status: done-deployed-live-gated
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

**Status:** SHIPPED AND DEPLOYED; MAIN-DEV LIVE GATE PASSED 2026-08-17. Audit
finding **BP-10**.

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

## Implemented durable outcome model

Migration `0165_officer_correctness_state.sql` adds
`officer_floor_wake_episodes`. `PostgresDB.queue_officer_floor_wake()` follows the existing
post -> thread -> wake/outbox lock order and defines success only as a verified durable
`session_wake_events` insert. The episode records `last_attempted_at`, `last_queued_at`,
`delivered_at`, failure class/error, retry state, and `next_retry_at` independently.

The six-hour policy debounce reads only `last_queued_at`. Transient retry backoff reads
`next_retry_at`; missing notifier, false return, exception, and savepoint/outbox rollback
therefore remain retryable and do not count as a wake. A random floor-episode identity is
serialized by the active-episode unique index, and the outbox deduplication key includes
project, Officer-post incarnation, pool, and that episode. Concurrent tick replicas can
durably queue one event only.

Delivery settlement updates the same episode to `delivered`; retryable/permanent outbox
delivery failures retain their truthful outcome. Hold keeps an already-durable event
pending under the existing wake claim policy. Decommission, serialized on the post row,
removes the obsolete outbox event and marks the old episode `superseded`, so a successor
incarnation cannot adopt it. SITREP, the post API, and Cockpit distinguish attempted,
durably queued, delivered, and failed outcomes.

## Acceptance evidence

- Unit/tick tests prove no successful-wake metric increment after a failed queue attempt.
- Real PostgreSQL tests cover missing/false/raising notifiers, injected failure after the
  outbox insert with full rollback, later retry, duplicate concurrent ticks, delivery
  settlement, hold behavior, decommission cleanup, and hold/decommission races.
- Focused Officer/provisioning/dispatch/wake regression: **431 passed** in **61.28 s**.
- Real PostgreSQL Officer/routing/pagination suite: **97 passed** in **232.40 s**.
- Cockpit Officer component: **64 passed** in **730 ms**.

The local checkpoint above was subsequently deployed. The bounded main-dev gate in
[[officer_correctness_live_gate_2026-08-17]] injected a fault after the outbox insert and
proved rollback left `last_attempted_at` but no event, `last_queued_at`, or consumed policy
debounce. Retry queued successfully; two concurrent floor attempts produced one durable
event and one `policy_debounce`; exact delivery settlement updated the episode; and a
queue/decommission race left no pending/sending event, active episode, or linked post.
Only the disposable synthetic Officer was in scope, all state was removed, and
`auto_pull` remained false fleet-wide.
