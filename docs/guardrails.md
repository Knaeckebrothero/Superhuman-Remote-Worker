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
1. **Manager architecture** - Dedicated managers for todos, plans, and memory
2. **Two-tier planning** - Strategy (`main_plan.md`) vs Tactics (todo list)
3. **File-based memory** - `workspace.md` as persistent long-term memory (like CLAUDE.md)
4. **Phase-based execution** - Work divided into 5-20 step phases
5. **Nested loop graph architecture** - Graph ENFORCES the workflow, not hopes the agent follows it
6. **Panic button** - Agent can rewind when stuck

---

## Manager Architecture

The agent uses three manager classes to handle different aspects of state:

```
src/agent/
├── managers/
│   ├── todo.py      # TodoManager - STATEFUL (holds todo list)
│   ├── plan.py      # PlanManager - SERVICE (main_plan.md operations)
│   └── memory.py    # MemoryManager - SERVICE (workspace.md operations)
│
├── core/
│   └── workspace.py # WorkspaceManager - FILESYSTEM (read/write/list files)
```

### Hierarchy

```
┌─────────────────────────────────────────────────────────────────┐
│                     WorkspaceManager                            │
│                  (filesystem operations)                        │
│              read_file, write_file, list_files                  │
└─────────────────────────────────────────────────────────────────┘
                              ▲
                              │ uses
        ┌─────────────────────┼─────────────────────┐
        │                     │                     │
        ▼                     ▼                     ▼
┌───────────────┐    ┌───────────────┐    ┌───────────────┐
│  TodoManager  │    │  PlanManager  │    │ MemoryManager │
│  (stateful)   │    │   (service)   │    │   (service)   │
│               │    │               │    │               │
│ - todos list  │    │ - read plan   │    │ - read memory │
│ - add/complete│    │ - parse phases│    │ - update      │
│ - format      │    │ - suggestions │    │ - sections    │
│ - archive     │    │               │    │               │
└───────────────┘    └───────────────┘    └───────────────┘
```

### TodoManager (Stateful)

Holds the current todo list in memory. Used by the graph's inner loop.

```python
class TodoManager:
    """Stateful manager for todo list operations."""

    def __init__(self, workspace: WorkspaceManager):
        self._workspace = workspace
        self._todos: List[TodoItem] = []
        self._phase_name: str = ""

    # Core operations
    def add(self, content: str, priority: str = "medium") -> TodoItem
    def complete(self, todo_id: str, notes: List[str] = None) -> TodoItem
    def start(self, todo_id: str) -> TodoItem
    def list_all(self) -> List[TodoItem]
    def get_current(self) -> Optional[TodoItem]
    def clear(self) -> None

    # Bulk operations
    def set_from_list(self, todos: List[Dict]) -> None
    def create_from_phase_steps(self, steps: List[str]) -> None

    # Display
    def format_for_display(self) -> str  # Layer 2 format for LLM
    def format_for_logging(self) -> str  # Structured for monitoring

    # Persistence
    def archive(self, phase_name: str) -> str  # Archive to workspace, returns path

    # Monitoring
    def log_state(self) -> None  # Log current state for debugging
    def get_metrics(self) -> Dict  # completed/pending/total counts
```

### PlanManager (Service)

Service class for `main_plan.md` operations. Does not hold state - operates on filesystem.

```python
class PlanManager:
    """Service class for main_plan.md operations."""

    PLAN_FILE = "main_plan.md"

    def __init__(self, workspace: WorkspaceManager):
        self._workspace = workspace

    # Lifecycle
    def exists(self) -> bool
    def create_initial(self, task_description: str) -> str  # Returns template for LLM
    def read(self) -> str
    def write(self, content: str) -> None

    # Parsing (flexible - works with any reasonable format)
    def extract_phases(self, content: str = None) -> List[PhaseInfo]
    def get_current_phase(self, content: str = None) -> Optional[PhaseInfo]
    def get_next_phase(self, content: str = None) -> Optional[PhaseInfo]
    def is_complete(self, content: str = None) -> bool

    # Guidance (not enforced)
    def suggest_phase_structure(self) -> str  # Template/guidance for LLM
```

**Important:** No strict format enforcement. The LLM creates plans in natural language. The parser is flexible and extracts what it can. If the LLM decides phases don't make sense for a task, that's fine - the system adapts.

### MemoryManager (Service)

Service class for `workspace.md` - the agent's long-term memory.

```python
class MemoryManager:
    """Service class for workspace.md (agent's long-term memory)."""

    MEMORY_FILE = "workspace.md"

    def __init__(self, workspace: WorkspaceManager):
        self._workspace = workspace

    # Lifecycle
    def exists(self) -> bool
    def create_initial(self) -> str  # From template
    def read(self) -> str
    def write(self, content: str) -> None

    # Section helpers (suggested structure, not enforced)
    def get_section(self, section_name: str) -> Optional[str]
    def update_section(self, section_name: str, content: str) -> None
```

---

## File-Based Memory

### The Key Insight

**Memory lives in files, not conversation history.**

If you wipe the conversation entirely, the agent can recover because:
- `main_plan.md` has the strategic direction
- `workspace.md` has learnings, decisions, and context
- Both are read at the start of every outer loop iteration

This is similar to how humans work:
- We write notes
- We come back to our notes
- We update our notes
- The notes ARE the memory, not our recall

### workspace.md (Long-Term Memory)

The `workspace.md` file serves the same role as `CLAUDE.md` in Claude Code - it's the agent's persistent memory that survives context resets.

**Always in system prompt:** The agent always has access to workspace.md content.

**Updated during todo recreation:** The graph forces updates at phase transitions.

**Agent-maintained:** The agent decides what's worth remembering.

#### Suggested Structure

```markdown
# Workspace Memory

## Workspace Overview
- `documents/` - Source documents for extraction
- `chunks/` - Extracted text chunks (42 files)
- `output/` - Generated requirements and reports
- `archive/` - Completed phase todos

## Current Approach
- Using section-by-section extraction
- Assessment structure: [functional, compliance, constraint]
- Citation format: [page:paragraph]

## Lessons Learned
- Large tables should be processed as separate chunks
- API rate limit: batch requests to 10 at a time
- Watch for duplicate requirements across sections

## Notes
- Document is in German (GoBD compliance)
- ~150 pages, estimated 40-60 requirements
```

This structure is **suggested, not enforced**. The agent can organize it however makes sense.

#### What Goes in workspace.md

| Category | Examples |
|----------|----------|
| **Workspace overview** | File/folder structure, what's where |
| **Current approach** | Patterns being used, assessment criteria |
| **Lessons learned** | Mistakes to avoid, things that worked |
| **Decisions** | Why certain approaches were chosen |
| **Notes** | Anything else worth remembering |

### main_plan.md (Strategic Direction)

The execution plan. Read at phase transitions, not continuously.

**Flexible format:** The LLM creates this naturally. No strict template enforced.

**Suggested structure** (guidance for LLM, not requirement):

```markdown
# Execution Plan

## Overview
Extract GoBD compliance requirements from the provided document.

## Phase 1: Document Analysis ✓ COMPLETE
- Understand document structure
- Identify key sections
- Plan extraction approach

## Phase 2: Requirement Extraction ← CURRENT
- Process pages 1-50
- Process pages 51-100
- Consolidate findings

## Phase 3: Final Review
- Verify all requirements
- Generate summary
- Complete job
```

The parser looks for phase-like structures but doesn't fail if the format differs.

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
║                              INITIALIZATION                               ║
║                           (runs once per job)                             ║
║                                                                           ║
║   create         read            create         create        clear       ║
║   workspace.md → instructions → main_plan.md → first todos → history     ║
║                                                                           ║
╚═══════════════════════════════════════════════════════════════════════════╝
                                      ↓
╔═══════════════════════════════════════════════════════════════════════════╗
║                         OUTER LOOP (Strategic)                            ║
║                                                                           ║
║   System prompt ALWAYS contains:                                          ║
║   - Instructions (from instructions.md)                                   ║
║   - workspace.md content (refreshed each iteration)                       ║
║   - Current todo list (Layer 2 display)                                   ║
║                                                                           ║
║   ┌─────────────────────────────────────────────────────────────────┐    ║
║   │                      PLAN PHASE                                 │    ║
║   │                                                                 │    ║
║   │   read plan.md  →  update workspace.md  →  create phase todos  │    ║
║   │   (via PlanManager)  (via MemoryManager)   (via TodoManager)   │    ║
║   │                                                                 │    ║
║   └─────────────────────────────────────────────────────────────────┘    ║
║                                    ↓                                      ║
║   ┌─────────────────────────────────────────────────────────────────┐    ║
║   │                     EXECUTE PHASE (inner loop)                  │    ║
║   │                                                                 │    ║
║   │      execute todo ──→ check todos ──→ todos done? ──no──┐      │    ║
║   │         (ReAct)            │                            │      │    ║
║   │                           yes                           │      │    ║
║   │                            ↓                            │      │    ║
║   │                      archive todos ←────────────────────┘      │    ║
║   │                            ↓                                   │    ║
║   │                   update workspace.md                          │    ║
║   │                  (learnings from phase)                        │    ║
║   └─────────────────────────────────────────────────────────────────┘    ║
║                                    ↓                                      ║
║                          read plan.md again                               ║
║                          check: more phases?                              ║
║                             ↓          ↓                                  ║
║                            yes        no                                  ║
║                             ↓          ↓                                  ║
║                    back to PLAN PHASE  END                               ║
║                                                                           ║
╚═══════════════════════════════════════════════════════════════════════════╝
```

### Initialization Flow

Before entering the main loop, the graph runs initialization (once per job):

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           INITIALIZATION                                    │
│                                                                             │
│   ┌──────────────┐     ┌──────────────┐     ┌──────────────┐               │
│   │   Create     │     │    Read      │     │   Create     │               │
│   │ workspace.md │ ──→ │ instructions │ ──→ │  plan.md     │               │
│   │  (template)  │     │ + workspace  │     │  (LLM call)  │               │
│   └──────────────┘     └──────────────┘     └──────────────┘               │
│                                                    │                        │
│                                                    ▼                        │
│                              ┌──────────────────────────────┐              │
│                              │   Create first todos         │              │
│                              │   from plan's first phase    │              │
│                              └──────────────────────────────┘              │
│                                                    │                        │
│                                                    ▼                        │
│                              ┌──────────────────────────────┐              │
│                              │   Clear conversation history │              │
│                              │   (keep only system prompt)  │              │
│                              └──────────────────────────────┘              │
│                                                    │                        │
└────────────────────────────────────────────────────┼────────────────────────┘
                                                     ▼
                                            ENTER MAIN LOOP
```

This ensures the agent starts with:
- A populated workspace.md (from template)
- A plan in main_plan.md (LLM-generated)
- Todos for the first phase
- A clean conversation history

### Why Nested Loops Work

| Aspect | Simple ReAct | Nested Loops |
|--------|--------------|--------------|
| Plan checking | Agent might forget | Graph forces it every phase |
| Todo management | Bolted-on tools | Managed by TodoManager, controlled by graph |
| Memory | Conversation history | Files on disk (workspace.md) |
| Context wipe | Catastrophic | Just read from disk |
| Recovery | Hope for the best | Built-in by design |
| Debugging | "Why did it do that?" | See exact node that triggered |

### LangGraph Implementation

```python
from langgraph.graph import StateGraph, START, END
from langgraph.prebuilt import create_react_agent

class AgentState(TypedDict):
    messages: Annotated[list, add_messages]
    workspace_memory: str       # Contents of workspace.md
    plan_content: str           # Contents of main_plan.md (read at phase transitions)
    todos: list[TodoItem]       # Current phase todos
    current_todo_idx: int
    phase_complete: bool
    goal_achieved: bool
    iteration_count: int

# Managers are initialized once and passed to nodes
todo_manager = TodoManager(workspace)
plan_manager = PlanManager(workspace)
memory_manager = MemoryManager(workspace)

graph = StateGraph(AgentState)

# ═══════════════════════════════════════════════════════════════════
# INITIALIZATION (runs once)
# ═══════════════════════════════════════════════════════════════════
graph.add_node("init_workspace", init_workspace)      # Create workspace.md
graph.add_node("create_plan", create_plan)            # LLM creates main_plan.md
graph.add_node("init_todos", init_todos)              # Create first todos

graph.add_edge(START, "init_workspace")
graph.add_edge("init_workspace", "create_plan")
graph.add_edge("create_plan", "init_todos")

# ═══════════════════════════════════════════════════════════════════
# PLAN PHASE (runs at start of each outer loop iteration)
# ═══════════════════════════════════════════════════════════════════
graph.add_node("read_plan", read_plan)                # PlanManager.read()
graph.add_node("update_memory", update_memory)        # MemoryManager.write()
graph.add_node("create_todos", create_todos)          # TodoManager.create_from_phase()

graph.add_edge("init_todos", "execute")               # First time: skip to execute
graph.add_edge("read_plan", "update_memory")
graph.add_edge("update_memory", "create_todos")
graph.add_edge("create_todos", "execute")

# ═══════════════════════════════════════════════════════════════════
# EXECUTE PHASE (inner loop)
# ═══════════════════════════════════════════════════════════════════
react_subgraph = create_react_agent(llm, tools)

graph.add_node("execute", react_subgraph)             # ReAct for current todo
graph.add_node("check_todos", check_todos)            # TodoManager.get_metrics()
graph.add_node("archive_phase", archive_phase)        # TodoManager.archive()

graph.add_edge("execute", "check_todos")
graph.add_conditional_edges(
    "check_todos",
    lambda s: "archive" if s["phase_complete"] else "execute",
    {"archive": "archive_phase", "execute": "execute"}
)

# ═══════════════════════════════════════════════════════════════════
# OUTER LOOP CHECK
# ═══════════════════════════════════════════════════════════════════
graph.add_node("check_goal", check_goal)              # PlanManager.is_complete()

graph.add_edge("archive_phase", "check_goal")
graph.add_conditional_edges(
    "check_goal",
    lambda s: END if s["goal_achieved"] else "read_plan",
    {END: END, "read_plan": "read_plan"}              # OUTER LOOP BACK
)
```

### Recommendation

**Approach 1 (Subgraph as Node)** is recommended because:

1. The inner execute loop is a natural ReAct pattern (`create_react_agent` already exists)
2. Both loops are visible in LangGraph Studio for debugging
3. State can share the `messages` key for tool call history
4. Managers encapsulate complexity - graph nodes are simple
5. Inner loop can be reused or swapped out easily

---

## Two-Tier Planning

### Strategic Layer: `main_plan.md`

The execution plan defines the "grand picture":
- Overall approach to the task
- Phases with clear objectives (suggested, not required)
- Progress tracking
- Adjustments based on findings

Managed by `PlanManager`. Read at phase transitions by graph nodes.

### Tactical Layer: Todo List

The todo list is the agent's "working memory":
- Contains only current phase tasks (5-20 items)
- Agent focuses on completing tasks one by one
- Managed by `TodoManager`
- Displayed in Layer 2 of context (always visible)

---

## Context Structure

The agent's context window is structured in layers:

```
┌─────────────────────────────┐
│ 1. System Prompt            │ ─┐
│    + workspace.md content   │  │ PROTECTED
├─────────────────────────────┤  │ (always present)
│ 2. Active Todo List         │ ─┤
├─────────────────────────────┤  │
│ 3. Tool Descriptions        │ ─┘
├─────────────────────────────┤
│ 4. Previous Summary         │ ─┐ SUMMARIZABLE
├─────────────────────────────┤  │ (compacted when needed)
│ 5. Current Conversation     │ ─┘
└─────────────────────────────┘
```

**Note:** `workspace.md` is always in the system prompt. `main_plan.md` is only read at phase transitions (not continuously present).

### Layer Details

1. **System Prompt + workspace.md** - Instructions and persistent memory. Always present.
2. **Active Todo List** - Current phase's 5-20 tasks. Always visible via TodoManager.format_for_display().
3. **Tool Descriptions** - Short descriptions with references to full docs.
4. **Previous Summary** - Compressed history of completed work.
5. **Current Conversation** - Recent messages since last summary.

---

## Phase-Based Execution

### Phase Size Guidelines

| Phase Size | Guidance |
|------------|----------|
| < 5 steps | Too small - combine with adjacent phase |
| 5-20 steps | Ideal range |
| > 20 steps | Too large - break into multiple phases |

The goal is that each phase fits comfortably in the todo list format.

### Phase Properties

Each phase should:
- Have a clear objective
- Be independently executable
- Produce measurable output
- Be completable within context limits

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

This simplicity is intentional. The complexity is handled by the graph and managers.

### Available Functions

#### `todo_complete()`

Called by the agent after finishing a task. Takes no arguments.

```python
todo_complete()
# Returns: "Task 3 'Process section 1-3' marked complete. 2 tasks remaining."
```

The TodoManager automatically:
1. Finds the first incomplete task
2. Marks it complete
3. Returns status to the agent

**On last task completion:** The graph's `check_todos` node detects this and routes to `archive_phase`.

#### `todo_rewind(issue: str)`

Panic button for when the agent realizes the current approach isn't working.

```python
todo_rewind(issue="Step 7 is impossible because the API doesn't support batch operations.")
```

Triggers:
1. Archive current todo list with failure note
2. Route back to plan phase for reconsideration
3. LLM updates plan and creates new todos

### Todo List Display (Layer 2)

The todo list appears in Layer 2 of the agent's context, formatted by `TodoManager.format_for_display()`:

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

---

## Error Handling

### Stuck Detection

If the agent loops without progress (same state for N iterations):
- System can force a phase checkpoint
- Or prompt the agent to use `todo_rewind()`

### Recovery Strategies

1. **Minor issue** - Continue with current todo, note in workspace.md
2. **Task blocked** - Use `todo_rewind()` to reconsider approach
3. **Phase failed** - `todo_rewind()` may revise multiple phases
4. **Unrecoverable** - `job_complete()` with low confidence and detailed notes

---

## Summary

The guardrails system keeps agents focused through:

1. **Manager architecture** - TodoManager, PlanManager, MemoryManager encapsulate complexity
2. **File-based memory** - workspace.md persists across context resets (like CLAUDE.md)
3. **Nested loop graph** - Graph ENFORCES workflow, doesn't hope agent follows it
4. **Phase boundaries** - Natural checkpoints every 5-20 tasks
5. **Forced re-orientation** - Graph structure requires reading plan at phase transitions
6. **Panic button** - Can rewind when stuck

### Architecture Overview

```
╔═══════════════════════════════════════════════════════════════╗
║  OUTER LOOP (Strategic)                                       ║
║                                                               ║
║  read_plan → update_memory → create_todos                     ║
║       ↑                              ↓                        ║
║       │                    ┌─────────────────┐                ║
║       │                    │  INNER LOOP     │                ║
║       │                    │  (Tactical)     │                ║
║       │                    │                 │                ║
║       │                    │  execute ←──┐   │                ║
║       │                    │     ↓       │   │                ║
║       │                    │  check_todos─┘  │                ║
║       │                    │     ↓           │                ║
║       │                    │  archive_phase  │                ║
║       │                    └─────────────────┘                ║
║       │                              ↓                        ║
║       └────────────── check_goal ────┴──→ END                 ║
║                       (not done)    (done)                    ║
╚═══════════════════════════════════════════════════════════════╝
```

### Key Insight

**Memory lives in files, not conversation history.**

If you wipe the conversation entirely, who cares? The agent reads `main_plan.md` and `workspace.md` at the start of every outer loop iteration. The graph structure forces this—no hooks, no injection, no hoping the agent remembers.

The managers encapsulate the complexity:
- `TodoManager` handles the todo list state
- `PlanManager` handles plan parsing and updates
- `MemoryManager` handles workspace.md operations

The graph just orchestrates the flow between them.
