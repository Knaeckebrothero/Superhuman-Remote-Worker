# Phase Numbering and Planning Issues

## Problem Statement

The agent frequently loses track of its plan and workspace state over extended executions. This manifests as:

1. **Phase confusion** - Summaries show illogical transitions like "Tactical Phase 5 → Tactical Phase 3"
2. **Inconsistent numbering** - Strategic and tactical phases have separate counters, leading to confusing pairs like "Tactical Phase 3" with "Strategic Phase 12"
3. **Poor long-term planning** - The agent creates todos reactively for "the next phase" rather than working from a comprehensive master plan
4. **Inadequate state tracking** - `workspace.md` isn't used effectively to track progress, issues, and decisions

## Examples

### Confusing Phase Transition
```
"The agent transitioned from Tactical Phase 5 (Requirement Extraction) to Tactical Phase 3
(Requirement Classification)."
```
Phases should progress forward, not regress from 5 to 3.

### Mismatched Numbering
```
"The agent finished Tactical Phase 3 (Domain Model Relevance Selection) by processing all
7 todos, archived the completed todo list, and then completed Strategic Phase 12."
```
"Tactical Phase 3" paired with "Strategic Phase 12" makes no intuitive sense since strategic/tactical phases always come in pairs.

## Root Causes

1. **No enforced structure** - `workspace.md` and `plan.md` are freeform; the agent can write whatever or nothing useful
2. **Phase identity is implicit** - No explicit "you are in phase 3 of 10" state; the agent reconstructs this from context, which degrades over time
3. **Strategic phase is too brief** - The agent does minimal planning, creates some todos, and jumps back to tactical without proper verification
4. **Context compaction destroys state** - When messages get summarized, subtle context about progress gets lost if not captured in workspace.md
5. **Separate counters for strategic/tactical** - Creates confusion about where you actually are in the process

## Proposed Solutions

### Option A: Paired Numbering (Recommended)

Strategic and tactical phases share the same number since they're logically paired:

```
Phase 1: Strategic (plan) → Tactical (execute)
Phase 2: Strategic (plan) → Tactical (execute)
Phase 3: Strategic (plan) → Tactical (execute)
```

Output would show "Strategic Phase 3" then "Tactical Phase 3" - clearly a unit.

### Option B: Single Sequential Counter

One counter that increments on every phase transition:

```
Phase 1: Strategic
Phase 2: Tactical
Phase 3: Strategic
Phase 4: Tactical
```

Simpler but loses the sense that strategic/tactical are paired halves of one unit.

### Option C: Named Phases with Sub-steps

Phases have semantic names with strategic/tactical as sub-steps:

```
Phase 3: Domain Model Relevance Selection
  - 3a: Planning (strategic)
  - 3b: Execution (tactical)
```

Keeps the meaningful name front and center. May help with the broader "losing track" problem since named phases like "Requirement Classification" are harder to confuse than bare numbers.

## Additional Improvements to Consider

### Structured Phase Registry

A `phases.yaml` file with explicit phase definitions, statuses, and completion criteria:

```yaml
phases:
  - id: 1
    name: "Document Analysis"
    status: completed
    artifacts: ["analysis/document_structure.md"]
  - id: 2
    name: "Requirement Extraction"
    status: in_progress
    todos_completed: 5
    todos_total: 7
  - id: 3
    name: "Requirement Classification"
    status: pending
```

### Mandatory Workspace Sections

Template `workspace.md` with required sections:

```markdown
## Completed Phases
- Phase 1: Document Analysis ✓
- Phase 2: Requirement Extraction ✓

## Current Phase
Phase 3: Requirement Classification (Strategic)

## Issues Encountered
- Found 3 requirements with ambiguous scope
- Citation tool returned partial matches for section 4.2

## Decisions Made
- Treating ambiguous requirements as separate items for now
- Will flag partial citations for manual review
```

### Phase Completion Gates

Before transitioning, require the agent to:
1. List what was accomplished
2. Verify artifacts exist
3. Update workspace.md with completion notes
4. Explicitly confirm readiness for next phase

### Phase Verification Tool

A tool that checks:
- Expected artifacts exist
- Todo list is empty or archived
- workspace.md has been updated
- Prompts agent to confirm completion before allowing transition

## Implementation Notes

Files likely involved in phase counting:
- `src/core/state.py` - Phase state definitions
- `src/graph.py` - LangGraph state machine and transitions
- `src/managers/` - Todo and phase management

## Status

Discussion in progress. No implementation changes made yet.
