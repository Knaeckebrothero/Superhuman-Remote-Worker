---
tags:
  - issue
  - officers
  - backlog
  - provisioning
  - jobs
  - liveness
status: open
priority: P1
created: 2026-08-15
aliases:
  - BP-07
  - auto-pull preflight race
  - infrastructure failure trips breaker
related:
  - "[[officer_control_plane_post_implementation_audit]]"
  - "[[officer_backlog_pools]]"
---

# Auto-pulled jobs become dispatchable before provisioning finishes

**Status:** OPEN — execution correctness and breaker semantics. Audit finding **BP-07**.

## Problem

The tick commits a job with ordinary `status='created'`, then provisions repository/cloud
state outside the transaction and finally nudges dispatch. The global dispatcher polls
created jobs independently, so it can lease the job before strict provisioning completes.

When provisioning raises, the tick marks the job as ordinary `failed`. A later breaker pass
counts it as a job failure, although the design explicitly excludes infrastructure
failures from the pool’s two-failure circuit breaker.

## Required direction

- Introduce or reuse a non-dispatchable preflight/parked state or queue state. The job may
  own its claim/capacity while provisioning, but no dispatcher may lease it.
- After all mandatory provisioning commits, atomically activate the job as dispatchable.
- Persist a normalized failure cause/class. Infrastructure/preflight failures remain
  visible and claim-holding until disposition, but are excluded from job-quality breaker
  outcomes.
- Recovery/retry must be idempotent and distinguish “not attempted,” “in progress,”
  “retryable failed,” and “permanent failed.”

## Acceptance

- Delay provisioning beyond multiple dispatcher polls; no agent starts the job early.
- Fail repository and cloud provisioning separately; the **next tick** does not count either
  as a job-failure breaker outcome.
- Two genuine agent failures on distinct tickets still open the breaker.
- Crash before and after activation recovers to one job/claim and never double-provisions or
  double-dispatches.
- Cockpit/SITREP distinguish preflight failure from worker failure.
- Manual and tick-created strict-provisioning jobs obey the same activation contract where
  applicable.

## Dependencies

Admission remains atomic under [[officer_admission_does_not_lock_the_durable_post]]; this
issue owns the post-commit activation boundary.
