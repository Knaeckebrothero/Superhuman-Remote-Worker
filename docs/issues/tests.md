---
tags:
  - testing
  - quality-assurance
  - tool-development
  - research
  - documentation
---

# Test Coverage Gaps

Current state: **746 tests** across **24 test files**, covering ~45 of ~86 source modules.

---

## 1. Core Engine

The agent's brain — orchestration, state machine, phase transitions, and initialization.

| Module | Lines | Tests | Status |
|--------|-------|-------|--------|
| `src/agent.py` | 1879 | 0 | No tests |
| `src/init.py` | 443 | 0 | No tests |
| `src/core/phase.py` | 877 | ~8 | Partial (indirect via test_graph.py) |
| `src/core/workspace.py` | 671 | 0 | No dedicated tests |
| `src/core/workspace_injection.py` | 149 | 0 | No tests |
| `src/core/phase_snapshot.py` | 505 | 0 | No tests |
| `src/core/state.py` | 198 | 0 | No dedicated tests |
| `src/graph.py` (gaps) | 2203 | 63 | Covered but with gaps (see 1.8) |
| `src/core/context.py` (gaps) | 1310 | 49 | Covered but with gaps (see 1.9) |

**Already well-covered in this category:**
- `src/core/loader.py` (2325 lines) — 105 tests (prompt matrix, instruction matrix, config routing)

### 1.1 `src/agent.py` — UniversalAgent (1879 lines, 0 tests)

Main orchestration class. Everything flows through here.

**What to test:**

_Construction and initialization:_
- `__init__` — sets all component slots to None, stores config snapshot in `_base_config`
- `from_config()` — classmethod that resolves config path and creates agent
- `initialize()` — idempotent (`_initialized` guard), calls `_setup_connections` + `_create_phase_llms`
- `initialize()` — double-call returns early with warning

_Phase LLM creation (`_create_phase_llms`):_
- No phase overrides — single LLM reused for all 4 slots (strategic, tactical, summarization, base)
- With phase overrides — creates separate LLMs per phase
- LLM reuse optimization — identical configs share same LLM instance (strategic == tactical)
- Summarization fallback — no explicit override reuses strategic LLM
- Summarization dedup — matches against both strategic and tactical before creating new

_Database connections (`_setup_connections`):_
- PostgreSQL from `DATABASE_URL` env var
- Missing `DATABASE_URL` logs warning, sets `postgres_conn = None`
- `connections.postgres = False` in config skips setup entirely

_Job processing (`process_job`):_
- Auto-calls `initialize()` if not initialized
- Resets `self.config` to `_base_config` before per-job overrides
- Fresh start — creates initial state, invokes graph
- Resume path — loads from phase snapshot, discovers thread_id
- Resume fallback — tries new thread_id format, then legacy format, then starts fresh
- Resume with feedback — injects feedback via `aupdate_state`
- Streaming mode — returns async generator from `_process_job_streaming`
- Error handling — catches exceptions, returns error state dict, cleans up connections
- Cleanup — always closes datasources and checkpointer (both success and error paths)

_Workspace setup (`_setup_job_workspace`):_
- Resume + DB — loads frozen config from JSONB (`load_config_from_resolved`)
- Expert config — `metadata["config_name"]` loads named expert config
- Config upload — `metadata["config_upload_id"]` downloads from orchestrator or local filesystem
- Inline override — `metadata["config_override"]` deep-merges on top of current config
- Config cascade — expert -> upload -> inline, each overrides the previous
- LLM recreation — triggers `_create_phase_llms()` when config changed
- Config freeze — stores resolved config to JSONB on first run (not resume)
- Document handling — upload_id, document_paths (list), document_path (single), zip extraction
- Zip extraction — `_extract_zip()` skips hidden files, `__MACOSX`, preserves directory structure
- Filename conflict — appends `_1`, `_2` etc. when destination file exists
- Pod handoff — clones workspace from Gitea when resuming on new pod
- Resume existing workspace — writes missing instructions.md, initializes git
- Requirement data — writes `analysis/requirement_input.md` for validator agent
- Instruction files — copies from config with FileResolver, skips todo_guide.md (handled separately)
- Git repo clone — `metadata["git_url"]` clones repo, injects context into workspace.md
- Git clone fallback — retries without `--branch` if branch doesn't exist

_Tool setup (`_setup_job_tools`):_
- Datasource connections — creates Neo4j, PostgreSQL, MongoDB from job metadata
- ToolContext creation — injects workspace, todo, postgres, datasources, config
- Tool loading — loads from registry, falls back to individual loading on error
- Description overrides — `apply_description_overrides()` for domain tools
- Instruction enforcement — `apply_instruction_enforcement()` wraps tools
- Parallel tool calls — disabled for o1/o3/o4 models
- Phase-specific LLM binding — strategic and tactical get separate bound LLMs
- Background document registration — fires-and-forgets `_register_initial_documents_background`

_Document registration (`_register_initial_documents`):_
- Scans `documents/` for supported extensions (PDF, DOCX, etc.)
- Skips `documents/external/` (web content)
- Parallel processing with ThreadPoolExecutor (max 4 workers)
- Each worker creates independent CitationEngine instance (thread safety)
- Non-fatal — failures logged but don't block job

_Datasource connections (`_create_datasource_connection`):_
- Neo4j — creates `Neo4jDB`, calls `connect()`
- PostgreSQL — `psycopg.connect()`, test query, rollback
- MongoDB — `MongoClient` with ping, extracts DB name from URL path
- Unknown type — raises `ValueError`

_Job approval (`approve_frozen_job`):_
- Reads `output/job_frozen.json`, converts to `job_completion.json`
- Removes frozen marker, updates DB status to 'completed'
- Error: workspace doesn't exist, job not frozen

_Other:_
- `shutdown()` — closes postgres, sets `_initialized = False`
- `get_status()` — returns agent metrics dict
- `_AiosqliteConnectionWrapper` — adds `is_alive()` for langgraph 3.x compatibility
- `_format_requirement_as_markdown()` — handles `source_location` as JSON string or dict
- `_inject_repo_context_to_workspace()` — parses git URL, builds Gitea API base, appends to workspace.md

**Approach:** Heavy mocking of LLM, database, workspace, and graph. Test the orchestration/wiring logic.

### 1.2 `src/init.py` — Workspace Initialization (443 lines, 0 tests)

Standalone workspace management (not the per-job setup in agent.py — that's `_setup_job_workspace`). This is the CLI entrypoint for `python -m src.init`.

**What to test:**

_Path resolution (`get_workspace_base_path`):_
- `WORKSPACE_PATH` env var takes priority
- Falls back to `/workspace` if it exists (container mode)
- Falls back to `./workspace` relative to project root (dev mode)

_Initialization (`init_workspace`):_
- Creates base directory + `checkpoints/` and `logs/` subdirectories
- Idempotent — safe to call multiple times
- Returns `False` on exception

_Cleanup (`cleanup_workspace`):_
- Removes all contents, preserves base directory
- Handles non-existent workspace gracefully
- Counts removed items, reports errors
- Returns `True` when workspace already empty

_Verification (`verify_workspace`):_
- Returns dict with exists, job_count, checkpoint_count, log_count, total_size_bytes
- Handles non-existent workspace
- Checks for standard subdirectories

_Backup/Restore:_
- `backup_workspace()` — copies entire workspace tree, counts jobs and checkpoints
- `restore_workspace()` — clears existing, copies from backup
- Handles empty workspace, non-existent backup
- Error paths — failed copy, permission denied

_CLI (`main`):_
- `--verify` flag runs verification only
- `--force-reset` runs cleanup then init
- `--backup PATH` creates backup
- `--restore PATH` restores from backup
- Return codes — 0 on success, 1 on failure

### 1.3 `src/core/phase.py` — Phase Transitions (877 lines, ~8 indirect tests)

The graph tests cover routing decisions and the `TestHandleTransitionNode` class (3 tests), but most of the actual transition logic is untested directly.

**What to test:**

_Predefined todo loading:_
- `get_initial_strategic_todos()` — loads from `strategic_todos_initial.yaml` template
- `get_transition_strategic_todos()` — loads from `strategic_todos_transition.yaml` template
- `get_resume_strategic_todos()` — loads from `strategic_todos_resume.yaml` template
- All three: fallback to hardcoded defaults when template not found
- PredefinedTodo `to_dict()` — produces TodoManager-compatible format

_YAML validation (`validate_todos_yaml`):_
- Already tested (5 tests in `TestTodosYamlValidation`) — valid, invalid syntax, too few, too many, missing fields
- **Missing:** duplicate IDs, content too short (<10 chars), non-dict items in list, non-string content, empty file, non-mapping root, integer content values

_Transition rejection (`reject_transition`):_
- Returns `TransitionResult(success=False)` with ToolMessage error
- Error message includes reason

_Job finalization (`finalize_job`):_
- Writes `output/job_frozen.json` with summary, deliverables, confidence, timestamp
- Includes optional `notes` field from final_data
- Archives remaining todos
- Git: commits, tags `job-frozen`, pushes
- Clears final phase data after freeze
- Returns `TransitionResult` with `should_stop=True`, `goal_achieved=True`
- Fallback data when `get_final_phase_data()` returns None

_Strategic -> Tactical (`on_strategic_phase_complete`):_
- Detects final phase (`is_final_phase` or `get_final_phase_data`) and delegates to `finalize_job`
- Rejects transition when no staged todos exist
- Applies staged todos to TodoManager
- Exports todo state for checkpointing
- Git: tags phase, commits, pushes
- Returns `TransitionResult` with flipped phase, incremented phase_number
- Phase marker message content includes phase name and todo count

_Tactical -> Strategic (`on_tactical_phase_complete`):_
- Loads predefined transition strategic todos from config
- Sets todos via `set_todos_from_list()`
- Git: tags phase, commits, pushes
- Returns `TransitionResult` with `is_strategic_phase=True`, incremented phase_number

_`_complete_phase_with_git`:_
- Creates tag `phase-{n}-{type}-complete`
- Commits with descriptive message including archived todo count
- Pushes to remote
- No-op when git manager is None or inactive
- Exception does not fail the transition

_Router (`handle_phase_transition`):_
- Delegates to `on_strategic_phase_complete` when `is_strategic_phase=True`
- Delegates to `on_tactical_phase_complete` when `is_strategic_phase=False`

### 1.4 `src/core/workspace.py` — WorkspaceManager (671 lines, 0 dedicated tests)

Manages per-job directories. Used by every tool and every graph node. Zero dedicated tests.

**What to test:**

_Config (`WorkspaceManagerConfig`):_
- Default values — structure list, git_versioning=True, git_ignore_patterns
- `from_dict()` — creates from dictionary, uses defaults for missing keys

_Path resolution:_
- `get_workspace_base_path()` — env var > container > dev mode (same logic as init.py, test both)
- `get_checkpoints_path()` — returns `workspace/checkpoints/`, creates if needed
- `get_logs_path()` — returns `workspace/logs/`, creates if needed

_WorkspaceManager construction:_
- `__init__` with explicit `base_path` override
- `__init__` with `config.base_path`
- `__init__` with default (uses `get_workspace_base_path()`)
- Job-specific path: `base_path / job_{job_id}`

_Initialization:_
- `initialize()` — creates workspace root + all subdirectories from config.structure
- `initialize()` — initializes git when `git_versioning=True`
- `is_initialized` — True when directory exists (even without explicit init)

_Git initialization (`_initialize_git`):_
- Creates `GitManager`, calls `init_repository()` with ignore patterns
- Configures remote when `git_remote_url` provided
- Sets `_git_manager = None` on failure

_Path safety:_
- `get_path("")` — returns workspace root resolved
- `get_path("subdir/file.txt")` — returns correct absolute path
- `get_path("../../etc/passwd")` — raises `ValueError` (path traversal)
- `get_path("../other_job/secrets")` — raises `ValueError`

_File operations:_
- `read_file()` — reads UTF-8 content
- `read_file()` — raises `FileNotFoundError` for missing file
- `read_file()` — raises `ValueError` for directory path
- `write_file()` — writes content, creates parent directories
- `write_file()` — returns absolute path
- `append_file()` — appends to existing file
- `append_file()` — creates file if not exists
- `exists()` — checks file/directory existence

_Directory operations:_
- `create_directory()` — creates with parents
- `delete_directory()` — removes recursively
- `delete_directory()` — raises `ValueError` for workspace root
- `delete_directory()` — returns `False` for non-existent
- `delete_file()` — deletes file, returns `True`
- `delete_file()` — deletes empty directory
- `delete_file()` — raises `ValueError` for non-empty directory
- `delete_file()` — returns `False` for non-existent

_Move/Copy:_
- `move_file()` — moves file, creates parent dirs
- `move_file()` — raises `FileNotFoundError` for missing source
- `copy_file()` — copies with metadata preservation
- `copy_file()` — raises `ValueError` for directory source

_Listing and search:_
- `list_files()` — returns sorted relative paths, dirs get trailing slash
- `list_files()` — with glob pattern filtering
- `list_files()` — returns empty list for non-existent directory
- `search_files()` — finds text in files, returns path/line_number/line
- `search_files()` — case insensitive by default
- `search_files()` — skips binary files (.pdf, .docx, .png, etc.)
- `search_files()` — searches single file when path is a file

_Summary and size:_
- `get_size()` — returns 0 for non-existent path
- `get_size()` — returns file size for single file
- `get_size()` — sums all files in directory recursively
- `get_summary()` — returns file counts and sizes by configured subdirectory
- `get_summary()` — handles non-existent workspace

_Cleanup:_
- `cleanup()` — removes entire workspace directory tree
- `cleanup()` — returns `False` for non-existent workspace
- `cleanup()` — sets `_initialized = False`

### 1.5 `src/core/workspace_injection.py` — Transient Memory Injection (149 lines, 0 tests)

Creates synthetic tool call messages to inject workspace.md, todos, and instruction files into LLM context without storing them in state.

**What to test:**

_Workspace injection (`create_workspace_tool_messages`):_
- Returns `(AIMessage, ToolMessage)` tuple
- AIMessage has empty content and single tool_call for `read_file("workspace.md")`
- ToolMessage content matches input workspace_content
- tool_call_id matches between AIMessage and ToolMessage
- tool_call_id starts with `WORKSPACE_TOOL_CALL_ID_PREFIX`

_Todos injection (`create_todos_human_message`):_
- Returns `HumanMessage` with content wrapped in `<active_tasks>` tags
- Content starts with `TODOS_INJECTION_CONTENT_PREFIX`

_Instruction injection (`create_instruction_tool_messages`):_
- Returns `(AIMessage, ToolMessage)` tuple with instruction file content
- file_path passed correctly in tool_call args
- tool_call_id starts with `INSTRUCTION_TOOL_CALL_ID_PREFIX`

_Detection (`is_workspace_injection_message`):_
- Detects workspace ToolMessage by tool_call_id prefix
- Detects instruction ToolMessage by tool_call_id prefix
- Detects workspace AIMessage by tool_call id in tool_calls list
- Detects todo HumanMessage by content prefix
- Returns `False` for regular HumanMessage, AIMessage, ToolMessage
- Returns `False` for AIMessage with non-injection tool_calls
- Handles AIMessage with empty tool_calls list
- Handles messages without expected attributes (getattr safety)

### 1.6 `src/core/phase_snapshot.py` — Phase Recovery (505 lines, 0 tests)

User-facing `--recover-phase` feature. Completely untested.

**What to test:**

_PhaseSnapshot dataclass:_
- `from_dict()` — creates from full dict
- `from_dict()` — handles missing optional fields (todos_completed, todos_total, thread_id)
- `to_dict()` — round-trips through `from_dict()`

_`discover_thread_id_from_checkpoint`:_
- Returns thread_id with most checkpoints matching job_id
- Returns `None` when checkpoint file doesn't exist
- Returns `None` when no rows match
- Returns `None` when no thread_ids contain job_id
- Handles corrupt/invalid SQLite file gracefully

_PhaseSnapshotManager construction:_
- Default base_path from `get_workspace_base_path()`
- Custom base_path override (for testing)
- `snapshots_dir` property returns correct path

_`create_snapshot`:_
- Copies checkpoint.db to snapshot directory
- Copies workspace.md, plan.md, todos.yaml
- Copies archive/ directory recursively
- Removes existing archive/ in snapshot before copying fresh
- Writes metadata.json with all PhaseSnapshot fields
- Returns PhaseSnapshot on success
- Returns `None` on exception
- Handles missing checkpoint.db (logs warning, continues)
- Handles missing workspace files (skips gracefully)
- Handles empty archive/ (no copy)
- Stores thread_id in metadata for resume

_`list_snapshots`:_
- Returns empty list when no snapshots directory exists
- Returns sorted list of PhaseSnapshot by phase_number
- Skips non-directory entries
- Skips directories not matching `phase_*` pattern
- Skips directories with invalid/missing metadata.json
- Handles corrupt JSON in metadata.json

_`get_snapshot`:_
- Returns PhaseSnapshot for existing phase
- Returns `None` for non-existent phase
- Handles corrupt metadata.json

_`recover_to_phase`:_
- Restores checkpoint.db (backs up current first)
- Restores workspace.md, plan.md, todos.yaml
- Restores archive/ directory (removes current archive first)
- Clears archive/ when snapshot has no archive
- Keeps existing files not in snapshot (e.g. instructions.md)
- Returns `True` on success
- Returns `False` when snapshot not found
- Returns `False` on exception
- Creates checkpoints directory if needed

_`delete_snapshots_after`:_
- Deletes all snapshots with phase_number > given number
- Returns count of deleted snapshots
- Returns 0 when no snapshots to delete
- Does not delete the given phase number itself

_`cleanup`:_
- Removes entire snapshots directory
- Returns `True` when directory doesn't exist
- Returns `False` on exception

_`get_latest_snapshot`:_
- Returns last snapshot in sorted list
- Returns `None` when no snapshots

_`format_snapshots_table`:_
- Returns "No phase snapshots available." for empty list
- Formats table with columns: Phase, Type, Iter, Messages, Todos, Timestamp
- Handles strategic/tactical type names
- Handles todos_total = 0 (shows "-")
- Shows total count at bottom

### 1.7 `src/core/state.py` — Agent State (198 lines, 0 dedicated tests)

Tested implicitly by graph tests, but `create_initial_state()` deserves direct tests.

**What to test:**

_`create_initial_state`:_
- Returns `UniversalAgentState` with correct defaults
- `messages` is empty list
- `initialized = False`, `phase_complete = False`, `goal_achieved = False`
- `is_strategic_phase = True` (starts in strategic mode)
- `phase_number = 1`
- `iteration = 0`
- `should_stop = False`
- `metadata` defaults to empty dict when None passed
- `metadata` preserves passed dict
- `todo_next_id = 1`
- All Optional fields are None (error, context_stats, tool_retry_state, etc.)

### 1.8 `src/graph.py` — Gaps in Existing Coverage (2203 lines, 63 tests)

The 63 tests cover routing functions, node creation, and phase alternation cycles well. But several areas have no coverage:

**What's missing:**

_Error handling in `create_execute_node`:_
- `ContextOverflowError` — triggers emergency compaction and retry
- Rate limit detection (`_extract_rate_limit_delay`) — exponential backoff
- Tool use failure (`_extract_tool_use_failed`) — feedback injection
- Consecutive LLM error counting — increments, caps at threshold, returns error state
- `_is_tool_error()` — detects error patterns in tool message content
- `_extract_markdown_content()` — extracts markdown from code blocks

_`create_restore_from_feedback_node`:_
- Loads resume strategic todos
- Writes feedback to `feedback.md` in workspace
- Clears `resume_feedback` from state
- Handles missing feedback gracefully

_`create_audited_tool_node`:_
- Archives tool calls and results to MongoDB
- Handles archiver being None
- Correctly associates tool results with their calls

_`build_phase_alternation_graph`:_
- Returns compiled StateGraph
- All nodes connected correctly
- Conditional edges wired properly
- Accepts all required parameters

_`run_graph_with_streaming`:_
- Yields state updates from async graph execution
- Handles errors during streaming

_`create_archive_phase_node` (partially covered — 3 tests):_
- Phase snapshot creation at phase boundary
- Todo state export for checkpointing
- Context compaction statistics tracking

_Helper functions:_
- `_extract_rate_limit_delay()` — parses delay from various error formats
- `_extract_tool_use_failed()` — extracts failed generation from OpenAI errors
- `_build_tool_use_failed_feedback()` — constructs correction message
- `_is_tool_error()` — pattern matching for error content

### 1.9 `src/core/context.py` — Gaps in Existing Coverage (1310 lines, 49 tests)

The 49 tests cover summarization (split, format, single-pass, recursive) and force-compaction. But several areas have no coverage:

**What's missing:**

_`ContextManager` methods not tested:_
- `set_current_phase()` — stores current phase
- `get_token_count()` — counts tokens in message list
- `should_compact()` — threshold-based decision (token count > limit)
- `should_summarize()` — message count threshold check
- `clear_old_tool_results()` — preserves N most recent, clears older ones
- `truncate_long_tool_results()` — shortens tool results exceeding max length
- `prepare_messages_for_llm()` — full message preparation pipeline
- `trim_messages()` — removes messages to fit within token budget
- `summarize_and_compact()` — full compaction pipeline (summarize + trim + sanitize)
- `create_pre_model_hook()` — returns callable that validates before LLM call

_`ToolRetryManager` (entire class untested):_
- `get_retry_delay()` — exponential backoff calculation
- `should_retry()` — checks attempt count against max
- `record_failure()` — increments failure count per tool
- `record_retry()` — tracks retry attempts
- `get_stats()` — returns failure/retry statistics

---

## 2. Tool Infrastructure

The framework layer that tools depend on — registry, dependency injection, documentation generation.

| Module | Lines | Tests | Status |
|--------|-------|-------|--------|
| `src/tools/registry.py` | 537 | 64 | Covered (`test_tool_registry.py`) |
| `src/tools/context.py` | 511 | 60 | Covered (`test_tool_context.py`) |
| `src/tools/description_manager.py` | 366 | 0 | No tests |
| `src/api/persistent_session.py` | 451 | 68 | Covered (`test_persistent_session.py`) |
| `src/tools/coding/shell_manager.py` | ~550 | 63 | Covered (`test_shell_manager.py`) |

### 2.1 `src/tools/registry.py` — Tool Registry + Phase Filtering (537 lines, 64 tests)

Controls which tools are available per phase. A bug here could expose `job_complete` to tactical phases or block strategic-only tools. Also handles tool loading with dependency injection, category grouping, custom tool registration, and instruction enforcement wrappers.

**What to test:**

_`TOOL_REGISTRY` metadata integrity:_
- Every registered tool has a `category` field
- Every registered tool has a `phases` list (or correctly defaults to both)
- Known strategic-only tools: `job_complete`, `next_phase_todos` — must have `phases: ["strategic"]`
- Known tactical-only tools: `todo_rewind` — must have `phases: ["tactical"]`
- Both-phase tools: `mark_complete`, `todo_complete`, `todo_list` — must have both phases
- All 10 category modules register at least one tool (workspace, core, document, research, citation, graph, sql, mongodb, git, coding)
- No duplicate tool names across categories

_`get_available_tools()`:_
- Returns a copy (modifying result doesn't affect TOOL_REGISTRY)
- Contains all registered tools

_`get_tools_by_category(category)`:_
- `"workspace"` returns file and filesystem tools
- `"core"` returns todo + job tools
- Unknown category returns empty list
- Each known category returns non-empty list

_`get_categories()`:_
- Returns set containing at least: workspace, core, document, research, citation, graph, sql, mongodb, git, coding

_`filter_tools_by_phase(tool_names, phase)`:_
- `"strategic"` includes `job_complete`, `next_phase_todos`, excludes `todo_rewind`
- `"tactical"` includes `todo_rewind`, excludes `job_complete`, `next_phase_todos`
- Both phases include `mark_complete`, `todo_complete`, `read_file`, `write_file`
- Unknown tool names are silently dropped (not in TOOL_REGISTRY → skipped)
- Empty tool_names list returns empty list
- Unknown phase name returns empty list (no tool has that phase)

_`get_tools_for_phase(phase)`:_
- Returns all non-placeholder tools available in the given phase
- Placeholder tools are excluded even if their phases match
- Strategic phase returns superset of strategic-only + both-phase tools
- Tactical phase returns superset of tactical-only + both-phase tools

_`get_phase_tool_summary()`:_
- Returns dict with "strategic" and "tactical" keys
- Each contains dict of category → tool name lists
- Placeholder tools excluded
- Unknown phases in tool metadata ignored (no KeyError)

_`load_tools(tool_names, context)`:_
- Loads requested tools by name, returns list of LangChain Tool objects
- Raises `ValueError` for unknown tool names (with list of available tools)
- Raises `ValueError` for placeholder tools (with message about later phases)
- Raises `ValueError` when workspace tools requested without workspace_manager
- Raises `ValueError` when core tools requested without todo_manager
- Raises `ValueError` when core tools requested without workspace_manager
- Graph tools: logs warning when no neo4j datasource, doesn't raise
- SQL tools: logs warning when no postgresql datasource, doesn't raise
- MongoDB tools: logs warning when no mongodb datasource, doesn't raise
- Git tools: logs warning when no git_manager on workspace_manager
- Document/research/citation tools: catches exceptions and logs warning (doesn't raise)
- Empty tool_names list returns empty list (no tools to load)
- Returned tools have correct `.name` attribute matching requested names
- Only requested tools are returned (not all tools in the category)

_`load_tools_for_phase(tool_names, phase, context)`:_
- Combines filter_tools_by_phase + load_tools in one call
- Logs warning when filtering removes all tools
- Returns empty list when no tools survive phase filtering

_`load_tools_by_category(category, context)`:_
- Loads all non-placeholder tools in a category
- Placeholder tools are filtered out before loading

_`register_tool()`:_
- Adds new tool to TOOL_REGISTRY with all provided metadata
- Overwrites existing tool with same name (logs warning)
- Custom kwargs are stored as additional metadata

_`unregister_tool()`:_
- Removes tool from TOOL_REGISTRY, returns True
- Returns False for non-existent tool name
- Tool is no longer returned by get_available_tools() after removal

_`apply_instruction_enforcement(tools, context)`:_
- Returns tools unmodified when context has no instruction_files
- Returns tools unmodified when no entries have enforce=True + before_tool trigger
- Wraps tool.func for tools matching enforcement entries
- Wrapped tool returns error string when required file not recently read
- Wrapped tool calls original func when required file was recently read
- Multiple enforcement files on same tool — all must be read
- Enforcement applies to correct tool only (other tools unaffected)
- Original tool function still accessible through wrapper (functools.wraps)

### 2.2 `src/tools/context.py` — ToolContext (511 lines, 0 tests)

Every tool receives a `ToolContext` instance for workspace, database handles, config, and citation engine access. Includes read-tracking, instruction enforcement, phase-aware multimodal config, source registration, web content saving, and async job status updates.

**What to test:**

_Construction and validation (`__init__`, `__post_init__`):_
- Default construction: all fields are None/empty
- With workspace_manager: must be initialized (raises ValueError if not)
- With uninitialized workspace_manager: raises ValueError with clear message
- All optional fields accept None gracefully

_`job_id` property:_
- Returns `_job_id` when set directly
- Falls back to `workspace_manager.job_id` when `_job_id` is None
- Returns None when neither is available
- `_job_id` takes priority over workspace_manager.job_id
- Setter stores value in `_job_id`

_Availability checks (`has_*` methods):_
- `has_workspace()` — True with workspace_manager, False without
- `has_todo()` — True with todo_manager, False without
- `has_postgres()` — True with postgres_db, False without
- `has_git()` — True only when workspace_manager exists AND git_manager exists AND git_manager.is_active
- `has_git()` — False when no workspace, False when git_manager is None, False when git_manager.is_active is False
- `has_datasource("neo4j")` — True when datasources dict has non-None neo4j entry
- `has_datasource("neo4j")` — False when key missing or value is None
- `get_datasource("neo4j")` — returns connection object or None

_`db` property:_
- Returns postgres_db when set
- Returns None when not set

_`get_config(key, default)`:_
- Returns value for existing key
- Returns default for missing key
- Returns None as default when no default provided

_Read tracking (`record_file_read`, `was_recently_read`):_
- `record_file_read("foo.md")` → `was_recently_read("foo.md")` returns True
- Path normalization: leading slash stripped (`"/foo.md"` → `"foo.md"`)
- Path normalization: whitespace stripped
- Deque maxlen=10: recording 11th file evicts the oldest
- Re-recording moves file to end of deque (doesn't duplicate)
- `was_recently_read()` returns False for never-read path
- `get_read_tracking_limit()` returns config value or default 10

_Instruction enforcement (`get_enforcement_files`, `check_tool_enforcement`):_
- `get_enforcement_files("next_phase_todos")` — returns file paths for matching entries
- Only matches entries with `enforce=True` AND `trigger_type="before_tool"` AND matching `trigger_target`
- Returns empty list when no enforcement entries match
- `check_tool_enforcement()` — returns None when all required files were recently read
- `check_tool_enforcement()` — returns error string when a required file was not read
- Error string includes the file path and tool name

_Phase instruction files (`get_phase_instruction_files`):_
- Returns entries with `trigger_type="phase"` matching given phase name
- Returns empty list when no entries match
- Filters correctly: "strategic" entries not returned for "tactical" query

_Phase and multimodal (`set_current_phase`, `get_phase_multimodal`):_
- `set_current_phase("strategic")` stores phase
- `get_phase_multimodal()` with llm_config + current_phase: uses phase-specific config
- `get_phase_multimodal()` without llm_config: falls back to `config["multimodal"]`
- `get_phase_multimodal()` without anything: returns False (default)

_Citation engine (`get_citation_engine`, `close_citation_engine`):_
- `get_citation_engine()` — lazy init on first call (creates CitationEngine)
- `get_citation_engine()` — returns cached instance on second call (same object)
- `close_citation_engine()` — closes engine, clears cache and source registry
- `close_citation_engine()` — safe to call when engine is None (no-op)

_Source registration (`get_or_register_doc_source`, `get_or_register_web_source`):_
- `get_or_register_doc_source(path)` — returns source_id, caches in `_source_registry`
- Second call with same path returns cached id (doesn't re-register)
- `get_or_register_web_source(url)` — returns (source_id, fetch_error) tuple
- Caches URL in `_source_registry`, returns cached on second call
- fetch_error is None on success, string on failure
- Failed URLs cached in `_inaccessible_sources`, returned on repeat calls

_Web content saving (`save_web_content_to_disk`):_
- Generates deterministic filename from URL hash (same URL → same file)
- Writes markdown with YAML front-matter (url, title, fetched_at, source_id)
- Returns workspace-relative path (e.g., `"documents/external/example_com_a1b2c3d4.md"`)
- Skips write if file already exists (first save wins, returns existing path)
- Returns None when no workspace_manager available
- Returns None on write failure (logs warning)
- Title with quotes is escaped in YAML front-matter
- Creates `documents/external/` directory if it doesn't exist
- Domain sanitization: special chars replaced with underscore

_Job status update (`update_job_status`):_
- Raises ValueError when no job_id available
- Returns False when no postgres_db available
- Executes correct SQL for status-only update
- Executes correct SQL for status + completed_at update
- Executes correct SQL for status + completed_at + error_message update
- Returns True on success
- Returns False on database exception (logs error)

### 2.3 `src/tools/description_manager.py` — DescriptionManager (366 lines, 0 tests)

Auto-generates per-tool markdown documentation into `workspace/job_<id>/tools/`. Also manages description overrides for deferred tools (tools whose full docs live in workspace files, so they get short descriptions for LLM binding).

**What to test:**

_`DescriptionManager` class:_

_`extract_docstrings(tools)`:_
- Extracts `tool.description` from each tool object and caches by `tool.name`
- Handles tools without `name` or `description` attributes gracefully (skips them)
- Overwrites cache entry on re-extraction

_`get_docstring(tool_name)`:_
- Returns cached docstring when available
- Falls back to TOOL_REGISTRY description when not cached
- Returns None when tool not in cache or registry

_`generate_tool_description(tool_name)`:_
- Returns markdown with `# tool_name` header
- Includes `**Category:**` from registry metadata
- Includes docstring when available (from cache)
- Falls back to registry description when no docstring cached
- Returns "Tool not found" message for unknown tool name

_`generate_tool_index(tool_names)`:_
- Groups tools by category
- Follows defined category order: workspace, core, document, research, citation, graph, other
- Creates markdown links to individual tool docs (`[tool_name](tool_name.md)`)
- Includes short description from TOOL_REGISTRY
- Handles tools not in registry (grouped under "other")

_`generate_workspace_docs(tool_names, output_dir)`:_
- Creates output directory if it doesn't exist
- Writes `README.md` (index) to output_dir
- Writes `<tool_name>.md` for each tool
- Returns count of files created (1 index + N tool docs)
- Overwrites existing files on regeneration

_`apply_overrides(tools)`:_
- Tools with `defer_to_workspace=True` in registry get `short_description` applied
- Tools without `defer_to_workspace` are returned unchanged
- Deferred tools missing `short_description` are returned unchanged (logs warning)
- Returns new list (doesn't modify input list)
- Logs count of deferred vs full-description tools

_`_copy_with_description(tool, new_description)`:_
- Uses `model_copy()` for Pydantic v2 StructuredTool
- Falls back to `.copy()` for older Pydantic
- Last resort: modifies tool in place (logs warning)

_Module-level convenience functions:_

_`generate_workspace_tool_docs(tool_names, output_dir, tools)`:_
- Uses singleton manager
- Extracts docstrings from tools if provided before generating docs
- Without tools parameter, still generates docs (from registry descriptions)

_`apply_description_overrides(tools)`:_
- Extracts docstrings first, then applies overrides
- Uses singleton manager

_`get_deferred_tools()`:_
- Returns tool names with `defer_to_workspace=True` in registry
- Currently includes MongoDB and SQL tools

_`get_core_tools()`:_
- Returns tool names WITHOUT `defer_to_workspace` (or False)
- Complement of `get_deferred_tools()`

_Singleton pattern (`_get_manager`):_
- First call creates DescriptionManager
- Second call returns same instance
- State (docstring cache) persists across calls

---

## 3. Tool Implementations

Actual tools the LLM calls. Grouped by tool category. All tools use closure-based dependency injection (created inside factory functions with captured `ToolContext`). Tests should mock the workspace manager and any external dependencies, then invoke the tool functions directly.

### 3a. Workspace Tools

| Module | Lines | Tests | Status |
|--------|-------|-------|--------|
| `src/tools/workspace/files.py` | 743 | 16 | Partial (read-tracking only) |
| `src/tools/workspace/filesystem.py` | 680 | 0 | No tests |

#### 3a.1 `src/tools/workspace/files.py` — File Operations (743 lines, 16 existing tests)

The 16 existing tests only cover read-tracking and edit/write guards. The tool functions themselves (`read_file`, `write_file`, `edit_file`) and all visual/document handling are untested.

**What to test:**

_`create_file_tools(context)`:_
- Raises ValueError when context has no workspace_manager
- Returns list of 3 tools: read_file, write_file, edit_file

_`read_file` tool:_
- Basic text file read — returns content with line numbers (cat -n format)
- Line-based pagination — `offset=10, limit=5` returns lines 10-14
- Default limit is 2000 lines, max capped at 2000
- Continuation hint appended when more lines exist (`[Lines X-Y of Z. Use offset=...]`)
- Long line truncation — lines > 2000 chars get `...` appended
- `offset < 1` returns error
- `offset > total_lines` returns error
- File not found returns error string (not exception)
- Path is a directory returns error suggesting `list_files`
- Records successful read via `context.record_file_read()`
- Does not record read on error

_`read_file` — PDF handling:_
- Reads PDF with page range (`page_start=1, page_end=3`)
- Default reads from page 1 within word limit
- Shows `[Page X of Y]` or `[Pages X-Y of Z]` header
- Truncated PDFs show continuation guidance
- pdfplumber not available returns install error
- Invalid page range returns error

_`read_file` — image handling:_
- Multimodal mode: returns base64-encoded image data with mime type
- Text-only mode: returns AI-generated description (mock VisionHelper)
- Vision services not available: returns graceful fallback message
- Description cache: hits cache on second read (mock DescriptionCache)
- Records read via `context.record_file_read()` on success

_`read_file` — visual documents (PPTX, DOCX):_
- PPTX: extracts text per slide + visual content
- PPTX: slide range validation (`slide_start > total` returns error)
- DOCX: extracts paragraphs + table text
- python-pptx/python-docx not installed returns install error

_`write_file` tool:_
- Creates new file, returns confirmation with byte count
- Creates parent directories automatically
- Blocks binary file extensions (.pdf, .png, .zip, etc.) — returns error
- Enforces word limit (default 10,000) — returns error with suggestion to split
- Enforces read-before-write for existing files — error if not recently read
- New file does not require prior read
- After `context.record_file_read(path)`, overwrite succeeds

_`edit_file` tool:_
- Replace mode: `old_string` found once → replaced, returns "Edited: path"
- Replace mode: `old_string` not found → error with file preview
- Replace mode: `old_string` found multiple times → error with count
- Replace mode: empty `old_string` → error suggesting append/prepend
- Append mode: `position="end"` → content added to end
- Prepend mode: `position="start"` → content added to start
- Invalid position value → error message
- Enforces read-before-write — error if not recently read
- File not found → error
- Path is directory → error

_Helper functions:_
- `_is_image_file()` — recognizes all IMAGE_EXTENSIONS
- `_is_visual_document()` — recognizes .pdf, .pptx, .docx
- `_get_mime_type()` — returns correct mime or fallback

#### 3a.2 `src/tools/workspace/filesystem.py` — Filesystem Operations (680 lines, 0 tests)

11 tools for filesystem navigation, CRUD, and workspace inspection.

**What to test:**

_`create_filesystem_tools(context)`:_
- Raises ValueError without workspace_manager
- Returns 11 tools

_`list_files` tool:_
- Lists root directory contents (files and directories)
- Directories shown with trailing slash
- Pattern filtering (e.g., `pattern="*.md"`)
- Depth=0: flat listing, no subdirectory contents
- Depth=1 (default): shows one level of subdirectory contents
- Depth capped at 3 (values > 3 treated as 3)
- Empty directory returns "No files found" message
- Pattern filter with no matches returns message with pattern info

_`delete_file` tool:_
- Deletes existing file, returns confirmation
- Non-existent file returns "Not found"
- Path validation (workspace boundary)

_`search_files` tool:_
- Finds matching text, returns file path + line number + context
- Case-insensitive by default
- `case_sensitive=True` respects case
- Results grouped by file
- Long matching lines truncated to 100 chars
- Results capped at `max_search_results` (default 50)
- Shows count message when results exceed limit
- No matches returns "No matches found"

_`file_exists` tool:_
- Existing file returns "Exists (file)" with size
- Existing directory returns "Exists (directory)"
- Non-existent path returns "Not found"

_`move_file` tool:_
- Moves file to new location
- Source not found returns error
- Path validation for workspace boundary

_`rename_file` tool:_
- Renames file in same directory
- Constructs correct new path (directory preserved)
- File not found returns error

_`copy_file` tool:_
- Copies file to destination
- Source not found returns error

_`get_workspace_summary` tool:_
- Returns job ID, total files, total size
- Lists directories with file counts and sizes

_`get_document_info` tool:_
- PDF: returns page count, estimated tokens, reading suggestions
- PDF exceeding word limit: suggests page-by-page approach
- Non-PDF: returns basic file info with word estimate
- File not found returns error

_`create_directory` tool:_
- Creates directory and parents
- Path validation for workspace boundary

_`delete_directory` tool:_
- Deletes directory and contents recursively
- Non-existent directory returns "Not found"
- Root deletion prevented (workspace boundary)

_`_infer_file_purpose()`:_
- Known files: plan.md → "Execution plan", etc.
- Pattern-based: "chunk_1.md" → "Document chunk"
- Extension-based: ".pdf" → "Source document"
- Unknown → "Working file"

### 3b. Core Tools (Todo + Job)

| Module | Lines | Tests | Status |
|--------|-------|-------|--------|
| `src/tools/core/todo.py` | 371 | 0 | No tests (TodoManager tested, tool wrappers not) |
| `src/tools/core/job.py` | 257 | 0 | No tests |

#### 3b.1 `src/tools/core/todo.py` — Todo Tool Wrappers (371 lines, 0 tests)

The `TodoManager` is well-tested (73 tests), but the tool layer that converts LLM tool calls to manager operations is not. These tools add validation, formatting, enforcement, and phase transition signaling on top of the manager.

**What to test:**

_`create_todo_tools(context)`:_
- Raises ValueError without todo_manager
- Returns 4 tools: next_phase_todos, todo_complete, todo_list, todo_rewind

_`next_phase_todos` tool:_
- Valid list of 5+ strings → calls `todo_mgr.stage_tactical_todos()`, returns success
- Backward-compat todo_guide.md enforcement: requires `was_recently_read("todo_guide.md")` when no instruction_files configured
- Skips todo_guide check when `context._instruction_files` is non-empty (enforcement handled by wrapper)
- String input (JSON) parsed correctly
- Non-list input returns error
- Non-string items in list return error with index and type
- All other strategic todos complete → message includes transition guidance
- Remaining strategic todos → message lists them
- TodoManager validation error (e.g., < 5 items) → returns error

_`todo_complete` tool:_
- No todo_id → calls `complete_first_pending_sync()`, returns result message
- Single todo_id → calls `todo_mgr.complete(id)`, returns completion message
- Invalid todo_id → returns error with list of available todo IDs
- Multiple comma-separated IDs → calls `complete_multiple(ids)`, returns batch result
- Some IDs not found in batch → message includes "Not found" list
- Last task completed → message includes `[PHASE_COMPLETE]` signal
- Not last task → message shows remaining count and next task

_`todo_list` tool:_
- Empty list returns "No todos in the current list"
- Shows completion count (X/Y complete)
- Each todo shows checkbox, ID, content, and status
- High-priority todos show `[!]` marker

_`todo_rewind` tool:_
- Valid issue → calls `todo_mgr.archive_with_failure_note()`, returns recovery instructions
- Empty issue → returns error requiring description
- Whitespace-only issue → returns error

#### 3b.2 `src/tools/core/job.py` — Job Lifecycle (257 lines, 0 tests)

`mark_complete` and `job_complete` tools, plus the global `_final_phase_data` storage used by `phase.py`.

**What to test:**

_`create_job_tools(context)`:_
- Raises ValueError without workspace_manager
- Returns 2 tools: mark_complete, job_complete

_`mark_complete` tool (async):_
- Writes `output/completion.json` with summary, deliverables, confidence, timestamp
- Confidence clamped to 0.0-1.0 range (values >1 become 1.0, <0 become 0.0)
- Optional notes included when provided
- Returns message containing file path and summary

_`job_complete` tool (async):_
- Rejects in tactical phase → error message about completing tactical todos first
- Rejects when staged todos exist → error about clearing staged work
- Already in final phase → returns "already marked as final"
- Valid call in strategic phase → stores data in `_final_phase_data[job_id]`
- No pending todos → message says "job will complete now"
- Pending todos → message shows count and says "complete remaining todos"
- Confidence clamped to 0.0-1.0

_Module-level functions:_
- `get_final_phase_data(job_id)` — returns stored data or None
- `clear_final_phase_data(job_id)` — removes entry, no error if missing

### 3c. Citation Tools

| Module | Lines | Tests | Status |
|--------|-------|-------|--------|
| `src/tools/citation/sources.py` | 1008 | 0 | No tests |

#### 3c.1 `src/tools/citation/sources.py` — Citation Management (1008 lines, 0 tests)

Full citation system with 11 tools. Mock `CitationEngine` and `ToolContext` for all tests.

**What to test:**

_`cite_document` tool:_
- CitationEngine installed: registers doc source, creates citation, returns formatted result
- CitationEngine not installed: falls back to stub mode with generated CIT-ID
- Source registration fails: falls back to stub mode
- Document not found: returns error
- Formats output with citation ID, source info, status, similarity score
- Verification notes included when present

_`cite_web` tool:_
- Registers web source, creates citation, saves content to disk
- Default `accessed_date` to today when not provided
- CitationEngine not installed: stub mode fallback
- Source registration failure: stub mode fallback
- Persists web content to disk via `context.save_web_content_to_disk()`
- Locator includes `accessed_at` and optional `title`

_`list_sources` tool:_
- No sources: returns "No sources registered" message
- With sources: formats list with [id], type, name
- CitationEngine not installed: returns message

_`get_citation` tool:_
- Returns full citation detail (source, location, claim, quote, status, scores)
- Citation not found: returns not-found message
- Long quotes truncated to 500 chars
- Includes extraction method, language, reasoning when present

_`list_citations` tool:_
- Lists all citations with preview, status, confidence
- Filters by source_id and/or verification status
- No matches with filters: returns filter-aware message
- Claim preview truncated to 50 chars

_`edit_citation` tool (async):_
- Updates specified fields only (non-None)
- Content change (claim/quote/context) resets verification_status
- Invalid locator JSON returns error
- No fields provided returns error
- Citation not found returns error
- No database connection returns error

_`annotate_source` tool:_
- Creates annotation with type (note/highlight/summary/question/critique)
- Optional page reference included
- Default type is "note"

_`get_annotations` tool:_
- Returns formatted annotation list
- Filters by annotation type
- No annotations: returns appropriate message

_`tag_source` tool:_
- Add action: tags added, returns current tag list
- Remove action: tags removed, returns current tag list
- Empty tag string: returns error
- Comma-separated parsing (whitespace trimmed)

_`search_library` tool:_
- Executes search with mode (hybrid/keyword/semantic)
- Filters by tags (comma-separated) and source_type
- Scope parameter (content/annotations/all)
- No results: returns query and mode info
- Results include evidence labels, source refs, chunk previews (truncated to 300)
- top_k respected

_`generate_bibliography` tool:_
- Generates formatted bibliography in specified style (bibtex/harvard/ieee/apa/inline)
- Specific citation IDs: processes subset
- No citation_ids: processes all job citations
- Invalid citation ID (non-numeric): returns error
- No output_path: returns formatted text directly
- With output_path (new file): writes to workspace
- With output_path (existing BibTeX): deduplicates by key, appends new entries
- With output_path (existing non-BibTeX): deduplicates by exact string match
- No workspace available with output_path: returns error

_Stub formatting functions:_
- `_format_stub_document_citation()` — formats with truncated text (500 chars)
- `_format_stub_web_citation()` — formats with URL, title, date, truncated text

### 3d. Document Tools

| Module | Lines | Tests | Status |
|--------|-------|-------|--------|
| `src/tools/document/processing.py` | 252 | 0 | No tests |

#### 3d.1 `src/tools/document/processing.py` — Document Chunking (252 lines, 0 tests)

**What to test:**

_`_load_document(file_path)`:_
- PDF: uses PyPDFLoader, returns text + page count
- DOCX: uses Docx2txtLoader, returns text + page_count=1
- TXT/MD: uses TextLoader, returns text
- HTML: uses UnstructuredHTMLLoader, returns text
- Unknown extension: reads as plain text fallback
- Nonexistent file: raises exception

_`_chunk_text(text, strategy, max_chunk_size, overlap)`:_
- Legal strategy: uses section/article-aware separators (§, Art.)
- Technical strategy: uses markdown-header-aware separators (#, ##, ###)
- General strategy: uses paragraph/sentence separators
- Chunk index is 0-based and sequential
- Overlap is applied between chunks
- Returns list of dicts with `text`, `chunk_index`, `section_hierarchy`

_`chunk_document` tool:_
- Resolves path via workspace, loads document, chunks text
- Persists chunks to `chunks/chunk_001.txt` through `chunks/chunk_NNN.txt`
- Each chunk file has header with chunk number and section info
- Returns summary with chunk count, strategy, size statistics
- No workspace: chunks not persisted, returns note
- Error in loading: returns error message

### 3e. Datasource Tools

| Module | Lines | Tests | Status |
|--------|-------|-------|--------|
| `src/tools/graph/neo4j.py` | 139 | 0 | No tests |
| `src/tools/sql/postgresql.py` | 290 | 0 | No tests |
| `src/tools/mongodb/mongo.py` | 348 | 0 | No tests |

#### 3e.1 `src/tools/graph/neo4j.py` — Neo4j Tools (139 lines, 0 tests)

**What to test:**

_`create_neo4j_tools(context)`:_
- Raises ValueError when neo4j datasource not available
- Returns 2 tools: execute_cypher_query, get_database_schema

_`execute_cypher_query` tool:_
- Executes query, formats results (up to 50 records)
- Empty result set returns "no results" message
- More than 50 results: shows count of remaining
- Database error returns error string

_`get_database_schema` tool:_
- Returns formatted schema with node labels, relationship types, property keys
- Property keys truncated at 30 (shows "and N more")
- Schema cache: second call uses cached result (mock `neo4j.get_schema()`)
- Empty schema returns error message

#### 3e.2 `src/tools/sql/postgresql.py` — PostgreSQL Tools (290 lines, 0 tests)

**What to test:**

_`create_postgresql_tools(context)`:_
- Raises ValueError when postgresql datasource not available
- Returns 3 tools: sql_query, sql_schema, sql_execute

_`sql_query` tool:_
- Executes SELECT with read-only transaction
- Formats results as JSON rows with column names
- Limits to 100 rows (shows count of remaining)
- 0 rows: returns column names
- Non-SELECT (no description): returns message to use sql_execute
- Rolls back transaction on error (no dangling state)

_`sql_schema` tool:_
- No table_name: lists all tables grouped by schema
- With table_name: shows columns (type, nullable, default), constraints (PK, FK, unique), indexes
- Supports `schema.table` notation
- Defaults to `public` schema
- Table not found: returns not-found message

_`sql_execute` tool:_
- Executes write statement, commits, returns affected row count
- Rolls back on error
- No connection: returns error message

#### 3e.3 `src/tools/mongodb/mongo.py` — MongoDB Tools (348 lines, 0 tests)

**What to test:**

_Helper functions:_
- `_MongoJSONEncoder` — serializes ObjectId, datetime to string
- `_json_dumps()` — handles non-serializable BSON types
- `_parse_json()` — valid JSON returns parsed object; invalid raises ValueError with label

_`create_mongo_tools(context)`:_
- Raises ValueError when mongodb datasource not available
- Returns 5 tools

_`mongo_query` tool:_
- Queries collection with filter, returns formatted documents
- Limit capped at 100
- Invalid JSON filter returns error with label
- No matches: returns "No documents found" message

_`mongo_aggregate` tool:_
- Runs aggregation pipeline, returns results (limited to 100)
- Pipeline must be JSON array (dict returns error)
- No results: returns message

_`mongo_schema` tool:_
- No collection: lists all collections with document counts
- With collection: shows document count, sample fields with types, indexes
- No collections: returns "No collections found"
- No documents in collection: shows appropriate message

_`mongo_insert` tool:_
- Single document (dict): inserts one, returns ID
- Array of documents: inserts many, returns IDs
- Empty array: returns error
- Non-dict/non-array: returns error
- Invalid JSON: returns error

_`mongo_update` tool:_
- Updates matching documents, returns matched/modified counts
- Invalid filter or update JSON: returns error

### 3f. Coding Tools

| Module | Lines | Tests | Status |
|--------|-------|-------|--------|
| `src/tools/coding/__init__.py` | 52 | 2 | Covered (`test_persistent_session.py` integration tests) |
| `src/tools/coding/coding_tools.py` | 218 | 0 | No tests |
| `src/tools/coding/shell_tools.py` | ~400 | 0 | No dedicated tests (run_command tested in `test_run_command.py` via ShellManager) |
| `src/tools/coding/claude_code.py` | 254 | 0 | No tests |

> **Known bug (fixed 2026-04-01):** `src/tools/coding/__init__.py:38` gates shell tool
> inclusion on `context.shell_manager is not None`. In `persistent_session.py`,
> `_setup_shell_manager()` was called AFTER `_setup_tools()`, so the gate always
> failed and `run_command`/`shell_read` were silently excluded from persistent
> agent sessions. The existing order-assertion test enforced the wrong order.
> Fix: moved shell init before tool loading; added integration tests that exercise
> the real gate rather than mocking it.
>
> **Lesson — shallow mocks hide ordering bugs:** `test_persistent_session.py` had
> 63 tests at the time and appeared comprehensive, but every `_setup_*` method was
> tested in isolation with `patch.object`, never exercising the real cross-method
> dependencies. The order-assertion test (`test_setup_calls_submethods_in_order`)
> tracked call sequence but couldn't detect that the sequence was wrong because it
> never ran the real code. Integration-style tests that call `create_coding_tools()`
> with a real `ToolContext` (shell_manager set vs None) caught the issue immediately.
> This pattern likely exists in other areas — see systemic note in Coverage Summary.

#### 3f.1 `src/tools/coding/coding_tools.py` — Shell Command Execution (218 lines, 0 tests)

**What to test:**

_`_truncate_output(text, max_chars, label)`:_
- Short text returned unchanged
- Long text truncated from start (keeps tail)
- Tries to start at line boundary (within 200 chars)
- Truncation notice includes char count removed

_`_check_blocked(command)`:_
- Blocked command (sudo, reboot, etc.) returns error message
- Allowed command returns None
- Empty command returns None

_`create_coding_tools(context)`:_
- Raises ValueError without workspace_manager
- Returns 1 tool: run_command

_`run_command` tool:_
- Executes command via subprocess in workspace directory
- Returns structured output: exit code + stdout + stderr
- No output → shows "(no output)"
- Blocked command (sudo) → error without execution
- Configurable blocklist: `run_command_blocked_commands=[]` disables blocking
- Custom working_dir (sandbox enabled): resolves relative to workspace, validates boundary
- Custom working_dir (sandbox disabled): accepts absolute paths
- Timeout: capped at 600s (10 minutes max)
- Timeout exceeded: returns timeout error message
- Long stdout/stderr truncated via `_truncate_output()`
- `max_output_chars` configurable via context config

#### 3f.2 `src/tools/coding/claude_code.py` — Claude Code Delegation (254 lines, 0 tests)

**What to test:**

_`_find_claude_cli()`:_
- Returns path when `claude` binary exists in PATH
- Returns None when not found

_`_truncate_output()`:_
- Same behavior as coding_tools version (keeps tail)

_`create_claude_code_tools(context)`:_
- Raises ValueError without workspace_manager
- Returns 1 tool: claude_code

_`claude_code` tool (async):_
- CLI not found: returns install error message
- New session (no session_id): builds command without `--resume`
- Resume session: builds command with `--resume <session_id>`
- Working directory resolved relative to workspace (with boundary check)
- Invalid working_dir: returns error
- Model and effort_level read from `context.get_config("claude_code", {})`
- Successful JSON result: extracts result text, session metadata (session_id, turns, cost, duration)
- Large output truncated to MAX_OUTPUT_CHARS
- JSON parse failure: returns raw stdout
- No stdout: returns stderr if present
- Non-zero exit with no output: returns exit code message
- Process exception: returns error with type name
- Environment: CLAUDECODE stripped, CLAUDE_CODE_EFFORT_LEVEL set

---

## 4. LLM Layer

Model interaction, key management, and error handling.

| Module | Lines | Tests | Status |
|--------|-------|-------|--------|
| `src/llm/reasoning_chat.py` | 640 | 25 | Partial — token counting and overflow covered, reasoning capture and key rotation not |
| `src/llm/exceptions.py` | 33 | 4 | Covered (in test_context_overflow.py) |

**Already well-covered in this category:**
- `src/llm/key_ring.py` (296 lines) — 35 tests (comprehensive: parsing, rotation, exhaustion, threading, registry)
- `count_request_tokens()` — 8 tests (simple, large, tools, multiple messages, tool calls, tool messages, empty, multimodal)
- `ReasoningCapturingClient` init + overflow — 5 tests (default/custom/env limits, overflow raise, non-chat skip)
- `AsyncReasoningCapturingClient` — 3 tests (overflow, non-chat skip, key injection)
- `ContextOverflowError` — 4 tests (attributes, messages, catchable)
- Integration — 2 tests (realistic under limit, very large overflow)

### 4.1 `src/llm/reasoning_chat.py` — Gaps in Existing Coverage (640 lines, 25 tests)

The 25 existing tests in `test_context_overflow.py` cover token counting and overflow detection well. The following areas have **zero coverage**:

**What to test:**

_`count_request_tokens` — Responses API format (untested):_
- `input` field (list of strings): tokens counted from each string
- `input` field (list of dicts with role/content): tokens counted like messages
- `input` with multimodal content list: text parts counted
- `instructions` field: tokens counted
- Empty `messages` with `input` present: uses input path
- Both `messages` and `input` present: messages path takes precedence (input only counted when messages empty)

_`count_request_tokens` — tiktoken fallback (untested):_
- When `TIKTOKEN_AVAILABLE=False`: falls back to `len(json.dumps(body)) // 4`
- Fallback gives reasonable estimate (within order of magnitude)

_`_extract_responses_api_reasoning()` (0 tests):_
- Content is list with reasoning block: extracts text from `summary` and `content` items into `additional_kwargs["reasoning_content"]`
- Content is list with mixed blocks: reasoning extracted, non-reasoning blocks flattened to string
- Content is list with text dict blocks: extracts `text` field
- Content is list with plain string blocks: preserved as-is
- Content is not a list (plain string): returns immediately, no modification
- No reasoning blocks: `additional_kwargs` unchanged, content cleaned
- Function_call blocks in non-reasoning: skipped during flatten (already in tool_calls)

_`ReasoningCapturingClient.send()` — reasoning capture (0 tests):_
- Chat completion response with `reasoning_content` in message: captured in `_last_reasoning_content`
- Chat completion response without `reasoning_content`: `_last_reasoning_content` set to None
- Response JSON parse failure: `_last_reasoning_content` unchanged (no crash)
- Non-chat URL: reasoning capture skipped

_`ReasoningCapturingClient.send()` — URL detection (0 tests):_
- `/chat/completions` in URL → is_chat=True, is_responses=False
- URL ending in `/responses` → is_chat=False, is_responses=True
- `/responses/` in URL path → is_chat=False, is_responses=True
- Embeddings URL → both False, no validation
- Token validation triggers for both chat and responses URLs

_`ReasoningCapturingClient` — key rotation (0 tests):_
- HTTP 401 response with key_ring: rotates key and retries
- HTTP 403 response with key_ring: rotates key and retries
- HTTP 429 with quota signal in body ("insufficient_quota"): rotates and retries
- HTTP 429 with short retry-after (< 3600) and no quota signal: does NOT rotate (rate limit)
- HTTP 429 with no retry-after and no quota signal: rotates (conservative default)
- Rotation fails (no alternatives): returns original response
- No key_ring configured: no rotation attempted
- `has_alternatives` is False: no rotation attempted
- Key injection: `request.headers["authorization"]` set to `Bearer {current_key}`
- All keys exhausted (`RuntimeError` from key_ring): sends with original header

_`_is_quota_error(response)` (0 tests):_
- Body contains "quota" → True
- Body contains "billing" → True
- Body contains "insufficient_quota" → True
- Body contains "exceeded" → True
- retry-after < 3600 without quota keywords → False (rate limit)
- retry-after >= 3600 → True (long backoff implies quota)
- No retry-after, no quota keywords → True (conservative)
- Non-parseable retry-after header → treated as None
- Response body read failure → treated as empty (no crash)

_`AsyncReasoningCapturingClient` — same gaps as sync (0 tests for these):_
- Reasoning capture from response
- Key rotation on 401/403/429
- `_is_quota_error` (shared logic, same test cases)
- `_rotate_and_retry` async version
- URL detection for responses API

_`ReasoningChatOpenAI` class (0 tests):_
- Constructor: creates both sync and async reasoning clients, passes to ChatOpenAI
- `max_context_tokens` and `key_ring` forwarded to both clients
- `timeout` forwarded to both clients

_`ReasoningChatOpenAI._post_process_result()` (0 tests):_
- Sync client has `_last_reasoning_content`: injected into `generation.message.additional_kwargs`
- Async client has `_last_reasoning_content`: injected (async client takes precedence when sync is None)
- After injection, `_last_reasoning_content` cleared to None on both clients
- No reasoning content: `additional_kwargs` unchanged
- Calls `_extract_responses_api_reasoning()` on each generation message
- Multiple generations: each processed independently

_`ReasoningChatOpenAI._post_process_result()` — debug stream (0 tests):_
- `DEBUG_LLM_STREAM=1`: writes content/reasoning/tool summaries to stderr
- Reasoning content: shows char count and tail
- Content: shows char count and tail
- Tool calls: shows tool name summary
- Empty response (no content, no tools): shows warning
- `DEBUG_LLM_STREAM` unset: no stderr output
- `DEBUG_LLM_TAIL` controls tail buffer size (default 500)

_`ReasoningChatOpenAI._generate()` and `_agenerate()` (0 tests):_
- Calls parent's `_generate`/`_agenerate` then `_post_process_result`
- End-to-end: reasoning content available in returned result

_Module-level helpers (0 tests):_
- `_is_debug_stream()` — checks `DEBUG_LLM_STREAM` env var for "1", "true", "yes"
- `_get_debug_tail_chars()` — reads `DEBUG_LLM_TAIL`, defaults to 500

### 4.2 `src/llm/exceptions.py` — ContextOverflowError (33 lines, 4 existing tests)

Fully covered by existing tests. No additional tests needed.

**Existing coverage:**
- Attribute storage (token_count, limit, request_size_bytes)
- Default message formatting (with comma-separated numbers)
- Custom message override
- Catchability as Exception subclass

---

## 5. Services

Helper services for vision, rendering, and caching. All untested.

| Module | Lines | Tests | Status |
|--------|-------|-------|--------|
| `src/services/document_renderer.py` | 498 | 0 | No tests |
| `src/services/vision_helper.py` | 271 | 0 | No tests |
| `src/services/description_cache.py` | 239 | 0 | No tests |

### 5.1 `src/services/document_renderer.py` — DocumentRenderer (498 lines, 0 tests)

Renders PDF/PPTX/DOCX pages as PNG images for multimodal processing. Used by `read_file` tool in `src/tools/workspace/files.py` (4 import sites). Has a module-level singleton via `get_document_renderer()`.

_Constructor and dependency detection (0 tests):_
- `__init__()` default DPI (`DEFAULT_DPI = 150`) and max_pages (`MAX_RENDER_PAGES = 20`)
- Custom `dpi` and `max_pages` override defaults
- Custom `libreoffice_path` skips auto-detection
- `_find_libreoffice()`: returns first found candidate from 6 paths, None when none found
- `_check_pdf2image()`: returns True when pdf2image importable, False with ImportError, False with other Exception

_`render_pdf_page()` (0 tests):_
- Renders single page at given DPI, returns PNG bytes
- `page_num < 1`: raises ValueError
- `page_num > max_pages`: raises ValueError with limit info
- `pdf2image` not installed: raises ImportError
- `convert_from_path` returns empty list: raises ValueError
- `convert_from_path` fails: raises RuntimeError
- Default DPI used when `dpi=None`
- Custom DPI overrides instance default

_`render_pptx_slide()` (0 tests):_
- No LibreOffice available: raises RuntimeError
- `slide_num < 1`: raises ValueError
- `slide_num > max_pages`: raises ValueError
- Converts PPTX to PDF via `_convert_to_pdf()`, then delegates to `render_pdf_page()`
- Temp PDF cleaned up after rendering (even on error via finally)
- Temp directory cleaned up if different from source parent

_`render_docx_page()` (0 tests):_
- Same validation as PPTX (no LibreOffice, page bounds)
- Converts DOCX to PDF, delegates to `render_pdf_page()`
- Same temp file cleanup logic as PPTX

_`render_page()` — unified dispatcher (0 tests):_
- `.pdf` suffix → `render_pdf_page()`
- `.pptx` suffix → `render_pptx_slide()`
- `.docx` suffix → `render_docx_page()`
- Unsupported suffix (e.g. `.txt`): raises ValueError listing supported types
- Case-insensitive suffix matching (`.PDF` works)

_`get_page_count()` — unified page counting (0 tests):_
- `.pdf` → `_get_pdf_page_count()` (uses pdfplumber, raises ImportError if missing)
- `.pptx` → `_get_pptx_slide_count()` (uses python-pptx, raises ImportError if missing)
- `.docx` → `_get_docx_page_count()` (converts to PDF if LibreOffice available)
- `.docx` without LibreOffice → `_estimate_docx_pages()` (word count / 500, min 1)
- Unsupported suffix: raises ValueError

_`_convert_to_pdf()` (0 tests):_
- No LibreOffice: raises RuntimeError
- Successful conversion: returns Path to temp PDF
- `subprocess.run` non-zero return: raises RuntimeError with stderr
- `subprocess.TimeoutExpired`: raises RuntimeError
- Other exception: cleans up temp dir, re-raises
- Output PDF name differs from expected: finds via glob fallback
- No PDF files in temp dir after conversion: raises RuntimeError

_`_estimate_docx_pages()` (0 tests):_
- Calculates word count from paragraphs, divides by 500
- Returns minimum of 1 page
- `python-docx` not installed: raises ImportError

_Singleton `get_document_renderer()` (0 tests):_
- First call creates instance, stores in `_document_renderer`
- Subsequent calls return same instance

### 5.2 `src/services/vision_helper.py` — VisionHelper (271 lines, 0 tests)

Generates text descriptions of images using a dedicated multimodal model (AsyncOpenAI). Used by `read_file` tool when `multimodal: false`. Has both async and sync interfaces plus a module-level singleton.

_`run_async()` helper (0 tests):_
- No running event loop: uses `asyncio.run()`
- Already in async context (running loop): delegates to `ThreadPoolExecutor` + `asyncio.run`

_Constructor (0 tests):_
- `VISION_API_KEY` set: uses it, otherwise falls back to `OPENAI_API_KEY`
- `VISION_BASE_URL` set: uses it, otherwise defaults to `OPENAI_API_URL` (not `LLM_BASE_URL`)
- `VISION_MODEL` set: uses it, otherwise defaults to `"gpt-4o-mini"`
- `VISION_TIMEOUT` set: uses it (converted to float), otherwise defaults to `120`
- No API key at all: logs warning (but doesn't raise)
- Creates `AsyncOpenAI` client with configured params

_`describe_image()` async (0 tests):_
- `image_data` as bytes: base64-encodes it
- `image_data` as str: uses directly (already base64)
- `query` provided: uses query as prompt text
- `query=None`: uses default prompt describing all visible content
- `mime_type` forwarded into data URL (e.g. `"data:image/jpeg;base64,..."`)
- API call succeeds: returns `response.choices[0].message.content`
- API call fails (any Exception): returns `"[Error analyzing image: ...]"`
- `max_tokens=1000` hardcoded

_`describe_image_sync()` (0 tests):_
- Delegates to `run_async(self.describe_image(...))` with all params forwarded

_`describe_document_page()` async (0 tests):_
- `page_image` as bytes: base64-encodes
- `page_image` as str: uses directly
- `query` provided: prompt is `"Regarding page {page_num}: {query}"`
- `query=None`: uses detailed multi-point default prompt focusing on visual content
- API call succeeds: returns content
- API call fails: returns `"[Error analyzing document page {page_num}: ...]"`
- `max_tokens=2000` hardcoded (different from `describe_image`)

_`describe_document_page_sync()` (0 tests):_
- Delegates to `run_async(self.describe_document_page(...))` with all params forwarded

_Singleton `get_vision_helper()` (0 tests):_
- First call creates instance, stores in `_vision_helper`
- Subsequent calls return same instance

### 5.3 `src/services/description_cache.py` — DescriptionCache (239 lines, 0 tests)

File-based cache for vision-generated descriptions. Content-addressable storage using SHA256 keys derived from file content hash + page + query. Easiest to test of the three services — no external dependencies, pure file I/O.

_Constructor (0 tests):_
- Default cache dir: `workspace/.vision_cache/`
- Custom `cache_dir` overrides default
- Creates directory if it doesn't exist (`mkdir parents=True, exist_ok=True`)

_`_hash_file_content()` (0 tests):_
- Returns SHA256 hex digest of file contents
- Reads in 8192-byte chunks (large file friendly)
- Same file content → same hash (deterministic)
- Different file content → different hash

_`_make_key()` (0 tests):_
- Combines content_hash + page + query into a JSON-based SHA256 key
- `page=None, query=None`: valid key (query normalized to `""`)
- Same file + same page + same query → same key (cache hit)
- Same file + different page → different key
- Same file + same page + different query → different key
- Different file content + same page + same query → different key (content-addressable)
- JSON `sort_keys=True` ensures deterministic serialization

_`get()` (0 tests):_
- Cache file exists: returns content as string (UTF-8)
- Cache file doesn't exist: returns None
- Exception during read: returns None (graceful degradation)

_`set()` (0 tests):_
- Writes description to `{cache_dir}/{key}.txt`
- Returns True on success
- Exception during write: returns False (graceful degradation)

_`clear()` (0 tests):_
- Deletes all `.txt` files in cache dir
- Returns count of deleted entries
- Empty cache: returns 0
- Exception during deletion: logs warning (partial cleanup OK)

_`get_stats()` (0 tests):_
- Returns dict with `entry_count`, `total_size_bytes`, `total_size_mb`, `cache_dir`
- Empty cache: `entry_count=0`, `total_size_bytes=0`
- Non-txt files in cache dir: excluded from stats

_Singleton `get_description_cache()` (0 tests):_
- First call creates instance (accepts optional `cache_dir`)
- Subsequent calls return same instance (ignores `cache_dir` arg on second call)

_Integration pattern (0 tests):_
- `read_file` tool (files.py:180-184): gets cache → checks `cache.get()` → on miss calls `vision.describe_image_sync()` → stores via `cache.set()`
- Correct cache invalidation when file content changes (content-addressable keys handle this automatically)

---

## 6. Database & Archival

Database clients and audit trail logging.

| Module | Lines | Tests | Status |
|--------|-------|-------|--------|
| `src/core/archiver.py` | 1079 | 0 | No tests |

**Already covered in this category:**
- `src/database/postgres_db.py` (1179 lines) — 19 tests (init, basic ops)
- `src/database/neo4j_db.py` (261 lines) — 2 tests (init, disconnect)
- `src/database/mongo_db.py` (345 lines) — 5 tests (init, graceful degradation)

### 6.1 `src/core/archiver.py` — LLMArchiver (1079 lines, 0 tests)

MongoDB audit trail logging. Every LLM call, tool invocation, and phase transition is archived. All methods gracefully degrade when MongoDB is unavailable (return `None`/`[]`/`{}`/`False`). Best tested with a mock MongoDB collection since all operations are direct pymongo calls.

_Module-level helpers (0 tests):_
- `_serialize_for_mongo()`: uuid.UUID → str conversion, recursive through dicts and lists/tuples, non-UUID values pass through
- `_normalize_content()`: str passes through, list of content blocks → joined text (extracts `"text"` from dicts), non-str/list → `str()`, `None`/empty → `""`
- `_message_to_dict()`: SystemMessage → `role: "system"`, HumanMessage → `role: "human"`, AIMessage → `role: "assistant"` + tool_calls if present, ToolMessage → `role: "tool"` + tool_call_id + name, includes `additional_kwargs` and `response_metadata` when non-empty

_`LLMArchiver.__init__()` (0 tests):_
- Stores mongodb_url, database_name, collection_name, audit_collection_name
- Creates `MongoDB` instance when url provided, None when empty
- Initial state: `_connected=False`, `_connection_attempted=False`
- Empty step/chat counters

_`LLMArchiver.from_env()` (0 tests):_
- `MONGODB_URL` not set: returns None
- `MONGODB_URL` set with db name in path: extracts db name (e.g. `mongodb://host/mydb` → `"mydb"`)
- `MONGODB_URL` with query params: extracts db name before `?`
- `MONGODB_URL` with empty path: defaults to `"graphrag_logs"`

_`_ensure_connected()` (0 tests):_
- Already connected: returns True immediately
- Already attempted: returns False immediately (no retry)
- No `_mongo_db`: returns False
- `_mongo_db.db` is None: returns False, sets `_connection_attempted=True`
- Successful connection: sets `_collection`, `_audit_collection`, `_chat_history_collection`, returns True
- Connection exception: returns False

_`_get_next_step_number()` (0 tests):_
- New job_id: queries MongoDB for max step_number, continues from there
- Existing job_id in counter: increments in-memory counter
- MongoDB query fails: starts from 0
- No audit_collection: starts from 0

_`_get_next_chat_sequence()` (0 tests):_
- New job_id: starts from 1
- Existing job_id: increments sequentially

_`_truncate_string()` (0 tests):_
- String shorter than max_length: passes through unchanged
- String longer than max_length: truncated with `"... [truncated]"` suffix
- None/empty string: returns as-is

_`archive()` (0 tests):_
- Not connected: returns None
- Builds document with job_id, agent_type, timestamp, model, request (messages + count), response
- Optional fields: latency_ms, iteration, metadata (serialized), tool_schemas, model_kwargs
- Metrics: input_chars sum, output_chars, tool_call count, token_usage from response_metadata
- Calls `_archive_chat_entry()` for chat history collection
- Returns inserted document ID string
- Exception during insert: returns None

_`_archive_chat_entry()` (0 tests):_
- Finds new inputs after last AIMessage (skips SystemMessages)
- Extracts response content + tool calls with previews
- Extracts reasoning from `additional_kwargs.reasoning_content`
- Includes sequence_number, phase, phase_number when provided
- Exception: logs warning, no re-raise

_`get_conversation()` (0 tests):_
- Not connected: returns `[]`
- Filters by job_id and optional agent_type
- Sorts by timestamp ascending, respects limit
- Exception: returns `[]`

_`get_job_stats()` (0 tests):_
- Not connected: returns `{}`
- Aggregation pipeline: total_requests, total_input/output_chars, total_tool_calls, avg_latency, first/last request, models_used
- No results: returns `{}`
- Exception: returns `{}`

_`get_recent_requests()` (0 tests):_
- Optional agent_type filter
- Sorted by timestamp descending, respects limit
- Not connected/exception: returns `[]`

_`audit_step()` (0 tests):_
- Assigns sequential step_number via `_get_next_step_number()`
- Stores job_id, agent_type, iteration, step_type, node_name, timestamp
- Optional: phase, phase_number, latency_ms, metadata (serialized)
- Merges `data` dict into document (serialized)
- Returns inserted ID or None

_`audit_tool_call()` (0 tests):_
- Truncates large string/dict/list arguments to 200 chars preview
- Delegates to `audit_step()` with `step_type="tool"`, `node_name="tools"`
- Includes null result fields (result_preview, result_size_bytes, success, error)
- Includes `started_at` timestamp and null `completed_at`

_`update_tool_result()` (0 tests):_
- Not connected: returns False
- Updates document by ObjectId with result_preview (500 chars), result_size_bytes, success, completed_at, latency_ms
- Error field added when provided
- `modified_count > 0`: returns True
- `modified_count == 0` (doc not found): returns False
- Exception: returns False

_`audit_llm_call()` (0 tests):_
- Delegates to `audit_step()` with `step_type="llm"`, `node_name="execute"`
- Includes model, input_message_count, state_message_count
- Null response fields (request_id, response_content_preview, tool_calls, metrics)

_`update_llm_response()` (0 tests):_
- Similar to `update_tool_result()` but for LLM response fields
- Updates request_id, response_content_preview, tool_calls, metrics, completed_at, latency_ms

_`get_job_audit_trail()` (0 tests):_
- Filters by job_id and optional step_type
- Sorted by step_number ascending
- Not connected/exception: returns `[]`

_`get_audit_stats()` (0 tests):_
- Two aggregation pipelines: by_step_type (count, avg/total latency) and timestamps (first/last step, max_iteration)
- Not connected/exception: returns `{}`

_`close()` (0 tests):_
- Closes MongoDB connection, sets `_connected=False`

_Module-level convenience functions (0 tests):_
- `get_archiver()`: singleton from `LLMArchiver.from_env()`
- `archive_llm_request()`: delegates to singleton's `archive()` method, returns None when no archiver

---

## 7. Utilities

Shared utility functions for document processing, citations, and config.

| Module | Lines | Tests | Status |
|--------|-------|-------|--------|
| `src/utils/document_processor.py` | 971 | 0 | No tests |
| `src/utils/citation_utils.py` | 417 | 0 | No tests |
| `src/utils/pdf.py` | 288 | 0 | No tests |
| `src/utils/config.py` | 312 | 0 | Partial (covered by loader tests) |

### 7.1 `src/utils/document_processor.py` — DocumentProcessor (971 lines, 0 tests)

Full document ingestion pipeline: extraction → detection → chunking. Uses optional dependencies (pdfplumber, python-docx, python-pptx, openpyxl, beautifulsoup4) with graceful `*_AVAILABLE` flags. Most functions are pure and highly testable.

_`detect_language()` (0 tests):_
- Samples first 500 words, scores against `LANGUAGE_INDICATORS` word lists
- German text → `"de"`, English text → `"en"`
- Empty text → returns one of the languages (max of zero scores)

_`detect_document_type()` (0 tests):_
- Scores text against `DOCUMENT_TYPE_PATTERNS` regex per category
- Legal indicators (vertrag, contract, §, GoBD, DSGVO) → `DocumentCategory.LEGAL`
- Technical indicators (API, system, specification) → `DocumentCategory.TECHNICAL`
- Policy indicators (richtlinie, workflow, compliance) → `DocumentCategory.POLICY`
- No matches → `DocumentCategory.GENERAL`

_`DocumentExtractor` (0 tests):_
- `_check_dependencies()`: sets `self.capabilities` dict for each format
- `extract()`: dispatches by suffix — `.pdf`, `.docx`, `.pptx`, `.xlsx`, `.html`, known `TEXT_FORMATS` (~40 extensions), else fallback
- `extract()` with missing file: raises FileNotFoundError
- `_extract_pdf()`: uses pdfplumber, returns text with `[PAGE N]` markers + metadata (title, author, page_count). Raises ValueError when pdfplumber not installed.
- `_extract_docx()`: uses python-docx, preserves heading levels as `[H1]`/`[H2]` prefixes, estimates page count from char length (~3000 chars/page)
- `_extract_pptx()`: uses python-pptx, includes table rows as `|` separated, `[SLIDE N]` markers
- `_extract_xlsx()`: uses openpyxl, formats as markdown tables, one `[SHEET]` per sheet
- `_extract_html()`: strips script/style/nav/footer, extracts text
- `_extract_txt()`: plain text with UTF-8 encoding (errors=replace)
- `_extract_fallback()`: binary detection (null bytes, >10% non-printable) raises ValueError; tries UTF-8 then latin-1

_`DocumentChunker` (0 tests):_
- `from_preset()`: loads from `CHUNKING_PRESETS` dict ("legal", "technical", "general", "by_page")
- Unknown preset: raises ValueError
- `chunk()`: empty text → returns `[]`
- `_detect_structure()`: finds `[PAGE N]` markers and section headers from boundary patterns
- `_chunk_by_page()`: one chunk per page marker; no markers → single chunk
- `_chunk_with_boundaries()`: splits at section boundaries, sub-splits large sections
- `_chunk_simple()`: character-based with sentence boundary seeking and overlap
- `_split_section()`: sentence splitting with configurable overlap
- `_find_page()`: returns page number for a text position
- `_estimate_tokens()`: `len(text) // 4`
- Overlap calculation between adjacent chunks

_`DocumentProcessor` (0 tests):_
- `process()`: combines extraction + language detection + type detection + chunking
- `_detect_jurisdiction()`: German indicators → `"DE"`, EU indicators → `"EU"`, German language → `"DE"`, English → None

_Module-level utility (0 tests):_
- `estimate_processing_time()`: size-based estimate per file type (PDF ~1s/MB, DOCX ~0.8s, PPTX ~1.2s, XLSX ~0.6s, others ~0.5s)

### 7.2 `src/utils/citation_utils.py` — CitationHelper (417 lines, 0 tests)

Citation Engine integration utilities. Wraps external CitationEngine pip package with error handling. Most methods are async thin wrappers.

_Module-level functions (0 tests):_
- `is_citation_engine_available()`: returns `_citation_engine_available` flag based on import success
- `get_citation_engine_config()`: reads env vars (`CITATION_DB_URL`/`DATABASE_URL`, `CITATION_LLM_URL`/`LLM_BASE_URL`, `CITATION_LLM_MODEL`, `CITATION_REASONING_REQUIRED`)
- `create_citation_engine()`: creates/initializes engine from config, returns None on failure

_`CitationHelper` class (0 tests):_
- `cite_document()`: creates citation with source metadata (type, path, page, section, article); returns citation ID or None on failure
- `cite_web()`: creates citation with web metadata (url, accessed_at); returns ID or None
- `cite_database()`: creates citation with query/result metadata; returns ID or None
- `verify()`: verifies citation by ID, returns bool; False when engine missing or error
- `get_citation()`: returns citation detail dict or None
- `list_citations()`: returns list of citation summaries; truncates claim at 100 chars
- All methods: return None/False/[] when `self.engine` is None (graceful degradation)

_`create_citation_tools()` (0 tests):_
- Returns list of 3 tool dicts (cite_document, cite_web, verify_citation) wrapping CitationHelper methods

### 7.3 `src/utils/pdf.py` — PDFReader (288 lines, 0 tests)

Page-based PDF reading with auto-pagination and word limits. No external service dependencies (only pdfplumber). Highly testable with real PDF fixtures.

_`PDFReader` class (0 tests):_
- `is_available()`: returns `PDF_AVAILABLE` flag
- `get_document_info()`: returns metadata dict (page_count, file_size, estimated chars/page from 5-page sample, title, author, creation_date). Raises ValueError when pdfplumber unavailable, FileNotFoundError when file missing.
- `read_pages()`: reads specified page range with word limit auto-truncation
  - `page_start=None` defaults to 1
  - `page_start < 1`: raises ValueError
  - `page_start > total_pages`: raises ValueError
  - `page_end < page_start`: raises ValueError
  - Auto-truncation: stops when `total_words + page_words > max_words` (only when `page_end` not explicitly set)
  - Always reads at least one page even if it exceeds limit
  - Returns `(text, read_info)` with `was_truncated`, `next_page`, `pages_read`, `words_read`

_Module-level formatters (0 tests):_
- `format_document_info()`: human-readable string with page count, file size, token estimates, title, author
- `format_read_info()`: continuation guidance with suggested next `read_file()` call when truncated

### 7.4 `src/utils/config.py` — Configuration Utilities (312 lines, 0 tests)

Legacy config utilities with `ENV > config file > default` priority chain. Used by older code paths. All functions are pure and highly testable.

_Core functions (0 tests):_
- `get_project_root()`: returns 3 levels up from this file
- `load_config()`: loads JSON from `config/` directory
- `load_prompt()`: loads text from `config/prompts/` directory
- `_get_llm_config()`: cached `llm_config.json` loader (returns `{}` on FileNotFoundError)

_Typed getters (0 tests):_
- `get_env_int()`: ENV → config JSON path → default; handles non-int env values gracefully
- `get_env_float()`: same pattern for floats
- `get_env_str()`: same pattern for strings
- Config path traversal: handles missing intermediate keys without error

_Specific config accessors (0 tests, all delegate to typed getters):_
- `get_creator_polling_interval()`: default 30
- `get_validator_polling_interval()`: default 10
- `get_agent_retry_delay()`: default 10
- `get_min_confidence_threshold()`: default 0.6
- `get_duplicate_similarity_threshold()`: default 0.95
- `get_fulfillment_confidence_threshold()`: default 0.7
- `get_context_compaction_threshold()`: default 100000
- `get_context_max_output_tokens()`: default 80000
- `get_job_timeout_hours()`: default 168
- `get_max_requirement_retries()`: default 5
- `get_workspace_base_path()`: default `""`
- `get_workspace_structure()`: default directory list

---

## 8. API Layer

FastAPI endpoints and client communication.

| Module | Lines | Tests | Status |
|--------|-------|-------|--------|
| `src/api/app.py` | 809 | 10 | Partial (basic only) |
| `src/api/models.py` | 345 | 0 | No tests (Pydantic models, low risk) |

**Already covered in this category:**
- `src/api/orchestrator_client.py` (426 lines) — 17 tests

### 8.1 `src/api/app.py` — Agent API Endpoints (809 lines, 10 existing tests)

Existing tests cover only basic endpoint existence (job start 202, cancel 404, resume, get current job). No request/response body validation, no error paths, no business logic.

_`lifespan()` context manager (0 tests):_
- Startup: loads config, creates agent, registers with orchestrator, starts heartbeat
- Shutdown: stops heartbeat, cancels heartbeat task, deregisters, cancels running job, shuts down agent

_Helper functions (0 tests):_
- `_get_agent_status_for_heartbeat()`: returns "booting"/"working"/"ready" based on agent state
- `_get_current_job_id()`: returns current job ID or None
- `_get_agent_metrics()`: collects psutil metrics (memory, CPU, ports); returns None when psutil not installed
- `_collect_system_info()`: returns system info dict for startup logging
- `_setup_job_file_logging()` / `_cleanup_job_file_handler()`: per-job file logging

_`_process_orchestrator_job()` (0 tests):_
- Sets `_current_job_id` global state
- Calls `agent.process_job()` with all params from request
- On completion: calls `_update_job_status_from_result()`
- On error: reports failure to orchestrator
- Always cleans up: clears `_current_job_id`, removes file handler

_`_update_job_status_from_result()` (0 tests):_
- Maps agent result to orchestrator status (completed/failed/frozen)
- Reports to orchestrator client with confidence, final notes, output

_Endpoint business logic not tested (10 existing tests cover routing only):_
- `POST /job/start`: validates no current job running, creates background task, returns 202. Returns 409 when already busy.
- `POST /job/cancel`: cancels `_current_job_task`, returns 200. Returns 404 when no job.
- `POST /job/resume`: resumes from checkpoint via background task. Returns 404 when no job ID.
- `GET /job/current`: returns current job ID and config or "idle" status
- `GET /health`: always 200 with agent init status
- `GET /ready`: depends on agent initialization state

_`create_app()` factory (0 tests):_
- Sets config path, creates FastAPI instance with lifespan and routes

---

## 9. Orchestrator Backend

Separate service — 13,436 lines with zero tests. Lower priority since it's a separate service boundary, but the scale of the gap is notable. Best approach: start with the database layer (pure SQL, easy to mock with asyncpg-stubs) then add endpoint tests with FastAPI TestClient.

| Module | Lines | Tests | Status |
|--------|-------|-------|--------|
| `orchestrator/main.py` | 3074 | 0 | No tests |
| `orchestrator/database/postgres.py` | 1923 | 0 | No tests |
| `orchestrator/database/mongodb.py` | 783 | 0 | No tests |
| `orchestrator/mcp/server.py` | 2661 | 0 | No tests |
| `orchestrator/mcp/client.py` | 1324 | 0 | No tests |
| `orchestrator/services/workspace.py` | 413 | 0 | No tests |
| `orchestrator/services/gitea.py` | 683 | 0 | No tests |
| `orchestrator/uploads.py` | 399 | 0 | No tests |
| `orchestrator/init.py` | 1042 | 0 | No tests |
| `orchestrator/graph_routes.py` | 588 | 0 | No tests |
| `orchestrator/services/builder_*.py` | 546 | 0 | No tests |

### 9.1 `orchestrator/database/postgres.py` — OrchestratorDB (1923 lines, 0 tests)

Async PostgreSQL database layer using asyncpg. Core data access for the entire orchestrator.

_Connection management (0 tests):_
- `connect()`: creates connection pool
- `close()` / `disconnect()`: closes pool
- `acquire()`: context manager for pool connections
- `execute()`, `fetch()`, `fetchrow()`, `fetchval()`: query methods with error handling

_Job CRUD (~15 methods, 0 tests):_
- `get_jobs()`: paginated with status/agent filters, offset/limit
- `get_job()`: by UUID, returns None when not found
- `create_job()`: creates with description, config, document, datasources, resolved_config
- `delete_job()`: by UUID, returns success bool
- `cancel_job()`: transitions status to cancelled
- `update_job_status()`: updates status, result, confidence, final_notes, output summary
- `update_job_context()`: stores JSONB context
- `get_job_progress()`: joins requirements for progress reporting
- `get_job_statistics()`: counts by status

_Requirements (~3 methods, 0 tests):_
- `get_requirements()`: by job_id with status filter
- `get_requirement_summary()`: counts by status

_Agent management (~8 methods, 0 tests):_
- `register_agent()`: upsert with capabilities, config, port
- `heartbeat()`: updates timestamp, status, metrics, current job
- `list_agents()`: all agents with status
- `get_agent()`: by agent_id
- `delete_agent()`: by agent_id
- `mark_stale_agents_offline()`: threshold-based staleness detection
- `get_ready_agents()`: agents with status "ready"

_Datasource management (~6 methods, 0 tests):_
- `list_datasources()`, `get_datasource()`, `create_datasource()`, `update_datasource()`, `delete_datasource()`
- `resolve_datasources_for_job()`: resolves attached + default datasources
- `upsert_default_datasource()`: creates or updates default datasource entries

_Schema management (~4 methods, 0 tests):_
- `create_database_if_not_exists()`, `ensure_schema()`, `reset_schema()`, `verify_schema()`

_Builder sessions (~5 methods, 0 tests):_
- `create_builder_session()`, `get_builder_session()`, `update_builder_session_job()`, `update_builder_session_summary()`, `create_builder_message()`, `get_builder_messages()`

### 9.2 `orchestrator/main.py` — FastAPI Endpoints (3074 lines, 0 tests)

~60 endpoints covering jobs, agents, datasources, workspace, git history, citations, statistics. Best tested with FastAPI TestClient and mocked OrchestratorDB.

_High-priority endpoint groups:_
- Job CRUD: `create_job`, `delete_job`, `cancel_job`, `resume_job`, `approve_job` — complex business logic, status transitions
- Agent registration: `register_agent`, `agent_heartbeat` — stale detection background task
- `assign_job_to_agent()`: agent selection, job delegation with datasource resolution, `_build_datasources_payload()` helper
- Datasource CRUD: `create_datasource`, `update_datasource`, `delete_datasource`, `test_datasource`
- `stale_agent_detector()`: background task that marks agents offline after timeout

_Medium-priority:_
- Workspace file access: `get_workspace_file`, `get_job_workspace` — path traversal safety
- Git history: `list_repo_commits`, `get_repo_diff`, `list_repo_tags`
- Citation/source library: `list_sources`, `get_source_detail`, `search_job_sources`, `get_citation_stats`
- Statistics: `get_job_statistics`, `get_daily_statistics`, `get_agent_statistics`, `get_stuck_jobs`
- Bulk data endpoints: `get_job_audit_bulk`, `get_job_chat_bulk`, `get_job_graph_bulk`

### 9.3 `orchestrator/database/mongodb.py` — OrchestratorMongoDB (783 lines, 0 tests)

MongoDB client for audit trails, chat history, and graph change tracking.

_Key methods to test:_
- Audit trail: `get_audit_trail()` with pagination and filters, `search_audit()`
- Chat history: `get_chat_history()` with deduplication logic
- Graph changes: `get_graph_changes()` with Cypher mutation parsing
- LLM request retrieval: `get_llm_request()` by ObjectId

### 9.4 `orchestrator/mcp/server.py` — MCP Server (2661 lines, 0 tests)

MCP protocol server exposing 43 tools. Low priority for unit tests — better validated via integration tests against running orchestrator.

### 9.5 `orchestrator/services/workspace.py` — Workspace Service (413 lines, 0 tests)

File operations for job workspaces. Key safety concern: path traversal prevention.

_What to test:_
- Path resolution and traversal guard (ensure paths stay within workspace)
- File listing with recursive depth
- Workspace overview generation
- Git operations delegation

### 9.6 `orchestrator/services/gitea.py` — Gitea Integration (683 lines, 0 tests)

Git hosting integration for workspace version control.

_What to test:_
- Repository creation and initialization
- Commit listing and diff retrieval
- Tag management
- Error handling when Gitea is unavailable

---

## Existing Coverage Summary

| Category | Covered Modules | Tests | Status |
|----------|----------------|-------|--------|
| Core Engine | graph.py, loader.py, context.py, state.py | 246 | Good |
| Tool Infrastructure | registry.py, context.py, persistent_session.py, shell_manager.py | 255 | Good |
| Tool Implementations | research/*, workspace (partial), git, run_command | 210 | Research good, rest sparse |
| LLM Layer | key_ring.py, overflow detection | 60 | Good |
| Services | — | 0 | None |
| Database & Archival | postgres, neo4j, mongo (init only) | 19 | Minimal |
| Utilities | — | 0 | None |
| API Layer | orchestrator_client, app (partial) | 27 | Sparse |
| Orchestrator Backend | — | 0 | None |
| **Managers** | **todo, git, plan, memory** | **190** | **Comprehensive** |
| **Total** | | **~1000+** | |

### Systemic issue: shallow mock-based tests

Many test files (especially `test_persistent_session.py`, `test_tool_registry.py`)
test each method in isolation by mocking all collaborators. This inflates test counts
but misses **cross-cutting integration bugs** where the correctness of one method
depends on another having run first, or on the real behavior of a dependency.

**Example:** 63 tests for `PersistentSession` all passed while `run_command` and
`shell_read` were silently missing from every persistent agent session. The
order-assertion test tracked the (wrong) call sequence, and `_setup_tools` tests
mocked `load_tools` so they never hit the real `create_coding_tools()` gate.

**What's needed:** For modules with init-order dependencies or gating logic, add
integration-style tests that use real objects (or minimal fakes) instead of
`patch.object` for the critical path. Priority areas:

- `persistent_session.py` setup flow (partially addressed — shell tool gate now tested)
- `agent.py` `_setup_job_tools` — same tool-loading pattern, same risk of ordering bugs
- `registry.py` `load_tools` with real `create_*_tools()` factories — verify that
  datasource/context gates produce the expected tool sets
- Any factory function gated on optional context fields (`has_shell`, `has_git`,
  `has_knowledge`, `has_datasource`)

---

## Recommended Implementation Order

Prioritized by risk, value, and ease of testing:

1. ~~**Tool registry + phase filtering** (Cat. 2)~~ — Done: 64 tests in `test_tool_registry.py`
2. ~~**ToolContext** (Cat. 2)~~ — Done: 60 tests in `test_tool_context.py`
3. **Phase snapshot recovery** (Cat. 1) — User-facing feature, purely file-based
4. **Workspace file operations** (Cat. 3a) — Expand existing tests with actual read/write/edit
5. **ReasoningChatOpenAI** (Cat. 4) — Layer 0 overflow protection, prevents crashes
6. **Agent init** (Cat. 1) — Job initialization pipeline
7. **Phase transitions** (Cat. 1) — Direct tests for transition logic
8. **LLMArchiver** (Cat. 6) — Audit trail reliability
9. **Todo/Job tool wrappers** (Cat. 3b) — Tool layer between LLM and managers
10. **Citation sources** (Cat. 3c) — Full citation workflow

## Related

- [[tool_issues]]
- [[job_debug]]
- [[agent_architecture]]
- [[context_management]]
