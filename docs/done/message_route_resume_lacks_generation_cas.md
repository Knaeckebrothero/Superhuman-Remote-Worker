---
tags:
  - resolved
  - issue
  - officers
  - communication
  - concurrency
  - liveness
status: resolved
priority: P0
created: 2026-08-15
aliases:
  - OC-04
  - blocking-message ABA resume
related:
  - "[[officer_control_plane_post_implementation_audit]]"
  - "[[officer_message_routing]]"
  - "[[direct_blocking_message_freeze_can_outlive_route]]"
---

# Message reply and timeout resume only the status, not the route generation

**Status:** RESOLVED 2026-08-15. Audit finding **OC-04**.

`freeze_data.route_id` is the generation token: the resume CAS takes
`expected_route_id` and requires the job to still be frozen on that route.
Reply, officer reply and the timeout reconciler all pass it; an unrouted freeze
passes None and keeps the status-only CAS. Acceptance is tested against real
Postgres — a stale generation is refused while the current one still wins, and
two actors racing one generation produce exactly one winner.

## Problem

`_route_inbound_reply` and the timeout reconciler inspect a route/freeze in Python, then
resume with a database CAS on `jobs.status='waiting_for_reply'`. The write in
`_queue_job_for_resume_on_conn` does not require `freeze_data.route_id` to equal the route
being resolved. Route transition and job resume are also separate durable writes.

This permits both an ordinary reply-vs-timeout split result and an ABA race: job waits on
route A, briefly resumes, then waits on route B; a delayed actor for A still sees the same
status and can resume B.

## Required direction

- Treat `route_id` as the freeze generation token.
- In one transaction, lock/claim the route and update the job only where status and
  `freeze_data.route_id` match the expected generation.
- Make reply, officer reply, user escalation reply, and total timeout use the same helper.
- Only the transaction winner resolves the route and queues the resume. Losers return an
  explicit stale/already-resolved result without changing the job.
- Correct comments/docstrings that currently claim a generation CAS not present in SQL.

## Acceptance

- Race reply and timeout for route A: exactly one wins; route and job state agree.
- Refreeze the job onto route B before a delayed A reply/timeout. A cannot resume or mutate B.
- Duplicate replies/timeouts are idempotent and do not enqueue multiple resumes.
- A crash after route claim cannot leave a resolved route with an unresumed matching job,
  or vice versa.
- Tests use real transactional interleavings in addition to mocked service calls.

## Dependencies

Coordinate route creation with [[direct_blocking_message_freeze_can_outlive_route]]. Both
should use the same route-generation invariant.
