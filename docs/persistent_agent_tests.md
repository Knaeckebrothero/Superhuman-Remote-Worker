# Persistent Agent System — Unit Test Plan

Status: **No tests exist.** ~6,500 lines of Python and ~2,200 lines of TypeScript with zero coverage.

## Suggested Test Files

| Source File | Test File |
|-------------|-----------|
| `src/persistent_graph.py` | `tests/test_persistent_graph.py` |
| `src/api/persistent_session.py` | `tests/test_persistent_session.py` |
| `src/api/persistent_app.py` | `tests/test_persistent_app.py` |
| `src/api/orchestrator_client.py` | `tests/test_orchestrator_client.py` (extend existing) |
| `orchestrator/services/persistent_provisioner.py` | `tests/test_persistent_provisioner.py` |
| `orchestrator/main.py` (thread endpoints) | `tests/test_thread_endpoints.py` |
| `cockpit/.../persistent-chat.service.ts` | `cockpit/.../persistent-chat.service.spec.ts` |
| `src/services/knowledge_graph.py` | `tests/test_knowledge_graph.py` |
| `src/services/knowledge_store.py` | `tests/test_knowledge_store.py` |
| `src/core/knowledge_injection.py` | `tests/test_knowledge_injection.py` |
| `src/tools/knowledge/knowledge_tools.py` | `tests/test_knowledge_tools.py` |
| `src/tools/knowledge/workspace_converter.py` | `tests/test_workspace_converter.py` |
| `src/tools/knowledge/memory_migrator.py` | `tests/test_memory_migrator.py` |
| `orchestrator/main.py` (knowledge endpoints) | `tests/test_knowledge_endpoints.py` |
| `cockpit/.../sessions-page.component.ts` | `cockpit/.../sessions-page.component.spec.ts` |

## Common Mock Fixtures

Most tests in this plan share a small set of mocks. Create these as reusable fixtures
in `conftest.py` or at the top of each test file:

- **`mock_llm`**: `AsyncMock` of `BaseChatModel` with `.astream()` yielding `AIMessage` chunks and `.ainvoke()` returning an `AIMessage`. Should support `.bind_tools()` returning self.
- **`mock_tool`**: `MagicMock` with `.name = "test_tool"` and `.ainvoke = AsyncMock(return_value="result")`.
- **`mock_callbacks`**: `PersistentLoopCallbacks` with all fields as `AsyncMock` / `MagicMock`. `check_interrupt` returns `False`, `permission_check` returns `True`, `get_user_input` returns from a queue.
- **`mock_context_manager`**: `AsyncMock` of `ContextManager`. `ensure_within_limits()` returns input unchanged.
- **`mock_config`**: Minimal `AgentConfig` (or `MagicMock` with `.llm.timeout = 600`, `.memory = None`, `.context_management.max_summary_length = 10000`, `.interactive.permission_mode = "supervised"`, `.interactive.greeting = "Hello"`, `.extra = {}`).
- **`mock_workspace_manager`**: `MagicMock` with `.read_file("workspace.md")`, `.path`, `.backend`, `.initialize()`, `.git_manager`.
- **`mock_tool_context`**: `MagicMock` of `ToolContext` with `.workspace_manager`, `.shell_manager`, `.consume_freeze_request()` returning `None`.
- **`mock_orchestrator_client`**: `AsyncMock` with `.create_thread()`, `.save_thread_message()`, `.get_thread_workspace()`, `.request_thread_vm_upgrade()`, `.register()`, `.deregister()`, `.connect()`, `.close()`.

---

## 1. `src/persistent_graph.py` (528 lines) — `tests/test_persistent_graph.py`

### 1.1 Sentinel constants
- `INTERRUPT_SENTINEL`, `APPROVE_SENTINEL`, `DENY_SENTINEL` are distinct string values
- Values do not collide with plausible user input

### 1.2 `TurnResult` dataclass
- Required fields: `turn_id`, `messages_added`, `tool_calls_made`
- `interrupted` defaults to `False`
- `error` defaults to `None`

### 1.3 `run_persistent_loop()` — main loop

#### System prompt insertion
- Inserts `SystemMessage(content=system_prompt)` at index 0 when `messages` is empty
- Inserts when `messages` is non-empty but `messages[0]` is NOT a `SystemMessage` (e.g. starts with `HumanMessage`)
- Does NOT insert when `messages[0]` is already a `SystemMessage`

#### Input handling
- `INTERRUPT_SENTINEL` input skips the turn: `turn_count` does not increment, `on_turn_start` not called
- Valid input increments `turn_count` by 1
- Valid input appends `HumanMessage(content=user_input)` to `messages` before calling `_execute_turn`
- `on_turn_start(turn_id)` fires before `_execute_turn`
- `on_turn_complete(turn_id)` fires after `_execute_turn` — even when `_execute_turn` raises

#### Cancellation
- `asyncio.CancelledError` during `get_user_input()` exits the loop (returns, does not raise)
- `asyncio.CancelledError` during `_execute_turn()` exits the loop

#### Error handling
- Non-cancellation exception in `_execute_turn` calls `on_error(str(e))` then continues loop
- After error, `tool_calls_this_turn` remains 0 (auto-commit does not fire)

#### Config extraction
- `llm_timeout` reads `config.llm.timeout`; defaults to 600 when attribute missing or `None`
- `extraction_interval` reads `config.memory.extraction_interval`; defaults to 5
- `extraction_prompt` reads `config.memory.extraction_prompt`; defaults to `""`
- All config reads use `getattr` with defaults — no `AttributeError` on missing fields

### 1.4 Memory extraction trigger
- Fires as `asyncio.create_task` when ALL of: `recall_store` truthy, `auxiliary_llm` truthy, `extraction_interval > 0`, `(turn_count - _last_extraction_turn) >= extraction_interval`
- `_last_extraction_turn` is updated to `turn_count` BEFORE the task is created (not after)
- Does NOT fire when `recall_store` is None
- Does NOT fire when `auxiliary_llm` is None
- Does NOT fire when `extraction_interval` is 0
- Does NOT fire when interval has not elapsed (e.g. interval=5, only 3 turns since last)
- Task creation failure (import error, etc.) is non-fatal — loop continues
- Passes correct `source_turn_start` and `source_turn_end` to `extract_and_store_memories`

### 1.5 Auto-commit git logic (after turn)
- Fires only when `tool_calls_this_turn > 0` AND `tool_context` is not None
- Traverses `tool_context.workspace_manager.git_manager` via `getattr` — no crash if any level is None
- Skipped when `git_manager.is_active` is False
- Calls `git_manager.commit()` only when `has_uncommitted_changes()` is True
- Commit message includes the `turn_id`
- Calls `git_manager.push()` when `turn_count % 5 == 0` — INDEPENDENT of whether a commit was made
- Does NOT push on non-5th turns
- Git exception is non-fatal (loop continues)

### 1.6 `_execute_turn()` — single turn execution

#### Interrupt handling
- `check_interrupt()` checked at the TOP of the inner while loop (before each LLM call)
- Returns `TurnResult(interrupted=True)` with accumulated `messages_added` and `tool_calls_made`

#### Memory retrieval
- `decrement_ttl()` called on `recall_store` with 5s timeout
- `decrement_ttl()` timeout or exception is non-fatal — retrieval still attempted
- Context text extracted from the LAST `HumanMessage` in `messages` (reversed traversal)
- Non-string `HumanMessage.content` converted via `str()`
- Empty string used as context if no `HumanMessage` found
- `recall_store.retrieve(context_text)` called with 5s timeout
- Empty retrieval result (`[]`) produces no memory block
- Timeout on retrieval → warning logged, no injection
- Exception on retrieval → warning logged, no injection

#### Knowledge retrieval
- Only runs when BOTH `knowledge_store` AND `project_id` are truthy
- `project_id` converted to `uuid.UUID()` before calling `hybrid_search` — invalid UUID will raise
- `hybrid_search` called with `match_count=5`
- Context extraction uses same reversed-HumanMessage pattern as memory
- Timeout and exception handling same as memory

#### Transient injection order (into `prepared` list)
- `prepared` is a COPY of `messages` (`list(messages)`) — original list not mutated
- Workspace: `SystemMessage` with `<workspace_memory>` tags inserted at index 1 (after system prompt) or index 0 (no system prompt)
- Memory: AI→Tool message pair inserted after workspace injection
- Knowledge: AI→Tool message pair inserted after memory injection
- All injections are non-fatal — exception only skips that injection
- No workspace injection when `workspace_content` is None or returns `""`

#### Context compaction
- `context_manager.ensure_within_limits()` called with the prepared list
- Git commit + push triggered when `len(prepared)` decreases after compaction
- Compaction git commit message includes before/after message counts
- Git `push()` called unconditionally during compaction (not gated by `has_uncommitted_changes`)
- Git failure during compaction is non-fatal

#### LLM streaming
- `astream()` called on `prepared` messages
- String content streamed token-by-token via `on_token` callback
- Anthropic list-of-dicts content: blocks with `type == "text"` have their `text` extracted and streamed
- Anthropic list-of-dicts content: plain string blocks streamed directly
- Empty text blocks (empty string) are NOT streamed
- Chunks concatenated via `+` operator into final `AIMessage` response
- Empty chunk list → `response` stays `None` → returns `TurnResult(error="Empty LLM response")`

#### LLM streaming fallback
- Fallback triggered by exception whose class NAME contains `"ResponseNotRead"` or `"APIConnectionError"` (string match on `type(err).__name__`, not `isinstance`)
- Other exceptions are re-raised (propagate to caller)
- Fallback calls `ainvoke(prepared)` and streams complete response in one shot
- Content handling logic in fallback identical to streaming path (list-of-dicts, string)

#### LLM timeout
- `asyncio.TimeoutError` caught at the outer try level
- Calls `on_error` with timeout message
- Returns `TurnResult(error=...)` — does NOT re-raise

#### Tool execution loop
- Iterates `response.tool_calls` list; accesses `name`, `args` (defaults `{}`), `id` from each dict
- **Permission denied**: `ToolMessage("User denied this tool call.")` appended, `messages_added` increments, `tool_calls_made` does NOT increment, `on_tool_start` NOT called, tool NOT invoked
- **Unknown tool**: `ToolMessage("Tool 'X' not found")` appended, `on_tool_result` called with error string, `messages_added` increments, `tool_calls_made` does NOT increment
- **Successful execution**: `tool.ainvoke(tool_args)` called, result converted to `str` (`None` → `""`), `ToolMessage` appended, both counters increment, `on_tool_start` called before, `on_tool_result` after
- **Tool exception**: caught, result string is `"Tool execution error: {e}"`, `ToolMessage` still appended, both counters still increment, `on_tool_result` still called
- Turn continues (inner while loop) after processing all tool calls — LLM sees results next iteration
- Turn ends (breaks inner loop) when response has no `tool_calls` attribute or `tool_calls` is empty/falsy

#### VM upgrade detection (after each tool call)
- Only checked when BOTH `tool_context` and `callbacks.on_vm_upgrade_needed` are truthy
- Calls `tool_context.consume_freeze_request()`
- Calls `on_vm_upgrade_needed(freeze_req)` only when `freeze_type == "vm_upgrade_required"`
- Other freeze types or `None` result → no callback

---

## 2. `src/api/persistent_session.py` (445 lines) — `tests/test_persistent_session.py`

### 2.1 `_EXCLUDED_TOOLS` constant
- Contains exactly: `next_phase_todos`, `todo_complete`, `todo_list`, `todo_rewind`, `mark_complete`, `job_complete`
- Is a `frozenset`

### 2.2 `PersistentSession` dataclass defaults
- `permission_mode` defaults to `"supervised"`
- `messages` defaults to a NEW empty list per instance (not shared)
- `turn_count` defaults to `0`
- All Optional fields (`workspace_manager`, `tools`, `llm_with_tools`, `context_manager`, `tool_context`, `auxiliary_llm`, `shell_manager`, `postgres_conn`, `vector_conn`, `recall_store`, `knowledge_store`, `project_id`, `_llm`) default to `None`
- `system_prompt` defaults to `""`

### 2.3 `setup()`
- Overwrites `permission_mode` from `config.interactive.permission_mode` (not kept from dataclass default)
- Stores references: `_llm = llm`, `auxiliary_llm`, `postgres_conn`, `vector_conn`
- Calls sub-methods in order: workspace → tools → bind → context → prompt → shell → memory
- Builds system prompt with `prompt_type="interactive"` and `is_strategic=False`

### 2.4 `_setup_workspace()`
- Backend resolution priority: `workspace_override["backend"]` > `config.workspace.backend` > local (None)
- Remote config priority: `workspace_override["remote"]` > `config.workspace.remote`
- `RemoteBackend` created with `host`, `port` (default 22), `username` (default `"agent-host"`), `key_path`, `workspace_path`, `job_id=self.thread_id`, `default_timeout`, `max_tabs` from shell config
- `RemoteBackend.connect()` called immediately after creation
- Exception during remote backend creation → falls back to local (warning logged)
- `WorkspaceManagerConfig` receives `git_remote_url` and `git_versioning` from config
- `workspace_manager.initialize()` called after construction
- `WORKSPACE_PATH` env var used as base path, defaults to `"./workspace"`

### 2.5 `_setup_tools()`
- `ToolContext` created with `todo_manager=None`, `_job_id=self.thread_id`
- All config tool names retrieved via `get_all_tool_names(config)`
- Phase-specific tools filtered out via `_EXCLUDED_TOOLS` frozenset
- 8 orchestrator delegation tools always appended (if not already present): `create_worker_job`, `list_worker_jobs`, `get_worker_job`, `get_job_workspace_file`, `approve_worker_job`, `resume_worker_job`, `cancel_worker_job`, `pause_worker_job`
- Duplicate orchestrator tools not added (checked with `if name not in tool_names`)
- `load_tools(tool_names, tool_context)` called; on `ValueError`, falls back to loading each tool individually (skips unimplemented tools)
- `apply_description_overrides` and `apply_instruction_enforcement` called on the final tool list

### 2.6 `_bind_tools()`
- No-op (early return) when `_llm` is falsy
- No-op (early return) when `tools` is falsy (None or empty list)
- For models whose lowercased name starts with `"o1"`, `"o3"`, or `"o4"`: `parallel_tool_calls` NOT passed to `bind_tools`
- For all other models: `parallel_tool_calls` passed from `config.llm.parallel_tool_calls`
- `startswith` check also catches variants like `"o1-mini"`, `"o3-mini"`, `"o4-mini"`
- Model name `None` handled via `(self.config.llm.model or "").lower()`

### 2.7 `_setup_context_manager()`
- `ContextManager` created with `keep_recent_tool_results` and `keep_recent_messages` from `config.context_management`
- Model string falls back to `"gpt-4"` when `config.llm.model` is None

### 2.8 `_setup_shell_manager()`
- Checks `workspace_manager.backend` for `supports_shell` attribute via `getattr` (defaults `False`)
- When `supports_shell` is True: creates `ShellManager` with `backend=ws_backend`
- When `supports_shell` is False and `tmux` not in PATH: returns early (no shell)
- When `supports_shell` is False and `tmux` available: creates local `ShellManager` with `backend=None`
- `ShellManager` receives: `job_id=thread_id`, `max_tabs`, `scrollback_limit`, `default_timeout`, `blocked_commands`, `sandbox_cwd` (workspace path when `sandbox=True`), `sudo_action` (defaults `"freeze"`)
- Sets `tool_context.shell_manager` after creation
- Exception during initialization is non-fatal (warning logged)

### 2.9 `_setup_memory()`
- Returns immediately when `vector_conn` is None
- `RecallStore` created only when `config.memory.enabled` is True
- `RecallStore` receives `job_id=uuid.UUID(self.thread_id)` — thread_id must be valid UUID
- Sets `tool_context.recall_store` after creation
- `KnowledgeStore` created unconditionally when `vector_conn` available (regardless of `memory.enabled`)
- Both store creation failures are non-fatal (warning logged)

### 2.10 `swap_backend()`
- Raises `RuntimeError("No workspace manager to swap backend on")` when `workspace_manager` is None
- Connects new backend via `new_backend.connect()` if it has a `connect` method AND `is_connected()` returns `False`
- If `is_connected` attribute missing: calls `connect()` (default lambda returns `False`)
- Disconnects old backend only if it has both `disconnect` and `is_connected` methods, and `is_connected()` returns `True`
- Old backend disconnect exception is non-fatal (warning logged)
- Sets `workspace_manager._backend` (private attribute) to new backend
- Calls `_setup_shell_manager()` to rebuild shell with new backend

### 2.11 `get_workspace_content()`
- Returns `workspace_manager.read_file("workspace.md")` content
- Returns `""` when `workspace_manager` is None
- Returns `""` on `FileNotFoundError`
- Returns `""` on `OSError`

### 2.12 `cleanup()`
- Calls `shell_manager.cleanup()` when shell manager exists; exception non-fatal
- Calls `backend.disconnect()` when backend has `disconnect` + `is_connected` methods and `is_connected()` is True
- Backend disconnect exception is non-fatal
- Skips backend cleanup when `workspace_manager` is None

---

## 3. `src/api/persistent_app.py` (858 lines) — `tests/test_persistent_app.py`

### 3.1 `_get_agent_metrics()`
- Returns `{"memory_mb": float, "cpu_percent": float}` from psutil
- Returns `None` when psutil is not installed
- Returns `None` on any exception

### 3.2 `_safe_serialize()`
- Returns JSON-serializable objects unchanged (dict, list, str, int, None)
- Converts non-serializable objects (datetime, custom class, set) to string via `str()`

### 3.3 `_save_message()`
- Calls `client.save_thread_message()` with `thread_id`, `role`, `content`, `tool_calls`, `turn_number`
- Exception caught and logged — does not propagate

### 3.4 `_save_turn_ai_messages()`
- Walks backwards from end of messages; stops at the first `HumanMessage` (type == "human")
- If no `HumanMessage` exists: collects ALL messages
- Collected messages reversed to restore chronological order
- For each message: `role` read from `.type` attribute; `content` from `.content`
- `tool_calls` extracted as list of `{name, args, id}` dicts when present
- Anthropic list-of-dicts content normalized: `[{type: "text", text: "..."}]` → joined string
- String list blocks converted via `str(b)`
- Each message saved individually via `client.save_thread_message()`
- Outer exception caught — does not propagate

### 3.5 `_generate_title()`
- Returns `None` when `auxiliary_llm` is None
- Returns `None` when `messages` is empty list
- Samples first 10 messages; only includes those with string `.content` (skips list content, None, empty string)
- Each sampled content truncated to 200 chars
- Returns `None` when no messages pass the filter (all have non-string or empty content)
- Calls `auxiliary_llm.run_task(SummarizeTask, ...)` with `mode="chain"`
- Result `.strip()` then `[:100]` — whitespace trimmed, max 100 chars
- Returns `None` when `result` is falsy (None or empty string)
- Exception returns `None` (non-fatal)

### 3.6 `_poll_workspace_ready()`
- Uses `time.monotonic()` for deadline calculation (immune to wall-clock changes)
- Returns `None` immediately when `client.get_thread_workspace()` returns `None` (no retry)
- VM check first: returns config when `vm_status == "ready"` AND `vm_ssh_host` present
- Container check second: returns config when `status == "ready"` AND `pod_ip` present
- VM config uses `vm_ssh_host` and `vm_ssh_port` (default 22)
- Container config uses `pod_ip` with hardcoded port 22
- Both configs include `"backend": "remote"`, `"username": "agent-host"`, `"key_path"`, `"workspace_path"`, `"git_remote_url"`
- Returns `None` when `status == "none"` and no `vm_status` (no workspace provisioned)
- Returns `None` when `status == "failed"` and `vm_status` is falsy or `"failed"`
- Intermediate statuses (e.g. `"provisioning"`) → sleeps `poll_interval` and retries
- Returns `None` on deadline timeout

### 3.7 `_poll_vm_ready()`
- Returns `{"ssh_host": ..., "ssh_port": ...}` when `vm_status == "ready"` and `vm_ssh_host` present
- Returns `None` immediately when `vm_status == "failed"`
- Continues polling (sleep + retry) when `get_thread_workspace()` returns `None`
- Continues polling for intermediate `vm_status` values
- Returns `None` on deadline timeout
- Default timeout 300s, poll_interval 3s

### 3.8 `_handle_compact()`
- Sends error WS event when `_session` or `_session.context_manager` is None
- Calls `context_manager.summarize_and_compact()` with session messages
- Mutates `_session.messages` in-place via slice assignment (`messages[:] = ...`)
- Sends `context.compacted` event with `before`, `after`, `focus` fields
- Git commit + push when `git_manager.is_active`:
  - Commit only when `has_uncommitted_changes()` is True
  - Commit message includes before/after counts
  - Push called unconditionally
  - Git failure is non-fatal
- Outer exception sends error WS event

### 3.9 `_handle_archive()`
- Sends error WS event when `_session` is None
- Gets `recall_store` from `_session.tool_context.recall_store` (NOT from `_session.recall_store`)
- Memory extraction requires ALL of: `recall_store`, `_session.auxiliary_llm`, `_session.messages` (non-empty)
- Memory extraction failure is non-fatal
- Title generation: reads thread via `_session.postgres_conn.get_thread()`; generates title only when existing title is in `(None, "Untitled Session", "")`
- Title update uses raw SQL: `UPDATE threads SET title = $2 WHERE id = $1` via `postgres_conn.acquire()`
- Title generation failure is non-fatal
- Thread ended via `_session.postgres_conn.end_thread(_thread_id)` — only when `postgres_conn` exists
- `end_thread` failure logged but does not prevent `session.ended` event
- Sends `session.ended` WS event with `thread_id`
- Outer exception sends error WS event

### 3.10 `permission_check()` logic
- `autonomous` mode → returns `True` immediately (no WS event, no queue wait)
- `auto_accept` mode → returns `True` for any tool NOT in `{"run_command", "shell_execute", "shell_read"}`
- `auto_accept` mode → falls through to ask user for shell tools
- `supervised` mode → always asks user
- Sends `permission.request` WS event with tool name and `_safe_serialize(tool_args)`
- Waits on `user_queue.get()` with 300s timeout
- `APPROVE_SENTINEL` response → returns `True`
- Any other response (including `DENY_SENTINEL`) → returns `False`
- `asyncio.TimeoutError` → returns `False`

### 3.11 `on_tool_result` callback
- Tool results truncated to 2000 chars + `"..."` suffix when `len(result) > 2000`
- Results <= 2000 chars sent unchanged

### 3.12 `check_interrupt()` closure
- Returns `True` and resets `interrupt_flag` to `False` when flag was True
- Returns `False` when flag is False (no side effect)

### 3.13 WebSocket message routing
- JSON parsing: valid JSON parsed normally; `JSONDecodeError` treats raw text as `{"method": "message", "content": raw}`
- `method: "message"` with non-empty `content` → updates `_last_user_content[0]`, puts content in `user_queue`
- `method: "message"` with empty/missing `content` → silently dropped (not queued)
- `method: "approve"` → puts `APPROVE_SENTINEL` in queue
- `method: "deny"` → puts `DENY_SENTINEL` in queue
- `method: "interrupt"` → sets `interrupt_flag = True` (does NOT put in queue)
- `method: "mode.set"` with valid mode (`"supervised"`, `"auto_accept"`, `"autonomous"`) → updates `_session.permission_mode`, sends `mode.changed` event
- `method: "mode.set"` with invalid mode → sends error event
- `method: "compact"` → creates async task for `_handle_compact(ws, focus)`
- `method: "archive"` → creates async task for `_handle_archive(ws)`
- `method: "upgrade-to-vm"` → creates async task for `_handle_vm_upgrade(ws)`
- Unknown method → sends error event with method name

### 3.14 WebSocket lifecycle
- Accepts connection, then checks session readiness
- Not ready → sends error event, closes with code 4503
- Sends `session.state` event immediately on connect (thread_id, permission_mode, turn_count, message_count)
- Sends greeting when `_session.messages` is empty OR `_session.turn_count == 0` (OR condition)
- No greeting when greeting string is falsy (empty string)
- No greeting when session has messages AND turn_count > 0
- `WebSocketDisconnect` exception → loop task cancelled, session logged
- Generic exception → loop task cancelled
- `finally` block always cancels loop task and awaits its `CancelledError`

### 3.15 `_handle_vm_upgrade()`
- Sends `vm_upgrade.failed` when any of `_session`, `_orchestrator_client`, `_thread_id` is falsy
- Sends `vm_upgrade.started` BEFORE attempting any provisioning
- Calls `_orchestrator_client.request_thread_vm_upgrade(_thread_id)`
- Rejected (returns False) → sends `vm_upgrade.failed` with rejection reason
- Polls for VM readiness via `_poll_vm_ready` with 300s timeout
- Poll timeout → sends `vm_upgrade.failed`
- Creates `RemoteBackend` with VM SSH config (host, port, username, key_path, workspace_path, thread_id, shell config)
- Calls `_session.swap_backend(new_backend)`
- Sets `_session.shell_manager.sudo_action = "allow"` (only when shell_manager has `sudo_action` attribute)
- Sends `vm_upgrade.complete` with thread_id, ssh_host, ssh_port
- Exception sends `vm_upgrade.failed` with exception string

### 3.16 Health endpoints
- `GET /health`: returns `{"status": "healthy", "mode": "persistent", "thread_id": ..., "uptime_seconds": ...}`
- `GET /ready`: 200 with `{"ready": true}` when `_session` and `_session.llm_with_tools` are not None; 503 with `{"ready": false}` otherwise
- `GET /status`: returns config path, permission mode, turn count, message count, tool name list

### 3.17 `_ws_send()`
- Calls `ws.send_json({"method": method, "params": params})`
- Catches ALL exceptions (including `RuntimeError`, `ConnectionResetError`) — silently drops

### 3.18 `create_persistent_app()`
- Sets module globals `_config_path` and `_thread_id`
- Returns `FastAPI` instance with lifespan function bound

### 3.19 `on_turn_start` / `on_turn_complete` callbacks
- `on_turn_start`: sets `_session.turn_count = turn_id`, sends `turn.started` event, saves user message to DB via `_save_message` (bounded 5s await)
- User message save timeout is non-fatal (proceeds after 5s)
- User message only saved when `_orchestrator_client` and `_last_user_content[0]` are truthy
- `on_turn_complete`: sends `turn.completed` event, saves AI messages via `_save_turn_ai_messages` (bounded 5s await)
- AI message save timeout is non-fatal

---

## 4. `src/api/orchestrator_client.py` (new persistent methods) — extend `tests/test_orchestrator_client.py`

### 4.1 `create_thread()`
- POSTs to `/api/agents/threads` with `{config_name, permission_mode, title}` payload
- Returns `thread_id` string from response JSON on 200
- Returns `None` on non-200 status (logs error)
- Returns `None` on request exception
- Calls `connect()` if `_client` not initialized

### 4.2 `save_thread_message()`
- POSTs to `/api/agents/threads/{thread_id}/messages` with `{role, content, tool_calls, turn_number}`
- Returns `True` on 200
- Returns `False` on non-200 status
- Returns `False` when `_client` is None (does NOT auto-connect)
- Exception returns `False` — fire-and-forget safe

### 4.3 `request_thread_vm_upgrade()`
- POSTs to `/api/agents/threads/{thread_id}/upgrade-to-vm` with `{cpu_cores, memory}` payload
- Returns `True` on 200
- Returns `False` on non-200 status
- Returns `False` on request exception
- Calls `connect()` if `_client` not initialized

### 4.4 `get_thread_workspace()`
- GETs `/api/agents/threads/{thread_id}/workspace`
- Returns parsed JSON dict on 200
- Returns `None` on non-200 status
- Returns `None` on request exception
- Calls `connect()` if `_client` not initialized

---

## 5. Orchestrator Thread Endpoints (`orchestrator/main.py`) — `tests/test_thread_endpoints.py`

### 5.1 `POST /api/agents/threads` (agent-facing, no auth)
- Creates thread with `user_id=NULL` via `postgres_db.create_thread()`
- Returns 200 with `{"thread_id": uuid, "status": "created"}`
- Creates Gitea repo named `thread-{thread_id[:8]}` when `gitea_client.is_initialized`
- Merges `git_remote_url` and `repo_name` into thread workspace context
- Triggers `container_provisioner.create_thread_workspace()` as background task when K8s available
- Returns 500 on database exception

### 5.2 `GET /api/agents/threads/{thread_id}/workspace`
- Returns 404 when thread not found
- Parses `metadata` from thread; handles JSON-string metadata (legacy format)
- Returns workspace_container fields: `status` (default `"none"`), `pod_ip`, `pod_name`, `namespace`, `git_remote_url`
- Returns VM fields: `vm_status`, `vm_ssh_host`, `vm_ssh_port`, `vm_name`
- Returns empty values when no workspace_container or vm in metadata

### 5.3 `POST /api/agents/threads/{thread_id}/upgrade-to-vm`
- Returns 404 when thread not found
- Returns 503 when `vm_provisioner.is_available` is False
- Returns existing status dict when VM already in `("provisioning", "created", "ready")`
- Calls `vm_provisioner.create_thread_vm()` for new requests
- Returns 500 when provisioner returns False
- Returns 200 with `{"status": "provisioning", "thread_id": ..., "vm_provisioner_mode": ...}`

### 5.4 `POST /api/agents/threads/{thread_id}/messages`
- Saves message via `postgres_db.save_thread_message()`
- Returns 200 with `{"message_id": uuid, "status": "saved"}`
- Returns 500 on database exception

### 5.5 `POST /api/persistent/threads` (user-facing, auth required)
- Returns 401/403 when user not authenticated or not approved
- Reads user `persistent_agent` settings: `model`, `permission_mode`, `greeting`, `idle_timeout_minutes`, `command_allowlist`
- Builds `config_override` dict from user settings (nested under `llm`, `interactive` keys)
- Stores `config_override` in thread metadata via `jsonb_set`
- Creates Gitea repo
- Triggers container provisioning in background
- Returns 200 with `{"thread_id": uuid, "status": "created"}`

### 5.6 `GET /api/persistent/threads`
- Returns 401/403 for unauthenticated users
- Returns `{"threads": [...]}` — threads for user (includes NULL user_id threads)
- Supports `project_id` and `status` query parameters

### 5.7 `GET /api/persistent/threads/{thread_id}`
- Returns 404 when thread not found
- Returns 403 when `thread.user_id` does not match authenticated user (and user_id is not NULL)
- Returns 200 with thread dict

### 5.8 `DELETE /api/persistent/threads/{thread_id}`
- Returns 404 when thread not found
- Returns 403 for non-owner
- Captures S3 snapshot via `snapshot_service` when service available, pod_ip present, and status is `"ready"`
- Deletes workspace container via `container_provisioner.delete_thread_workspace()`
- Deletes VM via `vm_provisioner.delete_thread_vm()` when VM status in `("provisioning", "created", "ready")`
- Deletes Gitea repo via `gitea_client.delete_repo(repo_name)`
- Marks thread as ended via `postgres_db.end_thread()`
- Returns 200 with `{"status": "ended"}`

### 5.9 `GET /api/persistent/threads/{thread_id}/messages`
- Returns 404 when thread not found
- Returns 403 for non-owner
- Returns paginated messages via `postgres_db.get_thread_messages_history()`
- Caps `limit` at 500 via `min(limit, 500)`
- Returns `{"messages": [...], "total": int, "thread_id": ...}`

### 5.10 `GET /api/persistent/threads/{thread_id}/ide`
- Returns 404 when thread not found
- Returns 403 for non-owner
- VM ready → returns `{"status": "active", "code_server_url": ..., "source": "live_vm", "gitea_url": ...}`
- Container ready → returns `{"status": "active", "code_server_url": ..., "source": "live_workspace", "gitea_url": ...}`
- VM takes precedence over container
- Provisioning in progress → returns `{"status": "restoring"}`
- No workspace → returns `{"status": "unavailable"}`
- `gitea_url` built from `GITEA_URL` + `GITEA_ADMIN_USER` env vars and `repo_name`
- `gitea_url` is `None` when no repo exists

### 5.11 `WS /ws/persistent/{thread_id}` (proxy)
- Accepts WS first (FastAPI requirement), then validates
- Closes 4404 when thread not found
- Restores suspended workspace via `workspace_suspension_service.restore_thread_workspace()` when workspace status is `"suspended"`
- Closes 4503 when restoration fails
- Closes 4404 when `thread.agent_id` is falsy
- Closes 4503 when agent not found in DB
- Closes 4503 when agent has no `pod_ip`
- Connects upstream to `ws://{pod_ip}:{pod_port}/ws/chat`
- Bidirectional forwarding: text frames forwarded as text, binary frames as bytes
- Runs both directions concurrently via `asyncio.wait(FIRST_COMPLETED)`; cancels pending task when one completes

### 5.12 `_inspect_session_event()` (notification helper)
- Returns immediately when `user_id` is falsy
- Only broadcasts for methods in `{"permission.request", "vm_upgrade.needed", "ready"}`
- Maps to SSE event types: `session.permission_request`, `session.vm_upgrade`, `session.waiting`
- Broadcasts via `notification_feed.broadcast()` with thread_id, title, config_name, tool/args/reason/command from params
- `JSONDecodeError` or any other exception silently ignored

### 5.13 `_inspect_browser_event()` (notification helper)
- Returns immediately when `user_id` is falsy
- Only broadcasts for methods in `{"approve", "deny"}`
- Broadcasts `session.resolved` event type with thread_id and resolution method
- Exceptions silently ignored

---

## 6. Database Layer (`orchestrator/database/postgres.py`) — `tests/test_thread_db.py`

Note: These require a live PostgreSQL instance (or a test DB fixture). Consider using the existing
test database patterns in the project.

### 6.1 `create_thread()`
- Returns UUID string
- All optional params work: `user_id=None`, `project_id=None`
- Default values: `config_name="defaults"`, `permission_mode="supervised"`, `title="Untitled Session"`

### 6.2 `get_thread()` / `get_thread()` not found
- Returns dict with all columns for existing thread
- Returns `None` for non-existent UUID

### 6.3 `list_threads()`
- No filters → returns all threads ordered by `created_at DESC`
- `user_id` filter → includes threads with matching user_id AND threads with `user_id IS NULL`
- `project_id` filter → only matching project
- `status` filter → only matching status
- Multiple filters combined with AND
- Capped at 50 results

### 6.4 `end_thread()`
- Sets `status = 'ended'` and `ended_at` to current timestamp
- Does not error on non-existent thread_id (UPDATE affects 0 rows)

### 6.5 `update_thread_status()`
- Updates `status` and `last_activity` to current timestamp

### 6.6 `update_thread_agent()`
- Binds `agent_id` to thread

### 6.7 `save_thread_message()`
- Returns UUID string for saved message
- `tool_calls` serialized to JSON string via `json.dumps` (None → SQL NULL)
- Updates thread `last_activity` to current timestamp
- Updates thread `total_turns` to `GREATEST(total_turns, turn_number)` — never decreases

### 6.8 `get_thread_messages_history()`
- Returns messages ordered by `created_at ASC` (chronological)
- Supports `limit` and `offset` pagination
- Deserializes `tool_calls` JSON (NULL → Python None)
- Formats `created_at` as ISO string (None → Python None)
- Returns list of dicts with keys: `id`, `role`, `content`, `tool_calls`, `turn_number`, `created_at`

### 6.9 `get_thread_message_count()`
- Returns integer count of messages for a thread
- Returns 0 for thread with no messages

### 6.10 `update_thread_tokens()`
- Increments `total_tokens` by given amount (additive, not replacement)

### 6.11 `merge_thread_workspace_context()` / `merge_thread_vm_context()`
- Deep-merges provided dict into `metadata.workspace_container` / `metadata.vm` JSONB path

---

## 7. `orchestrator/services/persistent_provisioner.py` (152 lines) — `tests/test_persistent_provisioner.py`

### 7.1 Properties
- `is_available` returns `False` when K8s not initialized, `True` when initialized
- `mode` returns `"k8s"` when available, `None` otherwise

### 7.2 `connect()`
- Stores db reference in `_db`
- Calls `_init_k8s()`

### 7.3 `_init_k8s()`
- Sets `_k8s_available = True` when K8s in-cluster or kubeconfig loads
- Handles `ImportError` (kubernetes not installed) → `_k8s_available` stays False
- Handles `ConfigException` (no cluster) → `_k8s_available` stays False
- Tries in-cluster first, then kubeconfig

### 7.4 `create_agent_pod()`
- Returns `False` and logs manual start instruction when K8s not available
- Log message includes `thread_id` and `config_name` for copy-paste

### 7.5 `delete_agent_pod()` / `get_pod_status()`
- Returns `False` / `None` when K8s not available

### 7.6 Module-level singleton
- `persistent_provisioner` is pre-instantiated at module level

---

## 8. Config Loading (`src/core/loader.py`) — extend `tests/test_loader_routing.py`

### 8.1 `InteractiveConfig` defaults
- `permission_mode` defaults to `"supervised"`
- `idle_timeout_minutes` defaults to `60` (not 120)
- `greeting` defaults to `"Hello! I'm ready to help. What would you like to work on?"`

### 8.2 Config parsing
- `interactive` YAML section parsed into `InteractiveConfig`
- Missing `interactive` section → defaults used
- Explicit values override defaults
- `InteractiveConfig` is a field on `AgentConfig` (default factory)

### 8.3 Prompt resolution for interactive mode
- `get_phase_system_prompt()` with `prompt_type="interactive"` resolves `systemprompt_interactive` template
- Falls back to matrix resolution when template not in resolved prompts

---

## 9. Cockpit Frontend (Angular, vitest)

### 9.1 `PersistentChatService` — `cockpit/.../persistent-chat.service.spec.ts`
- `connect()`: loads history via REST, then opens WS
- `loadHistory()`: fetches `/persistent/threads/{threadId}/messages`, populates `messages` signal
- `send()`: for normal text sends `{method: "message", content}`, for slash commands parses and sends appropriate method
- Slash command parsing: `/compact [focus]` → compact, `/done` → archive, `/auto` → mode.set auto_accept, `/supervised` → mode.set supervised, `/autonomous` → mode.set autonomous
- `approve()` / `deny()` / `interrupt()`: send correct method strings
- `setMode()`: sends `{method: "mode.set", mode}`
- `disconnect()`: closes WS, resets signals to defaults
- WS `token` handler: appends to `streamingText` signal
- WS `tool.started` handler: adds entry to `currentToolCalls`
- WS `tool.completed` handler: updates matching tool call in `currentToolCalls` with result
- WS `permission.request` handler: sets `pendingPermission` signal
- WS `turn.started` handler: sets `isStreaming = true`, resets `streamingText`
- WS `turn.completed` handler: moves `streamingText` to `messages` array, sets `isStreaming = false`
- WS `mode.changed` handler: updates `permissionMode` signal
- WS `session.state` handler: syncs `permissionMode`, `currentTurnId`
- WS `greeting` handler: adds greeting to messages
- WS `error` handler: sets `error` signal
- WS `session.ended` handler: sets `connectionState` to `"disconnected"`
- `connectionState` signal transitions: `disconnected` → `connecting` → `connected` / `error`

### 9.2 `SessionsPageComponent` — `cockpit/.../sessions-page.component.spec.ts`
- Thread list fetched on init from API
- Status filter (all/active/ended) controls API query params
- Create dialog submits POST to `/api/persistent/threads`
- Resume action navigates to `/chat/{threadId}`
- End session action calls DELETE endpoint
- Direct connect input submits WebSocket URL for local dev

---

## 10. `src/services/knowledge_graph.py` (713 lines) — `tests/test_knowledge_graph.py`

Note: Tests should mock `Neo4jDB` (`self._db`) since a live Neo4j instance may not be available.

### 10.1 Constants
- `NOTE_TYPES` is a frozenset of exactly 9 types: `goal`, `plan`, `decision`, `learning`, `code`, `source`, `question`, `state`, `retrospective`
- `NOTE_STATUSES` is a frozenset of exactly 4: `active`, `resolved`, `superseded`, `archived`
- `CONFIDENCE_LEVELS` is a frozenset of exactly 3: `high`, `medium`, `low`
- `RELATIONSHIP_TYPES` is a frozenset of exactly 8: `REFERENCES`, `DERIVED_FROM`, `SUPPORTS`, `CONTRADICTS`, `ANSWERS`, `DEPENDS_ON`, `SUPERSEDES`, `IMPLEMENTS`

### 10.2 `slugify()`
- Lowercases input: `"Chose JWT"` → `"chose-jwt"`
- Replaces spaces and underscores with hyphens: `"hello_world test"` → `"hello-world-test"`
- Strips non-alphanumeric characters: `"auth (v2)!"` → `"auth-v2"`
- Collapses multiple hyphens: `"a---b"` → `"a-b"`
- Strips leading/trailing hyphens: `"-test-"` → `"test"`
- Truncates to `max_length`: slug of 100-char title with `max_length=80` is 80 chars
- Empty title after stripping returns `""` (caller must handle)

### 10.3 `KnowledgeGraphDB.__init__()`
- Reads `NEO4J_URL` from env, defaults to `"bolt://localhost:7687"`
- Reads `NEO4J_USERNAME` from env, defaults to `"neo4j"`
- Reads `NEO4J_PASSWORD` from env, defaults to `""`
- Constructor params override env vars

### 10.4 `connect()` / `close()` / `is_connected`
- `connect()` delegates to `_db.connect()`, returns bool
- `close()` delegates to `_db.close()`
- `is_connected` delegates to `_db.is_connected`

### 10.5 `create_note()`
- Raises `ValueError` for invalid `note_type` (not in `NOTE_TYPES`)
- Raises `ValueError` for invalid `confidence` (not in `CONFIDENCE_LEVELS`)
- `None` confidence is valid (no validation)
- Generates slug from title via `slugify()`
- Falls back to `note-{random_hex}` when slugified title is empty
- Appends `{random_hex}` suffix on slug collision (existing note with same slug)
- Creates Note node with `status='active'` and `datetime()` timestamps
- Creates Tag nodes (lowercased) with `TAGGED` relationships for each tag
- Creates Keyword nodes (lowercased) with `HAS_KEYWORD` relationships for each keyword
- Creates typed relationships to other notes for each link in `links`
- Skips links with missing `target`
- Skips links with `rel_type` not in `RELATIONSHIP_TYPES` (warns, doesn't raise)
- Defaults link `type` to `"REFERENCES"` when not specified
- Returns the slug string

### 10.6 `read_note()`
- Returns `None` when note not found
- Returns dict with all node properties + `tags`, `keywords`, `relationships`, `incoming_relationships`
- Handles Neo4j Node objects (`.items()`) and plain dicts
- Returns empty `props` dict when node is neither Node nor dict
- `relationships` is a list of `{type, target, target_title}` dicts
- `incoming_relationships` is a list of `{type, source, source_title}` dicts

### 10.7 `list_notes()`
- Returns all notes for project when no filters
- Filters by `note_type` when provided
- Filters by `status` when provided
- Filters by `job_id` when provided
- Filters by `tag` (lowercased) when provided — uses `TAGGED` relationship join
- Multiple filters combined with AND
- Ordered by `n.modified DESC`
- Capped at `limit` results (default 50)

### 10.8 `update_note()`
- Returns `False` when note not found
- Always updates `modified` timestamp
- `content` replaces existing content entirely
- `append` concatenates with `"\n\n"` prefix to existing content
- `content` and `append` are mutually exclusive (`elif`): `content` takes priority
- Raises `ValueError` for invalid `status`
- Raises `ValueError` for invalid `confidence`
- Creates new Tag nodes and relationships for `add_tags` (lowercased)
- Creates new typed relationships for `add_links`
- Skips links with missing `target` or invalid `rel_type`
- Returns `True` on success

### 10.9 `get_related()`
- Caps `max_hops` at 3 (regardless of input value)
- Excludes the starting note from results
- Returns results with `id`, `title`, `type`, `status`, `distance`, `rel_types`
- Returns empty list when note not found or no related notes

### 10.10 `get_contradictions()`
- Returns only pairs where BOTH notes have `status='active'`
- Returns `note_a`, `title_a`, `note_b`, `title_b` for each pair
- Returns empty list when no contradictions exist

### 10.11 `get_provenance()`
- Follows `DERIVED_FROM` relationships only
- Caps `max_depth` at 10
- Excludes the starting note from results
- Returns results ordered by `depth`

### 10.12 `get_unanswered()`
- Returns only notes with `type='question'` and `status='active'`
- Excludes questions that have an outgoing `ANSWERS` relationship
- Excludes questions that have an incoming `ANSWERS` relationship
- Returns `id`, `title`, `content`, `created`, `job_id`

### 10.13 `get_all_notes_for_export()`
- Returns all notes with `tags`, `keywords`, `relationships`
- Handles Neo4j Node objects and plain dicts
- Skips nodes that are neither Node nor dict
- Fetches outgoing relationships separately for each note

### 10.14 `delete_project_knowledge()`
- Uses `DETACH DELETE` (removes nodes and all connected relationships)
- Matches ALL nodes with matching `project_id` (not just Note nodes)
- Returns count of deleted nodes
- Returns 0 when no nodes exist for project

### 10.15 `get_note_content_hash()`
- Returns SHA-256 hex digest of UTF-8 encoded content

---

## 11. `src/services/knowledge_store.py` (551 lines) — `tests/test_knowledge_store.py`

Note: Tests should mock `db` (asyncpg-style) and `embedding_service`.

### 11.1 `KnowledgeRecord` dataclass
- All fields have defaults (can construct empty record)
- `status` defaults to `"active"`
- `tags`, `keywords`, `retrieval_messages` default to empty lists
- `from_row()`: creates record from dict, handles missing keys with defaults
- `from_row()`: `None` values for `tags`/`keywords`/`retrieval_messages` become empty lists (via `or []`)

### 11.2 `KnowledgeStore._content_hash()`
- Returns SHA-256 hex digest of UTF-8 encoded content
- Static method (no instance needed)
- Consistent: same content always produces same hash

### 11.3 `KnowledgeStore._prepare_embedding()`
- Returns list unchanged when input is `List[float]`
- Parses string format `"[0.1,0.2,0.3]"` to `List[float]`
- Handles empty string `"[]"` → empty list
- Falls back to `list(embedding)` for other iterable types (e.g. numpy array)

### 11.4 `upsert_note()` — metadata-only update
- When `content_hash` matches existing row: updates metadata only (no embedding call)
- Metadata-only update sets: title, note_type, status, confidence, tags, keywords, job_id, phase, retrieval_messages, modified_at, indexed_at
- Does NOT call `embedding_service.embed()` for metadata-only update
- Returns the row UUID

### 11.5 `upsert_note()` — content changed
- When `content_hash` differs (or no existing row): generates new embedding
- Embedding text is `content + "\n\n" + "\n".join(retrieval_messages)` when retrieval_messages present
- Embedding text is just `content` when retrieval_messages empty
- Search text concatenates: title, content, tags, keywords, retrieval_messages (space-joined)
- Uses `ON CONFLICT (project_id, note_id) DO UPDATE` for upsert
- Returns the row UUID
- `None` values for tags/keywords/retrieval_messages normalized to empty lists

### 11.6 `delete_note()`
- Returns `True` when row deleted (RETURNING id is not None)
- Returns `False` when row not found (RETURNING id is None)

### 11.7 `hybrid_search()`
- Returns empty list when neither `project_id` nor `project_ids` provided
- Single project uses `knowledge_hybrid_search` SQL function
- Multiple projects uses `knowledge_multi_project_hybrid_search` SQL function
- `project_ids` takes priority over `project_id`
- Calls `embedding_service.embed(query)` for vector search
- Returns `List[KnowledgeRecord]` constructed via `from_row()`
- Default weights: dense=0.6, sparse=0.3, recency=0.1

### 11.8 `get_summary()`
- Returns `{"total": 0}` when no project IDs provided
- Single project uses `project_id = $1` clause
- Multiple projects uses `project_id = ANY($1)` clause
- Returns counts: total, active, decisions, learnings, open_questions, goals, code_notes, state_notes, last_modified
- Returns 5 most recent active notes as `recent_notes`

### 11.9 `rebuild_from_notes()`
- Deletes all existing index entries for the project first
- Iterates notes and calls `upsert_note()` for each
- Handles Neo4j DateTime objects via `.to_native()` conversion
- Converts string `job_id` to `uuid.UUID`
- Individual note failures are non-fatal (warning logged, continues)
- Returns count of successfully indexed notes

### 11.10 `format_note()` (static)
- Includes `note_type` in meta
- Includes `confidence` in meta only when present
- Includes `phase` in meta only when not None
- Includes tags only when present
- Truncates content to 497 chars + `"..."` when > 500 chars
- Returns formatted string with 1-based index

### 11.11 `assemble_knowledge_block()` (classmethod)
- Returns empty string when notes list is empty
- Wraps notes in `--- Project Knowledge ---` header and `--- End Knowledge ---` footer
- Footer includes note count and estimated token count (`len(content) // 4` sum)
- Each note formatted via `format_note()` with 1-based index

---

## 12. `src/core/knowledge_injection.py` (75 lines) — `tests/test_knowledge_injection.py`

### 12.1 `KNOWLEDGE_TOOL_CALL_ID_PREFIX`
- Is `"knowledge_inject_"`

### 12.2 `create_knowledge_injection_messages()`
- Returns `(AIMessage, ToolMessage)` tuple
- `AIMessage` has empty string content
- `AIMessage` has one tool_call with `name="kb_search"`, `args={"query": "current_task_context"}`
- Tool call ID starts with `KNOWLEDGE_TOOL_CALL_ID_PREFIX` + 8 hex chars
- `ToolMessage` content is the input `content` parameter
- `ToolMessage.tool_call_id` matches the `AIMessage` tool call ID
- Each call generates a unique tool call ID (UUID-based)

### 12.3 `is_knowledge_injection_message()`
- Returns `True` for `ToolMessage` whose `tool_call_id` starts with prefix
- Returns `True` for `AIMessage` with a tool_call whose `id` starts with prefix
- Returns `False` for `AIMessage` with no tool_calls
- Returns `False` for `AIMessage` with tool_calls that don't match prefix
- Returns `False` for `ToolMessage` with non-matching tool_call_id
- Returns `False` for `HumanMessage` (always)
- Returns `False` for `SystemMessage` (always)
- Handles missing `tool_call_id` attribute gracefully (defaults to `""`)
- Handles missing `tool_calls` attribute gracefully

---

## 13. `src/tools/knowledge/knowledge_tools.py` (808 lines) — `tests/test_knowledge_tools.py`

Note: Tests should mock `ToolContext` with `knowledge_graph` and `knowledge_store` mocks.
The `_run_async()` helper bridges sync tools to async stores — mock the underlying async methods.

### 13.1 `KNOWLEDGE_TOOLS_METADATA` registry
- Contains exactly 10 tools: `kb_write`, `kb_update`, `kb_read`, `kb_list`, `kb_search`, `kb_related`, `kb_contradictions`, `kb_provenance`, `kb_unanswered`, `kb_export`
- All tools have `category: "knowledge"`
- All tools have `phases: ["strategic", "tactical"]`

### 13.2 `create_kb_tools()`
- Raises `ValueError` when `knowledge_graph` is None
- Raises `ValueError` when `knowledge_store` is None
- Returns list of 10 tool functions
- Captures the running event loop at creation time for `_run_async`

### 13.3 `_get_project_id()` / `_get_project_ids()`
- `_get_project_id()` returns `context.project_id` (single project for writes)
- `_get_project_ids()` returns `context.project_ids` (multi-project for reads)

### 13.4 `kb_write`
- Returns error string when `project_id` is None/empty
- Calls `kg.create_note()` with correct params including `job_id` from context and `phase` from context config
- Calls `ks.upsert_note()` write-through with `uuid.UUID(project_id)` conversion
- pgvector write-through failure is non-fatal — Neo4j note still exists, returns success
- Returns success string with slug and type
- Returns error string on `ValueError` (invalid type/confidence)
- Returns error string on generic exception

### 13.5 `kb_update`
- Returns error string when `project_id` is None/empty
- Calls `kg.update_note()` with correct params
- Returns error string when note not found (`update_note` returns False)
- Re-reads note from Neo4j after update for write-through to pgvector
- pgvector write-through failure is non-fatal
- Returns summary string listing all changes made (content replaced, appended, status, confidence, tags, links)
- Returns error string on `ValueError`

### 13.6 `kb_read`
- Returns error string when `project_ids` is empty
- Searches across all project_ids until note found (first match wins)
- Returns formatted markdown with title, metadata, content, outgoing and incoming relationships
- Returns "not found" string when note doesn't exist in any project
- Formats outgoing relationships as `[[wikilinks]]`
- Formats incoming relationships as `[[source]] → this`

### 13.7 `kb_list`
- Returns error string when `project_ids` is empty
- Aggregates notes across all project_ids
- Returns "No knowledge notes found" with filter description when empty
- Formats each note with status icon (`●` active / `○` other), title, type, confidence

### 13.8 `kb_search`
- Returns error string when `project_ids` is empty
- Calls `ks.hybrid_search()` with `project_ids` as list of `uuid.UUID`
- Default `max_results` is 10
- Returns "No knowledge notes match" when results empty
- Truncates content preview to 200 chars + `"..."` for display
- Returns ranked results with index, note_id, title, type, confidence

### 13.9 `kb_related`
- Returns error string when `project_ids` is empty
- Aggregates related notes across all project_ids
- Passes `max_hops` to `kg.get_related()` (capped at 3 by graph)
- Formats distance as "hop" / "hops" (singular/plural)
- Shows relationship chain as `" → ".join(rel_types)`
- Shows non-active status in brackets

### 13.10 `kb_contradictions`
- Aggregates contradictions across all project_ids
- Returns "No active contradictions" when empty
- Formats pairs with bidirectional arrow (`⟷ CONTRADICTS ⟷`)

### 13.11 `kb_provenance`
- Aggregates provenance chains across all project_ids
- Indents by depth level (`'  ' * (depth - 1)`)
- Uses `↑` prefix for each chain node

### 13.12 `kb_unanswered`
- Aggregates unanswered questions across all project_ids
- Returns "No unanswered questions" when empty
- Truncates content preview to 150 chars

### 13.13 `kb_export`
- Returns error string when `project_ids` is empty
- Creates export directory if not exists (`mkdir parents=True, exist_ok=True`)
- Returns "Knowledge base is empty" when no notes
- Writes one `.md` file per note with YAML frontmatter
- Frontmatter includes: id, type, tags, keywords, confidence, status, job_id, phase, created, modified
- Groups outgoing relationships by type in `## Relationships` section
- Formats relationships as `[[wikilinks]]`
- Returns summary with count and path

---

## 14. `src/tools/knowledge/workspace_converter.py` (484 lines) — `tests/test_workspace_converter.py`

### 14.1 `_slugify()`
- Same behavior as `knowledge_graph.slugify()` (duplicate implementation)

### 14.2 `_classify_section()`
- Rules checked in priority order — first match wins
- `decision` matched by: decision, chose, picked, selected
- `learning` matched by: learned, discovered, found, issue, error, fix
- `goal` matched by: goal, objective, milestone
- `plan` matched by: plan, roadmap, phase
- `question` matched by: question, investigate, TODO, open
- `state` matched by: status, progress, current
- `code` matched by: code, implementation, module, class, function
- Default type is `"learning"` when no rule matches
- Matching is case-insensitive

### 14.3 `_extract_tags()`
- Extracts inline code identifiers (`\`SomeClass\``) as tags, lowercased, underscores → hyphens
- Extracts capitalized phrases as tags (proper nouns/terms)
- Extracts words after marker phrases (using, via, with, chose, selected, implemented)
- Filters out stop words
- Filters out tags ≤ 2 chars
- No duplicate tags
- Capped at `max_tags` (default 10)

### 14.4 `_parse_sections()`
- Splits on `##` and `###` headings only (not `#` or `####`)
- Returns `[{title: "Workspace Notes", level: "2", body: content}]` when no headings found and content non-empty
- Returns `[]` when content is empty/whitespace
- Empty body sections are filtered out
- Each section has `title`, `level`, `body` keys

### 14.5 `_infer_links()`
- Creates `REFERENCES` link when target heading title (≥4 chars, lowercased) appears in source body (lowercased)
- Does not self-link (skips when source == target)
- Short titles (< 4 chars) are excluded to avoid false positives

### 14.6 `convert_workspace()`
- Returns `[]` for empty/whitespace-only content
- Generates slug for each section; deduplicates with `-N` suffix on collision
- Falls back to `"untitled"` slug for empty titles
- Classifies each section via `_classify_section(title + " " + body)`
- Extracts tags from body
- Appends `"agent-{config_name}"` tag when config_name provided
- Appends `"migrated-from-workspace"` tag when job_id provided
- Infers cross-reference links between sections
- Output dicts have: `title`, `note_type`, `content`, `tags`, `slug`, `links`

### 14.7 `convert_and_write()`
- Returns 0 when no sections found
- Writes each note to Neo4j via `knowledge_graph.create_note()` first
- Then writes through to pgvector via `knowledge_store.upsert_note()`
- pgvector failure is non-fatal (Neo4j note still counts as written)
- Neo4j failure is non-fatal for individual notes (error logged, continues)
- Returns count of successfully written notes

---

## 15. `src/tools/knowledge/memory_migrator.py` (428 lines) — `tests/test_memory_migrator.py`

### 15.1 `_MEMORY_TYPE_MAP` constant
- Maps: `observer` → `learning`, `compaction_summary` → `state`, `phase_archive` → `retrospective`, `tool_error` → `learning`, `free` → `learning`

### 15.2 `_map_todo_completion()`
- Returns `"code"` when content matches code keywords (code, implementation, module, class, function, method, refactor, bug, patch)
- Returns `"learning"` when no code keywords match
- Case-insensitive matching

### 15.3 `_map_memory_type()`
- Returns `_map_todo_completion(content)` for `"todo_completion"` type
- Returns mapped value from `_MEMORY_TYPE_MAP` for known types
- Returns `"learning"` for unknown memory types (default)

### 15.4 `_generate_tags()`
- Uses `keywords` or `tags` field from memory row
- Handles string keywords (comma-separated) → splits to list
- Lowercases and strips all tags
- No duplicate tags
- Appends `"migrated-from-memory"` tag
- Appends `"error-solution"` tag for `tool_error` type
- Appends `"memory-{memory_type}"` tag for traceability

### 15.5 `_generate_title()`
- Takes first non-empty line of content
- Strips markdown heading markers (`# `, `## `, etc.)
- Truncates to 77 chars + `"..."` when > 80 chars
- Falls back to `"{memory_type} {id[:8]}"` when content is empty

### 15.6 `_is_duplicate()` (async)
- Uses hybrid search with content[:500] as query
- Returns `False` when search returns no results
- Computes word-level overlap between content and top result
- Returns `True` when overlap >= 0.92 (`_DUPLICATE_THRESHOLD`)
- Returns `False` on exception (non-fatal)
- Returns `False` when query_words is empty

### 15.7 `migrate_memories()` (async)
- Returns `{"migrated": 0, "skipped": 0, "duplicates": 0, "errors": 0}` on initial state
- Fetches memories via SQL: joins `memories` + `jobs` on `project_id`, filters by `min_importance`
- Returns early with 0s when no memories found
- Skips empty content (increments `skipped`)
- Maps memory type to note type via `_map_memory_type()`
- Maps importance to confidence: ≥0.8 → `high`, ≥0.5 → `medium`, <0.5 → `low`, None → None
- Checks for duplicates before writing (increments `duplicates` on match)
- Duplicate check exception is non-fatal (proceeds to write)
- Dry run mode: classifies and counts but does NOT write to databases
- Writes to Neo4j first, then pgvector write-through
- pgvector failure is non-fatal (Neo4j note still counts as migrated)
- Neo4j failure increments `errors`
- Returns final stats dict

---

## 16. Orchestrator Knowledge Endpoints (`orchestrator/main.py`) — `tests/test_knowledge_endpoints.py`

### 16.1 `_get_knowledge_graph()` (lazy singleton)
- Returns cached instance on subsequent calls
- Imports `KnowledgeGraphDB` from `src.services.knowledge_graph`
- Calls `connect()` and returns `None` when connection fails
- Returns `None` on `ImportError` or any other exception
- Adds project root to `sys.path` if needed

### 16.2 `KnowledgeSearchRequest` model
- `query`: required string
- `limit`: int, validated 1-50, default 10

### 16.3 `KnowledgeNoteUpdate` model
- `status`: optional string
- `add_tags`: optional list of strings
- `remove_tags`: optional list of strings

### 16.4 `GET /api/projects/{project_id}/knowledge/summary`
- Returns 404 when project not found
- Returns counts `by_type` (note_type → count) and `by_status` (status → count)
- Total calculated as sum of by_type values
- Returns 5 most recent notes (note_id, title, note_type, status, modified_at)
- Returns 500 on database error

### 16.5 `GET /api/projects/{project_id}/knowledge`
- Returns 404 when project not found
- Filters: `type` (alias for `note_type`), `status`, `tag` (checked via `ANY(tags)`), `job_id` (cast to UUID)
- Returns paginated result: `{notes, total, limit, offset}`
- Content truncated to 300 chars as `content_preview`
- Default limit 50, max 200
- Ordered by `modified_at DESC`
- Returns 500 on database error

### 16.6 `GET /api/projects/{project_id}/knowledge/{note_id}`
- Returns 404 when note not found in vector DB
- Removes `embedding` and `search_doc` fields from response
- Fetches `relationships` from Neo4j when `_get_knowledge_graph()` returns non-None
- Falls back to empty `relationships` list when Neo4j unavailable
- Neo4j read exception produces empty `relationships` (doesn't fail request)
- Returns 500 on database error

### 16.7 `POST /api/projects/{project_id}/knowledge/search`
- Returns 404 when project not found
- Attempts to generate embedding via `get_embedding_service()`
- Falls back to sparse-only search (tsvector `websearch_to_tsquery`) when embedding unavailable
- Dense+sparse search calls `knowledge_hybrid_search()` SQL function
- Removes `embedding` and `search_doc` from each result
- Returns `{notes, query, total}`
- Returns 500 on database error

### 16.8 `PATCH /api/projects/{project_id}/knowledge/{note_id}`
- Returns 400 when `status` not in `{active, resolved, superseded, archived}`
- Returns 404 when note not found
- Returns `{"status": "no_changes"}` when no fields provided
- Updates `status` in pgvector when provided
- Adds tags via `array_cat(tags, $N::text[])` in pgvector
- Removes tags via `array_remove(tags, $N)` in pgvector — iterates each tag
- Always updates `modified_at = NOW()` when changes made
- Updates Neo4j via `kg.update_note()` when knowledge graph available
- Neo4j update failure is non-fatal (warning logged)
- Returns `{"status": "updated"}` on success
- Returns 500 on database error

### 16.9 `DELETE /api/projects/{project_id}/knowledge/{note_id}`
- Returns 404 when DELETE affects 0 rows (`DELETE 0`)
- Deletes from pgvector (vector DB) first
- Deletes from Neo4j via `DETACH DELETE` when knowledge graph available
- Neo4j delete failure is non-fatal (warning logged)
- Returns `{"status": "deleted"}` on success
- Returns 500 on database error

---

## Priority Order

| Priority | Component | Why | Effort |
|----------|-----------|-----|--------|
| 1 | `_execute_turn()` (persistent_graph.py) | Core execution engine, highest logic density | Medium |
| 2 | `permission_check` + WS routing (persistent_app.py) | Security-critical; wrong permission logic = unauthorized tool execution | Low-Medium |
| 3 | Tool filtering + `_bind_tools` (persistent_session.py) | Wrong exclusion = phase tools leak into interactive mode | Low |
| 4 | `KnowledgeGraphDB` (knowledge_graph.py) | Source of truth for KB; validation, slugging, graph queries all need coverage | Medium |
| 5 | `KnowledgeStore` (knowledge_store.py) | Search correctness, content-hash change detection, embedding skip logic | Medium |
| 6 | `knowledge_tools.py` (10 kb_* tools) | Write-through correctness, multi-project reads, error handling | Medium |
| 7 | `_save_turn_ai_messages` + `_poll_workspace_ready` (persistent_app.py) | Complex traversal and state-machine polling logic | Medium |
| 8 | `run_persistent_loop` outer logic (persistent_graph.py) | Memory extraction triggers, auto-commit, interrupt handling | Medium |
| 9 | `knowledge_injection.py` | Simple but critical: injection pair must be recognized and excluded from summarization | Low |
| 10 | `workspace_converter.py` | Parsing, classification, tag extraction, cross-references — all pure functions, easy to test | Low-Medium |
| 11 | `memory_migrator.py` | Migration pipeline, duplicate detection, importance→confidence mapping | Medium |
| 12 | Orchestrator knowledge endpoints | CRUD + search + dual-store consistency | Medium |
| 13 | Orchestrator thread endpoints (orchestrator/main.py) | Auth + cleanup + resource lifecycle correctness | Medium-High |
| 14 | `orchestrator_client.py` new methods | HTTP contract validation (simple but important) | Low |
| 15 | `_handle_archive` + `_handle_compact` (persistent_app.py) | Session end flow, memory extraction, title generation | Low-Medium |
| 16 | Database thread methods | Query correctness (needs test DB) | Medium |
| 17 | `swap_backend` (persistent_session.py) | Hot-swap edge cases | Low |
| 18 | Config loading (InteractiveConfig) | Simple dataclass parsing | Low |
| 19 | `persistent_provisioner.py` | Mostly stubs | Low |
| 20 | Cockpit services/components (vitest) | Frontend tests | Medium |
