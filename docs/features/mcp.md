---
tags:
  - feature
  - analysis
  - mcp
  - tooling
aliases:
  - mcp gap analysis
  - orchestrator mcp
related:
  - "[[orchestrator]]"
  - "[[builder_tools]]"
---

# MCP Tool Gap Analysis

Analysis of MCP tool coverage based on two observed Claude Code debugging sessions against the same live running job (`60ab7f8c`). Across both sessions, the model made 28 curl/bash calls and 19 MCP tool calls — falling back to raw API access when MCP tools were missing, returned unusable output, or were forgotten after early friction.

**Session 1** focused on shell state and log inspection (tmux windows, interactive prompt detection, log grep). The model used bash for most of the work because no MCP tools exist for these.

**Session 2** focused on job progress monitoring (phase tracking, workspace state, audit trail). The model started with MCP tools, hit multiple UX issues (empty chat history, undiscoverable doc_ids, content-free search results), and abandoned MCP for curl mid-session.

## MCP Output Quality Issues

Problems with existing tools that caused the model to give up on MCP entirely. These require no new API endpoints — just better formatting in the MCP server (`orchestrator/mcp/server.py`).

### 1. `search_audit` returns entry numbers without content

```
Found 20 entries matching 'shell':
[34] tool
[48] tool
[50] tool
...
```

Just index numbers and types — no tool names, arguments, or result snippets. In session 2 the model ran 8 consecutive `search_audit` calls (`shell`, `todo_complete`, `fessi-neo4j`, `neo4j-admin`, `quadlet`, `systemctl`, `podman`, `write_file`) and got nothing actionable from any of them.

**Fix:** Include tool name + first ~100 chars of arguments per entry. E.g., `[34] tool: shell_execute("apt-get install -y sshpass 2>/dev/null || ...")`.

### 2. `get_audit_trail` doesn't expose MongoDB doc_ids

The audit trail shows entries like `[306] llm: 2026-03-03T10:21:06...` but never includes the MongoDB `_id`. In session 2, the model tried `get_llm_request(doc_id: "306")` — passing the entry number instead of a 24-char ObjectId — and got 404. This makes `get_llm_request` effectively unreachable through the normal workflow.

**Fix:** Include the MongoDB `_id` for `llm` type entries in `get_audit_trail` output.

### 3. `get_chat_history` returns empty turns

```
Chat history (page 15, 8 of 148 turns):
--- Turn 1 ---
[assistant]:
--- Turn 2 ---
[assistant]:
```

When the agent's responses are purely tool calls with no text content, every turn appears empty. The model gave up immediately: *"Chat history shows empty turns (content is in tool calls)."*

**Fix:** For tool-only turns, include tool name summaries. E.g., `[assistant]: [tool calls: shell_execute, todo_complete]`.

### 4. `list_jobs` status enum not documented

The model tried `list_jobs(status: "running")` and got zero results — the actual status value is `"processing"`. Had to retry without a filter and scan manually.

**Fix:** Document valid status values in the tool description: `created`, `processing`, `completed`, `failed`, `cancelled`, `pending_review`.

## MCP Discovery & Fallback Patterns

### Workspace tools exist but were bypassed

The MCP has `get_workspace_file(job_id, path)` and `get_workspace_overview(job_id)`, but the model used curl for all 8+ workspace reads in session 2. The sequence:

1. Tried local file reads → nothing (job runs remotely)
2. Went straight to `curl .../workspace/workspace.md`, `curl .../workspace/plan.md`, etc.
3. Never attempted MCP workspace tools

Likely cause: after hitting friction with `get_chat_history` (empty), `get_llm_request` (404), and `search_audit` (useless output), the model lost confidence in MCP tools and defaulted to curl for everything.

**Takeaway:** Early MCP failures create a cascading trust problem. Fixing the output quality issues above would likely prevent most curl fallback for workspace reads too.

### Curl used for raw JSON flexibility

Three curl calls in session 1 had direct MCP equivalents:

| Curl call | MCP equivalent | Why curl was used |
|-----------|---------------|-------------------|
| `curl .../api/jobs?status=processing` | `list_jobs(status="processing")` | Piped through `python3 -m json.tool \| head -60` |
| `curl .../api/jobs/{id}` | `get_job(job_id=...)` | Piped through `head -40` to truncate |
| `curl .../api/jobs/{id}/audit?limit=20` | `get_audit_trail(...)` | Custom python to extract timestamps + tool names |

The model wanted to slice, truncate, and reformat responses in ways MCP's pre-formatted text output doesn't support.

### Audit trail pagination on active jobs

The model made 8+ audit trail calls in session 2 trying to find recent activity. The job was actively growing (308 → 318 → 340 entries), so page counts shifted between calls:

```
get_audit_trail(page: -1, page_size: 15)    → page 21 (8 entries)
get_audit_trail(page: 20, page_size: 15)    → entries 286-300
get_audit_trail(page: 21, page_size: 20)    → empty (job grew, page count changed)
curl: audit?page=7&page_size=50             → entries 301-340
```

The model switched to curl with `page_size=50` to reduce round-trips.

## Gaps With No MCP Coverage

### No shell/tmux inspection (Session 1)

The model ran 4 bash calls to inspect the running job's terminal state:
- `tmux list-sessions` → find session name
- `tmux list-windows -t "agent_60ab7f8c-0f7"` → 3 windows (default, main, transfer)
- `tmux capture-pane -t "...:default" -p -S -50` → recent output per window
- `tmux capture-pane -t "...:main" -p -S -50`

No MCP tool or API endpoint exists for this.

### No agent log access (Session 1)

The model ran 4 bash calls to find and search log files:
- `find /home/ghost -name "job_60ab7f8c*.log"` → discover log path
- `grep -n "interactive prompt\|timed out\|..." .log` → search for events
- `grep -n -A5 -B5 "Interactive prompt detected" .log` → context around matches
- `tail -30 .log` → latest activity

No MCP tool or API endpoint exists for this. Logs live in `{workspace_base}/logs/` on the agent host.

### No archive directory listing

The model tried `curl .../workspace/archive/` (404) and `curl .../archive` (404) to list archive contents. Had to infer filenames from `get_todos` output, then guess paths — getting 404s on wrong guesses (`phase_4_retrospective.md` didn't exist, had to try `phase_3_retrospective.md`).

The workspace API can read individual files but can't list subdirectory contents.

### No LLM request listing

Both sessions needed to inspect LLM requests for a job. Session 1 tried `curl .../api/jobs/{id}/llm-requests` (404 — endpoint doesn't exist). The only path is: `search_audit` → find entry number → paginate `get_audit_trail` → extract MongoDB doc_id (not exposed) → `get_llm_request`. This workflow is broken at step 3 (doc_ids not shown).

## Unwrapped API Endpoints

Orchestrator endpoints that exist but have no MCP tool:

| API endpoint | Use case | Priority |
|-------------|----------|----------|
| `GET /api/jobs/{id}/audit/timerange` | Filter audit by time window — "what happened in the last 5 minutes" | High |
| `GET /api/jobs/{id}/audit/bulk` | Full audit dump without pagination (model paginated 8+ times) | High |
| `GET /api/jobs/{id}/chat/bulk` | Full chat dump without pagination | Medium |
| `PUT /api/jobs/{id}/pause` | Pause a running job | Medium |
| `GET /api/stats/daily` | Daily job/agent statistics | Low |
| `GET /api/jobs/{id}/version` | Job version metadata | Low |
| `PUT /api/jobs/{id}/workspace/{path}` | Write workspace files (read exists in MCP, write doesn't) | Low |
| `POST /api/experts/reload` | Force-reload expert config cache | Low |

## Proposed New Tools

### `get_job_summary` — High Priority

Composite MCP tool that answers "how is this job doing?" in one call. In session 2, the model needed 6+ calls to assemble this picture.

```
Parameters:
  job_id: string

Returns:
  status: string              # processing, completed, etc.
  description: string         # Job description
  current_phase: int          # e.g., 6
  phase_type: string          # strategic or tactical
  total_phases_completed: int # e.g., 5
  elapsed_time: string        # e.g., "1h 34m"
  audit_entries: int          # Total count
  recent_tools: list          # Last 10 tool calls with names + timestamps
  workspace_summary: string   # First ~500 chars of workspace.md
  plan_summary: string        # First ~500 chars of plan.md
  current_todos: list         # Active todo items (if in tactical phase)
  archived_phases: list       # Phase names with timestamps
```

**Implementation:** Server-side composition. Calls `get_job`, `get_job_progress`, `get_todos`, `get_workspace_file` (workspace.md, plan.md), and `get_audit_trail` (last page) internally. Single structured response.

### `get_job_log` — High Priority

Replaces the 5+ bash calls spent on log file reading in session 1. Requires a new API endpoint.

```
Parameters:
  job_id: string          # Resolves to log file path
  lines: int = 100        # Tail N lines
  grep: string = null     # Filter lines by pattern (case-insensitive)
  level: enum = null      # Filter by log level (DEBUG, INFO, WARNING, ERROR)

Returns: Matching log lines with timestamps
```

**Implementation:** Add `GET /api/jobs/{id}/logs` that reads from `{workspace_base}/logs/job_{id}.log` with tail + grep server-side.

### `get_shell_state` — High Priority

Replaces the 4 tmux bash calls from session 1. Requires a new API endpoint or agent proxy.

```
Parameters:
  job_id: string          # Resolves to tmux session name
  lines: int = 30         # Capture last N lines per pane
  tab: string = null      # Specific tab name (null = all tabs)

Returns: List of open tabs with name, type, and recent output
```

**Implementation:** Either proxy through the agent's API at port 8001 (co-located with tmux), or add to the existing `/system-info` endpoint which already runs on the agent host.

### `list_llm_requests` — Medium Priority

Replaces the broken `search_audit` → `get_llm_request` workflow. Requires a new API endpoint.

```
Parameters:
  job_id: string
  limit: int = 20
  offset: int = 0
  tool_filter: string = null   # Only show requests containing this tool name

Returns: List of LLM requests with doc_id, timestamp, model, token counts, tool names called
```

**Implementation:** Query MongoDB's LLM request collection filtered by `job_id`. Return summary rows (not full payloads) with `doc_id` for drill-down via existing `get_llm_request`.

### `get_audit_timerange` — Medium Priority

Wraps the existing `GET /api/jobs/{id}/audit/timerange` endpoint. Solves the pagination problem on active jobs.

```
Parameters:
  job_id: string
  start: datetime         # ISO 8601
  end: datetime           # ISO 8601
  filter: enum = "all"    # all, messages, tools, errors

Returns: Audit entries within the time window
```

### `get_audit_bulk` — Low Priority

Wraps `GET /api/jobs/{id}/audit/bulk`. Full audit dump without pagination — useful for scanning entire execution history.

```
Parameters:
  job_id: string
  filter: enum = "all"

Returns: All audit entries (may be large, consider truncation)
```

## Priority Summary

| # | Change | Type | Impact |
|---|--------|------|--------|
| 1 | `search_audit` — include tool names + argument preview | Output fix | 8 wasted search calls in session 2 |
| 2 | `get_audit_trail` — expose MongoDB doc_ids on llm entries | Output fix | Unblocks `get_llm_request` entirely |
| 3 | `get_chat_history` — summarize tool-only turns | Output fix | Makes chat history usable |
| 4 | `list_jobs` — document valid status values | Documentation fix | 1 wasted call per session |
| 5 | `get_job_summary` | New composite tool | Replaces 6+ calls for "how is this job doing?" |
| 6 | `get_job_log` | New endpoint + tool | Replaces 5+ bash calls for log access |
| 7 | `get_shell_state` | New endpoint + tool | Replaces 4 bash calls for tmux inspection |
| 8 | `get_audit_timerange` | Wrap existing endpoint | Eliminates pagination on active jobs |
| 9 | `list_llm_requests` | New endpoint + tool | Replaces broken multi-step discovery |
| 10 | `get_audit_bulk` | Wrap existing endpoint | Eliminates pagination for full dumps |

Items 1–4 are output quality fixes that require no new API endpoints — just formatting changes in the MCP server. Items 5–10 require new code. The output fixes would likely also reduce curl fallback for workspace reads (the cascading trust problem described above).
