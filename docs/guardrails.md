# Agent Guardrails System

This document describes the guardrails system designed to keep agents focused and on-track during long-running tasks. The system addresses context window limitations and agent drift by enforcing structured checkpoints and phase-based execution.

## Problem Statement

Current agent behavior exhibits several issues:
- Creates a plan but never revisits it
- Creates insufficient todos for complex tasks (e.g., 20 todos for a 50-page document)
- "Wanders around" and forgets to update the plan
- Context fills up with old conversation, pushing out important state
- No forced checkpoints to re-orient after long execution

## Solution Overview

The guardrails system introduces:
1. **Protected context structure** - Critical information survives summarization
2. **Two-tier planning** - Strategy (plan.md) vs Tactics (todo list)
3. **Phase-based execution** - Work divided into 5-20 step phases
4. **Automatic phase transitions** - Workflow triggers when phase completes
5. **Panic button** - Agent can rewind when stuck

---

## Context Structure

The agent's context window is structured in 5 layers:

```
┌─────────────────────────────┐
│ 1. System Prompt            │ ─┐
├─────────────────────────────┤  │ PROTECTED
│ 2. Active Todo List         │ ─┤ (never summarized)
├─────────────────────────────┤  │
│ 3. Tool Descriptions        │ ─┘
├─────────────────────────────┤
│ 4. Previous Summary         │ ─┐ SUMMARIZABLE
├─────────────────────────────┤  │ (compacted when needed)
│ 5. Current Conversation     │ ─┘
└─────────────────────────────┘
```

### Layer Details

1. **System Prompt** - Instructions, principles, how to work. Always present. Must emphasize focusing on the todo list.
2. **Active Todo List** - Current phase's 5-20 tasks. Always visible so agent knows what to do next.
3. **Tool Descriptions** - Short descriptions with references to full docs in `tools/` directory.
4. **Previous Summary** - Compressed history of completed phases and decisions.
5. **Current Conversation** - Recent messages since last summary.

### System Prompt Requirements

The system prompt must clearly instruct the agent to:
- **Focus on the todo list** - Complete one task at a time, in order
- **Call `todo_complete()` after each task** - This is the primary rhythm of work
- **Not worry about phase management** - The system handles transitions automatically
- **Trust the process** - When todos appear, work through them; when context resets, continue from the new todos

Example system prompt guidance:
```
Your work is organized into phases. Each phase has a todo list.

YOUR ONLY JOB: Complete the tasks in your todo list, one at a time.

After completing each task, call todo_complete(). The system will:
- Mark the task done
- Show you the next task
- Handle everything else automatically

Do not try to manage phases yourself. Just focus on the current task.
When you see a fresh todo list, start working through it.
```

### Summarization Rules

When summarization triggers:
- Layers 1-3 remain untouched
- Layers 4-5 are combined into a new summary (layer 4)
- Layer 5 is cleared
- New todos are NOT included in the summary (they're in layer 2)

---

## Two-Tier Planning

### Strategic Layer: `plan.md`

The execution plan defines the "grand picture":
- Overall approach to the task
- Phases with clear objectives
- Progress tracking (checkboxes)
- Adjustments based on findings

```markdown
# Execution Plan

## Overview
[Brief description of the task and approach]

## Phase 1: Document Analysis ✓ COMPLETE
- [x] Extract document structure
- [x] Identify key sections
- [x] Categorize content types

## Phase 2: Requirement Extraction ← CURRENT
- [ ] Process section 1-3
- [ ] Process section 4-6
- [ ] Consolidate findings

## Phase 3: Validation & Integration
- [ ] Validate against schema
- [ ] Check for duplicates
- [ ] Integrate into graph

## Phase 4: Final Review
- [ ] Generate summary report
- [ ] Call job_complete()
```

### Tactical Layer: Todo List

The todo list is the agent's "working memory":
- Contains only current phase tasks (5-20 items)
- Agent focuses on completing tasks one by one
- Calls `todo_complete()` after each task
- Automatically triggers phase transition on last task

---

## Phase-Based Execution

### Phase Size Guidelines

| Phase Size | Guidance |
|------------|----------|
| < 5 steps | Too small - combine with adjacent phase |
| 5-20 steps | Ideal range |
| > 20 steps | Too large - break into multiple phases |

The goal is that each phase fits comfortably in the todo list format. Occasional variance (e.g., 4 or 22 steps) is acceptable, but phases should generally stay within the 5-20 range.

### Phase Properties

Each phase should:
- Have a clear objective
- Be independently executable
- Produce measurable output
- Be completable within context limits

---

## Bootstrap Process

Every job starts with the same initialization sequence. The todo list is pre-populated with:

```
[ ] Generate workspace summary using generate_workspace_summary()
[ ] Read workspace_summary.md
[ ] Read instructions.md
[ ] Create comprehensive execution plan in plan.md
[ ] Divide plan into executable phases (5-20 steps each)
```

Once the agent completes these bootstrap tasks and marks the last one complete, the phase transition workflow triggers for the first time, creating the todo list for Phase 1.

---

## Phase Transition Workflow

When `todo_complete()` is called on the last task of a phase:

```
┌──────────────────────────────┐
│  Last todo marked complete   │
└──────────────┬───────────────┘
               ▼
┌──────────────────────────────┐
│  Archive completed todo list │
│  (move to archive/)          │
└──────────────┬───────────────┘
               ▼
┌──────────────────────────────┐
│  Generate workspace_summary  │
│  (current state of files)    │
└──────────────┬───────────────┘
               ▼
┌──────────────────────────────┐
│  LLM reads:                  │
│  - plan.md                   │
│  - instructions.md           │
│  - workspace_summary.md      │
└──────────────┬───────────────┘
               ▼
┌──────────────────────────────┐
│  LLM updates plan.md:        │
│  - Mark phase as COMPLETE    │
│  - Adjust if needed          │
│  - Mark next phase CURRENT   │
└──────────────┬───────────────┘
               ▼
┌──────────────────────────────┐
│  LLM creates new todo list   │
│  from next phase's steps     │
└──────────────┬───────────────┘
               ▼
┌──────────────────────────────┐
│  Trigger context summary     │
│  (clean slate for new phase) │
└──────────────┬───────────────┘
               ▼
┌──────────────────────────────┐
│  Agent continues with fresh  │
│  context and new todos       │
└──────────────────────────────┘
```

### Who Does What

| Step | Actor | Description |
|------|-------|-------------|
| Archive todos | System | Automatic file operation |
| Generate summary | System | Tool generates workspace_summary.md |
| Read files | LLM | Prompted to read plan, instructions, summary |
| Update plan | LLM | Marks completed phase, identifies next phase |
| Create todos | LLM | Extracts steps from next phase, creates todo list |
| Summarize context | System | Compacts conversation, preserves layers 1-3 |

The LLM is guided through steps 3-5 via a special "phase transition prompt" injected by the system. This prompt tells the LLM exactly what to do without the agent needing to understand the bigger picture.

### Summarization Timing

**Critical:** Summarization happens AFTER the new todo list is created.

Sequence:
1. LLM creates new todos for next phase
2. New todos are stored in layer 2 (Active Todo List)
3. THEN context summarization triggers
4. Layers 4-5 are compacted (old conversation goes away)
5. Agent sees: fresh context + new todos already present

This means:
- The agent doesn't see a "gap" or empty state
- New todos are NOT part of the summary (they're protected in layer 2)
- The agent wakes up with clear todos and a clean conversation history

### Why This Works

1. **Forced re-orientation** - Agent must re-read plan and instructions
2. **Context hygiene** - Old conversation is summarized away
3. **Progress tracking** - Plan is updated with completion status
4. **Fresh start** - Each phase begins with clean context
5. **No lost work** - New todos exist before old context is cleared

### Edge Case: All Phases Complete

When the LLM reads plan.md during phase transition and finds NO remaining phases:

```
┌──────────────────────────────┐
│  LLM reads plan.md           │
│  All phases marked COMPLETE  │
└──────────────┬───────────────┘
               ▼
┌──────────────────────────────┐
│  System detects: no next     │
│  phase available             │
└──────────────┬───────────────┘
               ▼
┌──────────────────────────────┐
│  Inject job completion todos │
│  instead of phase todos      │
└──────────────┬───────────────┘
               ▼
┌──────────────────────────────┐
│  Final todos:                │
│  - Verify all deliverables   │
│  - Generate summary report   │
│  - Call job_complete()       │
└──────────────────────────────┘
```

The "final review" todos can be:
1. Defined in instructions.md as a standard finale
2. Or generated dynamically based on what was accomplished

Either way, the last todo should always be `Call job_complete()`.

---

## Todo Tool Design

### Design Philosophy

The agent should have a **simple mental model**: "complete tasks, call todo_complete(), repeat."

The agent does NOT need to know:
- That completing the last task triggers a workflow
- How phases transition
- When summarization happens

The agent only needs to know:
- There's a todo list with tasks
- Call `todo_complete()` when done with a task
- Keep working until no tasks remain

This simplicity is intentional. The complexity is handled by the system, not the agent.

### Available Functions

#### `todo_complete()`

Called by the agent after finishing a task. Takes no arguments.

```python
todo_complete()
# Returns: "Task 3 'Process section 1-3' marked complete. 2 tasks remaining."
```

The system automatically:
1. Finds the first incomplete task
2. Marks it complete
3. Returns status to the agent

**On last task completion:** The system silently triggers the phase transition workflow. The agent doesn't see this - they just see a fresh todo list appear after the context is summarized.

#### `todo_rewind(issue: str)`

Panic button for when the agent realizes the current approach isn't working.

```python
todo_rewind(issue="Step 7 is impossible because the API doesn't support batch operations. Could try sequential processing instead.")
```

Triggers a modified workflow:
1. Archive current todo list with failure note
2. Generate workspace summary
3. Read plan.md, instructions.md, workspace_summary.md
4. **Reconsider phases** based on the issue
5. Update plan.md with adjusted approach
6. Create new todo list for revised phase
7. Trigger context summary

This allows the agent to recover from flawed plans without losing all progress.

### Todo List Display

The todo list appears in Layer 2 of the agent's context, injected on every turn. Format:

```
═══════════════════════════════════════════════════════════════════
                         ACTIVE TODO LIST
═══════════════════════════════════════════════════════════════════

Phase: Requirement Extraction (2 of 4)

[x] 1. Process section 1-3
[x] 2. Process section 4-6
[ ] 3. Consolidate findings      ← CURRENT
[ ] 4. Write extraction_results.md
[ ] 5. Validate format

Progress: 2/5 tasks complete

───────────────────────────────────────────────────────────────────
INSTRUCTION: Complete task 3, then call todo_complete()
═══════════════════════════════════════════════════════════════════
```

Key elements:
- **Phase indicator** - Shows where we are in the overall plan
- **Task list** - Clear checkboxes with current task marked
- **Progress** - X/Y format so agent knows how much remains
- **Instruction** - Explicit reminder of what to do next

The visual separators (═══) help the LLM recognize this as a distinct, important section.

---

## Workspace Summary

The `workspace_summary.md` file provides the agent with a snapshot of the current state:

```markdown
# Workspace Summary

Generated: 2024-01-15 14:32:00

## Files

| File | Purpose | Last Modified |
|------|---------|---------------|
| plan.md | Execution plan | 14:30 |
| instructions.md | Task instructions | 14:00 |
| document_analysis.md | Initial analysis | 14:15 |
| extraction_results.md | Extracted reqs | 14:28 |

## Accomplishments
- Completed document analysis (47 pages processed)
- Extracted 23 requirements from sections 1-6
- Identified 3 ambiguous requirements for review

## Current State
- Working on Phase 2: Requirement Extraction
- 2 of 5 tasks complete in current phase
- No blocking issues

## Notes
- Document uses non-standard formatting in section 4
- Some requirements span multiple pages
```

This acts as the agent's "memory" of what's been done, similar to how Claude Code uses CLAUDE.md.

---

## Job Completion

The last phase of any plan should include a final step:

```markdown
## Phase N: Final Review
- [ ] Verify all deliverables
- [ ] Generate summary report
- [ ] Call job_complete()
```

The `job_complete()` tool signals that the entire job is finished:

```python
job_complete(
    summary="Extracted 47 requirements from GoBD document",
    deliverables=["output/requirements.json", "output/summary.md"],
    confidence=0.92,
    notes="3 requirements flagged for human review"
)
```

---

## Error Handling

### Stuck Detection

If the agent loops without progress (same state for N iterations):
- System can force a phase checkpoint
- Or prompt the agent to use `todo_rewind()`

### Recovery Strategies

1. **Minor issue** - Continue with current todo, note in workspace
2. **Task blocked** - Use `todo_rewind()` to reconsider approach
3. **Phase failed** - `todo_rewind()` may revise multiple phases
4. **Unrecoverable** - `job_complete()` with low confidence and detailed notes

---

## Implementation Checklist

### Context Management
- [ ] Modify context assembly to structure the 5 layers
- [ ] Ensure layers 1-3 are protected during summarization
- [ ] Update summarization to only compact layers 4-5
- [ ] Inject todo list display into layer 2 on every turn

### Todo System
- [ ] Implement `todo_complete()` - marks next incomplete task
- [ ] Add phase transition detection (when last task completes)
- [ ] Implement `todo_rewind(issue)` - panic button with recovery flow
- [ ] Add bootstrap todo injection on job start
- [ ] Store phase metadata (phase number, total phases) with todo list

### Phase Transition Workflow
- [ ] Create phase transition prompt template
- [ ] Implement workflow: archive → summary → read → update plan → create todos → summarize
- [ ] Ensure new todos are in layer 2 BEFORE summarization triggers
- [ ] Handle edge case: no more phases (trigger job completion prompt)

### Workspace Tools
- [ ] Implement `generate_workspace_summary()` tool
- [ ] Ensure workspace_summary.md format matches spec

### Completion
- [x] Create `job_complete()` tool (DONE)
- [ ] Add job completion detection (when plan shows all phases complete)
- [ ] Update database status on job completion

### System Prompt
- [ ] Rewrite system prompt with focus-on-todos guidance
- [ ] Remove old two-tier planning instructions (agent shouldn't manage strategy)
- [ ] Add "trust the process" messaging

### Testing
- [ ] Test full bootstrap → phase 1 → transition → phase 2 cycle
- [ ] Test todo_rewind recovery flow
- [ ] Test context size stays bounded across many phases
- [ ] Test job completion detection

---

## Future Enhancements

### experiences.md (Deferred)

A learning document where the agent tracks issues and failed approaches:

```markdown
# Experiences

## Failed Approaches
- Tried batch API processing - doesn't support >100 items
- Regex extraction missed nested structures

## Lessons Learned
- Always check API limits before batch operations
- Use recursive parsing for nested documents

## Patterns That Work
- Processing documents in 10-page chunks
- Validating incrementally rather than at end
```

This would help the agent avoid repeating mistakes across phases. Deferred for later implementation as it adds complexity.

---

## Summary

The guardrails system keeps agents focused through:

1. **Protected context** - System prompt and todos always visible
2. **Phase boundaries** - Natural checkpoints every 5-20 tasks
3. **Forced re-orientation** - Must re-read plan at phase transitions
4. **Clean context** - Summarization between phases
5. **Panic button** - Can rewind when stuck
6. **Progress tracking** - Plan shows completed vs pending phases

This creates a cycle:
```
Bootstrap → Phase 1 → Transition → Phase 2 → ... → Phase N → job_complete()
```

Each cycle refreshes context and forces the agent to stay aligned with its plan.
