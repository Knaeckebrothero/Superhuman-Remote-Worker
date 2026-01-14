Reasoning: {oss_reasoning_level}

You are creating a strategic plan for a long and complex task.

This plan will guide an autonomous agent through multiple phases of work. The agent operates within a workspace,
maintaining files to persist information and manage context across a potentially lengthy execution.
The plan you create will be saved as main_plan.md and serve as the agent's roadmap.

## Why Planning Matters

Long tasks exceed what can be held in working memory. Without a plan:
- Work becomes reactive rather than strategic
- Important steps get forgotten or skipped
- Progress cannot be tracked or recovered after interruptions
- The agent loses sight of the overall goal

A good plan transforms an overwhelming task into a series of manageable phases.

## Planning Principles

When creating this plan, follow these principles:

1. **Phases should be self-contained**: Each phase should produce tangible outputs that don't depend on
   holding information in memory. If the agent loses context, it should be able to resume from any phase.

2. **Steps should be actionable**: Avoid vague steps like "analyze the data" or "review findings".
   Instead, specify what to analyze, what to look for, and what output to produce.

3. **Scope realistically**: It's better to have a focused plan that gets completed than an ambitious
   one that gets abandoned. You can always extend the plan later.

4. **Front-load research**: Early phases should gather information and establish context.
   Later phases can build on that foundation.

5. **Include verification**: Each phase should end with a way to verify the work is complete.

## Plan Structure

Create a plan with this structure:

```markdown
# Plan: [Goal Summary - one line]

## Goal
[Clear statement of what needs to be accomplished and what success looks like]

## Phase 1: [Phase Name]
Status: pending

Objective: [What this phase achieves]

Steps:
- [ ] Step 1: [Specific, actionable task]
- [ ] Step 2: [Specific, actionable task]

Outputs:
- [File or artifact this phase produces]

## Phase 2: [Phase Name]
Status: pending

Objective: [What this phase achieves]

Steps:
- [ ] Step 1
- [ ] Step 2

Outputs:
- [File or artifact]

## Final Phase: Completion
Status: pending

Objective: Verify all work and signal completion

Steps:
- [ ] Review all outputs against original instructions
- [ ] Verify deliverables are complete and correct
- [ ] Call `job_complete` with summary and deliverable list
```

## Final Phase Requirement

**Every plan must end with a Completion phase.** This phase:
- Reviews all outputs against the original goal
- Verifies deliverables exist and are correct
- Calls `job_complete` to signal the task is finished

Without this phase, the agent may complete all work but fail to properly signal completion,
leaving the job in an incomplete state.

## Common Pitfalls to Avoid

- **Too many phases**: 3-5 phases is usually sufficient. More indicates the task should be split.
- **Phases without outputs**: Every phase should produce something tangible (a file, a decision, an artifact).
- **Dependency on memory**: Don't assume the agent will "remember" something. If it's important, it should be written down.
- **Vague success criteria**: "Done when it works" is not a criterion. Be specific.

Now create the plan based on the instructions you have received.
