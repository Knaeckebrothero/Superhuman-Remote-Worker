Reasoning: {oss_reasoning_level}

You are updating the agent's persistent memory file (workspace.md).

The workspace.md file serves as the agent's long-term memory. Unlike the conversation history,
which gets compacted or lost over time, workspace.md persists throughout the entire task.
It is included in every system prompt, giving the agent continuous access to critical information.

## Why This Matters

Long tasks exceed the agent's context window. Without persistent memory:
- Important decisions get forgotten
- Work gets repeated
- Context is lost after compaction
- The agent cannot recover from interruptions

A well-maintained workspace.md allows the agent to stay oriented even after losing conversational context.

## What to Preserve

**Always include:**
- Current phase and progress status
- Key decisions and their reasoning
- Important entity IDs, names, or references discovered
- Blockers or issues that need attention
- Critical findings that inform future work

**Avoid including:**
- Detailed logs of every action (too verbose)
- Information already in other files (just reference the file)
- Temporary thoughts that won't matter later
- Duplicate information

## Section Guidelines

### Status
Keep this current. At minimum: phase name and progress indicator.
Example: "Phase 2: Requirement Extraction - 60% complete (pages 1-30 of 50)"

### Accomplishments
Record completed milestones, not individual steps.
Example: "Completed document structure analysis, identified 5 major sections"

### Key Decisions
Capture the decision AND the reasoning. Future-you needs to know why.
Example: "Chose to process document sequentially (not by section) because cross-references span sections"

### Entities
Track resolved IDs and relationships that will be referenced later.
Example: "Customer entity: BO-042, linked to Invoice (BO-015) and Payment (BO-023)"

### Notes
Working observations that don't fit elsewhere. Keep this lean.

## Current Task

Review the recent work and update workspace.md. Here is the current content:

{current_memory}

Return the complete updated workspace.md content.
- Preserve the markdown structure
- Be concise but complete
- Update sections based on recent progress
- Remove outdated information that's no longer relevant
