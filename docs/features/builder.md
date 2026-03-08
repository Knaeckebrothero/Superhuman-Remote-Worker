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
- Active job context (`active_job_id`) is also injected into the system prompt, guiding the LLM to use that job by default for inspection tools

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
- `get_todos` — Current and archived task lists
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
- `get_stuck_jobs` — Jobs stuck beyond a time threshold
- `list_agents` — Registered agents with status
- `list_experts` — Available expert configurations
- `get_expert` — Full expert config detail
- `list_datasources` — Configured datasources (read-only)
- `get_agent_system_info` — Container resource usage (CPU, memory, disk)

#### Database Inspection
- `list_tables` — Tables with row counts
- `query_table` — Paginated table data
- `get_table_schema` — Column definitions

#### Execution Debug
- `get_audit_trail` — Paginated LLM messages, tool calls, errors
- `get_audit_bulk` — Bulk audit entries (offset/limit)
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

#### Actions (Mutations)
- `approve_job` — Approve a pending_review job
- `resume_job_with_feedback` — Resume a frozen/failed job with feedback
- `cancel_job` — Cancel a running job
- `pause_job` — Cooperative pause at next safe point
- `delete_job` — Permanently delete a job
- `assign_job` — Assign a created job to a ready agent
- `create_job` / `create_follow_up_job` — Create a new job
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

## Gap Analysis (as of 2026-03-08)

### Missing: Project Management

The builder has no concept of projects. The orchestrator API exposes full project CRUD, member management, repository management, and project-scoped job creation, but none of this is available through the builder.

**Missing tools:**
- `list_projects` / `get_project` / `create_project` / `update_project` / `delete_project`
- `list_project_members` / `add_project_member` / `update_project_member` / `remove_project_member`
- `list_project_repositories` / `add_project_repository` / `remove_project_repository`
- `create_project_job` / `list_project_jobs`
- `get_project_experts` / `get_project_expert_detail`
- `promote_job` — promote a completed job to its own project

**Impact:** Users cannot organize work into projects, manage team access, or create project-scoped jobs through the builder. They must switch to other cockpit panels.

### Missing: Knowledge Base

The project knowledge base (notes accumulated from completed jobs) is inaccessible from the builder.

**Missing tools:**
- `get_knowledge_summary` — stats and recent notes for a project
- `list_knowledge_notes` — browse notes with type/status/tag filters
- `get_knowledge_note` — full note content with Neo4j relationships
- `search_knowledge` — hybrid search (dense + sparse) over notes
- `update_knowledge_note` — change note status/tags
- `delete_knowledge_note` — remove a note
- `export_knowledge` — export as Obsidian-compatible markdown

**Impact:** The builder can't help users leverage accumulated knowledge when writing instructions or reviewing job outputs. This is a significant gap for iterative refinement workflows.

### Missing: Datasource CRUD

The builder can `list_datasources` and `test_datasource`, but cannot create, update, or delete datasources.

**Missing tools:**
- `create_datasource` — create PostgreSQL/Neo4j/MongoDB datasource
- `update_datasource` — modify connection details, credentials, read-only flag
- `delete_datasource` — remove a datasource

**Impact:** Users must switch to the datasource panel to set up a new datasource, then return to the builder to attach it to a job.

### Missing: Minor Inspection Tools

Lower-priority tools that exist in the API but aren't exposed:

| Tool | API Endpoint | Purpose | Client method exists? |
|------|-------------|---------|----------------------|
| `get_daily_stats` | `GET /api/stats/daily` | Time-series job statistics | No |
| `list_todo_archives` | `GET /api/jobs/{id}/todos/archives` | Browse archived todos by phase | Yes (`list_archived_todos`) |
| `get_todo_archive` | `GET /api/jobs/{id}/todos/archives/{file}` | Read specific phase's archived todos | Yes (`get_archived_todos`) |
| `get_current_todos` | `GET /api/jobs/{id}/todos/current` | Active todos only (lightweight) | Yes (`get_current_todos`) |
| `get_audit_timerange` | `GET /api/jobs/{id}/audit/timerange` | First/last audit timestamps | Yes (`get_audit_time_range`) |
| `get_graph_bulk` | `GET /api/jobs/{id}/graph/bulk` | Bulk Neo4j mutation history | No |
| `reload_experts` | `POST /api/experts/reload` | Hot-reload expert configs from disk | No |
| `deregister_agent` | `DELETE /api/agents/{id}` | Remove an agent | No |

Note: 4 of these already have `AsyncCockpitClient` methods implemented but unused — they only need tool schemas and dispatch wiring.

### Missing: Document Upload

The cockpit UI supports drag-and-drop file upload during job creation. The builder can `create_job` but cannot attach documents. This is inherently limited by the chat interface but could potentially be addressed with a file-picker integration.

### Parity with MCP

The builder and MCP server (`orchestrator/mcp/server.py`) are at feature parity for inspection and action tools — both are missing the same categories (projects, knowledge, datasource CRUD). The builder additionally has artifact mutation tools, workspace edit proposals, and web search that the MCP does not.

## Implementation Roadmap

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

### Phase 1: Wire Up Existing Unused Client Methods

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

### Phase 2: Knowledge Base (Read-Only)

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

### Phase 3: Project Management (Core)

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

### Phase 4: Datasource CRUD

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

### Phase 5: Knowledge Base (Mutations) + Job Promotion

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

### Phase 6: Project Management (Extended)

**Effort:** Medium-high — many tools but each is straightforward CRUD.

| Tool | API Endpoint | Notes |
|------|-------------|-------|
| `update_project` | `PATCH /api/projects/{id}` | Name, description, goal, defaults |
| `delete_project` | `DELETE /api/projects/{id}` | Cascade cleanup |
| `list_project_members` | `GET /api/projects/{id}/members` | Members with roles |
| `add_project_member` | `POST /api/projects/{id}/members` | Add user by ID + role |
| `update_project_member` | `PATCH /api/projects/{id}/members/{user_id}` | Change role |
| `remove_project_member` | `DELETE /api/projects/{id}/members/{user_id}` | Remove member |
| `get_project_experts` | `GET /api/projects/{id}/experts` | Project-specific experts |

**Files to change:** Same pattern as prior phases across all 6 files.

### Phase 7: Minor Tools & Polish

**Effort:** Low-medium — fill remaining gaps, improve prompt quality.

| Tool | Notes |
|------|-------|
| `get_daily_stats` | New client method + formatter |
| `reload_experts` | New client method, admin-only |
| `deregister_agent` | New client method, admin-only |

**Prompt improvements:**
- Add active project context injection alongside active job context
- Improve tool grouping in the system prompt (projects, knowledge, datasources as top-level sections)
- Add guidance for when to use knowledge search vs web search
- Add a model selector dropdown to the builder UI

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
