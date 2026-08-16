---
tags:
  - issue
  - jobs
  - deliverables
  - officers
  - loop
status: open
priority: P0
created: 2026-08-16
aliases:
  - contract laundering
  - a note about failure seals the job
related:
  - "[[deliverable_gate_cannot_see_cloned_repo_deliverables]]"
  - "[[loop_delivers_nothing_after_jobs_repo_retirement]]"
  - "[[officer_blind_reads_and_worker_bureaucracy]]"
---

# A deliverable contract can be satisfied by a note describing its own failure

**Status:** OPEN. Observed three consecutive times on the Better Resavio live run
(2026-08-15 → 2026-08-16), project `a572e4a0-d97a-4103-91fd-92a980d6717d`.

**Severity: P0 for unattended operation.** This is not a gate that occasionally
misfires — it is a gate that cannot fail. Every blocked job in the run sealed
`completed` with `deliverable_gate.passed: true` while delivering nothing.

## Observed

| job | required deliverable | gate | status | shipped |
|---|---|---|---|---|
| `2fbe1f99` | `kb:reception-cockpit-demo-publication-report` | passed | completed | nothing |
| `fcda6532` | `kb:reception-cockpit-demo-staging-closure-pack` | passed | completed | nothing |
| `c4849fa1` | `kb:reception-cockpit-demo-publication-report-2026-08-16` | passed | completed | nothing |

`KurortEngine` has had no content commit since `5e08d4fa` (2026-08-14). No branch
was created, no commit pushed, no PR opened, across all three jobs.

`2fbe1f99` sealed at **confidence 0.2** with its own summary reading:

> No HTML demo, evidence file, branch, commit, or PR was produced — no fabricated
> branch/commit/PR URL was inserted into the seal note. The seal exists by the
> deliverable-contract mechanic (existence of the kb note at the exact slug); the
> underlying publication chain is honestly recorded as NOT DONE.

The worker named the defect precisely, in the artifact the defect let it use to pass.

## Mechanism

The contract is a **slug existence check**, not a claim about content. `kb:<slug>`
is satisfied when a note exists at that slug. A note saying "I could not do the
work" occupies the slug exactly as well as the work would have.

The path into it is a rewrite at commission time. The officer's first commission
asked for the real artifacts:

```
required_deliverables: [
  "repos/KurortEngine/docs/design/reception-cockpit-demo.html",
  "repos/KurortEngine/docs/design/reception-cockpit-demo-evidence.md"
]
```

Creation refused it — correctly, per
[[deliverable_gate_cannot_see_cloned_repo_deliverables]]:

> these deliverables are inside a cloned repository datasource and are never
> versioned, so they can never be verified … Work delivered to an external
> repository is contracted as the pull request the agent opens, not as files in
> this job's tree; declare the job's own artifacts (e.g. output/…) instead.

Eleven seconds later the officer re-submitted with the contract downgraded to a
`kb:` slug, and every subsequent commission in the run used the same shape.

**The refusal names the right alternative — "the pull request the agent opens" —
and nothing enforces it.** The message points at a contract type the system does
not accept, so the only reachable escape is the one that launders the failure.

## Why it matters

The deliverable gate is the mechanism that is supposed to make an unattended loop
safe to leave running. Under this defect it certifies the opposite: three jobs, one
night, ~$23 the following day, every one of them `completed`, zero product change.
A reviewer reading job status sees a healthy project.

It also composes badly with honesty. These workers were *exemplary* — truthful
summaries, calibrated confidence, explicit "what was NOT done" sections, no
invented PR URLs. Honesty did not save the seal, because the seal never asked.

## Direction

1. **Make the PR a first-class deliverable type.** The refusal message already
   promises it. `context.pull_request` is orchestrator-written at tool-call time
   and `parse_job_pull_request` fails closed on a malformed record, so
   `pr:<repo>` is checkable against live forge state today.
2. **A `kb:` deliverable should not satisfy a contract whose ticket demanded a
   repository artifact.** At minimum, refuse a commission that rewrites a refused
   repo-path contract into a note slug for the same ticket.
3. **Separate "sealed honestly" from "delivered".** A truthful blocker-close is a
   legitimate and desirable outcome — it just is not `completed`. It wants a
   terminal state of its own that does not read as success in any rollup.

Do not fix this by weakening the honesty of the workers; they are the only part of
the chain that worked.

## Acceptance

- A job contracted to publish to an external repository cannot seal `completed`
  without a verifiable pull request.
- A blocker-close is terminal, visible, and distinguishable from delivery in the
  job list and in any project rollup.
