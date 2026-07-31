---
tags:
  - issue
  - verification
  - critic
  - workspace
---

# The critic's brief is written into the workspace it *shares* with the target, and the target agent reads it as its own

**Filed:** 2026-07-31, from the live gate of the fail-closed verification
rewrite (dev job `6df02f64-b4d7-477e-877d-ba570610d54d`).
**Status:** Observed live. Root cause is a direct interaction between two
correct-in-isolation designs. UNFIXED.
**Severity:** **high** — a worker agent can conclude it is the reviewer and
spend rounds trying to render verdicts it has no tools for. Costs whole rounds
and can prevent a job from ever converging.
**Component:** `orchestrator/main.py` (critic spawn, `context["instructions"]`),
`src/agent.py` (~2209, `instructions.md` write), critic workspace inheritance.

## What happened

The verification rewrite fixed a long-standing defect where the critic's brief
was rendered and then discarded. The fix delivers it the same way the scholar
does — via `context["instructions"]`, which the agent writes to
`instructions.md` in its workspace root.

Separately, and by prior design, a verification critic **inherits the parent's
workspace** (`inherits_parent_workspace`) so it can read the deliverables it is
reviewing. Both pods share one filesystem.

So the critic's brief is written as `instructions.md` into the **same workspace
root the target agent reads from**. On its next resume the target picks it up
and believes it is the reviewer.

Observed verbatim in the target's own completion notes:

> the verdict-rendering tools `return_job_with_feedback` and `approve_job`
> documented in `instructions.md` are NOT in the exposed Critic tool namespace

That is a *worker* agent reporting that the tools its instructions tell it to
call do not exist — because those instructions were never addressed to it. The
same run recorded an explicit
"active_tasks-scaffold-vs-Critic-role conflict resolution", i.e. the agent
spent effort reconciling two contradictory identities.

The target's freeze summary for that job is written entirely in the critic's
voice ("Critic verification review of job … is complete. Rendered
verdict=returned…"), on a job that is not a critic and cannot render verdicts.

## Why it matters beyond the wasted rounds

1. **It corrupts the target's self-model**, which then corrupts its deliverable
   and its summary. The freeze the orchestrator stores no longer describes the
   work the job was asked to do.
2. **It is invisible in per-task tests.** Nothing in the suite runs two agents
   against one shared filesystem. This surfaced on the first real two-round run
   and would not have been found any other way.
3. It is a natural consequence of fixing the brief delivery. Before the fix the
   brief was discarded, so the collision could not occur — **the correct fix
   created the exposure.**

## Fix directions

1. **Namespace the file.** Write a critic's brief as
   `instructions_critic_<job_id>.md`, or under a per-subjob directory, so it
   cannot collide with the target's. Cheapest and most contained.
2. **Don't share the workspace for the brief.** The critic needs read access to
   deliverables, not a shared root for its own control files. Giving the critic
   its own seeded workspace has been proposed repeatedly for other reasons —
   see `docs/issues/unify_scholar_critic_subjob_provisioning.md` and the
   "should the critic get its own workspace seeded from the parent's snapshot?"
   open question in
   `docs/issues/verification_round_reset_spawns_blind_critic.md`.
3. **Make the target ignore instructions not addressed to it.** Weakest option —
   it relies on the model reading a header correctly, which is exactly the sort
   of guarantee this codebase has learned not to buy.

Options 1 and 2 are real fixes; 1 is the one to ship first.

## Related

- `docs/issues/verification_round_reset_spawns_blind_critic.md` — the rewrite
  whose brief-delivery fix created this exposure.
- `docs/issues/verification_fail_closed_followups.md` — live-gate results.
- `docs/issues/edit_file_append_lost_across_feedback_resume.md` — the other
  defect the same run surfaced.
