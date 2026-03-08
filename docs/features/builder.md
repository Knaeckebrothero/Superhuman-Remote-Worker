# Instruction Builder AI

The builder is an LLM-powered chat assistant embedded in the cockpit UI. It helps users craft agent instructions, configure job settings, inspect running jobs, and manage the system — all through a conversational interface with SSE streaming.

## Architecture

```
Cockpit (Angular)                         Orchestrator (FastAPI)
┌──────────────────────────┐              ┌─────────────────────────────────┐
│ instruction-builder      │──POST SSE──▶ │ /api/builder/sessions/          │
│   component.ts           │◀──stream──── │   /{id}/message                 │
│                          │              │                                 │
│ builder-stream.service   │              │ builder_prompt.py   (system)    │
│ job-artifact.service     │              │ builder_tools.py    (schemas)   │
│ job-context.service      │              │ builder_dispatch.py (execution) │
│                          │              │ builder_search.py   (tavily)    │
└──────────────────────────┘              └─────────────────────────────────┘
```

### LLM Configuration

The builder's model is configured via environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `BUILDER_MODEL` | `gpt-5.2-pro` | Model name |
| `BUILDER_LLM_PROVIDER` | auto-detect | Provider override (`openai` or `anthropic`) |
| `BUILDER_API_KEY` | falls back to provider key | API key override |
| `BUILDER_BASE_URL` | falls back to `OPENAI_BASE_URL` | Base URL override |

Provider is auto-detected from model name (`claude-*` → Anthropic, else OpenAI). API key falls back to `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` respectively.

Three streaming backends are supported:
- **OpenAI Responses API** — for models in `RESPONSES_API_MODELS` (e.g. `gpt-5.2-pro`)
- **OpenAI Chat Completions** — standard `/chat/completions` for all other OpenAI-compatible models
- **Anthropic** — native `messages.create()` with content blocks

The frontend allows per-request model override via `builderModel` signal (configurable at runtime in `cockpit/src/assets/env.js` via `window['env']['builderModels']`, defaults to GPT-5.2 Pro and Claude Opus 4.6). Currently there is no model selector dropdown in the UI — the first configured model is used.

Tool calls execute in an agentic loop (max 50 iterations per turn).

### Session Persistence & Context Management

- Sessions stored in PostgreSQL (`builder_sessions`, `builder_messages` tables)
- Current artifact state (instructions, config, description) is injected fresh into the system prompt on every turn — never stored in message history and never summarized
- Active job context (`active_job_id`) and active project context (`active_project_id`) are injected into the system prompt, guiding the LLM to use them by default for inspection and project-scoped tools

**Session summarization:** When total message tokens exceed the context budget (6000 tokens), older messages are trimmed and the builder LLM summarizes all but the last 4 messages into a compact summary (max 1024 tokens). Summarization requires at least 6 messages. The summary is stored on the session record and prepended to future turns as context.

### SSE Event Types

| Event | Description |
|-------|-------------|
| `token` | Streaming text token |
| `tool_call` | Artifact mutation (applied client-side by `JobArtifactService`) |
| `tool_executing` | Server-side tool started (shown as step in UI) |
| `tool_result` | Server-side tool completed (result shown in expandable panel) |
| `workspace_proposal` | Workspace edit requiring user approval |
| `step` | Step metadata (thought, tool_call, etc.) |
| `done` | Stream complete |
| `error` | Error message |

### Frontend Integration

`BuilderStreamService` uses raw `fetch()` + manual SSE parsing (Angular's HttpClient doesn't support streaming response bodies). Tool-call events are forwarded to `JobArtifactService` which maintains bidirectional sync between the builder chat and the job creation form:

1. User edits form fields → signals update directly
2. Builder streams mutations → `applyToolCall()` updates signals
3. Form reactively displays current signal values
4. On next chat message, current artifact state is sent in the request payload
5. `streaming` signal locks the editor during AI output to prevent conflicts

**Active job context:** `JobContextService` is the single source of truth for which job is selected across the cockpit. `JobArtifactService.activeJobId` delegates to it. The active job ID is sent with every builder message and injected into the system prompt so the LLM defaults to that job for inspection tools. Job selection happens outside the builder component (in the cockpit layout/sidebar).

**Workspace edit approval flow:**
1. LLM calls `write_workspace_file` or `edit_workspace_file`
2. Backend validates path, reads current file content, emits `workspace_proposal` SSE event
3. Frontend queues proposal in `JobArtifactService.pendingWorkspaceEdits`
4. Builder component renders approval card with diff preview
5. User clicks Apply → `api.writeWorkspaceFile()` commits the change
6. User clicks Dismiss → proposal is discarded

## Tool Categories

### 1. Artifact Mutation Tools (client-side)

Applied directly to the Angular `JobArtifactService` via SSE `tool_call` events. These modify the job creation form state.

| Tool | Purpose |
|------|---------|
| `update_instructions` | Full rewrite of instructions.md |
| `edit_instructions` | Find-and-replace within instructions |
| `insert_instructions` | Insert at a line number or append |
| `update_config` | Change model, temperature, reasoning, tools, per-phase overrides |
| `update_description` | Change the job description |

### 2. Workspace Edit Tools (approval-required)

Forwarded to the frontend as `workspace_proposal` SSE events. The user must approve before changes are applied. Used for modifying workspace files on frozen/paused jobs.

| Tool | Purpose |
|------|---------|
| `write_workspace_file` | Write or overwrite a workspace file (plan.md, workspace.md) |
| `edit_workspace_file` | Find-and-replace within a workspace file |

### 3. Server-Side Tools (executed on backend)

Dispatched via `builder_dispatch.py` → `AsyncCockpitClient` (loopback to orchestrator API on localhost:8085).

#### Job Inspection
- `list_jobs` — List jobs with optional status filter
- `get_job` — Job details (status, config, timestamps)
- `get_job_progress` — Phase and todo completion progress
- `get_job_requirements` — Extracted requirements with validation status
- `get_workspace_file` — Read workspace files (workspace.md, plan.md, etc.)
- `get_workspace_overview` — High-level workspace summary
- `get_frozen_job` — Completion summary for pending_review jobs
- `get_todos` — Current and archived task lists (full)
- `get_current_todos` — Active todos only (lightweight)
- `list_todo_archives` — List archived todo files by phase
- `get_todo_archive` — Read a specific phase's archived todos
- `get_chat_history` — Agent conversation history
- `get_job_summary` — Composite overview (job + progress + todos + audit in one call)

#### Git History
- `list_job_commits` — Git commits; `since_ref` filters by phase tag
- `get_job_diff` — Diff between two refs (e.g. `phase_2_end..HEAD`)
- `get_job_file` — Read a file at any ref (branch, tag, SHA)
- `list_job_files` — Browse directory tree at any ref
- `list_job_tags` — Phase tags (`phase_1_start`, `phase_1_end`, …)

#### Monitoring & System
- `get_job_stats` — Job queue counts by status
- `get_agent_stats` — Agent workforce summary
- `get_daily_stats` — Daily job statistics (created/completed/failed/cancelled) over N days
- `get_stuck_jobs` — Jobs stuck beyond a time threshold
- `list_agents` — Registered agents with status
- `deregister_agent` — Remove an offline or unneeded agent
- `list_experts` — Available expert configurations
- `get_expert` — Full expert config detail
- `reload_experts` — Hot-reload expert configs from disk
- `list_datasources` — Configured datasources
- `create_datasource` — Create a new datasource (PostgreSQL, Neo4j, MongoDB)
- `update_datasource` — Modify connection details, credentials, read-only flag
- `delete_datasource` — Remove a datasource
- `get_agent_system_info` — Container resource usage (CPU, memory, disk)

#### Database Inspection
- `list_tables` — Tables with row counts
- `query_table` — Paginated table data
- `get_table_schema` — Column definitions

#### Execution Debug
- `get_audit_trail` — Paginated LLM messages, tool calls, errors
- `get_audit_bulk` — Bulk audit entries (offset/limit)
- `get_audit_timerange` — Quick first/last timestamps for audit entries
- `get_chat_bulk` — Bulk chat entries
- `get_graph_changes` — Neo4j graph mutation timeline
- `get_llm_request` — Full LLM request/response by MongoDB doc ID
- `list_llm_requests` — List LLM requests for a job
- `search_audit` — Search audit entries by content pattern
- `get_job_log` — Agent log file (tail/grep/level filter)
- `get_shell_state` — Active tmux shell tabs

#### Citation & Source Library
- `list_job_sources` — Sources registered by a job
- `get_source_detail` — Full source record with content
- `list_job_citations` — Citations with verification status
- `get_citation_detail` — Full citation with claim, quote, verification
- `search_job_sources` — Search source library by keyword or semantic
- `get_source_annotations` — Notes, highlights, summaries on a source
- `get_source_tags` — Tags assigned to a source
- `get_citation_stats` — Citation statistics by status, type, confidence

#### Knowledge Base (project-scoped)
- `get_knowledge_summary` — Stats and recent notes for a project
- `list_knowledge_notes` — Browse notes with type/status/tag/job filters
- `get_knowledge_note` — Full note content with Neo4j relationships
- `search_knowledge` — Hybrid search (dense + sparse) over notes
- `update_knowledge_note` — Change note status/tags
- `delete_knowledge_note` — Remove a note
- `export_knowledge` — Export as Obsidian-compatible markdown

#### Project Management
- `list_projects` — List projects, optionally filtered by user
- `get_project` — Full project details (name, description, goal, config)
- `create_project` — Create a new project
- `update_project` — Update name, description, goal, status, default config
- `delete_project` — Permanently delete a project (not default projects)
- `list_project_jobs` — List jobs within a project
- `create_project_job` — Create a job scoped to a project
- `list_project_members` — List members with roles (owner, editor, viewer)
- `add_project_member` — Add a user to a project with a role
- `update_project_member` — Change a member's role
- `remove_project_member` — Remove a member (not the last owner)
- `list_project_experts` — List project-specific expert configurations
- `get_project_expert` — Detailed expert config with merged settings and instructions

#### Actions (Mutations)
- `approve_job` — Approve a pending_review job
- `resume_job_with_feedback` — Resume a frozen/failed job with feedback
- `cancel_job` — Cancel a running job
- `pause_job` — Cooperative pause at next safe point
- `delete_job` — Permanently delete a job
- `assign_job` — Assign a created job to a ready agent
- `create_job` / `create_follow_up_job` — Create a new job (standalone)
- `create_project_job` — Create a job within a project
- `promote_job` — Promote a completed job into a dedicated project
- `test_datasource` — Test datasource connectivity

#### Research
- `web_search` — Tavily web search (for researching domains before writing instructions)

## Builder AI Workflow

The system prompt (`builder_prompt.py`) defines a 4-phase instruction-writing process:

1. **Understand** — Ask 2-3 clarifying questions about goal, domain, constraints
2. **Research** — Use `web_search` to learn domain best practices (2-4 searches)
3. **Draft** — Write comprehensive instructions via `update_instructions`
4. **Refine** — Iterate on feedback with `edit_instructions` / `insert_instructions`

The builder also serves as a **job assistant**: when a job is selected as "active context," it defaults to inspecting/managing that job. It can check progress, read workspace files, browse git history, debug execution, review citations, and take actions.

## Gap Analysis

### Resolved (Phases 1–7, implemented 2026-03-08)

All planned gaps have been closed. All tools are available in both the builder and MCP server.

- **Project Management (Full)** — `list_projects`, `get_project`, `create_project`, `update_project`, `delete_project`, `list_project_jobs`, `create_project_job`, `list_project_members`, `add_project_member`, `update_project_member`, `remove_project_member`, `list_project_experts`, `get_project_expert`. Full project lifecycle including member management and project-scoped expert configs.
- **Knowledge Base (Full)** — `get_knowledge_summary`, `list_knowledge_notes`, `get_knowledge_note`, `search_knowledge`, `update_knowledge_note`, `delete_knowledge_note`, `export_knowledge`. Full read/write access to project knowledge bases.
- **Datasource CRUD** — `create_datasource`, `update_datasource`, `delete_datasource`. Connection URLs are masked in responses (passwords replaced with `***`).
- **Job Promotion** — `promote_job`. Completed jobs can be promoted into dedicated projects.
- **Todo Archives** — `get_current_todos`, `list_todo_archives`, `get_todo_archive`. Lightweight todo access and phase history browsing.
- **Audit Time Range** — `get_audit_timerange`. Quick first/last timestamps for job audit entries.
- **Minor Tools** — `get_daily_stats` (daily job statistics), `reload_experts` (hot-reload expert configs), `deregister_agent` (remove offline agents).
- **Prompt Improvements** — Active project context injection alongside active job context, knowledge search vs web search guidance, improved tool grouping.

### Remaining: Minor Gaps

| Gap | Notes |
|-----|-------|
| `get_graph_bulk` | Bulk Neo4j mutation history — low priority, `get_graph_changes` covers most use cases |
| Document upload | Builder can `create_job` but cannot attach documents — inherent chat interface limitation, would need file-picker UI integration |
| Model selector dropdown | UI-only change in the cockpit Angular frontend, not a backend tool |

### Parity with MCP

The builder and MCP server (`orchestrator/mcp/server.py`) are at full feature parity for all implemented tool categories. Both share the same `AsyncCockpitClient` methods and `formatters.py` output functions. The builder additionally has artifact mutation tools, workspace edit proposals, and web search that the MCP does not.

## Implementation Roadmap

**Status overview (as of 2026-03-08):** All 7 phases complete (31 new tools added, 91 total builder tools, 83 dispatch entries). Builder and MCP server are at full parity.

Each phase adds a self-contained set of tools. Phases are ordered by user impact and implementation ease. Within each phase, the work follows the same pattern:

**Per-tool implementation steps:**
1. Add client method to `AsyncCockpitClient` (if not already present)
2. Add formatter function to `formatters.py` (if not already present)
3. Add tool schema to `BUILDER_TOOLS` list in `builder_tools.py`
4. Add tool name to `SERVER_SIDE_TOOLS` set in `builder_tools.py`
5. Add dispatch handler to `builder_dispatch.py`
6. Add corresponding `@mcp.tool` to `mcp/server.py` (keep MCP at parity)
7. Update system prompt in `builder_prompt.py` to document the new tool
8. Test via builder chat and MCP

### Phase 1: Wire Up Existing Unused Client Methods ✅

**Status:** Implemented (2026-03-08)
**Effort:** Low — client methods already exist, only need schemas + dispatch + MCP wiring.

| Tool | Client Method | Notes |
|------|--------------|-------|
| `list_todo_archives` | `list_archived_todos()` | Phase history metadata |
| `get_todo_archive` | `get_archived_todos(filename)` | Full archived todo content |
| `get_current_todos` | `get_current_todos()` | Lightweight active-only todos |
| `get_audit_timerange` | `get_audit_time_range()` | Quick first/last timestamps |

**Files to change:**
- `builder_tools.py` — 4 tool schemas + add to `SERVER_SIDE_TOOLS`
- `builder_dispatch.py` — 4 handler functions + dispatch entries
- `mcp/server.py` — 4 `@mcp.tool` functions
- `builder_prompt.py` — add to "Execution debug" or "Job inspection" sections

### Phase 2: Knowledge Base (Read-Only) ✅

**Status:** Implemented (2026-03-08)
**Effort:** Medium — need new client methods + formatters. Read-only avoids mutation complexity.

| Tool | API Endpoint | Notes |
|------|-------------|-------|
| `get_knowledge_summary` | `GET /api/projects/{id}/knowledge/summary` | Stats + recent notes |
| `list_knowledge_notes` | `GET /api/projects/{id}/knowledge` | Filtered, paginated listing |
| `get_knowledge_note` | `GET /api/projects/{id}/knowledge/{note_id}` | Full note + relationships |
| `search_knowledge` | `POST /api/projects/{id}/knowledge/search` | Hybrid search |

**Files to change:**
- `mcp/client.py` — 4 new async methods
- `formatters.py` — 4 formatter functions (summary, note list, note detail, search results)
- `builder_tools.py` — 4 tool schemas + `SERVER_SIDE_TOOLS`
- `builder_dispatch.py` — 4 handlers + dispatch entries
- `mcp/server.py` — 4 `@mcp.tool` functions
- `builder_prompt.py` — new "Knowledge base" section in system prompt

**Design consideration:** These tools require a `project_id` parameter. The builder could resolve this from the active job's `project_id` (already available via `get_job`), or accept it explicitly. Consider adding a `active_project_id` to the system prompt injection alongside `active_job_id`.

### Phase 3: Project Management (Core) ✅

**Status:** Implemented (2026-03-08)
**Effort:** Medium — new client methods + formatters. Start with read + create, defer member/repo management.

| Tool | API Endpoint | Notes |
|------|-------------|-------|
| `list_projects` | `GET /api/projects` | With optional `user_id` filter |
| `get_project` | `GET /api/projects/{id}` | Project details |
| `create_project` | `POST /api/projects` | Name, description, goal |
| `list_project_jobs` | `GET /api/projects/{id}/jobs` | Jobs within a project |
| `create_project_job` | `POST /api/projects/{id}/jobs` | Create job scoped to project |

**Files to change:**
- `mcp/client.py` — 5 new async methods
- `formatters.py` — 3 formatter functions (project list, project detail, project jobs)
- `builder_tools.py` — 5 tool schemas + `SERVER_SIDE_TOOLS`
- `builder_dispatch.py` — 5 handlers + dispatch entries
- `mcp/server.py` — 5 `@mcp.tool` functions
- `builder_prompt.py` — new "Project management" section

**Design consideration:** `create_project_job` should accept the same parameters as `create_job` plus a `project_id`. Consider reusing the existing `_create_job` handler with an optional `project_id` parameter rather than duplicating logic.

### Phase 4: Datasource CRUD ✅

**Status:** Implemented (2026-03-08)
**Effort:** Medium — new client methods. Connection URLs contain credentials so formatters must mask sensitive data.

| Tool | API Endpoint | Notes |
|------|-------------|-------|
| `create_datasource` | `POST /api/datasources` | Type, name, URL, credentials, read-only |
| `update_datasource` | `PUT /api/datasources/{id}` | Modify connection details |
| `delete_datasource` | `DELETE /api/datasources/{id}` | Remove datasource |

**Files to change:**
- `mcp/client.py` — 3 new async methods
- `formatters.py` — action result formatters (reuse `format_action_result` pattern)
- `builder_tools.py` — 3 tool schemas + `SERVER_SIDE_TOOLS`
- `builder_dispatch.py` — 3 handlers + dispatch entries
- `mcp/server.py` — 3 `@mcp.tool` functions
- `builder_prompt.py` — expand "Monitoring & system" section

**Security note:** The builder LLM sees tool results. Connection URLs should be masked in responses (password replaced with `***`). The existing `datasource-list.component.ts` already does this client-side — apply the same masking in `formatters.py`.

### Phase 5: Knowledge Base (Mutations) + Job Promotion ✅

**Status:** Implemented (2026-03-08)
**Effort:** Medium — builds on Phase 2 client methods. Mutations need careful prompt guidance.

| Tool | API Endpoint | Notes |
|------|-------------|-------|
| `update_knowledge_note` | `PATCH /api/projects/{id}/knowledge/{note_id}` | Change status/tags |
| `delete_knowledge_note` | `DELETE /api/projects/{id}/knowledge/{note_id}` | Remove note |
| `export_knowledge` | `POST /api/projects/{id}/knowledge/export` | Obsidian markdown export |
| `promote_job` | `POST /api/jobs/{id}/promote` | Promote completed job to project |

**Files to change:**
- `mcp/client.py` — 4 new async methods
- `formatters.py` — action result formatters
- `builder_tools.py` — 4 tool schemas + `SERVER_SIDE_TOOLS`
- `builder_dispatch.py` — 4 handlers + dispatch entries
- `mcp/server.py` — 4 `@mcp.tool` functions
- `builder_prompt.py` — expand knowledge + add promotion section

### Phase 6: Project Management (Extended) ✅

**Status:** Implemented (2026-03-08)
**Effort:** Medium-high — 8 tools (7 planned + `get_project_expert` added).

| Tool | API Endpoint | Notes |
|------|-------------|-------|
| `update_project` | `PATCH /api/projects/{id}` | Name, description, goal, defaults |
| `delete_project` | `DELETE /api/projects/{id}` | Cascade cleanup |
| `list_project_members` | `GET /api/projects/{id}/members` | Members with roles |
| `add_project_member` | `POST /api/projects/{id}/members` | Add user by ID + role |
| `update_project_member` | `PATCH /api/projects/{id}/members/{user_id}` | Change role |
| `remove_project_member` | `DELETE /api/projects/{id}/members/{user_id}` | Remove member |
| `list_project_experts` | `GET /api/projects/{id}/experts` | Project-specific experts |
| `get_project_expert` | `GET /api/projects/{id}/experts/{name}` | Detailed expert config + instructions |

### Phase 7: Minor Tools & Polish ✅

**Status:** Implemented (2026-03-08)
**Effort:** Low-medium — 3 tools + prompt improvements.

| Tool | Notes |
|------|-------|
| `get_daily_stats` | New client method + formatter |
| `reload_experts` | New client method, admin-only |
| `deregister_agent` | New client method, admin-only |

**Prompt improvements:**
- ~~Improve tool grouping in the system prompt~~ ✅ Done
- ~~Add active project context injection alongside active job context~~ ✅ Done — `active_project_id` param added to `build_system_prompt` and `BuilderMessage` body, threaded through `main.py`
- ~~Add guidance for when to use knowledge search vs web search~~ ✅ Done — added to system prompt after knowledge base section
- Add a model selector dropdown to the builder UI — deferred (frontend-only change)

## Key Files

| File | Purpose |
|------|---------|
| `orchestrator/services/builder_prompt.py` | System prompt with artifact state injection |
| `orchestrator/services/builder_tools.py` | Tool schemas (OpenAI function-calling format), model config functions, context management |
| `orchestrator/services/builder_dispatch.py` | Server-side tool execution dispatch table |
| `orchestrator/services/builder_search.py` | Tavily web search integration |
| `orchestrator/services/formatters.py` | Shared output formatters (used by builder + MCP) |
| `orchestrator/mcp/client.py` | `AsyncCockpitClient` — shared HTTP client for orchestrator API |
| `orchestrator/mcp/server.py` | MCP server tool definitions (keep at parity with builder) |
| `orchestrator/main.py` | SSE streaming endpoint, session management, provider-specific streaming |
| `cockpit/src/app/components/instruction-builder/` | Angular chat component |
| `cockpit/src/app/core/services/builder-stream.service.ts` | SSE client with event parsing |
| `cockpit/src/app/core/services/job-artifact.service.ts` | Bidirectional artifact state management |
| `cockpit/src/app/core/services/job-context.service.ts` | Active job selection (shared across cockpit) |
| `cockpit/src/assets/env.js` | Runtime config (API URL, builder models) |
