---
tags:
  - issue
  - jobs
  - workspace
  - verification
  - officers
status: open
priority: P1
created: 2026-08-15
aliases:
  - LF-4
  - cross-job candidate unreachable
related:
  - "[[officer_backlog_pools_resavio_livefire]]"
  - "[[officer_backlog_pools]]"
---

# A verification job cannot reach the candidate commit it was sent to verify

**Status:** OPEN. Found live during the Resavio O6 run (2026-08-15) — and
diagnosed independently by the officer himself within two minutes of the
worker's report.

## Observed

Officer dispatched tester job `660a8eec` against ticket
`backlog-tester-final-runtime-acceptance-of-hotel-rheinland-ui-candidate-0140f70`,
correctly passing the refs in job context:

```
candidate_commit: 0140f70
candidate_branch: job/4268052c
git_remote_url:   .../job-660a8eec.git   ← its own fresh repo
```

Job provisioning creates a per-job Gitea repository. The candidate lives in
`job-4268052c.git` — a *different* job's repo — so the tester's workspace
contained no such commit and no such branch. All four runtime gates
(targeted pytest, scoped ruff, `run_demo.sh` syntax/executable, bounded live
HTTP smoke) failed by absence, and the job froze `pending_review` with a
truthful NOT-GREEN report.

The worker behaved correctly throughout: it did not fabricate a pass, and it
filed a reproducible absence finding naming the exact missing refs. The
officer then corrected the worker's *one* overreach — the report implied the
UI itself did not exist, when the evidence only showed it was unreachable
from this workspace — superseded that note, closed the ticket honestly, and
dispatched an executor ticket
(`backlog-executor-publish-ui-candidate-0140f70-as-durable-verifier-input`)
to publish the candidate as durable verifier-accessible evidence.

## Why it matters

Verification is a first-class work category. A tester ticket almost always
names an artifact produced by *another* job — that is what independent
verification means. Under per-job repo isolation, the default outcome of the
most common tester ticket shape is a false negative that costs a full worker
run and looks like a product failure rather than a plumbing one.

The category contract makes this worse before it makes it better: an honest
tester correctly reports NOT GREEN, so the pool's circuit breaker sees a
failure chain caused entirely by provisioning.

## Direction

The refs are already in context and already ignored. Options, cheapest first:

- **Provision from the named candidate.** When `candidate_commit` /
  `candidate_branch` (or an explicit source-job reference) are present,
  clone or add that job's repo as a remote and fetch the ref into the
  verification workspace.
- **Publish-then-verify as an explicit two-step**, which is what the officer
  improvised: an executor ticket promotes the candidate to a durable
  location, and the tester ticket names *that*. Correct, but it costs an
  extra worker run per verification and relies on the officer inventing it.
- At minimum: **fail fast and loud at dispatch** — if a job's context names a
  commit/branch its provisioned remote cannot contain, refuse the dispatch
  with the reason rather than letting a worker discover it after a full run,
  and do not count that failure toward the pool breaker.

## Acceptance

- A tester ticket naming another job's commit/branch runs its gates against
  that actual candidate.
- A candidate that genuinely cannot be resolved fails at dispatch with a
  precise message, and does not trip the category breaker.
