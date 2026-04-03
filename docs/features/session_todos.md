---
tags:
  - architecture
  - agent
  - sessions
  - todos
aliases:
  - shared todos
  - session task tracking
related:
  - "[[sessions]]"
  - "[[persistent_agent_assessment]]"
  - "[[agent_lifecycle]]"
---

# Shared Todo List for Persistent Sessions

Bidirectional, real-time task list shared between the persistent agent and the user.

**Status:** Design phase.

## Problem

The persistent agent has no task tracking. When the agent works on a multi-step problem, neither the agent nor the user has visibility into what's planned, what's done, and what's left. The worker agent has a full `TodoManager` + cockpit UI, but it's tightly coupled to the phase alternation model (staging, archiving, min/max counts, phase transitions). The persistent agent needs the same CRUD capability without the phase machinery.

Additionally, no competing product offers a **bidirectional** shared todo list — Claude Code, Cursor, Codex all have internal task tracking that the user can observe but not edit. Making this collaborative (both sides can add, edit, check off items) would be a differentiator.

## What Already Exists

| Component | Reuse | What's missing |
|---|---|---|
| **TodoManager** (`src/managers/todo.py`) | `add()`, `complete()`, `start()`, `get()`, `list_all()`, `list_pending()`, `get_progress()`, `TodoItem` dataclass, `export_state()`/`restore_state()` | Freeform mode flag to skip phase validation |
| **TodoItem** dataclass | `id`, `content`, `status`, `priority`, `notes`, `created_at`, `to_dict()`/`from_dict()` | `remove()` method on manager |
| **Cockpit todo UI** (`todo-list.component.ts`) | Progress bar, status icons, priority badges, notes display | It's read-only and wired to worker jobs, not persistent chat |
| **WebSocket transport** (`persistent_app.py`) | Bidirectional JSON-RPC, already handles `interrupt`, `mode.set`, `compact` | New `todo.*` methods |
| **threads.metadata JSONB** | Already used for `workspace_container`, `vm`, `snapshot` | Add `todos` key for persistence |
| **Worker todo tools** (`src/tools/core/todo.py`) | Tool registration pattern, `ToolContext` integration | New lightweight tool set |

## Design

### 1. TodoManager: Freeform Mode

Add a `freeform` flag to `TodoManager.__init__()`. When `True`:
- No min/max validation on todo count
- No staging (`stage_tactical_todos` / `apply_staged_todos`) — direct add
- No phase tracking (`is_strategic_phase`, `phase_number`, `increment_phase_number`)
- No archiving to workspace filesystem
- `format_for_injection()` returns a simpler format (no phase header, no phase-specific tool guide)
- New `remove(todo_id)` method (worker todos can't be deleted — freeform ones can)
- New `update(todo_id, content?, priority?)` method
- New `clear_all()` method (remove every item at once)
- Optional `on_change` callback — fired after every mutation for persistence + WS broadcast

Add a `created_by` field to `TodoItem` (`"agent"` or `"user"`, default `"agent"`). This is only used for UI display and has no effect on the manager logic.

The existing phase-mode behavior is unchanged when `freeform=False` (default).

```python
@dataclass
class TodoItem:
    # ... existing fields ...
    created_by: str = "agent"  # "agent" or "user"

class TodoManager:
    def __init__(
        self,
        workspace: "WorkspaceManager",
        min_todos: int = 5,
        max_todos: int = 20,
        freeform: bool = False,            # NEW
        on_change: Callable[[], None] = None,  # NEW — fired after every mutation
    ):
        self._freeform = freeform
        self._on_change = on_change
        if freeform:
            self._min_todos = 0
            self._max_todos = 999  # effectively unlimited
        # ... rest unchanged

    def _notify(self) -> None:
        """Fire on_change callback if registered."""
        if self._on_change:
            self._on_change()

    def add(self, content, priority="medium", created_by="agent") -> TodoItem:
        # ... existing logic ...
        self._notify()
        return item

    def complete(self, todo_id, notes=None) -> Optional[TodoItem]:
        # ... existing logic ...
        self._notify()
        return todo

    def remove(self, todo_id: str) -> Optional[TodoItem]:
        """Remove a todo entirely. Available in both modes but only
        exposed as a tool in freeform (session) mode."""
        for i, todo in enumerate(self._todos):
            if todo.id == todo_id:
                removed = self._todos.pop(i)
                self._notify()
                return removed
        return None

    def update(self, todo_id: str, content: str = None, priority: str = None) -> Optional[TodoItem]:
        """Update a todo's content or priority."""
        todo = self.get(todo_id)
        if not todo:
            return None
        if content is not None:
            todo.content = content
        if priority is not None:
            todo.priority = priority
        self._notify()
        return todo

    def clear_all(self) -> int:
        """Remove all todos. Returns count of removed items."""
        count = len(self._todos)
        self._todos = []
        self._next_id = 1
        if count:
            self._notify()
        return count
```

The `on_change` callback is the single integration point for persistence and real-time sync. Set during session setup, it handles both the DB write and the WebSocket `todos.updated` emission (see sections 3 and 5).

### 2. Agent Tools

New tool set for persistent sessions, registered in `src/tools/core/session_todo.py`. Tool names are prefixed with `session_` to avoid collisions with the worker's phase-based tools (`todo_complete`, `todo_list`) which share the same registry:

| Tool | Signature | Description |
|------|-----------|-------------|
| `session_todo_add` | `(items: list[str], priority: str = "medium")` | Add one or more items. Returns the updated list. |
| `session_todo_complete` | `(todo_id: str, notes: str = "")` | Mark an item done with optional notes. |
| `session_todo_update` | `(todo_id: str, content: str = None, priority: str = None)` | Edit an item's content or priority. |
| `session_todo_remove` | `(todo_id: str)` | Delete an item. |
| `session_todo_clear` | `()` | Remove all items (start fresh). |
| `session_todo_list` | `()` | Return the current list with progress. |

The persistent session's `_EXCLUDED_TOOLS` continues to block all phase tools. The session tools are added to the tool list instead. Both sets coexist in the registry without conflict.

The agent can use these proactively (e.g., breaking down a complex request into steps) or on user request ("make a plan for X"). The system prompt should encourage the agent to create todos for multi-step work but not mandate it for simple questions.

### 3. State Persistence

**Worker agent**: Todo state lives in the LangGraph checkpoint (SQLite). `export_state()` serializes to dict, stored in the state graph, restored on resume via `restore_state()`.

**Persistent session**: No LangGraph checkpoints. Instead, persist to the thread's `metadata.todos` JSONB. The format matches `export_state()` output (minus phase-only fields):

```json
{
  "todos": {
    "todos": [
      {"id": "todo_1", "content": "Set up test fixtures", "status": "completed", "priority": "high", "notes": ["Done with pytest"], "created_by": "agent", "created_at": "2026-04-01T10:00:00Z"},
      {"id": "todo_2", "content": "Write integration tests", "status": "in_progress", "priority": "medium", "notes": [], "created_by": "user", "created_at": "2026-04-01T10:05:00Z"}
    ],
    "next_id": 3
  }
}
```

**When to persist**: Driven by the `on_change` callback (section 1). The callback fires after every mutation (add, complete, update, remove, clear). It calls `save_thread_todo_state()` — an atomic `jsonb_set` on `threads.metadata.todos` using `export_state()`. Fire-and-forget, same pattern as message persistence.

**On session resume**: Load `metadata.todos` from the thread row, call `TodoManager.restore_state()`. The todo state survives server restarts, pod rescheduling, and reconnects.

### 4. Context Injection

Todos are injected into the agent's context the same way `workspace.md` is — as a transient fake tool result that survives context compaction. `TodoManager.format_for_injection()` already does this for the worker; the freeform variant produces a simpler format:

```
Current Tasks (3/5 complete)

Completed:
  - [x] todo_1: Set up test fixtures
  - [x] todo_2: Write unit tests
  - [x] todo_3: Fix linting errors

In Progress:
  - [>] todo_4: Write integration tests

Pending:
  - [ ] todo_5: Update documentation

Tools: Use session_todo_complete(todo_id="<id>") to mark done, session_todo_add(items=["..."]) to add tasks.
```

Injected every turn in `_execute_turn()`, right after workspace.md injection. Only injected when the list is non-empty.

### 5. WebSocket Protocol

New methods on the existing persistent session WebSocket:

**Server → Client events:**
```jsonc
// Emitted after any todo mutation (agent tool call OR user action)
{
  "type": "event",
  "event": "todos.updated",
  "data": {
    "todos": [...],          // Full list (TodoItem dicts)
    "progress": {            // Convenience
      "total": 5,
      "completed": 3,
      "pending": 2,
      "percentage": 60.0
    },
    "source": "agent"        // "agent" or "user" — who made the change
  }
}
```

**Client → Server methods:**
```jsonc
// Add todos
{"method": "todo.add", "params": {"items": ["Write tests", "Fix bug"], "priority": "medium"}}

// Complete a todo
{"method": "todo.complete", "params": {"todo_id": "todo_1", "notes": "Done"}}

// Update a todo
{"method": "todo.update", "params": {"todo_id": "todo_2", "content": "Updated text", "priority": "high"}}

// Remove a todo
{"method": "todo.remove", "params": {"todo_id": "todo_3"}}
```

**Both paths converge on `on_change`:** Whether a mutation comes from a user WS method or an agent tool call, the TodoManager's `on_change` callback fires, which (a) persists to DB and (b) emits `todos.updated` to the client. The user WS handler just calls the same `TodoManager.add()` / `complete()` / etc. methods the tools do — no separate persistence logic.

The agent sees the updated list on its next turn via context injection (section 4). If the user adds a todo while the agent is mid-turn, the agent won't see it until the next turn — this is fine because asyncio is single-threaded and the injection happens at turn start.

**Concurrency note:** The persistent loop is `asyncio` single-threaded. WS message handlers and tool execution never run truly in parallel — `await` points interleave them cooperatively. TodoManager mutations are synchronous, so no locking is needed.

### 6. Cockpit UI

**New component:** `cockpit/src/app/shared/components/session-todo/session-todo.component.ts`

Separate from the existing `todo-list.component.ts` (which is read-only and phase-oriented). The session todo component is interactive:

- **Add**: Input field at the top with priority selector (default: medium)
- **Check off**: Click checkbox → sends `todo.complete` via WS
- **Edit inline**: Click content text → inline edit → sends `todo.update` via WS
- **Delete**: Trash icon → sends `todo.remove` via WS (with confirm for completed items)
- **Reorder**: Drag handle (optional, deferred — priority covers most ordering needs)
- **Progress bar**: Same style as existing todo UI
- **Source indicator**: Small "agent" or "you" label per item, driven by `TodoItem.created_by`
- **Collapse/expand**: The panel can be collapsed to just the progress bar

**Placement:** Embedded in the persistent chat view as a collapsible sidebar panel or top section. The component is self-contained so users can position it in their debug dashboard layout. Receives the WebSocket service as input; subscribes to `todos.updated` events.

**Loading state:** On connect, the initial todo state comes from the first `todos.updated` event (emitted by the server on WebSocket open if todos exist). No separate REST endpoint needed — the WS is already open before the user interacts.

### 7. PersistentSession Integration

In `src/api/persistent_session.py`, the `PersistentSession` dataclass gets a TodoManager:

```python
@dataclass
class PersistentSession:
    # ... existing fields ...
    todo_manager: Optional[TodoManager] = None

    async def setup(
        self,
        llm,
        auxiliary_llm=None,
        postgres_conn=None,
        vector_conn=None,
        workspace_override=None,
        git_remote_url=None,
        thread_metadata=None,    # NEW — passed from persistent_app.py on connect
        on_todo_change=None,     # NEW — callback for persistence + WS broadcast
    ) -> None:
        # ... existing setup (steps 1-7) ...

        # 8. Set up freeform TodoManager
        self.todo_manager = TodoManager(
            workspace=self.workspace_manager,
            freeform=True,
            on_change=on_todo_change,
        )
        # Restore from thread metadata if resuming
        if thread_metadata and thread_metadata.get("todos"):
            self.todo_manager.restore_state(thread_metadata["todos"])

        # Wire into tool context
        if self.tool_context:
            self.tool_context.todo_manager = self.todo_manager
```

The `on_todo_change` callback is created in `persistent_app.py` at WS connect time, closing over both the DB connection and the WebSocket send function. This keeps the TodoManager and PersistentSession decoupled from transport concerns.

The `_EXCLUDED_TOOLS` set keeps blocking all phase-specific tools. The `session_todo_*` tools use distinct names, so no exclusion changes are needed — they're simply included in the tool list:

```python
_EXCLUDED_TOOLS = frozenset({
    "next_phase_todos",   # Phase staging
    "todo_complete",      # Phase-aware completion (signals [PHASE_COMPLETE])
    "todo_list",          # Phase-aware listing (includes phase header)
    "todo_rewind",        # Phase re-planning
    "mark_complete",      # Phase completion signal
    "job_complete",       # Job completion signal
})
# session_todo_* tools are separate registrations — no collision.
```

## Implementation Phases

### Phase 1: Agent-Side Todos (Core)

- [ ] Add `created_by` field to `TodoItem` dataclass (default `"agent"`)
- [ ] Add `freeform` mode to `TodoManager` (flag, `remove()`, `update()`, `clear_all()`, skip phase validation)
- [ ] Add `on_change` callback to `TodoManager` (fired after every mutation)
- [ ] Add `save_thread_todo_state()` to `orchestrator/database/postgres.py` (atomic `jsonb_set` on `metadata.todos`)
- [ ] Create `src/tools/core/session_todo.py` with `session_todo_add`, `session_todo_complete`, `session_todo_update`, `session_todo_remove`, `session_todo_clear`, `session_todo_list`
- [ ] Register new tools in `src/tools/registry.py`
- [ ] Wire `TodoManager(freeform=True)` into `PersistentSession.setup()` with `on_change` callback
- [ ] Add `thread_metadata` parameter to `PersistentSession.setup()` for restoring state on resume
- [ ] Inject todos into `_execute_turn()` context (transient, like workspace.md)

### Phase 2: Real-Time Sync (WebSocket)

- [ ] Add `todo.*` method handlers in `persistent_app.py` (calling the same TodoManager methods the tools use)
- [ ] Wire `on_change` callback in `persistent_app.py` to emit `todos.updated` event via WS
- [ ] Emit initial `todos.updated` on WS connect (if todos exist)

### Phase 3: Cockpit UI

- [ ] Create `session-todo.component.ts` (add, check off, inline edit, delete, clear, progress bar)
- [ ] Subscribe to `todos.updated` events from `PersistentChatService`
- [ ] Send `todo.*` methods via `PersistentChatService`
- [ ] Embed in persistent chat view as collapsible panel
- [ ] Source indicator per item driven by `created_by` field

## Open Questions

**1. Todo limits.** Should there be a soft cap (e.g., 50 items) before the agent is nudged to clean up? Large lists dilute context injection value. Leaning toward yes — the injection format becomes noise past ~30 items.

**2. Subtasks.** Worth supporting nested todos (parent/child)? Adds complexity to the data model, injection format, and UI. Start flat; add nesting later if real usage demands it.

**3. Slash command.** Should `/plan <description>` trigger the agent to auto-create a todo list? Natural UX but adds client-side parsing. Could also just be a user message ("plan out X") that the agent handles with its tools — no special parsing needed.

**4. History.** When the user checks off a todo, should it stay visible (struck through) or collapse into a "completed" section? The worker UI shows completed items — same here, with completed items grouped at the bottom.

**5. Freeform `format_for_injection()`.** The worker's version includes a tool usage guide referencing `todo_complete`, `todo_rewind`, `mark_complete`. The freeform variant should reference the `session_todo_*` names instead. Straightforward but easy to forget.
