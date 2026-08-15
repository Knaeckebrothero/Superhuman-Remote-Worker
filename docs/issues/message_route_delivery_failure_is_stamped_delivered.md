---
tags:
  - issue
  - officers
  - communication
  - notifications
  - liveness
status: open
priority: P3
created: 2026-08-15
aliases:
  - OC-06
  - false route delivery stamp
related:
  - "[[officer_control_plane_post_implementation_audit]]"
  - "[[officer_message_routing]]"
---

# Route delivery has no persisted attempt count or last error

**Status:** OPEN — RESCOPED 2026-08-15. Residue of audit finding **OC-06**.

**No longer a deployment blocker.** The false stamp is fixed: `classify_dispatch`
normalizes the notifier result into one `DeliveryOutcome`, only an accepted
outcome stamps `user_delivery_at`, and the message-log status derives from the
same value so the two cannot disagree. The reconciler retries exactly while the
stamp is null, so liveness holds.

What is left is observability, not correctness: persisting attempt count, last
error/class and next-retry on the route so a stuck delivery is diagnosable
without reading logs. That needs a schema change, which is why it was not done
under deployment pressure.

## Problem

`orchestrator/services/message_routing.py::deliver_route_to_user` records a failed
notifier result in `message_log`, then calls `mark_route_user_delivery` unconditionally.
`NotificationService` can return an error object rather than raise—for example when it is
not initialized. The reconciler retries only routes with `user_delivery_at IS NULL`.

The durable ledger therefore says “delivered” when the delivery provider said “failed,”
and the retry loop stops.

## Required direction

- Define a typed notification outcome: accepted/queued, delivered if known, retryable
  failure, permanent failure, and unavailable.
- Stamp `user_delivery_at` only for the contractually successful outcome. If the field means
  “provider accepted,” rename/document it accordingly rather than overstating delivery.
- Persist attempt count, last error/class, and next retry independently of success.
- Make message-log status and route state derive from the same normalized outcome.
- Keep retries idempotent with a delivery idempotency key.

## Acceptance

- `dispatch.error`, unavailable notifier, exception, and false/empty provider response leave
  the route retryable and do not set the success stamp.
- A queued/accepted result sets exactly the documented stamp once.
- Reconciler retry eventually succeeds without creating duplicate user-visible messages.
- Permanent failure is explicit and pages/escalates according to policy; it is not called
  delivered.
- Cockpit/audit/message log all render the same outcome.

## Dependencies

The durable route/outbox introduced by
[[direct_blocking_message_freeze_can_outlive_route]] should consume this outcome contract.
