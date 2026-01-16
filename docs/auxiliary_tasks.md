# Strategic Agent Architecture Refactor

## Critical Issue: Blind Planning LLM

### Problem Description

The current nested loop architecture has a fundamental flaw: **the planning/auxiliary LLM has no tools and is completely blind to the workspace**.

Looking at `src/graph.py`, the graph receives two LLM instances:

```python
def build_nested_loop_graph(
    llm: BaseChatModel,              # For auxiliary workflows (NO TOOLS)
    llm_with_tools: BaseChatModel,   # For execution (with tools bound)
    ...
)
```

The `llm` (without tools) is used by:
- `create_plan` - Creates main_plan.md
- `init_todos` - Extracts initial todos
- `update_memory` - Updates workspace.md
- `create_todos` - Extracts phase todos
- `archive_phase` - Summarizes context

### Symptoms

1. **Empty workspace.md**: The LLM updating workspace.md can't read files to know what to summarize. It only sees the current workspace.md content injected into the prompt.

2. **Generic plans**: The LLM creating plans can't explore the codebase or workspace. It only sees instructions.md content that was pre-read by a separate node.

3. **Weird file formatting**: Files contain content like:
   ```
   ```markdown
   <actual content>
   ```
   ```
   The LLM thinks it's in a chat UI outputting content, not writing file content directly. The `_extract_markdown_content()` helper (lines 102-136) is a band-aid trying to strip these wrappers.

4. **Disconnect from execution artifacts**: Files created during tactical execution are invisible to the planning LLM. It can't see what the agent actually produced.

### Root Cause

The comment at line 1384 reveals this was intentional:
> `llm: LLM for planning/memory updates (no tools)`

The design goal was probably to keep planning "focused" and prevent the LLM from going off on tangents. But this completely undermines the workspace-centric architecture - the planning agent can't actually work with the workspace.

---

## Proposed Solution: Single ReAct Loop with Phase Alternation

Instead of maintaining two separate LLMs (one with tools, one without), we use **one ReAct loop for everything**. The "nested loop" emerges from orchestration logic that alternates between strategic and tactical phases.

### Core Insight

Strategic work (planning, memory updates, todo creation) is itself a tactical job. The agent just needs different todos and a slightly different tool set.

### Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                         ORCHESTRATOR                            │
│                                                                 │
│   while not job_complete:                                       │
│       run_react_loop(current_todos, current_phase)              │
│       clear_conversation_history()                              │
│       flip_phase()                                              │
│       current_todos = load_next_todos()                         │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                     SINGLE REACT LOOP                           │
│                                                                 │
│   Same graph structure                                          │
│   Tools swapped based on phase                                  │
│   Prompts adjusted based on phase                               │
│   Todo list determines the work                                 │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### Phase Flow

```
JOB START
    │
    ▼
┌─────────────────────────────────────────┐
│ INITIAL STRATEGIC PHASE                 │
│                                         │
│ Predefined todos:                       │
│ □ Explore workspace, populate workspace.md
│ □ Create execution plan from instructions
│ □ Divide plan into phases               │
│ □ Write todos for first phase → todos.yaml
│                                         │
│ On last todo complete:                  │
│   → Validate todos.yaml (5-20 items)    │
│   → Clear messages                      │
│   → Load todos from todos.yaml          │
│   → Flip to tactical                    │
└─────────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────────┐
│ TACTICAL PHASE 1                        │
│                                         │
│ Todos from todos.yaml:                  │
│ □ [agent-created todo 1]                │
│ □ [agent-created todo 2]                │
│ □ ...                                   │
│                                         │
│ On last todo complete:                  │
│   → Archive todos                       │
│   → Clear messages                      │
│   → Load predefined strategic todos     │
│   → Flip to strategic                   │
└─────────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────────┐
│ TRANSITION STRATEGIC PHASE              │
│                                         │
│ Predefined todos:                       │
│ □ Summarize previous session            │
│ □ Update workspace.md with learnings    │
│ □ Update main_plan.md (mark progress)   │
│ □ Write todos for next phase → todos.yaml
│   (or call job_complete if done)        │
│                                         │
│ On last todo complete:                  │
│   → Validate todos.yaml                 │
│   → Clear messages                      │
│   → Load todos from todos.yaml          │
│   → Flip to tactical                    │
└─────────────────────────────────────────┘
    │
    ▼
TACTICAL PHASE 2 ... continues until job_complete
```

---

## Implementation Details

### State Structure

```python
class AgentState(TypedDict):
    messages: list[BaseMessage]
    todos: list[Todo]
    is_strategic_phase: bool  # Flips each transition
    phase_number: int         # For logging/tracking
    job_id: str
    workspace_path: Path
```

### Tool Sets by Phase

| Tool | Strategic | Tactical | Notes |
|------|:---------:|:--------:|-------|
| `read_file` | ✓ | ✓ | Workspace access |
| `write_file` | ✓ | ✓ | Workspace access |
| `list_files` | ✓ | ✓ | Workspace access |
| `search_files` | ✓ | ✓ | Workspace access |
| `todo_complete` | ✓ | ✓ | Mark todos done |
| `todo_write` | ✓ | ✗ | Creates todos.yaml |
| `job_complete` | ✓ | ✗ | Ends the job |
| Domain tools | ✗ | ✓ | Job-specific (extract, cypher, etc.) |

**Key restrictions:**
- Tactical agent cannot call `job_complete` (prevents premature termination)
- Tactical agent cannot call `todo_write` (strategic agent owns planning)
- Strategic agent has no domain tools (focused on planning, not execution)

### Transition Logic

#### Strategic → Tactical Transition

When the agent marks the last strategic todo complete:

```python
def on_strategic_phase_complete(state: AgentState, workspace: WorkspaceManager) -> AgentState:
    """Validate todos.yaml and transition to tactical phase."""

    # Check 1: File exists
    todos_path = workspace.path / "todos.yaml"
    if not todos_path.exists():
        return reject_transition(state, "todos.yaml not found. Create it with todo_write.")

    # Check 2: Valid YAML
    try:
        data = yaml.safe_load(todos_path.read_text())
    except yaml.YAMLError as e:
        return reject_transition(state, f"Invalid YAML: {e}")

    # Check 3: Correct structure
    if "todos" not in data or not isinstance(data["todos"], list):
        return reject_transition(state, "todos.yaml must have a 'todos' list.")

    # Check 4: 5-20 todos
    todo_count = len(data["todos"])
    if not (5 <= todo_count <= 20):
        return reject_transition(state, f"Expected 5-20 todos, got {todo_count}.")

    # All checks passed - transition
    return AgentState(
        messages=[],  # Clear conversation history
        todos=parse_todos(data["todos"]),
        is_strategic_phase=False,
        phase_number=state["phase_number"] + 1,
        job_id=state["job_id"],
        workspace_path=state["workspace_path"],
    )


def reject_transition(state: AgentState, reason: str) -> AgentState:
    """Reject transition and return error to agent."""
    # Keep the last todo incomplete
    state["todos"][-1].status = "in_progress"

    # Add error message to conversation
    error_msg = ToolMessage(
        content=f"Phase transition rejected: {reason}",
        tool_call_id="phase_transition",
    )
    state["messages"].append(error_msg)

    return state
```

#### Tactical → Strategic Transition

When the agent marks the last tactical todo complete:

```python
def on_tactical_phase_complete(state: AgentState, workspace: WorkspaceManager) -> AgentState:
    """Archive todos and transition to strategic phase."""

    # Archive completed todos
    archive_path = workspace.path / "archive" / f"phase_{state['phase_number']}.yaml"
    archive_todos(state["todos"], archive_path)

    # Load predefined strategic todos
    strategic_todos = get_transition_strategic_todos()

    return AgentState(
        messages=[],  # Clear conversation history
        todos=strategic_todos,
        is_strategic_phase=True,
        phase_number=state["phase_number"] + 1,
        job_id=state["job_id"],
        workspace_path=state["workspace_path"],
    )
```

### Predefined Strategic Todos

```python
def get_initial_strategic_todos() -> list[Todo]:
    """Todos for the first strategic phase (job start)."""
    return [
        Todo(id=1, content="Explore the workspace and populate workspace.md with an overview of the environment, available tools, and any existing context."),
        Todo(id=2, content="Read the instructions.md file and create an execution plan in main_plan.md. The plan should outline the phases needed to complete the task."),
        Todo(id=3, content="Divide the plan into phases, where each phase contains 5-20 concrete, actionable todos."),
        Todo(id=4, content="Write the todos for the first phase to todos.yaml using the todo_write tool."),
    ]


def get_transition_strategic_todos() -> list[Todo]:
    """Todos for strategic phases between tactical phases."""
    return [
        Todo(id=1, content="Summarize what was accomplished in the previous tactical phase. Note any issues encountered, decisions made, or discoveries."),
        Todo(id=2, content="Update workspace.md with new learnings, patterns discovered, or important context for future phases."),
        Todo(id=3, content="Update main_plan.md to mark completed phases and adjust upcoming phases if needed based on learnings."),
        Todo(id=4, content="Write todos for the next phase to todos.yaml, or call job_complete if the plan is fully executed."),
    ]
```

### todos.yaml Format

```yaml
phase: "Phase 1: Extract requirements from document"
description: "Process the uploaded PDF and extract all compliance requirements."
todos:
  - id: 1
    content: "Extract text from the uploaded PDF document"
  - id: 2
    content: "Identify sections related to GoBD compliance"
  - id: 3
    content: "Extract individual requirements from each section"
  - id: 4
    content: "Validate extracted requirements against known patterns"
  - id: 5
    content: "Write requirements to the database using add_requirement tool"
```

**Validation rules:**
- Must be valid YAML
- Must have `todos` key with a list value
- Must have 5-20 todo items
- Each todo must have `id` (int) and `content` (str)

### System Prompts by Phase

```python
def get_system_prompt(state: AgentState, workspace_md: str) -> str:
    """Generate system prompt based on current phase."""

    if state["is_strategic_phase"]:
        return f"""You are in STRATEGIC MODE. Your job is to plan and organize work.

Current phase: {state['phase_number']} (Strategic)

## Your Responsibilities
- Explore and understand the workspace
- Create and maintain the execution plan
- Break down work into manageable phases
- Update workspace.md with learnings
- Create clear, actionable todos for the tactical agent

## Workspace Memory
{workspace_md}

## Important
- Use workspace tools to read files before making decisions
- Write todos to todos.yaml (5-20 items per phase)
- Call job_complete when the entire plan is executed
- Do NOT execute domain-specific work - that's for tactical mode
"""
    else:
        return f"""You are in TACTICAL MODE. Your job is to execute the current phase.

Current phase: {state['phase_number']} (Tactical)

## Your Responsibilities
- Work through each todo systematically
- Use domain tools to accomplish tasks
- Update workspace files as needed
- Mark todos complete as you finish them

## Workspace Memory
{workspace_md}

## Important
- Focus on the current todo list
- Do NOT create new todos or modify the plan
- Do NOT call job_complete - that's for strategic mode
- When all todos are done, the system will transition to strategic mode
"""
```

---

## Configuration

### Config Structure

The single-loop approach simplifies configuration. Instead of separate tactical/strategic configs, we have phase-specific tool lists:

```
src/config/
├── defaults.json           # Base framework defaults
├── schema.json             # Validation schema
└── prompts/
    ├── strategic_system.md # Strategic phase prompt
    └── tactical_system.md  # Tactical phase prompt

configs/
├── creator/
│   ├── config.json         # Creator job config
│   └── instructions.md     # Task instructions
│
└── validator/
    ├── config.json         # Validator job config
    └── instructions.md     # Task instructions
```

### Job Config Example

```json
{
  "$extends": "defaults",
  "job_type": "creator",
  "display_name": "Creator",

  "llm": {
    "model": "gpt-4o",
    "reasoning_level": "high"
  },

  "tools": {
    "workspace": ["read_file", "write_file", "list_files", "search_files"],
    "strategic": ["todo_write", "job_complete"],
    "tactical": ["todo_complete"],
    "domain": ["extract_document_text", "chunk_document", "web_search", "add_requirement"]
  },

  "phase_settings": {
    "min_todos": 5,
    "max_todos": 20,
    "archive_on_transition": true
  },

  "connections": {
    "postgres": true,
    "neo4j": false
  }
}
```

**Tool resolution by phase:**
- Strategic: `workspace` + `strategic` + `todo_complete`
- Tactical: `workspace` + `tactical` + `domain`

---

## Conversation History Management

### The Reset Model

Each phase is a **separate conversation** with fresh message history. Context passes through files, not message history.

```
┌─────────────────────────────────────────────────────────────────┐
│ PHASE A (Strategic)                                             │
│                                                                 │
│ messages: [HumanMessage, AIMessage, ToolMessage, ...]          │
│                                                                 │
│ → Completes last todo                                           │
│ → Writes todos.yaml                                             │
│ → System validates and clears messages                          │
└─────────────────────────────────────────────────────────────────┘
                              │
                        messages = []
                        load todos.yaml
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│ PHASE B (Tactical) - FRESH START                                │
│                                                                 │
│ messages: []  ← starts empty!                                   │
│ System prompt includes workspace.md                             │
│                                                                 │
│ → Works through todos with domain tools                         │
│ → Completes last todo                                           │
│ → System archives and clears messages                           │
└─────────────────────────────────────────────────────────────────┘
                              │
                        messages = []
                        load strategic todos
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│ PHASE C (Strategic) - FRESH START                               │
│                                                                 │
│ messages: []  ← starts empty!                                   │
│                                                                 │
│ → Summarizes previous work                                      │
│ → Updates workspace.md                                          │
│ → Creates next phase todos                                      │
│ → ... cycle continues                                           │
└─────────────────────────────────────────────────────────────────┘
```

### What Persists vs What Resets

| Component | Persists Across Phases? | Mechanism |
|-----------|-------------------------|-----------|
| `workspace.md` | **Yes** | File on disk, injected into system prompt |
| `main_plan.md` | **Yes** | File on disk, read/updated by strategic agent |
| `todos.yaml` | **Temporary** | Written by strategic, consumed by tactical |
| `archive/` | **Yes** | Historical todos stored per phase |
| Conversation history | **No** | Fresh `[]` each phase |
| Tool results | **No** | Part of conversation history, cleared |
| Files in workspace | **Yes** | Persist on disk, accessible via tools |

### Memory Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        LONG-TERM MEMORY                         │
│                      (persists across job)                      │
│                                                                 │
│  workspace.md    - Accumulated learnings, patterns, decisions  │
│  main_plan.md    - Overall strategy, phase structure            │
│  archive/        - Historical todos and phase summaries         │
│  artifacts/      - Files created during execution               │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
                              │
                              │ injected into system prompt
                              │ accessible via tools
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                       WORKING MEMORY                            │
│                    (cleared each phase)                         │
│                                                                 │
│  messages[]      - Current conversation history                 │
│  tool results    - Intermediate outputs                         │
│  current todos   - Active task list for this phase              │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
                              │
                              │ summarized at phase end
                              │ written to workspace.md by strategic agent
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                       PHASE HANDOFF                             │
│                 (bridges between phases)                        │
│                                                                 │
│  todos.yaml      - Next phase todos (strategic → tactical)      │
│  workspace.md    - Updated context (strategic → tactical)       │
│  archive/        - Phase history (tactical → strategic)         │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### Mid-Phase Context Compaction

Within a single phase, the conversation can still grow large. We keep the existing compaction mechanism as a fallback:

```python
"limits": {
    "context_threshold_tokens": 80000
}
```

**Two-level context management:**

| Level | Trigger | Action |
|-------|---------|--------|
| **Phase boundary** | Last todo completed | Full reset, `messages: []`, file-based handoff |
| **Mid-phase** | Token count > 80,000 | Summarize older messages, keep recent context |

Most phases should complete before hitting 80k tokens. Compaction is insurance for complex phases with many tool calls.

---

## Implementation Checklist

### Phase 0: Foundation
- [ ] Add `is_strategic_phase: bool` to `AgentState` in `src/core/state.py`
- [ ] Add `phase_number: int` to `AgentState`
- [ ] Create `get_initial_strategic_todos()` helper
- [ ] Create `get_transition_strategic_todos()` helper
- [ ] Define `todos.yaml` schema and validation function

### Phase 1: Tool System Updates
- [ ] Create `todo_write` tool (writes todos.yaml)
- [ ] Update `todo_complete` to detect last-todo completion
- [ ] Ensure `job_complete` tool exists and works
- [ ] Implement tool filtering by phase in registry
- [ ] Update tool binding to use phase-aware filtering

### Phase 2: Transition Logic
- [ ] Implement `on_strategic_phase_complete()` with validation
- [ ] Implement `on_tactical_phase_complete()` with archiving
- [ ] Implement `reject_transition()` error handling
- [ ] Add transition hooks to the ReAct loop
- [ ] Test phase toggle behavior

### Phase 3: Prompt System
- [ ] Create `prompts/strategic_system.md`
- [ ] Create `prompts/tactical_system.md`
- [ ] Implement `get_system_prompt()` phase-aware loader
- [ ] Update graph to use dynamic system prompts

### Phase 4: Orchestration
- [ ] Update job runner to initialize with strategic todos
- [ ] Implement message clearing on phase transition
- [ ] Implement todo loading from todos.yaml
- [ ] Add phase logging and tracking
- [ ] Test full strategic → tactical → strategic cycle

### Phase 5: Cleanup & Migration
- [ ] Remove old `build_nested_loop_graph()` nested loop logic
- [ ] Remove toolless `llm` parameter from graph builder
- [ ] Update configs to use new tool categories
- [ ] Update CLAUDE.md documentation
- [ ] Add integration tests for phase alternation

---

## Benefits

1. **One ReAct loop**: No separate graph for planning. Same agent, different modes.

2. **Strategic agent has tools**: Can read files, explore workspace, make informed decisions.

3. **Clean context management**: Each phase starts fresh with only relevant context.

4. **Proper file formatting**: Agent writes through tools, no markdown-wrapper issues.

5. **Incremental improvements**: workspace.md accumulates knowledge across phases.

6. **Validation gates**: Transition only happens when todos.yaml meets criteria.

7. **Debuggability**: Clear phase boundaries make it easy to identify issues.

8. **Simpler codebase**: One graph structure instead of nested loops with special nodes.

---

## Comparison: Before vs After

| Aspect | Before (Nested Loop) | After (Phase Alternation) |
|--------|---------------------|---------------------------|
| Graph structure | Complex nested loop with special nodes | Single ReAct loop |
| Planning LLM | No tools, blind to workspace | Full tool access |
| Memory updates | Single LLM call, can't read files | Agent workflow with tools |
| Context management | Compaction only | Phase reset + compaction fallback |
| Configuration | Two LLM instances | One LLM, phase-specific tools |
| Todo creation | Hardcoded node extracts from LLM output | Agent writes todos.yaml |
| Validation | None | 5-20 todos required for transition |
