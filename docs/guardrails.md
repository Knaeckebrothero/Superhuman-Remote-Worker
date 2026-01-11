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
2. **Two-tier planning** - Strategy (`main_plan.md`) vs Tactics (todo list)
3. **Phase-based execution** - Work divided into 5-20 step phases
4. **Automatic phase transitions** - Workflow triggers when phase completes
5. **Panic button** - Agent can rewind when stuck
6. **Nested loop graph architecture** - Graph ENFORCES the workflow, not hopes the agent follows it

---

## Graph Architecture: Nested Loops

### The Problem with Simple ReAct Graphs

A basic ReAct-style graph looks like this:

```
initialize → process ←→ tools → check → END
```

This simple loop tries to handle everything through:
- Pre-model hooks (hidden magic)
- System message injection (bolted on)
- Hoping the agent "remembers" to check todos and update plans
- Protected context providers (more hidden magic)

**The fundamental problem:** We're trying to bolt planning, todos, and memory onto a graph that doesn't structurally enforce them.

### The Solution: Nested Loop Architecture

Instead of hoping the agent follows the workflow, the graph **makes** it happen:

```
╔═══════════════════════════════════════════════════════════════════════════╗
║                         OUTER LOOP (Strategic)                            ║
║                                                                           ║
║   ┌─────────────────────────────────────────────────────────────────┐    ║
║   │                      PLAN PHASE (no loop)                       │    ║
║   │                                                                 │    ║
║   │   examine      update        update       create               │    ║
║   │   workspace → memory docs → plan.md → phase todos              │    ║
║   │                                                                 │    ║
║   └─────────────────────────────────────────────────────────────────┘    ║
║                                    ↓                                      ║
║   ┌─────────────────────────────────────────────────────────────────┐    ║
║   │                     EXECUTE PHASE (inner loop)                  │    ║
║   │                                                                 │    ║
║   │         ┌────────────────────────────────────┐                 │    ║
║   │         ↓                                    │                 │    ║
║   │      process ──→ update todos ──→ todos done? ──no──┘         │    ║
║   │     (ReAct)                           │                        │    ║
║   │                                      yes                       │    ║
║   │                                       ↓                        │    ║
║   │                                 EXIT EXECUTE                   │    ║
║   └─────────────────────────────────────────────────────────────────┘    ║
║                                    ↓                                      ║
║                            goal achieved?                                 ║
║                             ↓          ↓                                  ║
║                            no         yes                                 ║
║                             ↓          ↓                                  ║
║                    ┌───────┘          END                                ║
║                    ↓                                                      ║
║              back to PLAN PHASE                                          ║
║                                                                           ║
╚═══════════════════════════════════════════════════════════════════════════╝
```

### Why Nested Loops Work

| Aspect | Simple ReAct | Nested Loops |
|--------|--------------|--------------|
| Plan checking | Agent might forget | Graph forces it every phase |
| Todo management | Bolted-on tools | Structural part of graph |
| Memory | Conversation history | Files on disk |
| Context wipe | Catastrophic | Just read plan.md |
| Recovery | Hope for the best | Built-in by design |
| Debugging | "Why did it do that?" | See exact node that triggered |

### The Key Insight: File-Based Memory

The magic is that every loop iteration, the agent **must** read from disk:

```python
# Each node reads from disk, not from conversation memory
def examine_workspace(state):
    plan = workspace.read("plan.md")       # Truth is on disk
    memory = workspace.read("workspace.md") # Persistent notes
    return {"current_plan": plan, "memory": memory}
```

**If you wipe the conversation entirely, who cares?** The plan is on disk. The notes are in workspace.md. The agent can recover because its memory isn't in the ephemeral conversation—it's persisted to files.

This is similar to how humans work:
- We write notes
- We come back to our notes
- We update our notes
- The notes ARE the memory, not our recall

### Summarization Becomes Optional

With file-based memory, context summarization becomes less critical:
- The agent writes findings to files as it goes
- `plan.md` captures strategic decisions
- `workspace.md` captures learnings and notes
- `progress.md` captures what was done

You don't need to summarize the conversation—you need to **persist** important information. The graph forces this at structural checkpoints.

### LangGraph Implementation

```python
from langgraph.graph import StateGraph, START, END

class AgentState(TypedDict):
    messages: list
    plan: str
    todos: list[Todo]
    current_todo_idx: int
    phase_complete: bool
    goal_achieved: bool

graph = StateGraph(AgentState)

# ═══════════════════════════════════════════════════════════
# PLAN PHASE (sequential, no loop)
# ═══════════════════════════════════════════════════════════
graph.add_node("examine_workspace", examine_workspace)
graph.add_node("update_memory", update_memory_docs)
graph.add_node("update_plan", update_plan)
graph.add_node("create_todos", create_phase_todos)

graph.add_edge(START, "examine_workspace")
graph.add_edge("examine_workspace", "update_memory")
graph.add_edge("update_memory", "update_plan")
graph.add_edge("update_plan", "create_todos")

# ═══════════════════════════════════════════════════════════
# EXECUTE PHASE (inner loop)
# ═══════════════════════════════════════════════════════════
graph.add_node("process", process_current_todo)      # ReAct subgraph
graph.add_node("update_todos", update_todo_status)
graph.add_node("check_todos", check_todos_complete)

graph.add_edge("create_todos", "process")            # Enter execute loop
graph.add_edge("process", "update_todos")
graph.add_edge("update_todos", "check_todos")

# Inner loop: todos done?
graph.add_conditional_edges(
    "check_todos",
    lambda s: "exit_execute" if s["phase_complete"] else "continue",
    {
        "continue": "process",          # ← INNER LOOP BACK
        "exit_execute": "check_goal",
    }
)

# ═══════════════════════════════════════════════════════════
# OUTER LOOP CHECK
# ═══════════════════════════════════════════════════════════
graph.add_node("check_goal", check_goal_achieved)

graph.add_conditional_edges(
    "check_goal",
    lambda s: "done" if s["goal_achieved"] else "next_phase",
    {
        "next_phase": "examine_workspace",  # ← OUTER LOOP BACK
        "done": END,
    }
)
```

### Process Node as ReAct Subgraph

The `process` node can itself be a ReAct loop for tool use:

```python
from langgraph.prebuilt import create_react_agent

react_subgraph = create_react_agent(llm, tools)

def process_current_todo(state):
    todo = state["todos"][state["current_todo_idx"]]

    # Run ReAct loop scoped to this ONE todo
    result = react_subgraph.invoke({
        "messages": [HumanMessage(f"Complete this todo: {todo.content}")]
    })

    # Persist to disk immediately (file-based memory)
    workspace.append("progress.md", f"- {todo.content}: done")

    return {"messages": state["messages"] + result["messages"]}
```

### Comparison with Current Implementation

The current implementation uses hooks and injection to achieve similar goals:

| Component | Current Approach | Nested Loop Approach |
|-----------|------------------|----------------------|
| Plan checking | `ProtectedContextProvider` injects plan | `examine_workspace` node reads plan |
| Todo display | Layer 2 injection via hook | State passed to `process` node |
| Phase transitions | `PhaseTransitionManager` triggered by tool | `check_todos` → `check_goal` edges |
| Memory persistence | Agent calls `write_file` | Graph nodes call `workspace.write` |
| Context compaction | `ContextManager` hooks | Less critical (file-based memory) |

The nested loop approach makes the workflow **explicit in the graph structure** rather than hidden in hooks and providers.

### Implementation Approaches

There are three ways to implement nested loops in LangGraph:

#### Approach 1: Subgraph as Node (Shared State) ✓ RECOMMENDED

When parent and child share state keys, add a compiled subgraph directly as a node:

```python
# Inner loop (execute phase) - can use prebuilt ReAct agent
from langgraph.prebuilt import create_react_agent

inner_graph = create_react_agent(llm, tools)

# Outer loop (plan phase)
class OuterState(TypedDict):
    messages: Annotated[list, add_messages]  # Shared with inner
    plan: str
    todos: list[Todo]
    phase_complete: bool
    goal_achieved: bool

outer = StateGraph(OuterState)
outer.add_node("examine_workspace", examine_workspace)
outer.add_node("update_plan", update_plan)
outer.add_node("create_todos", create_todos)
outer.add_node("execute", inner_graph)  # ← Compiled subgraph as node!
outer.add_node("check_todos", check_todos)
outer.add_node("check_goal", check_goal)

# Wire up the loops
outer.add_edge(START, "examine_workspace")
outer.add_edge("examine_workspace", "update_plan")
outer.add_edge("update_plan", "create_todos")
outer.add_edge("create_todos", "execute")
outer.add_edge("execute", "check_todos")
outer.add_conditional_edges("check_todos", lambda s: "execute" if not s["phase_complete"] else "check_goal")
outer.add_conditional_edges("check_goal", lambda s: END if s["goal_achieved"] else "examine_workspace")
```

**Pros:** Clean separation, subgraph visible in visualization, automatic state passing
**Cons:** Must share at least one state key (usually `messages`)

#### Approach 2: Wrapper Function (Different States)

When states are completely different, wrap the subgraph invocation with state transformation:

```python
class InnerState(TypedDict):
    current_todo: str
    tools_result: str

class OuterState(TypedDict):
    plan: str
    todos: list[str]
    phase_complete: bool

def execute_phase(state: OuterState) -> OuterState:
    """Wrapper transforms state, invokes subgraph, transforms back."""
    for todo in state["todos"]:
        # Transform to inner state
        inner_input = {"current_todo": todo, "tools_result": ""}
        # Run inner loop
        inner_output = inner_graph.invoke(inner_input)
        # Persist result
        workspace.append("progress.md", f"- {todo}: {inner_output['tools_result']}")

    return {"phase_complete": True}

outer.add_node("execute", execute_phase)  # Function wrapping graph
```

**Pros:** Complete state isolation, flexible transformation
**Cons:** Subgraph not visible in LangGraph Studio visualization

#### Approach 3: Single Graph with Conditional Edges (Flat)

Both loops in one graph using state flags and conditional edges:

```python
graph = StateGraph(AgentState)

# All nodes flat in one graph
graph.add_node("examine_workspace", examine_workspace)
graph.add_node("update_plan", update_plan)
graph.add_node("create_todos", create_todos)
graph.add_node("process_todo", process_todo)
graph.add_node("check_todos", check_todos)
graph.add_node("check_goal", check_goal)

# Inner loop via conditional edge
graph.add_conditional_edges(
    "check_todos",
    lambda s: "check_goal" if s["phase_complete"] else "process_todo",
)

# Outer loop via conditional edge
graph.add_conditional_edges(
    "check_goal",
    lambda s: END if s["goal_achieved"] else "examine_workspace",
)
```

**Pros:** Simple, everything visible, easy to debug
**Cons:** Gets complex with many nodes, harder to reuse inner loop elsewhere

### Recursion Limits and Safety

LangGraph enforces a default recursion limit of 25 steps. For nested loops, you'll need to increase this:

```python
# Increase limit for complex workflows
graph.invoke(input, {"recursion_limit": 100})
```

**Better approach:** Use state-based loop control instead of relying on recursion limits:

```python
class AgentState(TypedDict):
    max_iterations: int
    current_iteration: int

def should_continue(state: AgentState) -> str:
    if state["current_iteration"] >= state["max_iterations"]:
        return "exit"  # Safety valve
    if state["task_complete"]:
        return "exit"
    return "continue"
```

### Recommendation

For our plan → execute → check architecture, **Approach 1 (Subgraph as Node)** is recommended because:

1. The inner execute loop is a natural ReAct pattern (`create_react_agent` already exists)
2. Both loops are visible in LangGraph Studio for debugging
3. State can share the `messages` key for tool call history
4. Clean separation of concerns between strategic (outer) and tactical (inner) loops
5. Inner loop can be reused or swapped out easily

### References

- [LangGraph Subgraphs Documentation](https://docs.langchain.com/oss/python/langgraph/use-subgraphs)
- [Hierarchical Agent Teams Tutorial](https://langchain-ai.github.io/langgraph/tutorials/multi_agent/hierarchical_agent_teams/)
- [State Transform Between Subgraphs](https://langchain-opentutorial.gitbook.io/langchain-opentutorial/17-langgraph/01-core-features/14-langgraph-subgraph-transform-state)
- [GRAPH_RECURSION_LIMIT Error Guide](https://docs.langchain.com/oss/python/langgraph/errors/GRAPH_RECURSION_LIMIT)

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

### Strategic Layer: `main_plan.md`

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
[ ] Create comprehensive execution plan in main_plan.md
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
│  - main_plan.md              │
│  - instructions.md           │
│  - workspace_summary.md      │
└──────────────┬───────────────┘
               ▼
┌──────────────────────────────┐
│  LLM updates main_plan.md:   │
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

When the LLM reads `main_plan.md` during phase transition and finds NO remaining phases:

```
┌──────────────────────────────┐
│  LLM reads main_plan.md      │
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
3. Read `main_plan.md`, `instructions.md`, `workspace_summary.md`
4. **Reconsider phases** based on the issue
5. Update `main_plan.md` with adjusted approach
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
| main_plan.md | Execution plan | 14:30 |
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

## Design Decisions

This section documents key design decisions made during planning.

### Q1: Should `todo_complete()` exist separately from `todo_write()`?

**Decision: YES - implement `todo_complete()` as a simpler interface.**

Rationale:
- `todo_write()` requires the agent to manage the full list (Claude Code pattern)
- `todo_complete()` is simpler: agent calls it, system finds next incomplete task
- Having both gives flexibility: `todo_write()` for bulk updates, `todo_complete()` for incremental progress
- The spec's "simple mental model" benefits from `todo_complete()`

### Q2: Automatic vs agent-driven phase transitions?

**Decision: AUTOMATIC with agent fallback.**

Rationale:
- Automatic transitions reduce agent cognitive load (spec's goal)
- Agent still controls when to mark tasks complete
- System handles the mechanics: archive → summary → read plan → create new todos
- Agent can manually call `archive_and_reset()` if needed (escape hatch)

### Q3: How should the 5-layer context structure work?

**Decision: Use `ProtectedContextProvider` to inject Layer 2.**

Rationale:
- Current `ProtectedContextProvider` already reads `main_plan.md` and todos
- Extend it to format Layer 2 with the visual todo list display from spec
- Layer 2 injection happens in the process node before LLM call
- Layers 4-5 handled by `ContextManager` trimming

Implementation notes:
- `ProtectedContextProvider.get_protected_context()` returns Layer 2 content
- Inject as a `SystemMessage` or prefix to conversation
- Ensure visual separators (═══) are included for LLM recognition

### Q4: What about `todo_rewind()`?

**Decision: Implement as specified - critical for recovery.**

Rationale:
- Without `todo_rewind()`, stuck agents have no escape hatch
- Archives current state with failure note (preserves work)
- Triggers re-planning workflow (not just reset)
- Allows agent to recover from flawed plans

### Q5: Where should `workspace_summary.md` be written?

**Decision: Tool writes to file, agent reads it.**

Rationale:
- Current `get_workspace_summary()` returns text (doesn't write file)
- Change to `generate_workspace_summary()` that writes to `workspace_summary.md`
- Consistent with spec's workflow: generate → read → update plan
- File persists for debugging and audit

---

## Current Implementation Status

### What Exists (in `src/agent/`)

| Component | File | Status |
|-----------|------|--------|
| `todo_write()` | `tools/todo_tools.py` | ✓ Complete |
| `archive_and_reset()` | `tools/todo_tools.py` | ✓ Complete (with phase number) |
| `todo_complete()` | `tools/todo_tools.py` | ✓ Complete (Phase 1+4 transition) |
| `todo_rewind()` | `tools/todo_tools.py` | ✓ Complete (Phase 1+4 transition) |
| `mark_complete()` | `tools/completion_tools.py` | ✓ Complete |
| `job_complete()` | `tools/completion_tools.py` | ✓ Partial (needs DB update) |
| `get_workspace_summary()` | `tools/workspace_tools.py` | ✓ Complete (returns text) |
| `generate_workspace_summary()` | `tools/workspace_tools.py` | ✓ Complete (Phase 2) |
| `add_accomplishment()` | `tools/workspace_tools.py` | ✓ Complete (Phase 2) |
| `add_note()` | `tools/workspace_tools.py` | ✓ Complete (Phase 2) |
| `TodoManager` | `todo_manager.py` | ✓ Complete (with phase metadata) |
| `ContextManager` | `context_manager.py` | ✓ Complete |
| `ProtectedContextProvider` | `context.py` | ✓ Complete (Phase 3 Layer 2 format) |
| 4-node LangGraph workflow | `graph.py` | ✓ Complete |
| Completion detection | `graph.py` | ✓ Complete (file/tool/phrase) |
| `PhaseTransitionManager` | `phase_transition.py` | ✓ Complete (Phase 4) |
| Phase transition detection | `graph.py` | ✓ Complete (Phase 4) |
| `get_bootstrap_todos()` | `phase_transition.py` | ✓ Complete (Phase 5) |
| Bootstrap todo injection | `graph.py` | ✓ Complete (Phase 5) |
| `update_job_status()` | `tools/context.py` | ✓ Complete (Phase 5) |
| Database status update | `tools/completion_tools.py` | ✓ Complete (Phase 5) |
| Focus-on-todos guidance | `instructions/*.md` | ✓ Complete (Phase 5) |

### What's Missing

| Component | Priority | Notes |
|-----------|----------|-------|
| ~~`todo_complete()`~~ | ~~High~~ | ✓ Implemented in Phase 1 |
| ~~`todo_rewind()`~~ | ~~High~~ | ✓ Implemented in Phase 1 |
| ~~`generate_workspace_summary()`~~ | ~~Medium~~ | ✓ Implemented in Phase 2 |
| ~~Layer 2 formatted injection~~ | ~~Medium~~ | ✓ Implemented in Phase 3 |
| ~~Phase transition automation~~ | ~~Medium~~ | ✓ Implemented in Phase 4 |
| ~~Bootstrap todo injection~~ | ~~Medium~~ | ✓ Implemented in Phase 5 |
| ~~Database status update~~ | ~~Low~~ | ✓ Implemented in Phase 5 |

---

## Implementation Roadmap

### Phase 1: Core Todo Tools (Priority: High) ✓ COMPLETE

**Objective:** Complete the todo tool suite as specified.

**Status:** Completed on 2026-01-11

**Tasks:**
1. ✓ Implement `todo_complete()` in `todo_tools.py`
   - Takes no arguments
   - Finds first incomplete task, marks complete
   - Returns status message with remaining count
   - On last task: triggers phase transition detection

2. ✓ Implement `todo_rewind(issue: str)` in `todo_tools.py`
   - Archives current todos with failure note
   - Clears todo list
   - Returns message prompting re-planning

3. ✓ Add phase metadata to `TodoManager`
   - Track `phase_number`, `total_phases`
   - Include in archive file names
   - Display in progress messages

**Files modified:**
- `src/agent/tools/todo_tools.py`
- `src/agent/todo_manager.py`
- `tests/test_todo_tools.py`

### Phase 2: Workspace Summary (Priority: Medium) ✓ COMPLETE

**Objective:** Generate persistent workspace summaries.

**Status:** Completed on 2026-01-11

**Tasks:**
1. ✓ Create `generate_workspace_summary()` tool
   - Writes to `workspace_summary.md`
   - Includes: files, accomplishments, current state, notes
   - Format matches spec

2. ✓ Create accomplishment tracking tools
   - `add_accomplishment()` - record milestone achievements
   - `add_note()` - record working notes
   - Both integrate with `generate_workspace_summary()`

3. Integrate with phase transition workflow (deferred to Phase 4)
   - Called automatically on phase complete
   - Agent reads it during bootstrap and transitions

**Files modified:**
- `src/agent/tools/workspace_tools.py`
- `tests/test_workspace_tools.py`

### Phase 3: Context Layer Structure (Priority: Medium) ✓ COMPLETE

**Objective:** Implement the 5-layer context structure.

**Status:** Completed on 2026-01-11

**Tasks:**
1. ✓ Enhance `ProtectedContextProvider`
   - Format Layer 2 with visual separators (═══ and ───)
   - Include phase indicator (Phase: Name (X of Y))
   - Add explicit instruction line with current task
   - Add `get_layer2_todo_display()` method
   - Add `is_layer2_message()` helper function
   - Add `LAYER2_START_MARKER` and `LAYER2_TITLE` constants

2. ✓ Update context injection in `graph.py`
   - Inject Layer 2 before each LLM call (in process_node)
   - Insert as SystemMessage right after first system message
   - Always present (not summarized away)

3. ✓ Update `ContextManager` trimming
   - Document that Layer 2 content is never trimmed
   - All SystemMessages preserved in `trim_messages()`
   - Layer 2 injected AFTER preparation, so never subject to trimming

**Files modified:**
- `src/agent/context.py`
- `src/agent/graph.py`
- `tests/test_context_management.py`

### Phase 4: Phase Transition Automation (Priority: Medium) ✓ COMPLETE

**Objective:** Automate phase transitions on last task completion.

**Status:** Completed on 2026-01-11

**Tasks:**
1. ✓ Detect last task completion in `todo_complete()`
   - Check if all todos are complete via `is_last_task` flag
   - Trigger phase transition workflow via PhaseTransitionManager

2. ✓ Implement phase transition workflow
   - Archive completed todos automatically
   - Generate workspace summary prompt
   - Inject transition prompt (read plan, update, create new todos)
   - Trigger context summarization via state flag

3. ✓ Handle edge case: all phases complete
   - Automatically inject job completion todos when all phases done
   - Agent receives JOB_COMPLETE prompt with final verification tasks

**Files modified:**
- `src/agent/tools/todo_tools.py` - Integration with PhaseTransitionManager
- `src/agent/graph.py` - Phase transition detection and context summarization trigger
- `src/agent/state.py` - Added phase_transition field
- `src/agent/phase_transition.py` - New module with PhaseTransitionManager
- `tests/test_phase_transitions.py` - 27 tests for phase transitions

### Phase 5: Bootstrap & Completion (Priority: Low) ✓ COMPLETE

**Objective:** Automate job start and end.

**Status:** Completed on 2026-01-11

**Tasks:**
1. ✓ Implement bootstrap todo injection
   - Pre-populate initial todos on job start
   - Standard sequence: summary → read → plan → divide

2. ✓ Complete `job_complete()` database integration
   - Update `jobs.status = 'completed'`
   - Set `jobs.completed_at = now()`

3. ✓ Implement system prompt updates
   - Focus-on-todos guidance
   - "Trust the process" messaging

**Files modified:**
- `src/agent/phase_transition.py` - Added get_bootstrap_todos() and BOOTSTRAP_PROMPT
- `src/agent/graph.py` - Bootstrap injection in initialize_node
- `src/agent/tools/context.py` - Added update_job_status() method
- `src/agent/tools/completion_tools.py` - Database integration in job_complete()
- `src/config/agents/creator.json` - Added todo_complete and todo_rewind
- `src/config/agents/validator.json` - Added todo_complete and todo_rewind
- `src/config/agents/instructions/creator_instructions.md` - Focus-on-todos guidance
- `src/config/agents/instructions/validator_instructions.md` - Focus-on-todos guidance
- `tests/test_bootstrap_completion.py` - 20 tests for Phase 5

### Phase 6: Testing & Validation (Priority: High, parallel)

**Objective:** Verify the system works end-to-end.

**Tasks:**
1. Unit tests for new tools
   - `test_todo_complete()`
   - `test_todo_rewind()`
   - `test_generate_workspace_summary()`

2. Integration tests
   - Full bootstrap → phase 1 → transition → phase 2 cycle
   - Context size stays bounded across phases
   - Recovery via `todo_rewind()`

3. Manual validation
   - Run Creator agent on real document
   - Verify phase transitions work
   - Check context doesn't overflow

**Files to create:**
- `tests/test_guardrails.py`
- `tests/test_phase_transitions.py`

### Phase 7: Nested Loop Graph Architecture (Priority: High)

**Objective:** Refactor the graph to use nested loops instead of hook-based injection.

**Status:** Planned

**Rationale:** The current implementation uses hooks and providers to inject context, todos, and manage phase transitions. This works but hides the workflow logic. The nested loop architecture makes the workflow explicit in the graph structure, improving debuggability and reducing reliance on the agent "remembering" to do things.

**Tasks:**

1. Define new state schema with explicit loop control
   ```python
   class AgentState(TypedDict):
       messages: Annotated[list, add_messages]
       plan: str                    # Read from plan.md each iteration
       workspace_memory: str        # Read from workspace.md each iteration
       todos: list[Todo]
       current_todo_idx: int
       phase_complete: bool
       goal_achieved: bool
       iteration_count: int         # Safety counter
   ```

2. Create outer loop nodes (plan phase - no loop)
   - `examine_workspace` - Read plan.md, workspace.md into state
   - `update_memory` - Write learnings to workspace.md
   - `update_plan` - LLM updates plan.md based on progress
   - `create_todos` - LLM creates todos for next phase

3. Create inner loop using `create_react_agent` as subgraph
   - Use prebuilt ReAct agent for tool execution
   - Scope each invocation to ONE todo
   - Persist results to progress.md after each todo

4. Wire up conditional edges for both loops
   - Inner loop: `check_todos` → `execute` (if not phase_complete) or `check_goal`
   - Outer loop: `check_goal` → `examine_workspace` (if not goal_achieved) or END

5. Remove hook-based injection
   - Remove `ProtectedContextProvider` (state carries plan/todos)
   - Remove `PhaseTransitionManager` (graph edges handle transitions)
   - Simplify `ContextManager` (file-based memory reduces need for compaction)

6. Update recursion limit handling
   - Set appropriate limit for nested loops (e.g., 100)
   - Add state-based iteration counter as safety valve

**Files to modify:**
- `src/agent/graph.py` - Complete rewrite to nested loop structure
- `src/agent/state.py` - New state schema
- `src/agent/context.py` - Simplify (remove ProtectedContextProvider)
- `src/agent/phase_transition.py` - May be removed or simplified

**Files to create:**
- `src/agent/nodes/` - Directory for node implementations
  - `examine_workspace.py`
  - `update_plan.py`
  - `create_todos.py`
  - `check_goal.py`

**Migration strategy:**
1. Build new graph in parallel (`graph_v2.py`)
2. Test with simple tasks
3. Compare behavior with current implementation
4. Switch over once validated

---

## Implementation Checklist

### Phase 1: Core Todo Tools ✓ COMPLETE
- [x] Implement `todo_complete()` - marks next incomplete task
- [x] Implement `todo_rewind(issue)` - panic button with recovery flow
- [x] Add phase metadata (phase_number, total_phases) to TodoManager
- [x] Update `archive_and_reset()` to include phase number in filename

### Phase 2: Workspace Summary ✓ COMPLETE
- [x] Implement `generate_workspace_summary()` tool (writes file)
- [x] Ensure workspace_summary.md format matches spec
- [x] Add accomplishments tracking with `add_accomplishment()` and `add_note()` tools

### Phase 3: Context Layer Structure ✓ COMPLETE
- [x] Enhance `ProtectedContextProvider` with Layer 2 formatting
- [x] Add visual separators (═══) to todo display
- [x] Update `graph.py` to inject Layer 2 on every turn
- [x] Ensure Layers 1-3 are never trimmed

### Phase 4: Phase Transition Automation ✓ COMPLETE
- [x] Detect last task completion in `todo_complete()`
- [x] Create phase transition prompt templates (PHASE_TRANSITION_PROMPT, JOB_COMPLETE_PROMPT, REWIND_TRANSITION_PROMPT)
- [x] Implement workflow: archive → summary → read → update plan → create todos
- [x] Handle edge case: no more phases (inject job completion todos automatically)
- [x] Trigger context summarization after phase transition (via state flag)

### Phase 5: Bootstrap & Completion ✓ COMPLETE
- [x] Add bootstrap todo injection on job start
- [x] Update `job_complete()` to update PostgreSQL status
- [x] Rewrite system prompt with focus-on-todos guidance
- [x] Add "trust the process" messaging

### Phase 6: Testing
- [ ] Test `todo_complete()` flow
- [ ] Test `todo_rewind()` recovery flow
- [ ] Test full bootstrap → phase 1 → transition → phase 2 cycle
- [ ] Test context size stays bounded across many phases
- [ ] Test job completion detection

### Phase 7: Nested Loop Graph Architecture
- [ ] Define new `AgentState` with loop control fields
- [ ] Implement `examine_workspace` node (reads plan.md, workspace.md)
- [ ] Implement `update_memory` node (writes to workspace.md)
- [ ] Implement `update_plan` node (LLM updates plan.md)
- [ ] Implement `create_todos` node (LLM creates phase todos)
- [ ] Integrate `create_react_agent` as inner loop subgraph
- [ ] Wire up inner loop conditional edges (todo processing)
- [ ] Wire up outer loop conditional edges (phase transitions)
- [ ] Remove `ProtectedContextProvider` (no longer needed)
- [ ] Simplify `PhaseTransitionManager` (graph edges handle it)
- [ ] Add state-based iteration counter for safety
- [ ] Set recursion limit to 100 for nested loops
- [ ] Test new graph with simple task
- [ ] Compare behavior with current implementation
- [ ] Migration: switch from graph.py to graph_v2.py

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

1. **Nested loop graph architecture** - Graph ENFORCES workflow, doesn't hope agent follows it
2. **File-based memory** - plan.md and workspace.md persist across context resets
3. **Phase boundaries** - Natural checkpoints every 5-20 tasks
4. **Forced re-orientation** - Graph structure requires reading plan at phase transitions
5. **Panic button** - Can rewind when stuck
6. **Progress tracking** - Plan shows completed vs pending phases

### Architecture Overview

```
╔═══════════════════════════════════════════════════════════════╗
║  OUTER LOOP (Strategic)                                       ║
║                                                               ║
║  examine_workspace → update_plan → create_todos               ║
║         ↑                              ↓                      ║
║         │                    ┌─────────────────┐              ║
║         │                    │  INNER LOOP     │              ║
║         │                    │  (Tactical)     │              ║
║         │                    │                 │              ║
║         │                    │  execute ←──┐   │              ║
║         │                    │     ↓       │   │              ║
║         │                    │  check_todos─┘  │              ║
║         │                    │     ↓           │              ║
║         │                    └─────────────────┘              ║
║         │                              ↓                      ║
║         └────────────── check_goal ────┴──→ END               ║
║                         (not done)    (done)                  ║
╚═══════════════════════════════════════════════════════════════╝
```

### Key Insight

**Memory lives in files, not conversation history.**

If you wipe the conversation entirely, who cares? The agent reads plan.md and workspace.md at the start of every outer loop iteration. The graph structure forces this—no hooks, no injection, no hoping the agent remembers.

This creates a cycle:
```
Bootstrap → Phase 1 → Transition → Phase 2 → ... → Phase N → job_complete()
```

Each cycle reads fresh state from disk and forces the agent to stay aligned with its plan.
