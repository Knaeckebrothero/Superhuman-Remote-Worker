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
