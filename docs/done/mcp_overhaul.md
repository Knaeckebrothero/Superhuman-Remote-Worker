# MCP Overhaul: Full Feature Parity & Action Tools

## Motivation

The MCP currently exposes **8 out of 70** orchestrator API endpoints — all read-only, all focused on MongoDB audit data. This means the AI assistant (Claude Code) can inspect audit trails but cannot:

- Review and approve/deny frozen jobs
- Resume jobs with feedback
- See what the agent actually produced (git history, workspace files)
- Monitor system health (stats, stuck jobs, agent status)
- Manage datasources or view expert configs

This overhaul brings the MCP to feature parity with the cockpit UI and adds action tools that let the AI actively manage jobs — the single biggest capability unlock.

## Current State

### Existing MCP Tools (8 total, all read-only)

| Tool | Source | Purpose |
|------|--------|---------|
| `list_jobs` | PostgreSQL | List jobs with status filter |
| `get_job` | PostgreSQL | Job details by ID |
| `get_audit_trail` | MongoDB | Paginated audit entries (LLM messages, tool calls, errors) |
| `get_chat_history` | MongoDB | Clean conversation turns |
| `get_todos` | Filesystem | Current + archived todos |
| `get_graph_changes` | MongoDB | Neo4j mutation timeline |
| `get_llm_request` | MongoDB | Full LLM request/response by doc ID |
| `search_audit` | MongoDB | Text search across audit entries |

### Coverage Gap

| Category | Orchestrator Endpoints | MCP Tools | Coverage |
|----------|----------------------|-----------|----------|
| Job CRUD & lifecycle | 10 | 2 | 20% |
| Audit & monitoring | 9 | 5 | 56% |
| Workspace & files | 8 | 1 | 13% |
| Git/Gitea | 2 (+needs new) | 0 | 0% |
| Citations & sources | 0 (needs new) | 0 | 0% |
| Datasources | 7 | 0 | 0% |
| Agents | 5 | 0 | 0% |
| Statistics | 4 | 0 | 0% |
| Experts/Config | 2 | 0 | 0% |
| Database inspection | 3 | 0 | 0% |
| Uploads | 5 | 0 | 0% |
| Builder | 4 | 0 | 0% |
| **Total** | **~70** | **8** | **~11%** |

---

## Proposed New Tools

### Category A: Action & Operations Tools

These are the highest-impact additions — they let the AI actively manage jobs instead of just observing. All mutation tools are marked with confirmation requirements; `test_datasource` is the one read-only probe included here for operational grouping.

#### A1. `approve_job`

Approve a frozen job, marking it as completed.

**Parameters:**
- `job_id` (required) — Job UUID

**Behavior:** Reads `job_frozen.json` from Gitea, writes `job_completion.json`, deletes `job_frozen.json`, updates job status to `completed`.

**Backend:** `POST /api/jobs/{job_id}/approve`

**Confirmation required:** Yes — this is a terminal action.

---

#### A2. `resume_job_with_feedback`

Resume a frozen/failed job from its checkpoint, optionally injecting feedback.

**Parameters:**
- `job_id` (required) — Job UUID
- `feedback` (optional) — Natural language feedback to inject into the agent's context on resume

**Behavior:** Sends resume request to the assigned agent pod. If feedback is provided, it's injected into the job state before re-execution. If the originally assigned agent is offline or unavailable, the orchestrator auto-selects a ready agent.

**Status constraint:** Job can be in any status except `completed`. Supports resuming `failed`, `pending_review`, `cancelled`, and even `processing` (stale) jobs.

**Backend:** `POST /api/jobs/{job_id}/resume` with body `{ "feedback": "..." }`

**Confirmation required:** Yes — this starts a running agent.

---

#### A3. `cancel_job`

Cancel a running job.

**Parameters:**
- `job_id` (required) — Job UUID

**Behavior:** Sends cancel signal to the agent pod processing this job.

**Backend:** `PUT /api/jobs/{job_id}/cancel`

**Confirmation required:** Yes — irreversible for in-progress work.

---

#### A4. `create_job`

Create a new job for agent execution.

**Parameters:**
- `description` (required) — Natural language task description
- `config_name` (optional, default `"default"`) — Expert/agent config to use
- `datasource_ids` (optional) — List of datasource UUIDs to clone as job-scoped
- `instructions` (optional) — Additional inline markdown instructions
- `config_override` (optional) — Per-job config overrides as JSON (e.g., model settings, tool toggles)
- `context` (optional) — Additional context dictionary

**Behavior:** Creates job record, creates Gitea repo for workspace delivery, returns job ID.

**Backend:** `POST /api/jobs` with JSON body

**Confirmation required:** Yes — creates a job that may be auto-assigned to an agent.

**Note:** File uploads are out of scope for MCP. Jobs requiring input documents should be created via the cockpit UI. The MCP creation path is for text-based tasks.

---

#### A5. `delete_job`

Delete a job and its associated data.

**Parameters:**
- `job_id` (required) — Job UUID

**Behavior:** Deletes job record, requirements, and optionally the Gitea repo. The backend does not enforce status constraints — any job can be deleted regardless of status. The MCP tool description should warn that deleting a `processing` job may leave an orphaned agent.

**Backend:** `DELETE /api/jobs/{job_id}`

**Confirmation required:** Yes — destructive and irreversible.

---

#### A6. `assign_job`

Assign a created job to a ready agent.

**Parameters:**
- `job_id` (required) — Job UUID
- `agent_id` (required) — Agent UUID

**Behavior:** Sends `JobStartRequest` to the agent pod, updates job status to `processing`.

**Backend:** `POST /api/jobs/{job_id}/assign/{agent_id}`

**Confirmation required:** Yes — triggers agent execution.

---

#### A7. `test_datasource`

Test connectivity to a datasource.

**Parameters:**
- `datasource_id` (required) — Datasource UUID

**Returns:** Success/failure with error message if applicable.

**Backend:** `POST /api/datasources/{datasource_id}/test`

**Confirmation required:** No — read-only probe.

---

### Category B: Git History Tools (Gitea)

These expose the agent's git-tracked work history, which is the most natural way to review what a job produced.

#### B1. `list_job_commits`

List git commits for a job's repository.

**Parameters:**
- `job_id` (required) — Job UUID
- `ref` (optional) — Branch or tag to list from (default: `main`)
- `since_ref` (optional) — Only show commits after this ref (e.g., `phase_2_end`)
- `limit` (optional, default 20) — Max commits to return
- `page` (optional, default 1) — Pagination

**Returns:** List of commits with hash, message, timestamp, author.

**Use case:** "What did the agent do in phase 3?" → `list_job_commits(job_id, since_ref="phase_2_end")`

**Backend:** New orchestrator endpoint → Gitea API `GET /repos/{owner}/{repo}/commits`

---

#### B2. `get_job_diff`

Show the diff between two git refs in a job's repository.

**Parameters:**
- `job_id` (required) — Job UUID
- `base` (required) — Base ref (commit SHA, tag, or branch). Use `job-frozen` to diff since last freeze.
- `head` (optional, default `HEAD`) — Head ref
- `file_path` (optional) — Filter diff to a specific file
- `max_chars` (optional, default 50000) — Truncate diff output beyond this limit. Set to 0 for unlimited.

**Returns:** Unified diff output with file-level change summary (files changed, lines added/removed). Truncated with a `[truncated]` marker if exceeding `max_chars`.

**Use case:** "What changed since the job was last frozen?" → `get_job_diff(job_id, base="job-frozen")`

**Backend:** New orchestrator endpoint → Gitea API `GET /repos/{owner}/{repo}/compare/{base}...{head}`. Truncation is applied in the MCP formatter, not at the Gitea level.

---

#### B3. `get_job_file`

Read a specific file from the job's Gitea repo at any ref.

**Parameters:**
- `job_id` (required) — Job UUID
- `file_path` (required) — Path within the repo
- `ref` (optional, default `HEAD`) — Branch, tag, or commit SHA

**Returns:** File content as text.

**Use case:** "What did workspace.md look like at the end of phase 2?" → `get_job_file(job_id, "workspace.md", ref="phase_2_end")`

**Backend:** Existing endpoint extended with ref support → Gitea API `GET /repos/{owner}/{repo}/raw/{path}?ref={ref}`

---

#### B4. `list_job_files`

Browse the repository directory tree at any ref.

**Parameters:**
- `job_id` (required) — Job UUID
- `path` (optional, default `/`) — Directory path
- `ref` (optional, default `HEAD`) — Branch, tag, or commit SHA

**Returns:** Directory listing with file names, types (file/dir), and sizes.

**Use case:** "What output files did the agent produce?" → `list_job_files(job_id, path="output/")`

**Backend:** Existing `GET /api/jobs/{job_id}/repo/contents` (already supports ref via query param)

---

#### B5. `list_job_tags`

List phase tags to understand the job's phase history.

**Parameters:**
- `job_id` (required) — Job UUID

**Returns:** List of tags with name, commit SHA, and timestamp. Sorted chronologically.

**Use case:** "How many phases did this job go through?" → shows `phase_1_start`, `phase_1_end`, `phase_2_start`, ...

**Backend:** New orchestrator endpoint → Gitea API `GET /repos/{owner}/{repo}/tags`

---

### Category C: Workspace & Job Context Tools

These provide direct access to the agent's workspace files and job metadata without going through git.

#### C1. `get_frozen_job`

Get the frozen job review data (summary, confidence, deliverables, notes).

**Parameters:**
- `job_id` (required) — Job UUID

**Returns:** Frozen job data including agent summary, confidence score, deliverables list, and agent notes.

**Backend:** `GET /api/jobs/{job_id}/frozen`

---

#### C2. `get_workspace_file`

Read any file from the job's local workspace filesystem.

**Parameters:**
- `job_id` (required) — Job UUID
- `path` (required) — Relative path within the workspace (e.g., `workspace.md`, `plan.md`, `todos.yaml`, `archive/phase_1_retrospective.md`, `archive/todos_phase_2.yaml`)

**Returns:** File content as text.

**Backend:** `GET /api/jobs/{job_id}/workspace/{path}`

**Note:** Unlike `get_job_file` (Gitea, any ref), this reads the current local file. Useful when Gitea is unavailable or for real-time state. Path is sandboxed to the job workspace directory — no directory traversal allowed.

---

#### C3. `get_workspace_overview`

Get a summary of the workspace state (file listing, truncated workspace.md/plan.md, todo counts, archive count).

**Parameters:**
- `job_id` (required) — Job UUID

**Returns:** Workspace overview with file list, content previews, and statistics.

**Backend:** `GET /api/jobs/{job_id}/workspace`

---

#### C4. `get_job_progress`

Get detailed job progress including phase information and ETA.

**Parameters:**
- `job_id` (required) — Job UUID

**Returns:** Progress data with current phase, todo completion stats, estimated time remaining.

**Backend:** `GET /api/jobs/{job_id}/progress`

---

#### C5. `get_job_requirements`

Get extracted requirements for a job.

**Parameters:**
- `job_id` (required) — Job UUID
- `status` (optional) — Filter by validation status

**Returns:** List of requirements with status and metadata.

**Backend:** `GET /api/jobs/{job_id}/requirements`

---

### Category D: System Monitoring Tools

These give the AI visibility into the overall system state.

#### D1. `get_job_stats`

Get job queue statistics (counts by status).

**Parameters:** None.

**Returns:** Total jobs, counts per status (created, processing, completed, failed, cancelled, pending_review).

**Backend:** `GET /api/stats/jobs`

---

#### D2. `get_agent_stats`

Get agent workforce summary.

**Parameters:** None.

**Returns:** Total agents, counts per status (ready, working, booting, offline, failed).

**Backend:** `GET /api/stats/agents`

---

#### D3. `get_stuck_jobs`

Get jobs stuck in processing beyond a threshold.

**Parameters:**
- `threshold_minutes` (optional, default 30) — Minutes after which a job is considered stuck

**Returns:** List of stuck jobs with job ID, component, stuck reason, last update timestamp.

**Backend:** `GET /api/stats/stuck`

---

#### D4. `list_agents`

List registered agents with status and current assignment.

**Parameters:**
- `status` (optional) — Filter by agent status

**Returns:** Agent list with ID, config, hostname, status, current job, last heartbeat.

**Backend:** `GET /api/agents`

---

#### D5. `list_experts`

List available expert/agent configurations.

**Parameters:** None.

**Returns:** Expert configs with ID, display name, description, tags, available tools.

**Backend:** `GET /api/experts`

---

#### D6. `get_expert`

Get full detail for an expert config (merged config + instructions).

**Parameters:**
- `expert_id` (required) — Expert config ID

**Returns:** Full merged config, system prompt, tool list, and instructions markdown.

**Backend:** `GET /api/experts/{expert_id}`

---

#### D7. `list_datasources`

List configured datasources.

**Parameters:**
- `type` (optional) — Filter by type (postgresql, neo4j, mongodb)

**Returns:** Datasource list with ID, name, type, URL (masked), read-only flag, scope.

**Backend:** `GET /api/datasources`

---

### Category E: Database Inspection Tools

These let the AI inspect the system database directly — useful for debugging data issues.

#### E1. `list_tables`

List all database tables with row counts.

**Parameters:** None.

**Returns:** Table names with row counts.

**Backend:** `GET /api/tables`

---

#### E2. `query_table`

Get paginated data from a database table.

**Parameters:**
- `table_name` (required) — Table name
- `limit` (optional, default 50) — Rows per page
- `offset` (optional, default 0) — Pagination offset

**Returns:** Row data with column names.

**Backend:** `GET /api/tables/{table_name}?limit={limit}&offset={offset}`

---

#### E3. `get_table_schema`

Get column definitions for a table.

**Parameters:**
- `table_name` (required) — Table name

**Returns:** Column names, types, nullable flags, defaults.

**Backend:** `GET /api/tables/{table_name}/schema`

---

### Category F: Citation & Source Library Tools

These expose the CitationEngine's data — sources, citations, annotations, tags, and hybrid search — for verifying agent work and exploring the knowledge base a job built up.

**Background:** The CitationEngine stores data in PostgreSQL tables (`sources`, `citations`, `job_sources`, `source_annotations`, `source_tags`, `source_embeddings`). Agents use 11 citation tools during execution. But the orchestrator currently has **zero citation API endpoints**, so none of this is accessible via MCP. New orchestrator endpoints are required.

#### F1. `list_job_sources`

List sources registered by a job (documents, websites, databases, custom artifacts). Can also query across all jobs.

**Parameters:**
- `job_id` (optional) — Job UUID. If omitted, returns sources across all jobs.
- `source_type` (optional) — Filter: `document`, `website`, `database`, `custom`
- `limit` (optional, default 50) — Max results
- `offset` (optional, default 0) — Pagination offset

**Returns:** Source list with ID, type, name, identifier, content preview (truncated), content hash, created timestamp. When querying across jobs, also includes the list of job IDs that reference each source.

**Use case:** "What documents did the agent work with?" → `list_job_sources(job_id, source_type="document")`
**Use case:** "Has this PDF been used before?" → `list_job_sources(source_type="document")` → check if the identifier appears

**Backend:** New endpoint `GET /api/sources?job_id={job_id}&type={source_type}` — queries `sources` JOIN `job_sources`. When `job_id` is omitted, queries `sources` directly.

---

#### F2. `get_source_detail`

Get full detail for a single source, including content (truncated for large sources), metadata, and content hash.

**Parameters:**
- `source_id` (required) — Source ID (integer)
- `content_limit` (optional, default 2000) — Max characters of content to return

**Returns:** Full source record with type, identifier, name, version, content (truncated), metadata, content hash, creation date.

**Use case:** Verify what content the agent actually had access to when it cited a source.

**Backend:** New endpoint `GET /api/sources/{source_id}?content_limit={limit}` — queries `sources` table directly

---

#### F3. `list_job_citations`

List all citations for a job with verification status.

**Parameters:**
- `job_id` (required) — Job UUID
- `source_id` (optional) — Filter by source
- `verification_status` (optional) — Filter: `pending`, `verified`, `failed`, `unverified`
- `limit` (optional, default 50) — Max results
- `offset` (optional, default 0) — Pagination offset

**Returns:** Citation list with ID, claim (truncated), source ID, source name, verification status, confidence, extraction method, similarity score.

**Use case:** "How many citations failed verification?" → `list_job_citations(job_id, verification_status="failed")`

**Backend:** New endpoint `GET /api/jobs/{job_id}/citations?source_id={id}&status={status}` — queries `citations` table

---

#### F4. `get_citation_detail`

Get full citation record with source info, verification details, and locator.

**Parameters:**
- `citation_id` (required) — Citation ID (integer)

**Returns:** Full citation: claim, verbatim quote, quote context, source (with name and type), locator (page/section), extraction method, confidence, verification status, verification notes, similarity score, matched location, reasoning.

**Use case:** Spot-check a specific citation — see the exact quote, where it was found, and whether verification passed.

**Backend:** New endpoint `GET /api/citations/{citation_id}` — queries `citations` JOIN `sources`

---

#### F5. `search_job_sources`

Search a job's source library using keyword, semantic (pgvector), or hybrid (RRF fusion) retrieval with explainable evidence labels.

**Parameters:**
- `job_id` (required) — Job UUID
- `query` (required) — Natural language query or keywords
- `mode` (optional, default `hybrid`) — `hybrid`, `keyword`, or `semantic`
- `source_type` (optional) — Filter by source type
- `tags` (optional) — Comma-separated tags (AND logic)
- `top_k` (optional, default 10) — Max results

**Returns:** Search results with evidence labels (HIGH/MEDIUM/LOW), evidence reasons, source references, chunk text, overall evidence summary, and search mode used.

**Use case:** "Does any source actually mention 'data retention period'?" → `search_job_sources(job_id, query="data retention period")` → see evidence labels showing how well the sources support the claim.

**Backend:** New endpoint `GET /api/jobs/{job_id}/sources/search?query={q}&mode={m}&type={t}&tags={t}&top_k={k}` — instantiates CitationEngine in multi-agent mode, calls `search_library()`. This is the most complex endpoint since it requires the embedding service for semantic search. Searches source content only (annotations are queried separately via F6).

**Note:** Semantic search requires pgvector extension and an embedding model. Falls back to keyword-only if unavailable.

---

#### F6. `get_source_annotations`

Get annotations (notes, highlights, summaries, questions, critiques) for a source within a job.

**Parameters:**
- `job_id` (required) — Job UUID
- `source_id` (required) — Source ID
- `annotation_type` (optional) — Filter: `note`, `highlight`, `summary`, `question`, `critique`

**Returns:** Annotation list with ID, type, content, page reference, created timestamp.

**Use case:** "What did the agent note about this document?" → see its highlights, summaries, and questions.

**Backend:** New endpoint `GET /api/jobs/{job_id}/sources/{source_id}/annotations?type={type}` — queries `source_annotations`

---

#### F7. `get_source_tags`

Get tags assigned to a source within a job.

**Parameters:**
- `job_id` (required) — Job UUID
- `source_id` (required) — Source ID

**Returns:** List of tag strings.

**Backend:** New endpoint `GET /api/jobs/{job_id}/sources/{source_id}/tags` — queries `source_tags`

---

#### F8. `get_citation_stats`

Get citation statistics for a job — counts by verification status, source type, confidence level.

**Parameters:**
- `job_id` (required) — Job UUID

**Returns:** Statistics: total sources, sources by type, total citations, citations by verification status, citations by confidence, citations by extraction method.

**Use case:** Quick health check — "Are most citations verified or still pending?"

**Backend:** New endpoint `GET /api/jobs/{job_id}/citations/stats` — aggregate queries on `citations` and `sources` filtered by job

---

## Tool Summary

| Category | Tools | Count | Permission |
|----------|-------|-------|------------|
| **A: Actions & Ops** | approve_job, resume_job_with_feedback, cancel_job, create_job, delete_job, assign_job | 6 | Confirmation required |
| **A: Actions & Ops** | test_datasource | 1 | Always allowed |
| **B: Git History** | list_job_commits, get_job_diff, get_job_file, list_job_files, list_job_tags | 5 | Always allowed |
| **C: Workspace** | get_frozen_job, get_workspace_file, get_workspace_overview, get_job_progress, get_job_requirements | 5 | Always allowed |
| **D: Monitoring** | get_job_stats, get_agent_stats, get_stuck_jobs, list_agents, list_experts, get_expert, list_datasources | 7 | Always allowed |
| **E: Database** | list_tables, query_table, get_table_schema | 3 | Always allowed |
| **F: Citations** | list_job_sources, get_source_detail, list_job_citations, get_citation_detail, search_job_sources, get_source_annotations, get_source_tags, get_citation_stats | 8 | Always allowed |
| **Existing** | list_jobs, get_job, get_audit_trail, get_chat_history, get_todos, get_graph_changes, get_llm_request, search_audit | 8 | Always allowed |
| **Total** | | **43** | **6 confirmation / 37 always allowed** |

---

## Permission Model

All 43 tools fall into two permission tiers based on whether they mutate state:

### Read Tools — Always Allowed (37 tools)

Categories B, C, D, E, F, and the 8 existing tools are **read-only**. They should be configured as **always allowed** in Claude Code's permission settings so the AI can freely query job state, git history, workspace files, citations, etc. without prompting the user.

This means the AI can autonomously gather context (e.g., read a frozen job, check diffs, review citations) before proposing any action.

### Mutation Tools — Require Confirmation (6 tools)

The 6 mutation tools in Category A (`approve_job`, `resume_job_with_feedback`, `cancel_job`, `create_job`, `delete_job`, `assign_job`) **must require explicit user confirmation** before execution. Claude Code's built-in permission system handles this — when the AI calls a mutation tool, the user is prompted to approve or deny.

`test_datasource` is grouped in Category A for organizational purposes but is read-only and should be always allowed.

### How It Works

1. AI freely calls read tools to gather context (no prompts)
2. AI proposes an action and calls a mutation tool (e.g., `approve_job`)
3. Claude Code's permission system prompts the user to approve/deny
4. Only on approval does the mutation execute

### Implementation

Tool descriptions must clearly communicate mutation semantics so Claude Code's permission system can distinguish them:

```python
# Read tool — no special annotation needed
@mcp.tool()
async def get_frozen_job(job_id: str) -> str:
    """Read the frozen job summary including confidence and deliverables."""

# Mutation tool — description must signal the mutation
@mcp.tool()
async def approve_job(job_id: str) -> str:
    """Approve a frozen job, marking it as completed.

    MUTATION: This marks the job as completed and writes job_completion.json.
    The job must be in 'pending_review' status.
    This action cannot be undone.
    """
```

Each mutation tool's description must include:
- **`MUTATION:` prefix** in the detailed description to signal it's not read-only
- What the **consequence** is (e.g., "marks the job as completed")
- What **state the job must be in** for the action to succeed
- Whether the action is **reversible**

This leverages Claude Code's built-in permission model — no custom confirmation UI needed.

---

## Frozen Job Review Workflow (Updated)

With the full tool set, reviewing and acting on a frozen job becomes a single conversation:

```
1. get_frozen_job(job_id)                          → read summary, confidence, deliverables, notes
2. list_job_tags(job_id)                             → see phase history
3. get_job_diff(job_id, base="phase_3_start")      → see what changed in the final phase
4. get_job_file(job_id, "workspace.md")            → read the agent's memory
5. get_job_file(job_id, "plan.md")                 → read the strategic plan
6. get_todos(job_id)                               → see completed vs pending work

   # Then either:
7a. approve_job(job_id)                            → accept the work
   # Or:
7b. resume_job_with_feedback(job_id,               → send back with guidance
      feedback="The methodology section needs...")
```

### System Health Check Workflow

```
1. get_job_stats()                                 → overview of queue
2. get_stuck_jobs()                                → any jobs stuck?
3. list_agents(status="ready")                     → any idle agents?
4. list_jobs(status="created")                     → any unassigned jobs?
5. assign_job(job_id, agent_id)                    → assign work to idle agent
```

### Citation Verification Workflow

```
1. get_citation_stats(job_id)                      → overview: 24 citations, 20 verified, 3 failed, 1 pending
2. list_job_citations(job_id,                      → see which citations failed
     verification_status="failed")
3. get_citation_detail(citation_id=17)             → see the claim, quote, source, and why it failed
4. get_source_detail(source_id=5)                  → read the actual source content
5. search_job_sources(job_id,                      → search for the evidence the agent claimed
     query="data retention period of 10 years")
6. get_source_annotations(job_id, source_id=5)     → see what the agent noted about this source

   # Decision:
7a. approve_job(job_id)                            → citations are acceptable
7b. resume_job_with_feedback(job_id,               → agent needs to fix citations
      feedback="Citations [17] and [22] reference
      content not found in the source. Please
      re-verify against the actual document text.")
```

### Job Creation Workflow

```
1. list_experts()                                  → see available agent configs
2. get_expert("researcher")                        → check config details
3. list_datasources(type="neo4j")                  → find relevant datasources
4. create_job(                                     → create the job
     description="...",
     config_name="researcher",
     datasource_ids=["ds-uuid-1"]
   )
5. list_agents(status="ready")                     → find an agent
6. assign_job(job_id, agent_id)                    → start it
```

---

## Out of Scope

The following cockpit features are intentionally excluded from the MCP:

| Feature | Reason |
|---------|--------|
| **File uploads** | Binary file handling doesn't fit the MCP text-tool model. Jobs needing documents should use the cockpit UI. |
| **Instruction builder** | The AI-assisted conversational builder is a cockpit-specific UX with SSE streaming. Claude Code itself serves the same role. |
| **Layout management** | Cockpit-only UI concern (panel arrangement, split views). |
| **Agent registration/heartbeat** | Infrastructure concern — agents self-register. No reason for the AI to do this. |
| **Bulk audit/chat endpoints** | These exist for cockpit IndexedDB caching optimization. The MCP's paginated tools cover the same data. |
| **Daily stats** | Low value for AI — the cockpit chart visualization is the point. `get_job_stats` and `get_stuck_jobs` cover operational needs. |

---

## Implementation Plan

### Phase 1: Action Tools (Highest Impact)

1. Add async methods to `AsyncCockpitClient` for approve, resume, cancel, create, delete, assign
2. Add MCP tool definitions with clear mutation descriptions
3. Test confirmation flow with Claude Code's permission system
4. Manual verification: trigger each action via Claude Code, confirm permission prompts work

**Files changed:** `orchestrator/mcp/server.py`, `orchestrator/mcp/client.py`

### Phase 2: Git History Tools

1. Add `get_commits()`, `compare()`, `get_tags()` to `GiteaClient`
2. Add orchestrator endpoints: `/repo/commits`, `/repo/diff`, `/repo/tags`
3. Update `/repo/file` to accept `ref` query parameter
4. Add MCP tools with formatters
5. Graceful degradation when Gitea unavailable

**Files changed:** `orchestrator/services/gitea.py`, `orchestrator/main.py`, `orchestrator/mcp/server.py`, `orchestrator/mcp/client.py`

### Phase 3: Workspace & Job Context Tools

1. Add async client methods for frozen job, workspace files, progress, requirements
2. Add MCP tools with formatters

**Files changed:** `orchestrator/mcp/server.py`, `orchestrator/mcp/client.py`

### Phase 4: Monitoring & Database Tools

1. Add async client methods for stats, agents, experts, datasources, tables
2. Add MCP tools with formatters

**Files changed:** `orchestrator/mcp/server.py`, `orchestrator/mcp/client.py`

### Phase 5a: Citation Read Tools (SQL only)

Simple SQL queries against existing tables — no CitationEngine dependency required.

1. Add citation API endpoints to `orchestrator/main.py`:
   - `GET /api/jobs/{job_id}/sources` — list sources via `job_sources` join
   - `GET /api/sources/{source_id}` — single source detail
   - `GET /api/jobs/{job_id}/citations` — list citations with filters
   - `GET /api/citations/{citation_id}` — single citation with source join
   - `GET /api/jobs/{job_id}/sources/{source_id}/annotations` — annotations
   - `GET /api/jobs/{job_id}/sources/{source_id}/tags` — tags
   - `GET /api/jobs/{job_id}/citations/stats` — aggregate statistics
2. Add async client methods to `AsyncCockpitClient`
3. Add MCP tools with formatters

**Files changed:** `orchestrator/main.py`, `orchestrator/mcp/server.py`, `orchestrator/mcp/client.py`

### Phase 5b: Citation Search Tool (CitationEngine)

The `search_job_sources` tool is separated because it has a different dependency profile.

**Decision:** Run CitationEngine as a dependency in the orchestrator. This gives full hybrid search (keyword + semantic + RRF fusion) and positions the orchestrator to later expose citation features in the cockpit UI as well.

1. Add `citation_engine` to `orchestrator/requirements.txt` (with `[postgresql,vector]` extras)
2. Add `GET /api/jobs/{job_id}/sources/search` endpoint
3. Instantiate `CitationEngine` in multi-agent mode with existing PostgreSQL connection
4. Handle CitationEngine import gracefully (fallback to keyword-only SQL if import fails)
5. Add MCP tool with formatter

**Files changed:** `orchestrator/requirements.txt`, `orchestrator/main.py`, `orchestrator/mcp/server.py`, `orchestrator/mcp/client.py`

### Testing Strategy

Each phase should include:
- **Unit tests** for new `AsyncCockpitClient` methods (mock HTTP responses)
- **Integration smoke test** via Claude Code: invoke each new tool against a real job and verify formatted output
- **Graceful degradation tests**: verify tools return clean error messages when backend/Gitea/CitationEngine is unavailable

### All Phases: Graceful Degradation

Every new tool must handle backend unavailability gracefully:
- Return clear error messages (not stack traces)
- Never crash the MCP server
- Gitea tools specifically: "Gitea is not available — git history tools require a running Gitea instance"
- Citation tools: "CitationEngine not available" or "No sources found for this job" (not stack traces)

---

## Resolved Decisions

| # | Question | Decision |
|---|----------|----------|
| Q1 | Does `job_frozen.json` contain a commit ref? | **No, but the agent creates a `job-frozen` git tag** after writing the file. Tools can use `base="job-frozen"` for diffs. No code change needed. |
| Q2 | Diff size limits? | **MCP tool exposes `max_chars` parameter** (default 50k). Truncation in the MCP formatter with `[truncated]` marker. |
| Q3 | Create job: `config_override` sufficient? | **Yes.** `config_override` as a catch-all JSON field is enough. Simple jobs just use `description` + `config_name`. |
| Q4 | Datasource CRUD via MCP? | **List + test only.** Datasource management stays in the cockpit UI. |
| Q5 | Search: CitationEngine vs raw SQL? | **Run CitationEngine in the orchestrator.** Full hybrid search. Also positions the orchestrator for future cockpit citation UI. |
| Q7 | Cross-job source visibility? | **Yes.** `list_job_sources` accepts optional `job_id` — omit it to query across all jobs. |

## Open Questions

1. **[Phase 5a] Source content in MCP responses:** Source content can be very large (full documents). The `get_source_detail` tool truncates by default (2000 chars), but should `search_job_sources` chunk results also be truncatable via a parameter? Current default is 500 chars per chunk in the engine's `to_dict()`.
