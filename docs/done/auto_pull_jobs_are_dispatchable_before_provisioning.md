---
tags:
  - issue
  - officers
  - backlog
  - provisioning
  - jobs
  - liveness
status: done-local-undeployed
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

**Status:** DONE LOCALLY 2026-08-17; UNDEPLOYED. Audit finding **BP-07**.

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

## Implemented architecture

Strict Officer admission now creates the claim and job in the existing authoritative
transaction, but the job is born as `status='paused'` with
`freeze_data.freeze_type='officer_preflight'`. The dispatcher queries and their final
claim CAS require `freeze_data IS NULL`, so the row holds lineage capacity and its BP-05
claim without becoming runnable. This avoids adding a public job status.

`orchestrator/services/officer_preflight.py` owns the durable state machine stored in the
server-authored `jobs.context.provisioning_preflight` object:

```text
not-attempted -> in-progress -> activated
                         \-> retryable-failed -> in-progress
                         \-> permanent-failed
```

`PostgresDB.claim_officer_job_preflight()` leases an attempt with a token. After mandatory
repository and cloud provisioning, `finish_officer_job_preflight()` performs one CAS that
sets the normalized state to `activated`, changes `paused -> created`, and clears the
freeze. Failed repository/cloud attempts leave the job paused, claimed, capacity-holding,
and machine-classified as `failure_class=infrastructure`. Breaker history excludes that
classification in SQL while continuing to count ordinary terminal worker failures on
distinct tickets.

Tick creation and manual ticketed creation both request strict provisioning through
`officer_admission.admit_and_create_job(..., strict_provisioning=True)`. Tick recovery
reclaims due leases even while `auto_pull=false`; create-or-get provisioning plus the
attempt CAS makes replay idempotent. Deterministic fault hooks immediately before and
after activation prove both transaction edges. Cockpit and SITREP surface preflight state,
phase, and infrastructure failure separately from worker failure without rendering the
job's model-authored context.

## Acceptance evidence

- Delayed provisioning was held across five dispatcher polls; the real-PostgreSQL test
  also proves `claim_job_for_agent()` refuses the parked row.
- Repository and cloud failures independently produce `retryable-failed`; the next
  breaker-history query returns neither. Existing breaker tests still prove two genuine
  worker failures on distinct tickets open the pool breaker.
- Faults after provisioning/before activation and immediately after activation recover to
  one job, one claim, one external resource identity, and one activation. Concurrent
  recovery attempts obtain one lease and run one provisioning effect.
- Focused Officer/provisioning/dispatch/wake regression: **431 passed** in **61.28 s**.
- Real PostgreSQL Officer/routing/pagination suite: **97 passed** in **232.40 s**.
- Cockpit Officer component: **64 passed** in **730 ms**.

This is a local deterministic and real-database closure only. No deployment or shared-
cluster interruption was performed, and no live gate is claimed. `auto_pull` remained
false and unexposed.
