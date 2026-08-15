---
tags:
  - issue
  - officers
  - communication
  - jobs
  - liveness
status: open
priority: P0
created: 2026-08-15
aliases:
  - OC-01
  - untracked user-direct freeze
related:
  - "[[officer_control_plane_post_implementation_audit]]"
  - "[[officer_message_routing]]"
  - "[[officer_backlog_pools]]"
---

# A direct blocking message can freeze a job before its recovery route exists

**Status:** OPEN — blocks unattended `auto_pull`. Audit finding **OC-01**.

## Problem

The default `user_direct` path in `orchestrator/main.py::send_agent_message` publishes a
`waiting_for_reply` freeze before it dispatches/logs the notification and before it creates
the corresponding `job_message_routes` row. Route bookkeeping is later, best-effort, and
its exception handler explicitly accepts losing total-timeout coverage.

The officer-routed blocking path already proves the right shape with
`PostgresDB.create_routed_blocking_freeze`: durable routing intent and the job transition
must commit together. The default policy is `user_direct`, so the unsafe path is the common
one.

## Impact

A crash or route INSERT failure after the freeze leaves no durable object for the timeout
reconciler to claim. The job can wait forever. With backlog pools it also holds its
one-shot ticket claim and pool capacity forever; one question can strand the executor
singleton.

## Required direction

- Generalize the transactional blocking-send helper to cover `user_direct`.
- In one transaction, persist the message/route intent and freeze the exact job generation
  with the same `route_id`.
- Perform external delivery after commit and retry it from the durable route/outbox. An
  external notification cannot be part of the database transaction, but its intent can.
- If the transaction fails, the job must remain runnable. Do not rely on a compensating
  “unfreeze” after a partial commit.
- Add an invariant/repair query for any blocking freeze whose `route_id` has no route row.

## Acceptance

- Kill the process after every database/external-delivery step. Restart always finds either
  an unfrozen job or one complete, timeout-recoverable route+freeze generation.
- Route INSERT failure cannot leave `waiting_for_reply` behind.
- Notification failure leaves `user_delivery_at` null and remains retryable.
- Existing direct replies and the total-timeout reconciler resume the same route generation.
- A claimed executor ticket releases capacity according to the normal resume/terminal
  lifecycle; it cannot become an untracked permanent claim.

## Dependencies

Coordinate the generation CAS with [[message_route_resume_lacks_generation_cas]]. The two
issues may share database helpers, but creation atomicity has its own kill-point acceptance.
