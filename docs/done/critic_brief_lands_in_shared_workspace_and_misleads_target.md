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

**Status:** **FIXED.** Closed in two steps, and the second one is what makes it
stay closed.

1. **2026-08-01 — precondition eliminated, coincidentally.** The
   virtual-directories migration (Slice 1, live-gated) made `instructions.md`
   and `task_brief.md` in-process virtual files, served per agent and never
   written to the workspace. Neither proposed fix direction was taken; the
   collision simply became unreachable on the default path. Recorded by the
   2026-08-06 doc-truth sweep (batch #3).
2. **2026-08-07 — the reopen path removed.** That same sweep noted the catch:
   `VIRTUAL_DIRS_ENABLED=false` materialized both files back into the workspace
   root and **would reopen this exact bug**. A kill switch whose "off" position
   reintroduces a high-severity defect is not a rollback. The flag,
   `materialize_single_file_providers`, and the disabled-path branch in
   `_deploy_instruction_files` are all deleted — there is now no route back to
   the write.

**Severity (as filed):** **high** — a worker agent can conclude it is the
reviewer and spend rounds trying to render verdicts it has no tools for. Costs
whole rounds and can prevent a job from ever converging.

**Component:** `orchestrator/main.py` (critic spawn, `context["instructions"]`),
`src/agent.py` (`instructions.md` write — deleted), critic workspace
inheritance.

> **The sharing itself is unchanged.** `inherits_parent_workspace` is still live
> (`orchestrator/main.py:4649`, `:4896`, `:13805`, `:13808`). Of the three known
> consequences of that one shared root, two are now closed — the *branch* half
> by `ensure_job_branch`
> (`docs/done/resumed_job_inherits_subjob_git_branch.md`) and the
> *instructions* half here. The third is still open: the critic writes its
> verdict artifacts (`output/critic_verdict.json`,
> `output/verification_report*.json`) into the **target's** `output/`, which is
> what keeps the verification no-progress guard inert. See
> `docs/issues/verification_fail_closed_followups.md`.

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

## Fix directions (as proposed — none was taken)

Kept for the reasoning, not as a plan. What actually closed this was a
migration done for unrelated reasons: making the file virtual removes the
shared-root write entirely, which is a stronger version of option 1 (there is
no file to collide, rather than a file with a distinct name). Option 2 remains
worth doing on its own merits — the critic still writes its verdict artifacts
into the target's `output/`.

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

**What shipped instead:** `instructions.md` / `task_brief.md` became in-process
virtual files (`src/core/virtual_dirs/`), registered per agent on its own
`WorkspaceManager` and served from that agent's own job metadata. Two agents
sharing one filesystem now serve two different briefs from two different
processes, and neither is on disk for the other to find. Then the
`VIRTUAL_DIRS_ENABLED` escape hatch was removed so the write cannot come back,
with the guarantee it existed for — never boot an agent that was never told its
task — moved into `src/graph.py`, which now raises instead of warning when both
briefs resolve empty. That covers every way the overlay can fail, where the flag
covered one.

## Related

- `docs/issues/verification_round_reset_spawns_blind_critic.md` — the rewrite
  whose brief-delivery fix created this exposure.
- `docs/issues/verification_fail_closed_followups.md` — live-gate results.
- `docs/done/resumed_job_inherits_subjob_git_branch.md` — the other
  defect the same run surfaced.
