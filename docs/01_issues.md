# MCP Overhaul — Issues Found During Testing

Tested by debugging job `6298b72e` ("Refine documentation") — the multi-feedback-round job with 4 `job_complete` calls, 24 phases, 1614 audit entries.

## Issue 1: New MCP tools not exposed to Claude Code

**Severity**: Critical — defeats the purpose of the overhaul.

**Symptom**: Claude Code only sees the original 8 tools (`list_jobs`, `get_job`, `get_audit_trail`, `get_chat_history`, `get_todos`, `get_graph_changes`, `get_llm_request`, `search_audit`). The 35 new tools defined in `orchestrator/mcp/server.py` are not available.

**Evidence**: The `.mcp.json` points to `http://localhost:8055/mcp`. The MCP health endpoint at `http://localhost:8055/health` returns `{"status": "healthy", "backend": "connected"}`, so the server is running. But Claude Code's tool list does not include any of the new tools (no `approve_job`, `list_job_commits`, `get_frozen_job`, etc.).

**Likely cause**: The MCP server process was started before commit `5f621a4` and hasn't been restarted. FastMCP discovers tools at startup, so the running process still serves the old 8-tool schema. Alternatively, there could be a registration issue in the FastMCP 2.0 HTTP transport.

**Fix**: Restart the MCP server process so it picks up the new tool definitions. Then verify with `curl http://localhost:8055/mcp` or by checking Claude Code's tool list.

---

## Issue 2: WorkspaceService path calculation is wrong

**Severity**: High — breaks all local filesystem workspace tools.

**Symptom**: `GET /api/jobs/{id}/workspace/{path}` returns 404 for files that exist on disk. The `get_todos`, `get_workspace_file`, and `get_workspace_overview` MCP tools all report "No workspace found."

**File**: `orchestrator/services/workspace.py:33-35`

```python
# Project root: four levels up from this file
# (services/workspace.py -> services/ -> api/ -> cockpit/ -> project root)
project_root = Path(__file__).parent.parent.parent.parent
```

**Problem**: The comment references an old directory structure (`services/ -> api/ -> cockpit/ -> project root`). The actual path is:

```
orchestrator/services/workspace.py
         ^services/  ^orchestrator/  ^PROJECT_ROOT
```

That's 2 levels up, not 4. With `.parent.parent.parent.parent` (4 levels), the resolved path becomes `/home/ghost/Repositories/` instead of `/home/ghost/Repositories/Superhuman-Remote-Worker/`.

**Verification**: The workspace directory exists at `/home/ghost/Repositories/Superhuman-Remote-Worker/workspace/job_6298b72e-14c2-417b-8664-b766eb34355a/` and contains `workspace.md` (3273 bytes), `plan.md`, and 39 archive files.

**Fix**: Change line 35 to:
```python
project_root = Path(__file__).parent.parent.parent
```

Or better — use the `WORKSPACE_PATH` env var in production (the code already supports it at line 39).

**Affected tools**: `get_todos`, `get_workspace_file`, `get_workspace_overview`. The Gitea-backed tools (`get_job_file`, `list_job_files`, etc.) are unaffected since they read from Gitea, not the local filesystem.

---

## Issue 3: Job status mismatch — DB says `processing`, job is actually frozen

**Severity**: Medium — misleading status in `list_jobs` output.

**Symptom**: `list_jobs` and `get_job` report status `processing` for job `6298b72e`, but the job has a `job_frozen.json` in the Gitea repo and a `job-frozen` git tag. The frozen data shows 95% confidence and lists all deliverables.

**Evidence**:
- `GET /api/jobs/{id}` → `"status": "processing"`
- `GET /api/jobs/{id}/frozen` → returns full frozen data with confidence 0.95
- `GET /api/jobs/{id}/repo/tags` → includes `job-frozen` tag
- Job last updated at `2026-02-11T18:06:33` (>18 hours ago, clearly not actively processing)

**Likely cause**: The status transition from `processing` → `pending_review` may have failed during the freeze, or the agent wrote `job_frozen.json` to Gitea without updating the orchestrator's PostgreSQL status.

**Impact**: The `list_jobs(status="pending_review")` filter would miss this job entirely. The `get_stuck_jobs` tool would flag it as stuck (last update 18+ hours ago).

---

## Issue 4: `search_audit` returns minimal context

**Severity**: Low — functional but not very useful.

**Symptom**: Search results show step number and type but no content preview. For example, searching "feedback" returns entries like:

```
[725] tool

[729] tool

[733] tool
```

No tool name, no argument snippet, no result preview. The formatter in `server.py` (`_search_audit` → lines 1509-1528) does format content, but the entries coming back from the API may not include the expected fields (`tool.name`, `tool.arguments`, `result`).

**Likely cause**: The search function in the MCP server iterates over raw audit entries and checks for matches, but the formatted output depends on entry structure that may differ between the old audit API response format and what the formatter expects.

---

## Issue 5: `list_jobs` status enum doesn't match actual statuses

**Severity**: Low — cosmetic/usability.

**Symptom**: The `list_jobs` tool accepts `status: Literal["pending", "running", "completed", "failed"]` but the actual job statuses in the system are `created`, `processing`, `pending_review`, `completed`, `failed`, `cancelled`. There's no `pending` or `running` in the real status set.

**File**: `orchestrator/mcp/server.py:63`

```python
status: Literal["pending", "running", "completed", "failed"] | None = None,
```

**Fix**: Update the enum to match actual statuses:
```python
status: Literal["created", "processing", "pending_review", "completed", "failed", "cancelled"] | None = None,
```

---

## Issue 6: Job status never updates from `processing` — broken async DB update

**Severity**: Critical — every job stays `processing` forever, breaking the cockpit UI and `list_jobs` filters.

**Symptom**: When an agent calls `job_complete` and the job freezes (writes `job_frozen.json`), the PostgreSQL job status remains `processing` instead of transitioning to `pending_review`. Reproduced on jobs `6298b72e`, `251f6723`, `63fe5596`.

**Root cause (two layers)**:

### Layer 1: `finalize_job()` used broken async pattern (`src/core/phase.py`)

The original `finalize_job()` attempted to update the database using `ThreadPoolExecutor` + `asyncio.run()`:

```python
# BROKEN — creates a new event loop in a worker thread,
# but asyncpg pool is bound to the main event loop
with ThreadPoolExecutor() as pool:
    pool.submit(asyncio.run, postgres_db.jobs.update_status(job_id, status="pending_review"))
```

This fails silently because asyncpg connection pools are bound to the event loop they were created on. The `asyncio.run()` creates a new loop, and the cross-loop usage raises an exception caught by a blanket `except Exception` that only logs at debug level.

**Fix applied**: Removed the broken async code from `finalize_job()`. Made the `handle_transition` graph node `async def` so it can properly `await` the asyncpg call on the correct event loop. Added defense-in-depth DB update in `graph.py` after `handle_phase_transition()` returns with `should_stop=True`.

### Layer 2: `_process_orchestrator_job()` never updated job status (`src/api/app.py`)

After the graph finishes execution, `_process_orchestrator_job()` logged the result but never wrote the final status back to PostgreSQL. The `should_stop` and `goal_achieved` flags in the final graph state were ignored.

**Fix applied**: Added `_update_job_status_from_result()` function that maps the final state to DB status:
- `error` present → `failed`
- `should_stop=True` → `pending_review` (frozen for human review)
- Otherwise → leave as `processing` (only explicit approval sets `completed`)

Called after both normal completion and in the exception handler. Also fixed the `_resume_job()` function which had the same missing status update.

**Files modified**: `src/core/phase.py`, `src/graph.py`, `src/api/app.py`

---

## Issue 7: Context summarization hangs — `with_structured_output()` incompatible with LLM proxy

**Severity**: Critical — blocks every job at the first phase transition, causing indefinite hangs.

**Symptom**: After the initial strategic phase completes all todos, the graph moves to the `archive_phase` node for context compaction. The LLM summarization call hangs indefinitely. The agent keeps sending heartbeat as `working` but no progress is made. Reproduced on jobs `251f6723` (hung 70+ min) and `63fe5596` (hung 15+ min).

**Evidence** (from job logs):
```
09:54:40 - Archiving phase: Phase 1 – Research & Taxonomy Design (Strategic)
09:54:40 - Context compaction triggered: 33 messages, 26661 tokens
09:54:40 - Starting single-pass summarization (298 tokens)
[... no more graph events, only heartbeats ...]
10:25:13 - Heartbeat sent: status=working
```

**Root cause**: The `_single_pass_summarize()` method in `src/core/context.py:786-788` uses:

```python
structured_llm = llm.with_structured_output(ConversationSummary)
result = await structured_llm.ainvoke([HumanMessage(content=prompt)])
```

`with_structured_output()` (default method) sends the request with `response_format: { type: "json_schema", json_schema: {...} }`. This parameter is not properly handled by the LLM proxy at `localhost:8080`, causing the request to hang instead of returning or erroring.

Regular tool/function calling works fine through the same proxy (the strategic phase completed 16 successful LLM iterations with tool calls). Only the `response_format`-based structured output fails.

**Additional factor**: The `ReasoningChatOpenAI` wrapper sets a custom sync `httpx.Client` (`http_client`) for key ring integration, but does NOT set `http_async_client`. When `ainvoke()` is called, LangChain's `ChatOpenAI` uses a default async client that may not inherit the same timeout or connection settings.

**Impact**: No job can ever complete a phase transition. The graph hangs at `archive_phase` and the job stays `processing` forever.

**Proposed fix**: Change `with_structured_output()` to use `method="function_calling"` (same mechanism that works for tool calls), and add an `asyncio.wait_for()` timeout as a safety net:

```python
structured_llm = llm.with_structured_output(ConversationSummary, method="function_calling")
result = await asyncio.wait_for(
    structured_llm.ainvoke([HumanMessage(content=prompt)]),
    timeout=120,
)
```

**File**: `src/core/context.py:786-788`

---

## ~~Issue 8: Both phase LLMs routed through local proxy instead of respective providers~~ RESOLVED

**Status**: Resolved — phase-aware model routing and audit trail now correctly reflect per-phase LLM configuration.

**Fix applied** (`src/graph.py`, `src/llm/reasoning_chat.py`, `src/core/archiver.py`):
- Split single `model_kwargs` into `strategic_model_kwargs` / `tactical_model_kwargs`
- Resolve `phase_model` via `config.llm.get_phase_config(phase_str).model` inside `execute()`
- Audit trail (`audit_llm_call`, `archive`) now logs the phase-specific model name instead of `config.llm.model`
- Added `_agenerate()` override so async LLM calls (used by the graph) also capture reasoning
- Added `_extract_responses_api_reasoning()` for GPT-5.2-pro's Responses API content block format
- Added `_normalize_content()` in archiver to flatten list content blocks to clean strings

**Tests**: 18 new unit tests in `tests/test_responses_api.py`, no regressions in existing suite.

<details>
<summary>Original issue description</summary>

**Severity**: Medium — misconfiguration, works by accident but fragile.

**Symptom**: Both the strategic model (`gpt-5.2-pro`, intended for OpenAI API) and the tactical model (`openai/gpt-oss-120b`, intended for local llama.cpp) are configured with `base_url=http://localhost:8080/v1`.

**Evidence** (from job log):
```
Created OpenAI LLM: model=gpt-5.2-pro, base_url=http://localhost:8080/v1, keys=2 key(s)
Created OpenAI LLM: model=openai/gpt-oss-120b, base_url=http://localhost:8080/v1, keys=2 key(s)
```

**Problem**: The `base_url` is set globally (via `LLM_BASE_URL` env var or the base `llm.base_url` config field) and applies to all phase LLMs. Phase-specific overrides (`llm.strategic.base_url`, `llm.tactical.base_url`) exist in the config schema but are apparently not set, so both models inherit the same base URL.

This works only because the proxy at `:8080` happens to route `gpt-5.2-pro` to the OpenAI API and `openai/gpt-oss-120b` to the local llama.cpp server. But the proxy's `/v1/models` endpoint only lists the local model (`ggml-org/gpt-oss-120b-GGUF`), so `gpt-5.2-pro` is invisible and can't be verified directly.

**Risk**: If the proxy doesn't handle certain request parameters (like `response_format` for structured output — see Issue 7), requests silently hang instead of being forwarded correctly. The proxy becomes a single point of failure for all LLM calls.

</details>

---

## Testing Summary

### Tools verified working (via REST API fallback)

| Endpoint | Result |
|----------|--------|
| `GET /api/jobs/{id}/repo/tags` | 27 phase tags returned |
| `GET /api/jobs/{id}/repo/contents` | 9 root entries |
| `GET /api/jobs/{id}/repo/contents?path=output` | 12 output files |
| `GET /api/jobs/{id}/repo/file?path=workspace.md` | Full content (3233 bytes) |
| `GET /api/jobs/{id}/repo/file?path=plan.md` | Full content (8045 bytes) |
| `GET /api/jobs/{id}/frozen` | Frozen data with 95% confidence |
| `GET /api/jobs/{id}/progress` | Progress data returned |
| `GET /api/jobs/{id}/citations/stats` | 347 sources, 10 citations |
| `GET /api/health` | `{"status": "ok"}` |
| `GET http://localhost:8055/health` | `{"status": "healthy"}` |

### Tools verified broken

| Tool / Endpoint | Issue |
|----------------|-------|
| `GET /api/jobs/{id}/workspace/*` | 404 — path bug (Issue 2) |
| All 35 new MCP tools | Not exposed to Claude Code (Issue 1) |

### Job data recovered via working tools

- **workspace.md**: All deliverables marked COMPLETED, feedback items K1-K5, F1-F5, M1-M7 resolved
- **plan.md**: "ALLE 9 FEEDBACK-PUNKTE ERFOLGREICH ABGESCHLOSSEN"
- **Deliverables**: 11 German academic chapters + editorial report in `output/`
- **Citations**: 347 sources (111 web, 236 documents), 10 citations (4 verified, 6 failed)
- **Phases**: 24 phases (0-23), 4 `job_complete` attempts at audit steps 679, 711, 1143, 1609
