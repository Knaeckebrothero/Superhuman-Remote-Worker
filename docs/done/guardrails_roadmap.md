# Guardrails Implementation Roadmap

This document outlines the implementation plan for the nested loop graph architecture described in `guardrails.md`. The goal is to refactor the agent from a simple ReAct loop with bolted-on features to a structurally-enforced nested loop workflow with proper manager abstractions.

## Current State Analysis

### What We Have

```
src/agent/
├── agent.py              # Main UniversalAgent class
├── graph.py              # Current simple ReAct graph
├── api/
│   ├── app.py            # FastAPI application
│   └── models.py         # Pydantic models
├── core/
│   ├── state.py          # Agent state TypedDict
│   ├── loader.py         # Config loading
│   ├── context.py        # ContextManager + ProtectedContextProvider
│   ├── workspace.py      # WorkspaceManager (filesystem)
│   ├── todo.py           # TodoManager (stateful)
│   ├── transitions.py    # PhaseTransitionManager
│   └── archiver.py       # LLM archiver
└── tools/
    └── ...               # Tool implementations
```

### Problems to Solve

| Component | Current Approach | Problem |
|-----------|------------------|---------|
| Plan checking | `ProtectedContextProvider` injects plan | Hidden magic, no structural revisit |
| Todo display | Layer 2 injection via hook | Bolted on, not part of workflow |
| Phase transitions | Global `_last_transition_result` | Hacky side-channel communication |
| Memory | Hopes agent writes to files | No enforcement |
| Initialization | Mixed responsibilities | No clear separation |

---

## Target Architecture

### Manager Layer

Three focused managers built on WorkspaceManager:

```
┌─────────────────────────────────────────────────────────────┐
│                      Graph Nodes                            │
│  (init, read_plan, execute, check_todos, archive_phase)     │
└─────────────────────────────┬───────────────────────────────┘
                              │
        ┌─────────────────────┼─────────────────────┐
        │                     │                     │
        ▼                     ▼                     ▼
┌───────────────┐    ┌───────────────┐    ┌───────────────┐
│  TodoManager  │    │  PlanManager  │    │ MemoryManager │
│  (stateful)   │    │  (service)    │    │   (service)   │
│               │    │               │    │               │
│ - add()       │    │ - read()      │    │ - read()      │
│ - complete()  │    │ - write()     │    │ - write()     │
│ - list_all()  │    │ - exists()    │    │ - exists()    │
│ - archive()   │    │ - is_complete│    │ - get_section│
└───────┬───────┘    └───────┬───────┘    └───────┬───────┘
        │                     │                     │
        └─────────────────────┼─────────────────────┘
                              │
                              ▼
                    ┌───────────────────┐
                    │  WorkspaceManager │
                    │   (filesystem)    │
                    │                   │
                    │  - read_file()    │
                    │  - write_file()   │
                    │  - list_files()   │
                    └───────────────────┘
```

### Graph Flow

```
╔═══════════════════════════════════════════════════════════════════════════╗
║                         INITIALIZATION (runs once)                        ║
║                                                                           ║
║   init_workspace → read_instructions → create_plan → init_todos          ║
║                                          │                                ║
║                                          ▼                                ║
║                                   clear_history (fresh start)             ║
║                                                                           ║
╠═══════════════════════════════════════════════════════════════════════════╣
║                         OUTER LOOP (Strategic)                            ║
║                                                                           ║
║   ┌─────────────────────────────────────────────────────────────────┐    ║
║   │                      PLAN PHASE (sequential)                    │    ║
║   │                                                                 │    ║
║   │   read_plan → update_memory → create_todos                      │    ║
║   │                                                                 │    ║
║   └─────────────────────────────────────────────────────────────────┘    ║
║                                    ↓                                      ║
║   ┌─────────────────────────────────────────────────────────────────┐    ║
║   │                     EXECUTE PHASE (inner loop)                  │    ║
║   │                                                                 │    ║
║   │         ┌────────────────────────────────────────┐             │    ║
║   │         ↓                                        │             │    ║
║   │      execute ──→ check_todos ──→ todos done? ───no───┘         │    ║
║   │      (ReAct)           │                                       │    ║
║   │                       yes                                      │    ║
║   │                        ↓                                       │    ║
║   │                  archive_phase                                 │    ║
║   └─────────────────────────────────────────────────────────────────┘    ║
║                                    ↓                                      ║
║                            check_goal                                     ║
║                             ↓          ↓                                  ║
║                            no         yes                                 ║
║                             ↓          ↓                                  ║
║                    back to PLAN PHASE  END                               ║
║                                                                           ║
╚═══════════════════════════════════════════════════════════════════════════╝
```

### File-Based Memory

| File | Purpose | Injection |
|------|---------|-----------|
| `workspace.md` | Long-term memory (like CLAUDE.md) | Always in system prompt |
| `plan.md` | Strategic direction | Read only at phase transitions |
| `instructions.md` | Task instructions | Read once at initialization |
| `archive/` | Completed todos by phase | Written on phase completion |

---

## Implementation Phases

### Phase 1: Create Managers Package

Create the managers/ package with TodoManager, PlanManager, and MemoryManager.

#### 1.1 Create Package Structure

**Files to create:**
- `src/agent/managers/__init__.py`
- `src/agent/managers/todo.py`
- `src/agent/managers/plan.py`
- `src/agent/managers/memory.py`

#### 1.2 Implement TodoManager

**File:** `src/agent/managers/todo.py`

```python
class TodoManager:
    """Stateful manager for todo list operations."""

    def __init__(self, workspace: WorkspaceManager):
        self._workspace = workspace
        self._todos: List[TodoItem] = []

    def add(self, content: str, priority: str = "medium") -> TodoItem
    def complete(self, todo_id: str, notes: List[str] = None) -> TodoItem
    def list_all(self) -> List[TodoItem]
    def list_pending(self) -> List[TodoItem]
    def format_for_display(self) -> str  # Layer 2 format
    def archive(self, phase_name: str) -> str
    def clear(self) -> None
    def log_state(self) -> None  # For monitoring
```

Tasks:
- [ ] Create TodoItem dataclass (id, content, status, priority, notes, created_at)
- [ ] Implement CRUD operations (add, complete, list_all, list_pending)
- [ ] Implement format_for_display() for Layer 2 injection
- [ ] Implement archive() to write completed todos to workspace
- [ ] Add logging for state changes

#### 1.3 Implement PlanManager

**File:** `src/agent/managers/plan.py`

```python
class PlanManager:
    """Service for plan.md operations."""

    PLAN_FILE = "plan.md"

    def __init__(self, workspace: WorkspaceManager):
        self._workspace = workspace

    def exists(self) -> bool
    def read(self) -> str
    def write(self, content: str) -> None
    def is_complete(self, content: str = None) -> bool
```

Tasks:
- [ ] Implement basic file operations (exists, read, write)
- [ ] Implement is_complete() to check if all phases marked done
- [ ] Keep it simple - no strict format enforcement

#### 1.4 Implement MemoryManager

**File:** `src/agent/managers/memory.py`

```python
class MemoryManager:
    """Service for workspace.md (long-term memory) operations."""

    MEMORY_FILE = "workspace.md"

    def __init__(self, workspace: WorkspaceManager):
        self._workspace = workspace

    def exists(self) -> bool
    def read(self) -> str
    def write(self, content: str) -> None
    def get_section(self, section_name: str) -> Optional[str]
```

Tasks:
- [ ] Implement basic file operations (exists, read, write)
- [ ] Implement get_section() for extracting specific sections
- [ ] Keep it simple - workspace.md format is flexible

#### 1.5 Create Package Exports

**File:** `src/agent/managers/__init__.py`

```python
from .todo import TodoManager, TodoItem
from .plan import PlanManager
from .memory import MemoryManager

__all__ = ["TodoManager", "TodoItem", "PlanManager", "MemoryManager"]
```

Tasks:
- [ ] Export all manager classes
- [ ] Add type hints for IDE support

---

### Phase 2: Update State Schema

Update the agent state to support nested loops and managers.

#### 2.1 Add Loop Control Fields

**File:** `src/agent/core/state.py`

```python
class AgentState(TypedDict):
    # Core LangGraph state
    messages: Annotated[List[BaseMessage], add_messages]

    # Job identification
    job_id: str
    workspace_path: str

    # File-based context (read from workspace.md into state)
    workspace_memory: str       # Contents of workspace.md

    # Loop control
    phase_complete: bool        # Inner loop exit condition
    goal_achieved: bool         # Outer loop exit condition
    iteration_count: int        # Safety counter
    max_iterations: int         # Configurable limit
    initialized: bool           # Has initialization completed

    # Execution control
    error: Optional[Dict[str, Any]]
    should_stop: bool

    # Metadata
    metadata: Dict[str, Any]
```

Tasks:
- [ ] Add loop control fields (phase_complete, goal_achieved, iteration_count)
- [ ] Add workspace_memory field for system prompt injection
- [ ] Add initialized flag for initialization flow
- [ ] Update create_initial_state() to set new fields
- [ ] Keep backwards compatibility

---

### Phase 3: Build Nested Loop Graph

Build the new graph structure with initialization and nested loops.

#### 3.1 Create Initialization Nodes

**File:** `src/agent/graph.py`

Nodes:
- `init_workspace` - Create workspace.md from template
- `read_instructions` - Read instructions.md into context
- `create_plan` - LLM creates plan.md from instructions
- `init_todos` - Extract first phase todos from plan
- `clear_history` - Wipe message history for fresh start

Tasks:
- [ ] Implement init_workspace() node
- [ ] Implement read_instructions() node
- [ ] Implement create_plan() node (LLM call)
- [ ] Implement init_todos() node (LLM call)
- [ ] Implement clear_history() node
- [ ] Wire initialization sequence with edges

#### 3.2 Create Plan Phase Nodes

**File:** `src/agent/graph.py`

Nodes:
- `read_plan` - Read plan.md via PlanManager
- `update_memory` - LLM updates workspace.md with learnings
- `create_todos` - LLM extracts todos for current phase

Tasks:
- [ ] Implement read_plan() node
- [ ] Implement update_memory() node (LLM call)
- [ ] Implement create_todos() node (LLM call)
- [ ] Wire plan phase edges

#### 3.3 Create Execute Phase Nodes

**File:** `src/agent/graph.py`

Nodes:
- `execute` - ReAct subgraph for todo execution
- `check_todos` - Check if all todos complete
- `archive_phase` - Archive completed todos

Tasks:
- [ ] Create ReAct subgraph using create_react_agent()
- [ ] Implement check_todos() node
- [ ] Implement archive_phase() node
- [ ] Wire inner loop edges (conditional: todos_done?)

#### 3.4 Create Goal Check Node

**File:** `src/agent/graph.py`

Node:
- `check_goal` - Check if all plan phases complete

Tasks:
- [ ] Implement check_goal() node
- [ ] Wire outer loop edges (conditional: goal_achieved?)

#### 3.5 Integrate Managers into Graph

**File:** `src/agent/graph.py`

Tasks:
- [ ] Instantiate managers in build_agent_graph()
- [ ] Pass managers to nodes that need them
- [ ] Inject workspace.md into system prompt
- [ ] Remove ProtectedContextProvider usage

---

### Phase 4: Remove Obsolete Code

Clean up code that's no longer needed.

#### 4.1 Remove ProtectedContextProvider

**File:** `src/agent/core/context.py`

Tasks:
- [ ] Remove ProtectedContextProvider class
- [ ] Remove ProtectedContextConfig dataclass
- [ ] Remove is_layer2_message() helper
- [ ] Keep ContextManager class

#### 4.2 Simplify transitions.py

**File:** `src/agent/core/transitions.py`

Tasks:
- [ ] Remove PhaseTransitionManager class
- [ ] Remove transition prompt templates
- [ ] Remove get_bootstrap_todos()
- [ ] Keep TransitionType enum (if needed for logging)
- [ ] Consider removing file entirely if unused

#### 4.3 Clean Up todo_tools.py

**File:** `src/agent/tools/todo_tools.py`

Tasks:
- [ ] Remove global _last_transition_result
- [ ] Remove get_last_transition_result()
- [ ] Remove _set_transition_result()
- [ ] Simplify tools to use TodoManager directly

#### 4.4 Remove Old TodoManager

**File:** `src/agent/core/todo.py`

Tasks:
- [ ] Remove old TodoManager class
- [ ] Update imports to use managers.todo
- [ ] Delete file if empty

#### 4.5 Update Config

**File:** `src/agent/core/loader.py`

Tasks:
- [ ] Remove protected_context_* config fields
- [ ] Add any new config fields for nested loops
- [ ] Update AgentConfig dataclass

---

### Phase 5: Testing & Documentation

#### 5.1 Update Tests

**Files:** `tests/test_*.py`

Tasks:
- [ ] Add tests for TodoManager in managers/
- [ ] Add tests for PlanManager in managers/
- [ ] Add tests for MemoryManager in managers/
- [ ] Add tests for graph nodes
- [ ] Add integration test for full initialization flow
- [ ] Add integration test for nested loop execution
- [ ] Remove tests for ProtectedContextProvider
- [ ] Update tests that use old TodoManager

#### 5.2 Update Documentation

**Files:** `docs/guardrails.md`, `CLAUDE.md`

Tasks:
- [ ] Update CLAUDE.md with new file locations
- [ ] Add manager documentation to guardrails.md
- [ ] Add examples of how graph nodes work
- [ ] Document workspace.md format and purpose

---

## Final Structure

```
src/agent/
├── __init__.py
├── agent.py              # Main UniversalAgent class
├── graph.py              # Nested loop graph
│
├── api/
│   ├── app.py            # FastAPI application
│   └── models.py         # Pydantic models
│
├── core/
│   ├── state.py          # Agent state (with loop control)
│   ├── loader.py         # Config loading
│   ├── context.py        # ContextManager (simplified)
│   ├── workspace.py      # WorkspaceManager (filesystem)
│   └── archiver.py       # LLM archiver
│
├── managers/
│   ├── __init__.py
│   ├── todo.py           # TodoManager (stateful)
│   ├── plan.py           # PlanManager (service)
│   └── memory.py         # MemoryManager (service)
│
└── tools/
    └── ...               # Tool implementations
```

---

## Success Criteria

### Functional Requirements

1. Agent completes multi-phase jobs without wandering
2. workspace.md is always in the system prompt (like CLAUDE.md)
3. plan.md is read at the start of every phase
4. Context wipes don't cause agent to lose progress (file-based memory)
5. Phase transitions happen automatically at correct times
6. No hidden injection magic - all workflow visible in graph

### Non-Functional Requirements

1. Graph is visualizable in LangGraph Studio
2. Each manager has single responsibility
3. No global state or side-channel communication
4. Managers are testable in isolation
5. Existing functionality preserved (after updating tests)

---

## Risk Mitigation

| Risk | Mitigation |
|------|------------|
| Breaking existing functionality | Build incrementally, test each phase |
| ReAct subgraph complexity | Start with simple tools, add complexity incrementally |
| Manager coupling | Keep managers independent, only share WorkspaceManager |
| State management bugs | Add comprehensive logging at each node |
| LLM not following node instructions | Clear, simple prompts per node |

---

## Dependencies

### External

- `langgraph>=0.2.0` - For `create_react_agent` and subgraph support
- `langchain-core>=0.3.0` - Message types and tool bindings

### Internal

- WorkspaceManager must exist and work correctly
- Config schema may need updates for new options

---

## Phase Order

Phases are ordered by dependency:

1. **Phase 1** (Managers) must complete before Phase 3 (Graph)
2. **Phase 2** (State) must complete before Phase 3 (Graph)
3. **Phase 3** (Graph) must complete before Phase 4 (Cleanup)
4. **Phase 4** (Cleanup) can be done incrementally
5. **Phase 5** (Testing) should run throughout but complete after Phase 4

Each phase has clear checkpoints. Commit after each completed task to enable easy rollback if issues arise.
