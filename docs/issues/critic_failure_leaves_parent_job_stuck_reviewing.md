---
tags:
  - issue
  - jobs
  - critic
  - workspace-lifecycle
---

# Failed/cancelled critic leaves the parent job stuck in `reviewing` (and its workspace pod alive)

**Filed:** 2026-06-12, found during a zombie-workspace sweep on dev.

## Symptom

Jobs sit in `status='reviewing'` indefinitely, and because `reviewing`
is non-terminal, their workspace pods are never torn down. On dev:

| Job | Config | Stuck since | Critic subjob | Workspace pod |
|---|---|---|---|---|
| `6ffd0c16` | bughunter | 05-21 | (cron-tick test artifact) | already gone |
| `4486f28a` | scholar ("Research 03") | 06-04 | `8a3fc7d1` **cancelled** 06-03 | `workspace-4486f28a`, **9 days old** |
| `abc15bd5` | developer | 06-12 00:31 | `826e5bfe` **failed** 06-12 02:03 | `workspace-abc15bd5` |

## Root cause

`_handle_critic_verdict` in `orchestrator/main.py` (~line 7608) only
un-sticks the target when the critic **completed**:

- critic completed *with* verdict → approve / return-with-feedback path
- critic completed *without* verdict → implicit approval (the existing
  "doesn't get stuck in reviewing" safeguard)
- critic **failed or cancelled** → `return` — the parent stays in
  `reviewing` forever, no retry, no fallback, no notification

Nothing else ever transitions a `reviewing` job. (It *is* still
manually actionable — the approve endpoint accepts
`pending_review`/`reviewing` — but nothing surfaces that the review
pipeline died.)

## Effects

- Job appears perpetually "in review" in the cockpit.
- Workspace pod runs forever (the lifecycle honors non-terminal jobs),
  eating a pool slot + node resources — the 9-day-old pod above.

## Proposed fix

On critic terminal failure/cancellation, in `_handle_critic_verdict`
(or the completion service):

1. Flip the target job back to `pending_review` (human review takes
   over where automated review died) and emit a notification, or
   respawn the critic with a bounded retry count before falling back.
2. Optionally: idle-suspend workspaces of jobs that have been in
   `reviewing`/`pending_review` beyond a threshold — review only needs
   the Gitea branch, not a live pod.
