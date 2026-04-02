"""MCP server exposing debug cockpit tools.

Provides tools to inspect agent jobs, audit trails, todos, and graph changes
via the Model Context Protocol using FastMCP.
"""

from __future__ import annotations

import os
from typing import Any, Literal

from fastmcp import FastMCP
from starlette.responses import JSONResponse

try:
    from .client import AsyncCockpitClient
except ImportError:
    from client import AsyncCockpitClient  # type: ignore[no-redef]

try:
    from ..services import formatters as fmt
except ImportError:
    try:
        from services import formatters as fmt  # type: ignore[no-redef]
    except ImportError:
        import importlib

        fmt = importlib.import_module("orchestrator.services.formatters")  # type: ignore[assignment]

# Conditional auth: HTTP transport uses token verification, stdio skips it
_transport = os.environ.get("MCP_TRANSPORT", "http").lower()
_auth = None
if _transport == "http":
    try:
        from .auth import McpTokenVerifier
    except ImportError:
        from auth import McpTokenVerifier  # type: ignore[no-redef]

    _token_verifier = McpTokenVerifier()

    if os.environ.get("MCP_OAUTH_ENABLED", "").lower() == "true":
        try:
            from .oauth_bridge import SRWOAuthProxy
        except ImportError:
            from oauth_bridge import SRWOAuthProxy  # type: ignore[no-redef]

        _base_url = os.environ.get(
            "MCP_BASE_URL", "https://mcp.superhuman-remote-worker.com"
        )
        _auth = SRWOAuthProxy(
            config_url=os.environ.get(
                "MCP_OIDC_CONFIG_URL",
                "http://keycloak:8080/realms/srw/.well-known/openid-configuration",
            ),
            client_id=os.environ["MCP_OIDC_CLIENT_ID"],
            client_secret=os.environ["MCP_OIDC_CLIENT_SECRET"],
            base_url=_base_url,
            # issuer_url = base_url (the proxy IS the authorization server
            # from the client's perspective; Keycloak is only used internally
            # via config_url for OIDC discovery)
            mcp_verifier=_token_verifier,
            verify_id_token=True,  # Keycloak access tokens may be opaque
        )
    else:
        _auth = _token_verifier

# Create the MCP server instance
mcp = FastMCP("cockpit-debug", auth=_auth)

# Global client instance (initialized lazily)
_client: AsyncCockpitClient | None = None


def _get_client() -> AsyncCockpitClient:
    """Get or create the async client instance.

    When running with auth, injects scope headers from the authenticated token
    so the orchestrator can apply per-user filtering.
    """
    global _client
    if _client is None:
        _client = AsyncCockpitClient()

    # Inject scope headers from authenticated MCP token (if present)
    try:
        from mcp.server.auth.middleware.auth_context import get_access_token

        token = get_access_token()
        if token:
            _client.set_scope_headers(
                user_id=token.client_id,
                scope=token.scopes[0] if token.scopes else "user",
            )
        else:
            _client.clear_scope_headers()
    except Exception:
        _client.clear_scope_headers()

    return _client


# =============================================================================
# Health Check Endpoint
# =============================================================================


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request):
    """Kubernetes health probe endpoint."""
    try:
        client = _get_client()
        await client.health_check()
        return JSONResponse({"status": "healthy", "backend": "connected"})
    except Exception as e:
        return JSONResponse(
            {"status": "degraded", "error": str(e)},
            status_code=503,
        )


# =============================================================================
# MCP Tools
# =============================================================================


@mcp.tool
async def list_jobs(
    status: Literal[
        "created", "processing", "completed", "failed", "cancelled", "pending_review"
    ]
    | None = None,
    limit: int = 20,
) -> str:
    """List agent jobs with optional status filter.

    Returns job ID, status, config name, timestamps, and audit entry count.
    Use this to find jobs to investigate.

    Args:
        status: Filter by status (created, processing, completed, failed, cancelled, pending_review)
        limit: Maximum jobs to return (1-100, default 20)

    Returns:
        Formatted list of jobs with ID, status, config, timestamps
    """
    if limit < 1:
        limit = 1
    elif limit > 100:
        limit = 100

    client = _get_client()
    jobs = await client.list_jobs(status=status, limit=limit)
    return fmt.format_jobs(jobs)


@mcp.tool
async def get_job(job_id: str) -> str:
    """Get detailed information about a specific job by ID.

    Returns full job details including description, config, status,
    timestamps, and audit count.

    Args:
        job_id: Job UUID to retrieve

    Returns:
        Formatted job details
    """
    client = _get_client()
    job = await client.get_job(job_id)
    return fmt.format_job_detail(job)


@mcp.tool
async def get_audit_trail(
    job_id: str,
    page: int = 1,
    page_size: int = 20,
    filter: Literal["all", "messages", "tools", "errors"] = "all",
) -> str:
    """Get paginated audit entries for a job's execution.

    Shows LLM messages, tool calls, and errors.
    Use filter to narrow results. Page -1 returns the last page.

    Args:
        job_id: Job UUID to get audit for
        page: Page number (1-indexed, -1 for last page)
        page_size: Entries per page (max 200, default 20)
        filter: Filter category (all, messages, tools, errors)

    Returns:
        Formatted audit trail entries
    """
    if page_size < 1:
        page_size = 1
    elif page_size > 200:
        page_size = 200

    client = _get_client()
    audit = await client.get_audit_trail(
        job_id=job_id,
        page=page,
        page_size=page_size,
        filter_category=filter,
    )
    return fmt.format_audit(audit)


@mcp.tool
async def get_audit_bulk(
    job_id: str,
    offset: int = 0,
    limit: int = 500,
    filter: Literal["all", "messages", "tools", "errors"] = "all",
) -> str:
    """Get bulk audit entries using offset/limit pagination.

    Better than page-based audit trail for scanning large histories.
    Supports up to 500 entries per request.

    Args:
        job_id: Job UUID to get audit for
        offset: Number of entries to skip (default: 0)
        limit: Maximum entries to return (max 500, default 500)
        filter: Filter category (all, messages, tools, errors)

    Returns:
        Formatted audit entries with offset metadata
    """
    if limit < 1:
        limit = 1
    elif limit > 500:
        limit = 500

    client = _get_client()
    data = await client.get_audit_bulk(
        job_id=job_id,
        offset=offset,
        limit=limit,
    )
    return fmt.format_audit_bulk(data)


@mcp.tool
async def get_chat_history(
    job_id: str,
    page: int = 1,
    page_size: int = 20,
) -> str:
    """Get paginated chat history for a job showing conversation turns.

    Returns clean sequential view of input/response pairs without duplicates.
    Use this to understand the agent's reasoning flow.

    Args:
        job_id: Job UUID to get chat history for
        page: Page number (1-indexed, -1 for last page)
        page_size: Entries per page (max 200, default 20)

    Returns:
        Formatted chat history
    """
    if page_size < 1:
        page_size = 1
    elif page_size > 200:
        page_size = 200

    client = _get_client()
    chat = await client.get_chat_history(
        job_id=job_id,
        page=page,
        page_size=page_size,
    )
    return fmt.format_chat_history(chat)


@mcp.tool
async def get_chat_bulk(
    job_id: str,
    offset: int = 0,
    limit: int = 500,
) -> str:
    """Get bulk chat history using offset/limit pagination.

    Better than page-based chat history for scanning large conversations.
    Supports up to 500 entries per request.

    Args:
        job_id: Job UUID to get chat history for
        offset: Number of entries to skip (default: 0)
        limit: Maximum entries to return (max 500, default 500)

    Returns:
        Formatted chat turns with offset metadata
    """
    if limit < 1:
        limit = 1
    elif limit > 500:
        limit = 500

    client = _get_client()
    data = await client.get_chat_bulk(
        job_id=job_id,
        offset=offset,
        limit=limit,
    )
    return fmt.format_chat_bulk(data)


@mcp.tool
async def get_todos(job_id: str) -> str:
    """Get all todos for a job including current active todos and archives.

    Shows task planning and execution progress across phases.

    Args:
        job_id: Job UUID to get todos for

    Returns:
        Formatted todos with current and archived phases
    """
    client = _get_client()
    todos = await client.get_todos(job_id)
    return fmt.format_todos(todos)


@mcp.tool
async def get_graph_changes(job_id: str) -> str:
    """Get timeline of Neo4j graph mutations for a job.

    Returns parsed Cypher queries showing nodes/relationships
    created, modified, and deleted. Includes summary statistics.

    Args:
        job_id: Job UUID to get graph changes for

    Returns:
        Formatted graph changes timeline with summary
    """
    client = _get_client()
    changes = await client.get_graph_changes(job_id)
    return fmt.format_graph_changes(changes)


@mcp.tool
async def get_llm_request(doc_id: str) -> str:
    """Get full LLM request/response by MongoDB document ID.

    Returns complete message history, model response, and token usage.
    Use document IDs from audit trail entries.

    Args:
        doc_id: MongoDB ObjectId (24 hex characters)

    Returns:
        Formatted LLM request with messages and response
    """
    client = _get_client()
    request = await client.get_llm_request(doc_id)
    return fmt.format_llm_request(request)


@mcp.tool
async def get_job_summary(job_id: str) -> str:
    """Get a comprehensive one-shot summary of a job.

    Fetches status, progress, todos, workspace overview, and recent tool
    calls in parallel. Returns everything in a single response — ideal for
    understanding a job's current state without multiple tool calls.

    Args:
        job_id: Job UUID to summarize

    Returns:
        Combined summary with status, progress, todos, workspace, and recent activity
    """
    import asyncio

    client = _get_client()

    results = await asyncio.gather(
        client.get_job(job_id),
        client.get_job_progress(job_id),
        client.get_todos(job_id),
        client.get_workspace_overview(job_id),
        client.get_audit_trail(job_id, page=-1, page_size=10, filter_category="tools"),
        return_exceptions=True,
    )

    return fmt.format_job_summary(*results)


@mcp.tool
async def search_audit(
    job_id: str,
    query: str,
    limit: int = 20,
) -> str:
    """Search audit entries by content pattern.

    Searches across message content, tool names, and arguments.
    Returns matching entries with context.

    Args:
        job_id: Job UUID to search within
        query: Search string (case-insensitive substring match)
        limit: Maximum results to return (1-100, default 20)

    Returns:
        Formatted search results
    """
    if limit < 1:
        limit = 1
    elif limit > 100:
        limit = 100

    client = _get_client()
    return await _search_audit(client, job_id=job_id, query=query, limit=limit)


# =============================================================================
# Action & Operations Tools (Category A)
# =============================================================================


@mcp.tool
async def approve_job(job_id: str) -> str:
    """Approve a frozen job, marking it as completed.

    MUTATION: This marks the job as completed, writes job_completion.json,
    and deletes job_frozen.json. The job must be in 'pending_review' status.
    This action cannot be undone.

    Args:
        job_id: Job UUID to approve

    Returns:
        Approval result with completion details
    """
    client = _get_client()
    try:
        result = await client.approve_job(job_id)
        return fmt.format_action_result("approve", job_id, result)
    except Exception as e:
        return fmt.format_action_error("approve", job_id, e)


@mcp.tool
async def resume_job_with_feedback(
    job_id: str,
    feedback: str | None = None,
) -> str:
    """Resume a frozen/failed job from its checkpoint, optionally injecting feedback.

    MUTATION: This resumes agent execution on the job. If feedback is provided,
    it is injected into the agent's context before re-execution. The job can be
    in any status except 'completed'. If the originally assigned agent is
    unavailable, the orchestrator auto-selects a ready agent.

    Args:
        job_id: Job UUID to resume
        feedback: Natural language feedback to inject into the agent's context

    Returns:
        Resume result with status
    """
    client = _get_client()
    try:
        result = await client.resume_job(job_id, feedback=feedback)
        return fmt.format_action_result("resume", job_id, result, feedback=feedback)
    except Exception as e:
        return fmt.format_action_error("resume", job_id, e)


@mcp.tool
async def cancel_job(job_id: str) -> str:
    """Cancel a running job.

    MUTATION: This cancels the job and sends a cancel signal to the agent pod
    if one is assigned. The job must not already be completed or cancelled.
    In-progress work may be lost.

    Args:
        job_id: Job UUID to cancel

    Returns:
        Cancellation result
    """
    client = _get_client()
    try:
        result = await client.cancel_job(job_id)
        return fmt.format_action_result("cancel", job_id, result)
    except Exception as e:
        return fmt.format_action_error("cancel", job_id, e)


@mcp.tool
async def pause_job(job_id: str) -> str:
    """Pause a running job.

    MUTATION: This sends a graceful pause request to the agent. The agent
    finishes its current node, saves a checkpoint, and becomes available
    for other work. The job must be in 'processing' status.

    Args:
        job_id: Job UUID to pause

    Returns:
        Pause result with status
    """
    client = _get_client()
    try:
        result = await client.pause_job(job_id)
        return fmt.format_action_result("pause", job_id, result)
    except Exception as e:
        return fmt.format_action_error("pause", job_id, e)


@mcp.tool
async def create_job(
    description: str,
    config_name: str = "default",
    datasource_ids: list[str] | None = None,
    instructions: str | None = None,
    config_override: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
) -> str:
    """Create a new job for agent execution.

    MUTATION: This creates a job record and a Gitea repository. The job starts
    in 'created' status and must be assigned to an agent to begin processing.
    Jobs requiring input documents should use the cockpit UI instead.

    Args:
        description: Natural language task description
        config_name: Expert/agent config to use (default: "default")
        datasource_ids: List of global datasource UUIDs to clone as job-scoped
        instructions: Additional inline markdown instructions
        config_override: Per-job config overrides as JSON
        context: Additional context dictionary

    Returns:
        Created job details with ID
    """
    client = _get_client()
    try:
        result = await client.create_job(
            description=description,
            config_name=config_name,
            datasource_ids=datasource_ids,
            instructions=instructions,
            config_override=config_override,
            context=context,
        )
        return fmt.format_created_job(result, config_name)
    except Exception as e:
        return fmt.format_action_error("create", "N/A", e)


@mcp.tool
async def delete_job(job_id: str) -> str:
    """Delete a job and its associated data.

    MUTATION: This permanently deletes the job record and its requirements.
    Any job can be deleted regardless of status. WARNING: Deleting a job in
    'processing' status may leave an orphaned agent. This action is irreversible.

    Args:
        job_id: Job UUID to delete

    Returns:
        Deletion result
    """
    client = _get_client()
    try:
        result = await client.delete_job(job_id)
        return fmt.format_action_result("delete", job_id, result)
    except Exception as e:
        return fmt.format_action_error("delete", job_id, e)


@mcp.tool
async def assign_job(job_id: str, agent_id: str) -> str:
    """Assign a created job to a ready agent.

    MUTATION: This sends a JobStartRequest to the agent pod and updates the
    job status to 'processing'. The job must be in 'created' or 'failed' status,
    and the agent must be in 'ready' status.

    Args:
        job_id: Job UUID to assign
        agent_id: Agent UUID to assign to

    Returns:
        Assignment result
    """
    client = _get_client()
    try:
        result = await client.assign_job(job_id, agent_id)
        return fmt.format_action_result("assign", job_id, result, agent_id=agent_id)
    except Exception as e:
        return fmt.format_action_error("assign", job_id, e)


@mcp.tool
async def test_datasource(datasource_id: str) -> str:
    """Test connectivity to a datasource.

    Attempts to connect to the datasource using stored connection details.
    Supports PostgreSQL, Neo4j, and MongoDB. Does not modify any data.

    Args:
        datasource_id: Datasource UUID to test

    Returns:
        Test result with status and connection details
    """
    client = _get_client()
    try:
        result = await client.test_datasource(datasource_id)
        return fmt.format_datasource_test(datasource_id, result)
    except Exception as e:
        return fmt.format_action_error("test_datasource", datasource_id, e)


# =============================================================================
# Git History Tools (Category B)
# =============================================================================


@mcp.tool
async def list_job_commits(
    job_id: str,
    ref: str = "main",
    since_ref: str | None = None,
    limit: int = 20,
    page: int = 1,
) -> str:
    """List git commits for a job's repository.

    Shows the agent's work history as git commits. Use since_ref to see only
    commits after a specific phase tag (e.g., "phase_2_end").

    Args:
        job_id: Job UUID
        ref: Branch or tag to list from (default: main)
        since_ref: Only show commits after this ref (e.g., "phase_2_end")
        limit: Max commits to return (default: 20)
        page: Page number for pagination (default: 1)

    Returns:
        List of commits with hash, message, author, and timestamp
    """
    if limit < 1:
        limit = 1
    elif limit > 100:
        limit = 100

    client = _get_client()
    try:
        result = await client.list_job_commits(
            job_id, sha=ref, since_ref=since_ref, page=page, limit=limit
        )
        return fmt.format_commits(job_id, result, ref=ref, since_ref=since_ref)
    except Exception as e:
        return fmt.format_git_error("list commits", job_id, e)


@mcp.tool
async def get_job_diff(
    job_id: str,
    base: str,
    head: str = "HEAD",
    file_path: str | None = None,
    max_chars: int = 50000,
) -> str:
    """Show the diff between two git refs in a job's repository.

    Use base="job-frozen" to see changes since the last freeze, or base="phase_2_end"
    to see what changed in phase 3.

    Args:
        job_id: Job UUID
        base: Base ref (commit SHA, tag, or branch)
        head: Head ref (default: HEAD)
        file_path: Filter diff to a specific file (optional)
        max_chars: Truncate diff beyond this limit (default: 50000, 0 for unlimited)

    Returns:
        Unified diff output, truncated if exceeding max_chars
    """
    client = _get_client()
    try:
        result = await client.get_job_diff(job_id, base=base, head=head)
        diff_text = result.get("diff", "")

        # Filter to specific file if requested
        if file_path and diff_text:
            diff_text = fmt.filter_diff_by_file(diff_text, file_path)

        return fmt.format_diff(job_id, base, head, diff_text, max_chars=max_chars)
    except Exception as e:
        return fmt.format_git_error("get diff", job_id, e)


@mcp.tool
async def get_job_file(
    job_id: str,
    file_path: str,
    ref: str | None = None,
) -> str:
    """Read a specific file from the job's Gitea repo at any ref.

    View files at different points in time using refs (branch, tag, or commit SHA).
    For example, ref="phase_2_end" shows the file at the end of phase 2.

    Args:
        job_id: Job UUID
        file_path: Path within the repo (e.g., "workspace.md", "output/report.md")
        ref: Branch, tag, or commit SHA (default: HEAD)

    Returns:
        File content as text
    """
    client = _get_client()
    try:
        result = await client.get_job_file(job_id, path=file_path, ref=ref)
        content = result.get("content", "")
        ref_label = ref or "HEAD"
        size = result.get("size", len(content))
        header = f"File: {file_path} (ref: {ref_label}, {size} bytes)\n"
        return header + "---\n" + content
    except Exception as e:
        return fmt.format_git_error(f"read file '{file_path}'", job_id, e)


@mcp.tool
async def list_job_files(
    job_id: str,
    path: str = "",
    ref: str | None = None,
) -> str:
    """Browse the repository directory tree at any ref.

    Lists files and directories at a given path. Use ref to browse
    at a specific point in history.

    Args:
        job_id: Job UUID
        path: Directory path (default: root)
        ref: Branch, tag, or commit SHA (default: HEAD)

    Returns:
        Directory listing with file names, types, and sizes
    """
    client = _get_client()
    try:
        entries = await client.list_job_files(job_id, path=path, ref=ref)
        return fmt.format_file_listing(job_id, path, entries, ref=ref)
    except Exception as e:
        return fmt.format_git_error(f"list files at '{path or '/'}'", job_id, e)


@mcp.tool
async def list_job_tags(job_id: str) -> str:
    """List phase tags to understand the job's phase history.

    Shows tags like phase_1_start, phase_1_end, phase_2_start, etc.
    Use these tag names as refs in other git tools.

    Args:
        job_id: Job UUID

    Returns:
        List of tags with name and commit SHA, sorted chronologically
    """
    client = _get_client()
    try:
        tags = await client.list_job_tags(job_id)
        return fmt.format_tags(job_id, tags)
    except Exception as e:
        return fmt.format_git_error("list tags", job_id, e)


# =============================================================================
# Workspace & Job Context Tools (Category C)
# =============================================================================


@mcp.tool
async def get_frozen_job(job_id: str) -> str:
    """Get the frozen job review data including summary, confidence, and deliverables.

    Returns the agent's self-assessment when it froze the job for review.
    The job must be in 'pending_review' status (or the frozen data must still exist).

    Args:
        job_id: Job UUID

    Returns:
        Frozen job summary with confidence score, deliverables, and agent notes
    """
    client = _get_client()
    try:
        data = await client.get_frozen_job(job_id)
        return fmt.format_frozen_job(job_id, data)
    except Exception as e:
        return fmt.format_workspace_error("get frozen job data", job_id, e)


@mcp.tool
async def get_workspace_file(job_id: str, path: str) -> str:
    """Read any file from the job's local workspace filesystem.

    Unlike get_job_file (which reads from Gitea at any ref), this reads the
    current local file. Useful when Gitea is unavailable or for real-time state.

    Args:
        job_id: Job UUID
        path: Relative path within the workspace (e.g., "workspace.md", "plan.md",
              "todos.yaml", "archive/phase_1_retrospective.md")

    Returns:
        File content as text
    """
    client = _get_client()
    try:
        result = await client.get_workspace_file(job_id, path)
        content = result.get("content", "")
        return f"Workspace file: {path} (job {job_id})\n---\n{content}"
    except Exception as e:
        return fmt.format_workspace_error(f"read workspace file '{path}'", job_id, e)


@mcp.tool
async def get_workspace_overview(job_id: str) -> str:
    """Get a summary of the workspace state.

    Returns file listing, truncated workspace.md/plan.md previews,
    current todo counts, and archive count.

    Args:
        job_id: Job UUID

    Returns:
        Workspace overview with file list, content previews, and statistics
    """
    client = _get_client()
    try:
        data = await client.get_workspace_overview(job_id)
        return fmt.format_workspace_overview(job_id, data)
    except Exception as e:
        return fmt.format_workspace_error("get workspace overview", job_id, e)


@mcp.tool
async def get_job_progress(job_id: str) -> str:
    """Get detailed job progress including phase information and ETA.

    Shows current status, requirement completion stats, elapsed time,
    and estimated time remaining.

    Args:
        job_id: Job UUID

    Returns:
        Progress data with phase info and completion statistics
    """
    client = _get_client()
    try:
        data = await client.get_job_progress(job_id)
        return fmt.format_job_progress(job_id, data)
    except Exception as e:
        return fmt.format_workspace_error("get job progress", job_id, e)


# =============================================================================
# System Monitoring Tools (Category D)
# =============================================================================


@mcp.tool
async def get_job_stats() -> str:
    """Get job queue statistics with counts by status.

    Returns:
        Total jobs and counts per status (created, processing, completed, etc.)
    """
    client = _get_client()
    try:
        data = await client.get_job_stats()
        return fmt.format_job_stats(data)
    except Exception as e:
        return fmt.format_monitoring_error("get job stats", e)


@mcp.tool
async def get_agent_stats() -> str:
    """Get agent workforce summary with counts by status.

    Returns:
        Total agents and counts per status (ready, working, offline, etc.)
    """
    client = _get_client()
    try:
        data = await client.get_agent_stats()
        return fmt.format_agent_stats(data)
    except Exception as e:
        return fmt.format_monitoring_error("get agent stats", e)


@mcp.tool
async def get_stuck_jobs(threshold_minutes: int = 30) -> str:
    """Get jobs stuck in processing beyond a threshold.

    A job is considered stuck if it's in 'processing' status but hasn't
    been updated within the threshold period.

    Args:
        threshold_minutes: Minutes after which a job is considered stuck (default: 30)

    Returns:
        List of stuck jobs with details and last update time
    """
    if threshold_minutes < 1:
        threshold_minutes = 1
    elif threshold_minutes > 1440:
        threshold_minutes = 1440

    client = _get_client()
    try:
        data = await client.get_stuck_jobs(threshold_minutes)
        return fmt.format_stuck_jobs(data, threshold_minutes)
    except Exception as e:
        return fmt.format_monitoring_error("get stuck jobs", e)


@mcp.tool
async def list_agents(status: str | None = None) -> str:
    """List registered agents with status and current assignment.

    Args:
        status: Filter by status (booting, ready, working, completed, failed, offline)

    Returns:
        Agent list with ID, config, hostname, status, current job, last heartbeat
    """
    client = _get_client()
    try:
        agents = await client.list_agents(status=status)
        return fmt.format_agents(agents, status_filter=status)
    except Exception as e:
        return fmt.format_monitoring_error("list agents", e)


@mcp.tool
async def list_experts() -> str:
    """List available expert/agent configurations.

    Returns:
        Expert configs with ID, display name, description, and tags
    """
    client = _get_client()
    try:
        experts = await client.list_experts()
        return fmt.format_experts(experts)
    except Exception as e:
        return fmt.format_monitoring_error("list experts", e)


@mcp.tool
async def get_expert(expert_id: str) -> str:
    """Get full detail for an expert config including merged config and instructions.

    Args:
        expert_id: Expert config ID (e.g., "default", "researcher")

    Returns:
        Full config detail with system prompt, tool list, and instructions
    """
    client = _get_client()
    try:
        data = await client.get_expert(expert_id)
        return fmt.format_expert_detail(expert_id, data)
    except Exception as e:
        return fmt.format_monitoring_error(f"get expert '{expert_id}'", e)


@mcp.tool
async def list_datasources(ds_type: str | None = None) -> str:
    """List configured datasources.

    Args:
        ds_type: Filter by type (postgresql, neo4j, mongodb)

    Returns:
        Datasource list with ID, name, type, connection info, and scope
    """
    client = _get_client()
    try:
        datasources = await client.list_datasources(ds_type=ds_type)
        return fmt.format_datasources(datasources, type_filter=ds_type)
    except Exception as e:
        return fmt.format_monitoring_error("list datasources", e)


# =============================================================================
# Database Inspection Tools (Category E)
# =============================================================================


@mcp.tool
async def list_tables() -> str:
    """List all database tables with row counts.

    Returns:
        Table names with row counts
    """
    client = _get_client()
    try:
        tables = await client.list_tables()
        return fmt.format_tables(tables)
    except Exception as e:
        return fmt.format_monitoring_error("list tables", e)


@mcp.tool
async def query_table(
    table_name: str,
    limit: int = 50,
    offset: int = 0,
) -> str:
    """Get paginated data from a database table.

    Args:
        table_name: Table name (e.g., jobs, requirements, citations)
        limit: Rows per page (default: 50, max: 500)
        offset: Pagination offset (default: 0)

    Returns:
        Row data with column names
    """
    if limit < 1:
        limit = 1
    elif limit > 500:
        limit = 500

    # Convert offset to page number (1-indexed)
    page = (offset // limit) + 1

    client = _get_client()
    try:
        data = await client.get_table_data(table_name, page=page, page_size=limit)
        return fmt.format_table_data(table_name, data)
    except Exception as e:
        return fmt.format_monitoring_error(f"query table '{table_name}'", e)


@mcp.tool
async def get_table_schema(table_name: str) -> str:
    """Get column definitions for a database table.

    Args:
        table_name: Table name

    Returns:
        Column names, types, nullable flags, and defaults
    """
    client = _get_client()
    try:
        columns = await client.get_table_schema(table_name)
        return fmt.format_table_schema(table_name, columns)
    except Exception as e:
        return fmt.format_monitoring_error(f"get schema for '{table_name}'", e)


# =============================================================================
# Citation & Source Library Tools (Category F)
# =============================================================================


@mcp.tool
async def list_job_sources(
    job_id: str | None = None,
    source_type: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> str:
    """List sources registered by a job (documents, websites, databases, custom artifacts).

    When job_id is omitted, returns sources across all jobs with their linked job IDs.

    Args:
        job_id: Job UUID (omit to query across all jobs)
        source_type: Filter by type (document, website, database, custom)
        limit: Max results (default: 50)
        offset: Pagination offset (default: 0)

    Returns:
        Source list with ID, type, name, identifier, and content preview
    """
    if limit < 1:
        limit = 1
    elif limit > 500:
        limit = 500

    client = _get_client()
    try:
        data = await client.list_job_sources(
            job_id=job_id, source_type=source_type, limit=limit, offset=offset
        )
        return fmt.format_sources(data, job_id=job_id, type_filter=source_type)
    except Exception as e:
        return fmt.format_citation_error("list sources", e, job_id=job_id)


@mcp.tool
async def get_source_detail(
    source_id: int,
    content_limit: int = 2000,
) -> str:
    """Get full detail for a single source including content, metadata, and content hash.

    Args:
        source_id: Source ID (integer)
        content_limit: Max characters of content to return (default: 2000, 0 for full)

    Returns:
        Source record with type, identifier, name, content, and metadata
    """
    client = _get_client()
    try:
        data = await client.get_source_detail(source_id, content_limit=content_limit)
        return fmt.format_source_detail(data)
    except Exception as e:
        return fmt.format_citation_error(f"get source {source_id}", e)


@mcp.tool
async def list_job_citations(
    job_id: str,
    source_id: int | None = None,
    verification_status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> str:
    """List all citations for a job with verification status.

    Args:
        job_id: Job UUID
        source_id: Filter by source ID
        verification_status: Filter by status (pending, verified, failed, unverified)
        limit: Max results (default: 50)
        offset: Pagination offset (default: 0)

    Returns:
        Citation list with claim, source, verification status, and confidence
    """
    if limit < 1:
        limit = 1
    elif limit > 500:
        limit = 500

    client = _get_client()
    try:
        data = await client.list_job_citations(
            job_id,
            source_id=source_id,
            verification_status=verification_status,
            limit=limit,
            offset=offset,
        )
        return fmt.format_citations(job_id, data, status_filter=verification_status)
    except Exception as e:
        return fmt.format_citation_error("list citations", e, job_id=job_id)


@mcp.tool
async def get_citation_detail(citation_id: int) -> str:
    """Get full citation record with source info, verification details, and locator.

    Args:
        citation_id: Citation ID (integer)

    Returns:
        Full citation with claim, quote, source, locator, verification, and reasoning
    """
    client = _get_client()
    try:
        data = await client.get_citation_detail(citation_id)
        return fmt.format_citation_detail(data)
    except Exception as e:
        return fmt.format_citation_error(f"get citation {citation_id}", e)


@mcp.tool
async def search_job_sources(
    job_id: str,
    query: str,
    mode: str = "keyword",
    source_type: str | None = None,
    tags: str | None = None,
    top_k: int = 10,
) -> str:
    """Search a job's source library using keyword search with evidence labels.

    Uses PostgreSQL full-text search to find matching source content.

    Args:
        job_id: Job UUID
        query: Natural language query or keywords
        mode: Search mode (currently only "keyword" supported)
        source_type: Filter by source type
        tags: Comma-separated tags (AND logic)
        top_k: Max results (default: 10)

    Returns:
        Search results with evidence labels (HIGH/MEDIUM/LOW) and snippets
    """
    if top_k < 1:
        top_k = 1
    elif top_k > 50:
        top_k = 50

    client = _get_client()
    try:
        data = await client.search_job_sources(
            job_id,
            query=query,
            mode=mode,
            source_type=source_type,
            tags=tags,
            top_k=top_k,
        )
        return fmt.format_source_search(data)
    except Exception as e:
        return fmt.format_citation_error("search sources", e, job_id=job_id)


@mcp.tool
async def get_source_annotations(
    job_id: str,
    source_id: int,
    annotation_type: str | None = None,
) -> str:
    """Get annotations (notes, highlights, summaries, questions, critiques) for a source.

    Args:
        job_id: Job UUID
        source_id: Source ID
        annotation_type: Filter by type (note, highlight, summary, question, critique)

    Returns:
        List of annotations with type, content, and page reference
    """
    client = _get_client()
    try:
        annotations = await client.get_source_annotations(
            job_id,
            source_id,
            annotation_type=annotation_type,
        )
        return fmt.format_annotations(
            job_id, source_id, annotations, type_filter=annotation_type
        )
    except Exception as e:
        return fmt.format_citation_error(
            f"get annotations for source {source_id}", e, job_id=job_id
        )


@mcp.tool
async def get_source_tags(job_id: str, source_id: int) -> str:
    """Get tags assigned to a source within a job.

    Args:
        job_id: Job UUID
        source_id: Source ID

    Returns:
        List of tag strings
    """
    client = _get_client()
    try:
        tags = await client.get_source_tags(job_id, source_id)
        return fmt.format_source_tags(job_id, source_id, tags)
    except Exception as e:
        return fmt.format_citation_error(
            f"get tags for source {source_id}", e, job_id=job_id
        )


@mcp.tool
async def get_citation_stats(job_id: str) -> str:
    """Get citation statistics for a job — counts by verification status, source type, confidence.

    Args:
        job_id: Job UUID

    Returns:
        Statistics overview with source and citation breakdowns
    """
    client = _get_client()
    try:
        data = await client.get_citation_stats(job_id)
        return fmt.format_citation_stats(job_id, data)
    except Exception as e:
        return fmt.format_citation_error("get citation stats", e, job_id=job_id)


@mcp.tool
async def get_memory_stats(job_id: str) -> str:
    """Get memory statistics for a job — counts by type, source channel, tokens, and access patterns.

    Args:
        job_id: Job UUID

    Returns:
        Formatted memory statistics overview
    """
    client = _get_client()
    try:
        data = await client.get_memory_stats(job_id)
        return fmt.format_memory_stats(job_id, data)
    except Exception as e:
        return fmt.format_citation_error("get memory stats", e, job_id=job_id)


@mcp.tool
async def get_agent_system_info(agent_id: str) -> str:
    """Get system information from an agent's container.

    Returns CPU, memory, disk usage, listening ports, top processes by memory,
    established network connections, and current agent state. Useful for
    monitoring what the agent is doing inside its container.

    Args:
        agent_id: Agent UUID

    Returns:
        Formatted system info with resource usage and process details
    """
    client = _get_client()
    try:
        data = await client.get_agent_system_info(agent_id)
        return fmt.format_system_info(agent_id, data)
    except Exception as e:
        error_msg = str(e)
        if hasattr(e, "response"):
            try:
                detail = e.response.json().get("detail", error_msg)  # type: ignore[union-attr]
                error_msg = detail
            except Exception:
                error_msg = f"HTTP {e.response.status_code}: {error_msg}"  # type: ignore[union-attr]
        return f"Failed to get system info for agent {agent_id}:\n{error_msg}"


@mcp.tool
async def get_daily_stats(days: int = 7) -> str:
    """Get daily job statistics for the past N days.

    Shows jobs created, completed, failed, and cancelled per day.
    Useful for tracking system activity trends.

    Args:
        days: Number of days to look back (1-90, default 7)

    Returns:
        Daily statistics table
    """
    client = _get_client()
    data = await client.get_daily_stats(days=days)
    return fmt.format_daily_stats(data, days)


@mcp.tool
async def reload_experts() -> str:
    """Force reload of expert configurations from disk.

    Use after modifying expert YAML files to pick up changes
    without restarting the orchestrator.

    Returns:
        Reload confirmation with expert count
    """
    client = _get_client()
    result = await client.reload_experts()
    count = result.get("count", 0)
    return f"Expert configurations reloaded ({count} experts loaded)."


@mcp.tool
async def deregister_agent(agent_id: str) -> str:
    """Remove an agent from the system.

    Use for cleaning up agents that are offline or no longer needed.
    Returns 404 if the agent doesn't exist.

    Args:
        agent_id: Agent UUID to deregister

    Returns:
        Deregistration confirmation
    """
    client = _get_client()
    result = await client.deregister_agent(agent_id)
    s = result.get("status", "unknown")
    return f"Agent {agent_id} deregistered ({s})."


# =============================================================================
# Logs, LLM Requests & Shell State Tools
# =============================================================================


@mcp.tool
async def get_job_log(
    job_id: str,
    lines: int = 100,
    grep: str | None = None,
    level: str | None = None,
) -> str:
    """Read the tail of a job's log file with optional filtering.

    Returns the last N lines of the log file, optionally filtered by log
    level and/or grep pattern. Useful for diagnosing agent errors.

    Args:
        job_id: Job UUID
        lines: Number of tail lines to return (max 1000, default 100)
        grep: Case-insensitive substring filter
        level: Log level filter (DEBUG, INFO, WARNING, ERROR)

    Returns:
        Formatted log output with line count and filter info
    """
    if lines < 1:
        lines = 1
    elif lines > 1000:
        lines = 1000

    client = _get_client()
    try:
        data = await client.get_job_logs(
            job_id=job_id, lines=lines, grep=grep, level=level
        )
        return fmt.format_job_log(job_id, data)
    except Exception as e:
        return fmt.format_workspace_error("get job log", job_id, e)


@mcp.tool
async def list_llm_requests(
    job_id: str,
    limit: int = 20,
    offset: int = 0,
) -> str:
    """List LLM requests for a job with token usage and tool call summaries.

    Shows each request's model, timestamp, token counts, iteration number,
    and which tools were called. Use the doc_id with get_llm_request to see
    full message history for a specific request.

    Args:
        job_id: Job UUID
        limit: Maximum entries to return (max 100, default 20)
        offset: Pagination offset (default: 0)

    Returns:
        Formatted list of LLM requests with token usage and tool calls
    """
    if limit < 1:
        limit = 1
    elif limit > 100:
        limit = 100

    client = _get_client()
    try:
        data = await client.list_llm_requests(job_id=job_id, limit=limit, offset=offset)
        return fmt.format_llm_requests(job_id, data)
    except Exception as e:
        return fmt.format_workspace_error("list LLM requests", job_id, e)


@mcp.tool
async def get_shell_state(job_id: str) -> str:
    """Get shell tab state from the agent processing a job.

    Returns the list of open terminal tabs with their type (ssh, repl,
    claude-code, etc.) and recent output. Requires the job to be actively
    processing on an agent.

    Args:
        job_id: Job UUID (must be in 'processing' status)

    Returns:
        Shell state with tab names, types, and recent output
    """
    client = _get_client()
    try:
        data = await client.get_shell_state(job_id)
        return fmt.format_shell_state(job_id, data)
    except Exception as e:
        error_msg = str(e)
        if hasattr(e, "response"):
            try:
                detail = e.response.json().get("detail", error_msg)
                error_msg = detail
            except Exception:
                error_msg = f"HTTP {e.response.status_code}: {error_msg}"
        return f"Failed to get shell state for job {job_id}:\n{error_msg}"


# =============================================================================
# Todo Archives & Current Todos
# =============================================================================


@mcp.tool
async def get_current_todos(job_id: str) -> str:
    """Get only the current active todos from todos.yaml.

    Lighter than get_todos which includes all archives. Shows pending,
    in-progress, and completed items with a progress summary.

    Args:
        job_id: Job UUID to get current todos for

    Returns:
        Formatted current todos with progress count
    """
    client = _get_client()
    data = await client.get_current_todos(job_id)
    if data is None:
        return f"No current todos found for job {job_id}."
    return fmt.format_current_todos(data)


@mcp.tool
async def list_todo_archives(job_id: str) -> str:
    """List all archived todo files for a job.

    Returns metadata for each phase archive (filename, phase name, timestamp).
    Use get_todo_archive to read the full content of a specific archive.

    Args:
        job_id: Job UUID to list archives for

    Returns:
        List of archived todo files with metadata
    """
    client = _get_client()
    archives = await client.list_archived_todos(job_id)
    return fmt.format_todo_archives(job_id, archives)


@mcp.tool
async def get_todo_archive(job_id: str, filename: str) -> str:
    """Get the full content of an archived todo file for a specific phase.

    Use list_todo_archives first to get available filenames.

    Args:
        job_id: Job UUID
        filename: Archive filename (e.g. 'todos_phase1_20260124_183618.md')

    Returns:
        Full archived todos with status and notes
    """
    client = _get_client()
    data = await client.get_archived_todos(job_id, filename)
    if data is None:
        return f"Archive '{filename}' not found for job {job_id}."
    return fmt.format_todo_archive_detail(job_id, filename, data)


@mcp.tool
async def get_audit_timerange(job_id: str) -> str:
    """Get the first and last timestamps for a job's audit entries.

    Quick way to see when a job started and when it last had activity.
    Requires MongoDB to be available.

    Args:
        job_id: Job UUID to get time range for

    Returns:
        Start and end timestamps, or error if MongoDB unavailable
    """
    client = _get_client()
    data = await client.get_audit_time_range(job_id)
    if data is None:
        return f"No audit time range available for job {job_id} (MongoDB may be unavailable)."
    start = data.get("start", "unknown")
    end = data.get("end", "unknown")
    return f"Audit time range for job {job_id}:\n  Start: {start}\n  End:   {end}"


# =============================================================================
# Knowledge Base
# =============================================================================


@mcp.tool
async def get_knowledge_summary(project_id: str) -> str:
    """Get knowledge base summary statistics for a project.

    Shows total notes, counts by type and status, and the 5 most
    recently modified notes.

    Args:
        project_id: Project UUID

    Returns:
        Formatted knowledge base summary
    """
    client = _get_client()
    data = await client.get_knowledge_summary(project_id)
    return fmt.format_knowledge_summary(project_id, data)


@mcp.tool
async def list_knowledge_notes(
    project_id: str,
    note_type: str | None = None,
    status: str | None = None,
    tag: str | None = None,
    job_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> str:
    """List knowledge notes for a project with optional filters.

    Shows note previews with type, status, tags, and confidence scores.
    Use get_knowledge_note to read full content of a specific note.

    Args:
        project_id: Project UUID
        note_type: Filter by type (insight, decision, pattern, issue, etc.)
        status: Filter by status (active, resolved, superseded, archived)
        tag: Filter by tag
        job_id: Filter by originating job UUID
        limit: Max results (1-200, default 50)
        offset: Pagination offset (default 0)

    Returns:
        Formatted note list with previews
    """
    client = _get_client()
    data = await client.list_knowledge_notes(
        project_id=project_id,
        note_type=note_type,
        status=status,
        tag=tag,
        job_id=job_id,
        limit=limit,
        offset=offset,
    )
    return fmt.format_knowledge_notes(data)


@mcp.tool
async def get_knowledge_note(project_id: str, note_id: str) -> str:
    """Get a single knowledge note with full content and relationships.

    Returns the complete note including content, metadata, tags, keywords,
    confidence score, and Neo4j graph relationships.

    Args:
        project_id: Project UUID
        note_id: Note ID

    Returns:
        Full note detail with content and relationships
    """
    client = _get_client()
    data = await client.get_knowledge_note(project_id, note_id)
    return fmt.format_knowledge_note_detail(data)


@mcp.tool
async def search_knowledge(
    project_id: str,
    query: str,
    limit: int = 10,
) -> str:
    """Search the project knowledge base using hybrid search.

    Uses dense vector + sparse keyword search when embeddings are
    available, falls back to keyword-only search otherwise. Use this
    to find relevant knowledge from previous jobs.

    Args:
        project_id: Project UUID
        query: Search query text
        limit: Max results (1-50, default 10)

    Returns:
        Ranked search results with note previews
    """
    client = _get_client()
    data = await client.search_knowledge(
        project_id=project_id,
        query=query,
        limit=limit,
    )
    return fmt.format_knowledge_search(data)


# =============================================================================
# Projects
# =============================================================================


@mcp.tool
async def list_projects(user_id: str | None = None) -> str:
    """List projects, optionally filtered by user membership.

    Shows project name, status, goal, and last update time.

    Args:
        user_id: Filter to projects this user belongs to (optional)

    Returns:
        Formatted list of projects
    """
    client = _get_client()
    projects = await client.list_projects(user_id=user_id)
    return fmt.format_projects(projects)


@mcp.tool
async def get_project(project_id: str) -> str:
    """Get full details for a specific project.

    Returns name, description, goal, default config, and timestamps.

    Args:
        project_id: Project UUID

    Returns:
        Formatted project details
    """
    client = _get_client()
    project = await client.get_project(project_id)
    return fmt.format_project_detail(project)


@mcp.tool
async def create_project(
    name: str,
    user_id: str,
    description: str | None = None,
    goal: str | None = None,
    default_config_name: str | None = None,
) -> str:
    """Create a new project.

    Projects organize related jobs, accumulate knowledge across jobs,
    and can have team members with different roles.

    Args:
        name: Project name
        user_id: Owner user UUID
        description: Project description (optional)
        goal: Project goal statement (optional)
        default_config_name: Default agent config for new jobs (optional)

    Returns:
        Created project summary with ID
    """
    client = _get_client()
    result = await client.create_project(
        name=name,
        user_id=user_id,
        description=description,
        goal=goal,
        default_config_name=default_config_name,
    )
    return fmt.format_created_project(result)


@mcp.tool
async def list_project_jobs(
    project_id: str,
    status: str | None = None,
    limit: int = 100,
) -> str:
    """List jobs belonging to a project.

    Shows job status, config, timestamps, and audit entry counts.

    Args:
        project_id: Project UUID
        status: Filter by status (created, processing, completed, failed, etc.)
        limit: Max results (1-500, default 100)

    Returns:
        Formatted job list
    """
    client = _get_client()
    jobs = await client.list_project_jobs(
        project_id=project_id,
        status=status,
        limit=limit,
    )
    return fmt.format_jobs(jobs)


@mcp.tool
async def create_project_job(
    project_id: str,
    description: str,
    config_name: str = "default",
    instructions: str | None = None,
    config_override: dict[str, Any] | None = None,
    datasource_ids: list[str] | None = None,
) -> str:
    """Create a job within a project context.

    Uses the project's default config if not overridden. The job is
    automatically linked to the project and gets a Gitea branch.

    Args:
        project_id: Project UUID
        description: Task description
        config_name: Expert/agent config (default: uses project default)
        instructions: Additional inline instructions (optional)
        config_override: Per-job config overrides (optional)
        datasource_ids: Global datasource IDs to clone (optional)

    Returns:
        Created job summary with ID
    """
    client = _get_client()
    result = await client.create_project_job(
        project_id=project_id,
        description=description,
        config_name=config_name,
        instructions=instructions,
        config_override=config_override,
        datasource_ids=datasource_ids,
    )
    return fmt.format_created_job(result, config_name)


# =============================================================================
# Project Management (Extended)
# =============================================================================


@mcp.tool
async def update_project(
    project_id: str,
    name: str | None = None,
    description: str | None = None,
    goal: str | None = None,
    status: str | None = None,
    default_config_name: str | None = None,
) -> str:
    """Update a project's metadata or default config.

    Only provided fields are updated; others remain unchanged.

    Args:
        project_id: Project UUID
        name: New project name (optional)
        description: New description (optional)
        goal: New goal statement (optional)
        status: New status (optional)
        default_config_name: Default agent config for new jobs (optional)

    Returns:
        Update confirmation
    """
    client = _get_client()
    result = await client.update_project(
        project_id=project_id,
        name=name,
        description=description,
        goal=goal,
        status=status,
        default_config_name=default_config_name,
    )
    s = result.get("status", "unknown")
    return f"Project {project_id} updated ({s})."


@mcp.tool
async def delete_project(project_id: str) -> str:
    """Permanently delete a project and its associated data.

    Cannot delete default projects. This is irreversible — all project
    data including repos and knowledge base entries will be removed.

    Args:
        project_id: Project UUID

    Returns:
        Deletion confirmation
    """
    client = _get_client()
    result = await client.delete_project(project_id)
    s = result.get("status", "unknown")
    return f"Project {project_id} deleted ({s})."


@mcp.tool
async def list_project_members(project_id: str) -> str:
    """List all members of a project with their roles.

    Shows display name, email, role (owner/editor/viewer), and
    when they were added.

    Args:
        project_id: Project UUID

    Returns:
        Formatted member list with roles
    """
    client = _get_client()
    members = await client.list_project_members(project_id)
    return fmt.format_project_members(project_id, members)


@mcp.tool
async def add_project_member(
    project_id: str,
    user_id: str,
    role: str = "editor",
) -> str:
    """Add a user to a project with a specified role.

    Returns 409 if the user is already a member.

    Args:
        project_id: Project UUID
        user_id: User UUID to add
        role: Member role — owner, editor, or viewer (default: editor)

    Returns:
        Confirmation with member name and role
    """
    client = _get_client()
    result = await client.add_project_member(
        project_id=project_id,
        user_id=user_id,
        role=role,
    )
    name = result.get("display_name", user_id)
    actual_role = result.get("role", role)
    return f"Added {name} to project {project_id} as {actual_role}."


@mcp.tool
async def update_project_member(
    project_id: str,
    user_id: str,
    role: str,
) -> str:
    """Change a project member's role.

    Valid roles: owner, editor, viewer.

    Args:
        project_id: Project UUID
        user_id: User UUID of the member to update
        role: New role — owner, editor, or viewer

    Returns:
        Update confirmation
    """
    client = _get_client()
    result = await client.update_project_member(
        project_id=project_id,
        user_id=user_id,
        role=role,
    )
    s = result.get("status", "unknown")
    return f"Updated member {user_id} role to {role} in project {project_id} ({s})."


@mcp.tool
async def remove_project_member(
    project_id: str,
    user_id: str,
) -> str:
    """Remove a member from a project.

    Cannot remove the last owner of a project.

    Args:
        project_id: Project UUID
        user_id: User UUID to remove

    Returns:
        Removal confirmation
    """
    client = _get_client()
    result = await client.remove_project_member(
        project_id=project_id,
        user_id=user_id,
    )
    s = result.get("status", "unknown")
    return f"Removed member {user_id} from project {project_id} ({s})."


@mcp.tool
async def list_project_experts(project_id: str) -> str:
    """List project-specific expert configurations.

    These are experts stored in the project's Gitea repository
    under the experts/ directory, separate from global experts.

    Args:
        project_id: Project UUID

    Returns:
        Formatted list of project experts with descriptions and tags
    """
    client = _get_client()
    experts = await client.list_project_experts(project_id)
    return fmt.format_project_experts(project_id, experts)


@mcp.tool
async def get_project_expert(
    project_id: str,
    expert_name: str,
) -> str:
    """Get detailed configuration for a project-specific expert.

    Returns the expert's merged config (defaults + overrides)
    and the full instructions.md content.

    Args:
        project_id: Project UUID
        expert_name: Expert config name (e.g. 'scholar', 'developer')

    Returns:
        Expert detail with config and instructions
    """
    client = _get_client()
    data = await client.get_project_expert(project_id, expert_name)
    return fmt.format_project_expert_detail(project_id, data)


# =============================================================================
# Datasource CRUD
# =============================================================================


@mcp.tool
async def create_datasource(
    name: str,
    type: str,
    connection_url: str | None = None,
    description: str | None = None,
    credentials: dict[str, Any] | None = None,
    job_id: str | None = None,
    cli_hint: str | None = None,
    default_branch: str | None = None,
) -> str:
    """Create a new datasource.

    Types: generic (CLI via env vars), repository (git repo),
    postgresql, neo4j, mongodb, webdav (managed connectors).
    Use job_id=null for global datasources available to all jobs.

    Args:
        name: User-provided label
        type: Datasource type (generic, repository, postgresql, neo4j, mongodb, webdav)
        connection_url: Connection string (optional for generic)
        description: What this datasource contains (optional)
        credentials: Auth details (optional)
        job_id: Job UUID for job-scoped datasource (omit for global)
        cli_hint: Suggested CLI command (optional, for generic type)
        default_branch: Branch to clone (optional, for repository type)

    Returns:
        Created datasource summary with masked URL
    """
    client = _get_client()
    result = await client.create_datasource(
        name=name,
        ds_type=type,
        connection_url=connection_url,
        description=description,
        credentials=credentials,
        job_id=job_id,
        cli_hint=cli_hint,
        default_branch=default_branch,
    )
    return fmt.format_created_datasource(result)


@mcp.tool
async def update_datasource(
    datasource_id: str,
    name: str | None = None,
    description: str | None = None,
    connection_url: str | None = None,
    credentials: dict[str, Any] | None = None,
    cli_hint: str | None = None,
    default_branch: str | None = None,
) -> str:
    """Update an existing datasource's connection details or metadata.

    Only provided fields are updated; others remain unchanged.

    Args:
        datasource_id: Datasource UUID
        name: New label (optional)
        description: New description (optional)
        connection_url: New connection string (optional)
        credentials: New auth details (optional)
        cli_hint: New CLI hint (optional)
        default_branch: New default branch (optional)

    Returns:
        Update confirmation
    """
    client = _get_client()
    result = await client.update_datasource(
        datasource_id=datasource_id,
        name=name,
        description=description,
        connection_url=connection_url,
        credentials=credentials,
        cli_hint=cli_hint,
        default_branch=default_branch,
    )
    status = result.get("status", "unknown")
    return f"Datasource {datasource_id} updated ({status})."


@mcp.tool
async def delete_datasource(datasource_id: str) -> str:
    """Permanently delete a datasource.

    This does not affect jobs that have already cloned it.

    Args:
        datasource_id: Datasource UUID

    Returns:
        Deletion confirmation
    """
    client = _get_client()
    result = await client.delete_datasource(datasource_id)
    status = result.get("status", "unknown")
    return f"Datasource {datasource_id} deleted ({status})."


# =============================================================================
# Project ↔ Datasource (N:M)
# =============================================================================


@mcp.tool
async def list_project_datasources(project_id: str) -> str:
    """List datasources linked to a project.

    Args:
        project_id: Project UUID

    Returns:
        Formatted datasource list
    """
    client = _get_client()
    datasources = await client.list_project_datasources(project_id)
    if not datasources:
        return f"No datasources linked to project {project_id}."
    return fmt.format_datasources(datasources)


@mcp.tool
async def link_datasource_to_project(project_id: str, datasource_id: str) -> str:
    """Link a datasource to a project.

    Creates a knowledge entry so agents can discover the datasource
    through the project's knowledge base.

    Args:
        project_id: Project UUID
        datasource_id: Datasource UUID

    Returns:
        Link confirmation
    """
    client = _get_client()
    result = await client.link_datasource_to_project(project_id, datasource_id)
    status = result.get("status", "unknown")
    return f"Datasource {datasource_id} linked to project {project_id} ({status})."


@mcp.tool
async def unlink_datasource_from_project(project_id: str, datasource_id: str) -> str:
    """Unlink a datasource from a project.

    Removes the corresponding knowledge entry.

    Args:
        project_id: Project UUID
        datasource_id: Datasource UUID

    Returns:
        Unlink confirmation
    """
    client = _get_client()
    result = await client.unlink_datasource_from_project(project_id, datasource_id)
    status = result.get("status", "unknown")
    return f"Datasource {datasource_id} unlinked from project {project_id} ({status})."


# =============================================================================
# Knowledge Mutations
# =============================================================================


@mcp.tool
async def update_knowledge_note(
    project_id: str,
    note_id: str,
    status: str | None = None,
    add_tags: list[str] | None = None,
    remove_tags: list[str] | None = None,
) -> str:
    """Update a knowledge note's status or tags.

    Use to mark notes as resolved, superseded, or archived,
    or to organize notes with tags.

    Args:
        project_id: Project UUID
        note_id: Note ID
        status: New status (active, resolved, superseded, archived)
        add_tags: Tags to add (optional)
        remove_tags: Tags to remove (optional)

    Returns:
        Update confirmation
    """
    client = _get_client()
    result = await client.update_knowledge_note(
        project_id=project_id,
        note_id=note_id,
        status=status,
        add_tags=add_tags,
        remove_tags=remove_tags,
    )
    s = result.get("status", "unknown")
    return f"Knowledge note {note_id} updated ({s})."


@mcp.tool
async def delete_knowledge_note(project_id: str, note_id: str) -> str:
    """Permanently delete a knowledge note from both PostgreSQL and Neo4j.

    This is irreversible.

    Args:
        project_id: Project UUID
        note_id: Note ID

    Returns:
        Deletion confirmation
    """
    client = _get_client()
    result = await client.delete_knowledge_note(project_id, note_id)
    s = result.get("status", "unknown")
    return f"Knowledge note {note_id} deleted ({s})."


@mcp.tool
async def export_knowledge(project_id: str) -> str:
    """Export a project's knowledge base as Obsidian-compatible markdown.

    Each note becomes a .md file with YAML frontmatter and wikilink
    relationships. Requires Neo4j to be available.

    Args:
        project_id: Project UUID

    Returns:
        Export summary with path and note count
    """
    client = _get_client()
    result = await client.export_knowledge(project_id)
    path = result.get("path", "unknown")
    count = result.get("note_count", 0)
    name = result.get("project_name", "")
    return (
        f"Knowledge base exported for project '{name}'.\n"
        f"  Notes exported: {count}\n"
        f"  Export path: {path}\n"
        f"  Format: Obsidian-compatible markdown with YAML frontmatter"
    )


# =============================================================================
# Job Promotion
# =============================================================================


@mcp.tool
async def promote_job(
    job_id: str,
    name: str,
    user_id: str,
    description: str | None = None,
    goal: str | None = None,
) -> str:
    """Promote a completed job into a dedicated project.

    Creates a new project, seeds its repo from the job's branch
    (preserving git history), and moves the job. The job must be
    completed and in a default project.

    Args:
        job_id: Job UUID (must be completed)
        name: Name for the new project
        user_id: Owner user UUID
        description: Project description (optional)
        goal: Project goal (optional)

    Returns:
        Promotion summary with new project ID
    """
    client = _get_client()
    result = await client.promote_job(
        job_id=job_id,
        name=name,
        user_id=user_id,
        description=description,
        goal=goal,
    )
    project_id = result.get("project_id", "unknown")
    project_name = result.get("project_name", name)
    return (
        f"Job {job_id} promoted to project '{project_name}'.\n"
        f"  New project ID: {project_id}\n"
        f"  Git history preserved from job branch."
    )


# =============================================================================
# Internal Async Helpers (depend on client, not extracted to formatters)
# =============================================================================


async def _search_audit(
    client: AsyncCockpitClient,
    job_id: str,
    query: str,
    limit: int = 20,
) -> str:
    """Search audit entries for a pattern."""
    import json as _json

    query_lower = query.lower()
    matches: list[dict] = []

    # Fetch pages until we have enough matches or run out
    page = 1
    while len(matches) < limit:
        audit = await client.get_audit_trail(
            job_id=job_id,
            page=page,
            page_size=100,
            filter_category="all",
        )

        if audit.get("error") or not audit.get("entries"):
            break

        for entry in audit["entries"]:
            if fmt.entry_matches(entry, query_lower):
                matches.append(entry)
                if len(matches) >= limit:
                    break

        if not audit.get("hasMore"):
            break
        page += 1

    if not matches:
        return f"No audit entries matching '{query}' found."

    lines = [f"Found {len(matches)} entries matching '{query}':\n"]

    for entry in matches:
        step_num = entry.get("step_number", "?")
        step_type = entry.get("step_type", "unknown")

        if step_type == "tool":
            tool = entry.get("tool", {})
            tool_name = tool.get("name", "unknown")
            lines.append(f"[{step_num}] Tool: {tool_name}")
            args = _json.dumps(tool.get("arguments", {}))[:150]
            lines.append(f"    Args: {args}")
            result_preview = tool.get("result_preview")
            if result_preview:
                lines.append(f"    Result: {str(result_preview)[:150]}")
        elif step_type == "llm":
            llm = entry.get("llm", {})
            preview = (llm.get("response_content_preview", "") or "")[:150]
            request_id = llm.get("request_id")
            line = f"[{step_num}] LLM: {preview}"
            if request_id:
                line += f" (doc_id: {request_id})"
            lines.append(line)
        elif step_type == "error":
            error = entry.get("error", "Unknown error")
            lines.append(f"[{step_num}] ERROR: {error}")
        else:
            lines.append(f"[{step_num}] {step_type}")
        lines.append("")

    return "\n".join(lines)


# =============================================================================
# Sudo Approval Gate
# =============================================================================


@mcp.tool
async def list_sudo_requests(
    job_id: str | None = None,
    status: str | None = None,
    limit: int = 20,
) -> str:
    """List sudo approval requests from agent VMs.

    Shows pending, approved, denied, and expired sudo requests.
    Use this to see what privileged commands agents are requesting.

    Args:
        job_id: Filter by job ID
        status: Filter by status (pending, approved, denied, expired, auto_approved, auto_denied)
        limit: Maximum requests to return (1-100, default 20)

    Returns:
        Formatted list of sudo approval requests
    """
    if limit < 1:
        limit = 1
    elif limit > 100:
        limit = 100

    client = _get_client()
    requests = await client.list_sudo_requests(
        job_id=job_id, status=status, limit=limit
    )

    if not requests:
        return "No sudo approval requests found."

    lines = [f"Found {len(requests)} sudo request(s):\n"]
    for req in requests:
        rid = req.get("id", "?")[:8]
        jid = req.get("job_id", "?")[:8]
        cmd = req.get("command", "?")
        argv = req.get("arguments", [])
        cmd_str = " ".join(argv) if argv else cmd
        st = req.get("status", "?")
        user = req.get("requesting_user", "?")
        target = req.get("target_user", "root")
        vm = req.get("vm_name", "?")
        ts = req.get("requested_at", "?")

        status_icon = {
            "pending": "⏳",
            "approved": "✅",
            "denied": "❌",
            "expired": "⏰",
        }.get(st, "•")
        lines.append(f"{status_icon} [{rid}] {st.upper()}")
        lines.append(f"  Command: {cmd_str}")
        lines.append(f"  User: {user} → {target} | VM: {vm} | Job: {jid}")
        lines.append(f"  Requested: {ts}")
        if req.get("decided_by"):
            lines.append(
                f"  Decided by: {req['decided_by']} — {req.get('decision_reason', '')}"
            )
        lines.append("")

    return "\n".join(lines)


@mcp.tool
async def approve_sudo_request(
    request_id: str,
    reason: str = "",
) -> str:
    """Approve a pending sudo approval request.

    This sends the approval to the agent's VM, allowing the sudo command
    to execute. The agent's run_command call will unblock and return output.

    Args:
        request_id: UUID of the sudo request to approve
        reason: Optional reason for approval

    Returns:
        Confirmation message
    """
    client = _get_client()
    try:
        result = await client.approve_sudo_request(request_id, reason=reason)
        return f"Approved sudo request {request_id}: {result.get('status', 'ok')}"
    except Exception as e:
        return f"Failed to approve: {e}"


@mcp.tool
async def deny_sudo_request(
    request_id: str,
    reason: str,
) -> str:
    """Deny a pending sudo approval request.

    This sends a denial to the agent's VM. The sudo command will be rejected
    and the agent will see 'sudo request denied by operator' in stderr.

    Args:
        request_id: UUID of the sudo request to deny
        reason: Reason for denial (required)

    Returns:
        Confirmation message
    """
    client = _get_client()
    try:
        result = await client.deny_sudo_request(request_id, reason=reason)
        return f"Denied sudo request {request_id}: {result.get('status', 'ok')}"
    except Exception as e:
        return f"Failed to deny: {e}"


# =============================================================================
# Messaging Tools (Live Communication)
# =============================================================================


@mcp.tool
async def list_message_threads(job_id: str) -> str:
    """List message threads for an agent job.

    Shows all communication threads between the agent and humans,
    including thread ID, subject, message count, and status.

    Args:
        job_id: Job UUID to list threads for

    Returns:
        Formatted list of message threads
    """
    client = _get_client()
    data = await client.list_message_threads(job_id)
    return fmt.format_message_threads(data.get("threads", []))


@mcp.tool
async def send_message_to_job(
    job_id: str,
    thread_id: str,
    message: str,
    urgent: bool = False,
) -> str:
    """Send a reply to an agent's message thread (as a human).

    Routes the reply to the agent. If the agent is waiting for a reply
    on this thread (blocking mode), it resumes immediately. Otherwise
    the reply is queued for the next strategic phase.

    Args:
        job_id: Job UUID
        thread_id: Thread ID to reply to
        message: Reply body text
        urgent: If true, deliver as immediate interrupt regardless of mode

    Returns:
        Delivery confirmation with strategy used
    """
    client = _get_client()
    try:
        result = await client.reply_to_message(
            job_id,
            thread_id,
            message,
            urgent=urgent,
        )
        strategy = result.get("delivery_strategy", "unknown")
        seq = result.get("sequence", "?")
        return (
            f"Reply delivered to thread {thread_id} (message #{seq}).\n"
            f"Delivery strategy: {strategy}"
        )
    except Exception as e:
        return f"Failed to send reply: {e}"


@mcp.tool
async def get_message_thread(job_id: str, thread_id: str) -> str:
    """Get full message history for a specific thread.

    Shows all messages in chronological order with direction,
    timestamps, and content.

    Args:
        job_id: Job UUID
        thread_id: Thread ID to retrieve

    Returns:
        Formatted thread message history
    """
    client = _get_client()
    data = await client.list_message_threads(job_id)
    threads = data.get("threads", [])

    # Find the matching thread and its messages
    target = None
    for t in threads:
        if t.get("thread_id") == thread_id:
            target = t
            break

    if not target:
        return f"Thread {thread_id} not found in job {job_id}."

    messages = target.get("messages", [])
    if not messages:
        # Thread found but no inline messages — format what we have
        return fmt.format_message_threads([target])

    return fmt.format_thread_messages(messages, thread_id)


# =============================================================================
# Persistent Session/Thread Management
# =============================================================================


@mcp.tool
async def create_persistent_thread(
    title: str = "Untitled Session",
    config_name: str = "defaults",
    permission_mode: Literal["supervised", "auto_accept", "autonomous"] = "supervised",
    project_id: str | None = None,
    project_ids: list[str] | None = None,
    model: str | None = None,
    temperature: float | None = None,
) -> str:
    """Create a new persistent thread (interactive agent session).

    MUTATION: This creates a persistent thread, provisions a workspace
    container and agent pod. The thread starts in 'created' status and
    waits for an agent to connect.

    Args:
        title: Human-readable session title
        config_name: Agent config to use (default: "defaults")
        permission_mode: Tool approval mode (supervised, auto_accept, autonomous)
        project_id: Single project UUID to scope (legacy)
        project_ids: List of project UUIDs to scope
        model: LLM model override (e.g. "openai/gpt-oss-120b")
        temperature: Temperature override

    Returns:
        Created thread ID and status
    """
    client = _get_client()
    try:
        result = await client.create_persistent_thread(
            config_name=config_name,
            title=title,
            permission_mode=permission_mode,
            project_id=project_id,
            project_ids=project_ids,
            model=model,
            temperature=temperature,
        )
        return fmt.format_created_thread(result, config_name, title)
    except Exception as e:
        return fmt.format_action_error("create_thread", "N/A", e)


@mcp.tool
async def list_persistent_threads(
    project_id: str | None = None,
    status: Literal["created", "active", "idle", "ended"] | None = None,
) -> str:
    """List persistent threads for the authenticated user.

    Returns all persistent sessions with status, config, and activity info.
    Use filters to narrow results.

    Args:
        project_id: Filter by project UUID
        status: Filter by thread status (created, active, idle, ended)

    Returns:
        Formatted list of persistent threads
    """
    client = _get_client()
    data = await client.list_persistent_threads(project_id=project_id, status=status)
    return fmt.format_persistent_threads(data.get("threads", []))


@mcp.tool
async def get_persistent_thread(thread_id: str) -> str:
    """Get detailed information about a specific persistent thread.

    Returns full thread details including status, config, workspace state,
    and metadata.

    Args:
        thread_id: Thread UUID to retrieve

    Returns:
        Formatted thread details
    """
    client = _get_client()
    thread = await client.get_persistent_thread(thread_id)
    return fmt.format_persistent_thread_detail(thread)


@mcp.tool
async def end_persistent_thread(thread_id: str, permanent: bool = False) -> str:
    """End or permanently delete a persistent thread.

    MUTATION: If permanent=False (default), the thread is soft-ended and
    can be resumed later. If permanent=True, the thread and ALL its
    messages are permanently deleted along with workspace containers,
    agent pods, and VMs.

    Args:
        thread_id: Thread UUID to end or delete
        permanent: If true, permanently delete instead of soft-end

    Returns:
        Action result with status
    """
    client = _get_client()
    try:
        result = await client.end_persistent_thread(thread_id, permanent=permanent)
        return fmt.format_thread_action_result(
            "delete_thread" if permanent else "end_thread", thread_id, result
        )
    except Exception as e:
        return fmt.format_action_error("end_thread", thread_id, e)


@mcp.tool
async def resume_persistent_thread(thread_id: str) -> str:
    """Resume an ended or idle persistent thread.

    MUTATION: Resets the thread status to 'created', clears the stale
    agent binding, and re-provisions the agent pod. The thread must be
    in 'ended' or 'idle' status.

    Args:
        thread_id: Thread UUID to resume

    Returns:
        Action result with new status
    """
    client = _get_client()
    try:
        result = await client.resume_persistent_thread(thread_id)
        return fmt.format_thread_action_result("resume_thread", thread_id, result)
    except Exception as e:
        return fmt.format_action_error("resume_thread", thread_id, e)


@mcp.tool
async def get_persistent_thread_messages(
    thread_id: str,
    limit: int = 50,
    offset: int = 0,
) -> str:
    """Get message history for a persistent thread session.

    Returns conversation messages in chronological order with role,
    content preview, and tool call info. Paginated.

    Args:
        thread_id: Thread UUID to get messages for
        limit: Maximum messages to return (1-500, default 50)
        offset: Pagination offset (default 0)

    Returns:
        Formatted message history
    """
    if limit < 1:
        limit = 1
    elif limit > 500:
        limit = 500

    client = _get_client()
    data = await client.get_persistent_thread_messages(
        thread_id, limit=limit, offset=offset
    )
    return fmt.format_persistent_thread_messages(data)


@mcp.tool
async def get_persistent_thread_ide(thread_id: str) -> str:
    """Get IDE/workspace session status for a persistent thread.

    Returns the workspace container or VM status with a code-server URL
    when the workspace is ready.

    Args:
        thread_id: Thread UUID to check IDE status for

    Returns:
        IDE session status with URLs
    """
    client = _get_client()
    data = await client.get_persistent_thread_ide(thread_id)
    return fmt.format_persistent_thread_ide(data)
