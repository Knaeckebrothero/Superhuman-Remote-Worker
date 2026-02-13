"""MCP server exposing debug cockpit tools.

Provides tools to inspect agent jobs, audit trails, todos, and graph changes
via the Model Context Protocol using FastMCP 2.0.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from fastmcp import FastMCP
from starlette.responses import JSONResponse

try:
    from .client import AsyncCockpitClient
except ImportError:
    from client import AsyncCockpitClient  # type: ignore[no-redef]


# Create the MCP server instance
mcp = FastMCP("cockpit-debug", stateless_http=True)

# Global client instance (initialized lazily)
_client: AsyncCockpitClient | None = None


def _get_client() -> AsyncCockpitClient:
    """Get or create the async client instance."""
    global _client
    if _client is None:
        _client = AsyncCockpitClient()
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
    status: Literal["pending", "running", "completed", "failed"] | None = None,
    limit: int = 20,
) -> str:
    """List agent jobs with optional status filter.

    Returns job ID, status, config name, timestamps, and audit entry count.
    Use this to find jobs to investigate.

    Args:
        status: Filter by status (pending, running, completed, failed)
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
    return _format_jobs(jobs)


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
    return _format_job_detail(job)


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
    return _format_audit(audit)


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
    return _format_chat_history(chat)


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
    return _format_todos(todos)


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
    return _format_graph_changes(changes)


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
    return _format_llm_request(request)


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
        return _format_action_result("approve", job_id, result)
    except Exception as e:
        return _format_action_error("approve", job_id, e)


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
        return _format_action_result("resume", job_id, result, feedback=feedback)
    except Exception as e:
        return _format_action_error("resume", job_id, e)


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
        return _format_action_result("cancel", job_id, result)
    except Exception as e:
        return _format_action_error("cancel", job_id, e)


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
        return _format_created_job(result, config_name)
    except Exception as e:
        return _format_action_error("create", "N/A", e)


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
        return _format_action_result("delete", job_id, result)
    except Exception as e:
        return _format_action_error("delete", job_id, e)


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
        return _format_action_result("assign", job_id, result, agent_id=agent_id)
    except Exception as e:
        return _format_action_error("assign", job_id, e)


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
        return _format_datasource_test(datasource_id, result)
    except Exception as e:
        return _format_action_error("test_datasource", datasource_id, e)


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
        return _format_commits(job_id, result, ref=ref, since_ref=since_ref)
    except Exception as e:
        return _format_git_error("list commits", job_id, e)


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
            diff_text = _filter_diff_by_file(diff_text, file_path)

        return _format_diff(job_id, base, head, diff_text, max_chars=max_chars)
    except Exception as e:
        return _format_git_error("get diff", job_id, e)


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
        return _format_git_error(f"read file '{file_path}'", job_id, e)


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
        return _format_file_listing(job_id, path, entries, ref=ref)
    except Exception as e:
        return _format_git_error(f"list files at '{path or '/'}'", job_id, e)


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
        return _format_tags(job_id, tags)
    except Exception as e:
        return _format_git_error("list tags", job_id, e)


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
        return _format_frozen_job(job_id, data)
    except Exception as e:
        return _format_workspace_error("get frozen job data", job_id, e)


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
        return _format_workspace_error(f"read workspace file '{path}'", job_id, e)


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
        return _format_workspace_overview(job_id, data)
    except Exception as e:
        return _format_workspace_error("get workspace overview", job_id, e)


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
        return _format_job_progress(job_id, data)
    except Exception as e:
        return _format_workspace_error("get job progress", job_id, e)


@mcp.tool
async def get_job_requirements(
    job_id: str,
    status: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> str:
    """Get extracted requirements for a job.

    Lists requirements with their validation status, priority, and metadata.

    Args:
        job_id: Job UUID
        status: Filter by status (pending, validating, integrated, rejected, failed)
        limit: Max results (default: 100)
        offset: Pagination offset (default: 0)

    Returns:
        List of requirements with status and metadata
    """
    if limit < 1:
        limit = 1
    elif limit > 1000:
        limit = 1000

    client = _get_client()
    try:
        data = await client.get_job_requirements(
            job_id, status=status, limit=limit, offset=offset
        )
        return _format_requirements(job_id, data)
    except Exception as e:
        return _format_workspace_error("get requirements", job_id, e)


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
        return _format_job_stats(data)
    except Exception as e:
        return _format_monitoring_error("get job stats", e)


@mcp.tool
async def get_agent_stats() -> str:
    """Get agent workforce summary with counts by status.

    Returns:
        Total agents and counts per status (ready, working, offline, etc.)
    """
    client = _get_client()
    try:
        data = await client.get_agent_stats()
        return _format_agent_stats(data)
    except Exception as e:
        return _format_monitoring_error("get agent stats", e)


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
        return _format_stuck_jobs(data, threshold_minutes)
    except Exception as e:
        return _format_monitoring_error("get stuck jobs", e)


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
        return _format_agents(agents, status_filter=status)
    except Exception as e:
        return _format_monitoring_error("list agents", e)


@mcp.tool
async def list_experts() -> str:
    """List available expert/agent configurations.

    Returns:
        Expert configs with ID, display name, description, and tags
    """
    client = _get_client()
    try:
        experts = await client.list_experts()
        return _format_experts(experts)
    except Exception as e:
        return _format_monitoring_error("list experts", e)


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
        return _format_expert_detail(expert_id, data)
    except Exception as e:
        return _format_monitoring_error(f"get expert '{expert_id}'", e)


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
        return _format_datasources(datasources, type_filter=ds_type)
    except Exception as e:
        return _format_monitoring_error("list datasources", e)


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
        return _format_tables(tables)
    except Exception as e:
        return _format_monitoring_error("list tables", e)


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
        return _format_table_data(table_name, data)
    except Exception as e:
        return _format_monitoring_error(f"query table '{table_name}'", e)


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
        return _format_table_schema(table_name, columns)
    except Exception as e:
        return _format_monitoring_error(f"get schema for '{table_name}'", e)


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
        return _format_sources(data, job_id=job_id, type_filter=source_type)
    except Exception as e:
        return _format_citation_error("list sources", e, job_id=job_id)


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
        return _format_source_detail(data)
    except Exception as e:
        return _format_citation_error(f"get source {source_id}", e)


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
        return _format_citations(job_id, data, status_filter=verification_status)
    except Exception as e:
        return _format_citation_error("list citations", e, job_id=job_id)


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
        return _format_citation_detail(data)
    except Exception as e:
        return _format_citation_error(f"get citation {citation_id}", e)


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
            job_id, query=query, mode=mode,
            source_type=source_type, tags=tags, top_k=top_k,
        )
        return _format_source_search(data)
    except Exception as e:
        return _format_citation_error("search sources", e, job_id=job_id)


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
            job_id, source_id, annotation_type=annotation_type,
        )
        return _format_annotations(job_id, source_id, annotations, type_filter=annotation_type)
    except Exception as e:
        return _format_citation_error(
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
        return _format_source_tags(job_id, source_id, tags)
    except Exception as e:
        return _format_citation_error(
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
        return _format_citation_stats(job_id, data)
    except Exception as e:
        return _format_citation_error("get citation stats", e, job_id=job_id)


# =============================================================================
# Formatters - Convert API responses to readable text
# =============================================================================


def _format_jobs(jobs: list[dict[str, Any]]) -> str:
    """Format job list for display."""
    if not jobs:
        return "No jobs found."

    lines = [f"Found {len(jobs)} job(s):\n"]
    for job in jobs:
        status_icon = {
            "pending": "⏳",
            "running": "🔄",
            "completed": "✅",
            "failed": "❌",
        }.get(job.get("status", ""), "❓")

        lines.append(
            f"{status_icon} {job['id']}\n"
            f"   Config: {job.get('config', 'N/A')}\n"
            f"   Status: {job.get('status', 'N/A')}\n"
            f"   Created: {job.get('created_at', 'N/A')}\n"
            f"   Audit entries: {job.get('audit_count', 'N/A')}\n"
        )

    return "\n".join(lines)


def _format_job_detail(job: dict[str, Any]) -> str:
    """Format single job details."""
    lines = [
        f"Job: {job['id']}",
        f"Status: {job.get('status', 'N/A')}",
        f"Config: {job.get('config', 'N/A')}",
        f"Created: {job.get('created_at', 'N/A')}",
        f"Updated: {job.get('updated_at', 'N/A')}",
        f"Audit entries: {job.get('audit_count', 'N/A')}",
    ]

    if job.get("description"):
        description = job["description"]
        if len(description) > 500:
            description = description[:500] + "..."
        lines.append(f"\nDescription:\n{description}")

    if job.get("error"):
        lines.append(f"\nError: {job['error']}")

    return "\n".join(lines)


def _format_audit(audit: dict[str, Any]) -> str:
    """Format audit trail entries."""
    entries = audit.get("entries", [])
    total = audit.get("total", 0)
    page = audit.get("page", 1)
    has_more = audit.get("hasMore", False)

    if audit.get("error"):
        return f"Audit unavailable: {audit['error']}"

    if not entries:
        return "No audit entries found."

    lines = [
        f"Audit trail (page {page}, showing {len(entries)} of {total} entries):\n"
    ]

    for entry in entries:
        step_type = entry.get("step_type", "unknown")
        step_num = entry.get("step_number", "?")
        timestamp = entry.get("timestamp", "")

        if step_type == "llm_response":
            content = entry.get("content", {})
            msg_type = content.get("type", "unknown")
            text = content.get("text", "")[:200]
            if len(content.get("text", "")) > 200:
                text += "..."
            lines.append(f"[{step_num}] LLM Response ({msg_type}): {text}")

        elif step_type == "tool_call":
            tool = entry.get("tool", {})
            tool_name = tool.get("name", "unknown")
            args = tool.get("arguments", {})
            args_preview = json.dumps(args)[:100]
            if len(json.dumps(args)) > 100:
                args_preview += "..."
            lines.append(f"[{step_num}] Tool Call: {tool_name}({args_preview})")

        elif step_type == "tool_result":
            tool = entry.get("tool", {})
            tool_name = tool.get("name", "unknown")
            result = str(entry.get("result", ""))[:150]
            if len(str(entry.get("result", ""))) > 150:
                result += "..."
            lines.append(f"[{step_num}] Tool Result ({tool_name}): {result}")

        elif step_type == "error":
            error = entry.get("error", "Unknown error")
            lines.append(f"[{step_num}] ERROR: {error}")

        else:
            lines.append(f"[{step_num}] {step_type}: {timestamp}")

    if has_more:
        lines.append(f"\n... more entries available (use page={page + 1})")

    return "\n".join(lines)


def _format_chat_history(chat: dict[str, Any]) -> str:
    """Format chat history entries."""
    entries = chat.get("entries", [])
    total = chat.get("total", 0)
    page = chat.get("page", 1)
    has_more = chat.get("hasMore", False)

    if chat.get("error"):
        return f"Chat history unavailable: {chat['error']}"

    if not entries:
        return "No chat history found."

    lines = [f"Chat history (page {page}, {len(entries)} of {total} turns):\n"]

    for i, entry in enumerate(entries, 1):
        lines.append(f"--- Turn {entry.get('turn_number', i)} ---")

        # Input messages
        inputs = entry.get("input_messages", [])
        for msg in inputs:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            if isinstance(content, str):
                preview = content[:300]
                if len(content) > 300:
                    preview += "..."
            else:
                preview = str(content)[:300]
            lines.append(f"[{role}]: {preview}")

        # Response
        response = entry.get("response", {})
        resp_content = response.get("content", "")
        if isinstance(resp_content, str):
            preview = resp_content[:300]
            if len(resp_content) > 300:
                preview += "..."
            lines.append(f"[assistant]: {preview}")

        lines.append("")

    if has_more:
        lines.append(f"... more turns available (use page={page + 1})")

    return "\n".join(lines)


def _format_todos(todos: dict[str, Any]) -> str:
    """Format todos (current + archives)."""
    lines = [f"Todos for job: {todos.get('job_id', 'unknown')}\n"]

    if not todos.get("has_workspace"):
        return "No workspace found for this job."

    # Current todos
    current = todos.get("current")
    if current:
        lines.append("=== Current Todos ===")
        for todo in current.get("todos", []):
            status_icon = {
                "pending": "○",
                "in_progress": "◐",
                "completed": "●",
                "skipped": "⊘",
            }.get(todo.get("status", ""), "?")
            lines.append(f"  {status_icon} {todo.get('subject', 'Untitled')}")
            if todo.get("description"):
                desc = todo["description"][:100]
                if len(todo["description"]) > 100:
                    desc += "..."
                lines.append(f"      {desc}")
        lines.append("")

    # Archives
    archives = todos.get("archives", [])
    if archives:
        lines.append(f"=== Archived Phases ({len(archives)}) ===")
        for archive in archives:
            lines.append(f"  - {archive.get('filename', 'unknown')}")
            if archive.get("phase_name"):
                lines.append(f"    Phase: {archive['phase_name']}")

    if not current and not archives:
        lines.append("No todos found.")

    return "\n".join(lines)


def _format_graph_changes(changes: dict[str, Any]) -> str:
    """Format graph changes timeline."""
    summary = changes.get("summary", {})
    deltas = changes.get("deltas", [])
    time_range = changes.get("timeRange")

    lines = [f"Graph changes for job: {changes.get('jobId', 'unknown')}\n"]

    # Summary
    lines.append("=== Summary ===")
    lines.append(f"  Total tool calls: {summary.get('totalToolCalls', 0)}")
    lines.append(f"  Graph operations: {summary.get('graphToolCalls', 0)}")
    lines.append(f"  Nodes created: {summary.get('nodesCreated', 0)}")
    lines.append(f"  Nodes deleted: {summary.get('nodesDeleted', 0)}")
    lines.append(f"  Nodes modified: {summary.get('nodesModified', 0)}")
    lines.append(f"  Relationships created: {summary.get('relationshipsCreated', 0)}")
    lines.append(f"  Relationships deleted: {summary.get('relationshipsDeleted', 0)}")

    if time_range:
        lines.append(f"\nTime range: {time_range.get('start')} to {time_range.get('end')}")

    # Recent deltas (last 10)
    if deltas:
        lines.append(f"\n=== Recent Operations (last 10 of {len(deltas)}) ===")
        for delta in deltas[-10:]:
            idx = delta.get("toolCallIndex", "?")
            query = delta.get("cypherQuery", "")[:80]
            if len(delta.get("cypherQuery", "")) > 80:
                query += "..."
            changes_summary = delta.get("changes", {})
            nodes_created = len(changes_summary.get("nodesCreated", []))
            rels_created = len(changes_summary.get("relationshipsCreated", []))
            lines.append(f"  [{idx}] {query}")
            if nodes_created or rels_created:
                lines.append(f"       +{nodes_created} nodes, +{rels_created} rels")

    return "\n".join(lines)


def _format_llm_request(request: dict[str, Any]) -> str:
    """Format LLM request/response from MongoDB llm_requests document."""
    lines = [f"LLM Request: {request.get('_id', 'unknown')}\n"]

    lines.append(f"Job: {request.get('job_id', 'N/A')}")
    lines.append(f"Model: {request.get('model', 'N/A')}")
    lines.append(f"Timestamp: {request.get('timestamp', 'N/A')}")
    if request.get("iteration") is not None:
        lines.append(f"Iteration: {request['iteration']}")
    if request.get("latency_ms") is not None:
        lines.append(f"Latency: {request['latency_ms']}ms")

    # Token usage from metrics
    metrics = request.get("metrics", {})
    usage = metrics.get("token_usage", {})
    if usage:
        parts = [f"{usage.get('prompt_tokens', 0)} prompt"]
        parts.append(f"{usage.get('completion_tokens', 0)} completion")
        if usage.get("reasoning_tokens"):
            parts.append(f"{usage['reasoning_tokens']} reasoning")
        lines.append(f"Tokens: {', '.join(parts)}")

    # Response metadata
    resp = request.get("response", {})
    resp_meta = resp.get("response_metadata", {})
    if resp_meta.get("finish_reason"):
        lines.append(f"Finish reason: {resp_meta['finish_reason']}")
    if resp_meta.get("system_fingerprint"):
        lines.append(f"System fingerprint: {resp_meta['system_fingerprint']}")

    # Tool definitions
    req_data = request.get("request", {})
    tools = req_data.get("tools", [])
    if tools:
        tool_names = [t.get("function", {}).get("name", "?") for t in tools]
        lines.append(f"\n=== Tool Definitions ({len(tools)}) ===")
        lines.append(", ".join(tool_names))

    # Model kwargs
    model_kwargs = req_data.get("model_kwargs", {})
    if model_kwargs:
        lines.append("\n=== Model Parameters ===")
        for k, v in model_kwargs.items():
            lines.append(f"  {k}: {v}")

    # Messages (last 5)
    messages = req_data.get("messages", [])
    if messages:
        lines.append(f"\n=== Messages ({len(messages)}) ===")
        for msg in messages[-5:]:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            if isinstance(content, str):
                preview = content[:200]
                if len(content) > 200:
                    preview += "..."
            else:
                preview = str(content)[:200]
            lines.append(f"[{role}]: {preview}")

    # Response content
    if resp:
        lines.append("\n=== Response ===")
        content = resp.get("content", "")
        if content:
            preview = content[:500]
            if len(content) > 500:
                preview += "..."
            lines.append(preview)

        # Tool calls in response
        tool_calls = resp.get("tool_calls", [])
        if tool_calls:
            lines.append(f"\nTool calls ({len(tool_calls)}):")
            for tc in tool_calls:
                lines.append(f"  - {tc.get('name', '?')} (id: {tc.get('id', '?')})")

    return "\n".join(lines)


async def _search_audit(
    client: AsyncCockpitClient,
    job_id: str,
    query: str,
    limit: int = 20,
) -> str:
    """Search audit entries for a pattern."""
    query_lower = query.lower()
    matches: list[dict[str, Any]] = []

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
            if _entry_matches(entry, query_lower):
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

        if step_type == "tool_call":
            tool = entry.get("tool", {})
            lines.append(f"[{step_num}] Tool: {tool.get('name', 'unknown')}")
            args = json.dumps(tool.get("arguments", {}))[:150]
            lines.append(f"    Args: {args}")
        elif step_type == "tool_result":
            tool = entry.get("tool", {})
            result = str(entry.get("result", ""))[:150]
            lines.append(f"[{step_num}] Result ({tool.get('name', '')}): {result}")
        elif step_type == "llm_response":
            content = entry.get("content", {})
            text = content.get("text", "")[:150]
            lines.append(f"[{step_num}] LLM: {text}")
        else:
            lines.append(f"[{step_num}] {step_type}")
        lines.append("")

    return "\n".join(lines)


def _entry_matches(entry: dict[str, Any], query: str) -> bool:
    """Check if audit entry matches search query."""
    # Check tool name/args
    tool = entry.get("tool", {})
    if query in tool.get("name", "").lower():
        return True
    if query in json.dumps(tool.get("arguments", {})).lower():
        return True

    # Check result
    result = str(entry.get("result", "")).lower()
    if query in result:
        return True

    # Check LLM content
    content = entry.get("content", {})
    if query in content.get("text", "").lower():
        return True

    # Check error
    if query in str(entry.get("error", "")).lower():
        return True

    return False


# =============================================================================
# Action Tool Formatters
# =============================================================================


def _format_action_result(
    action: str,
    job_id: str,
    result: dict[str, Any],
    **extra: Any,
) -> str:
    """Format a generic action result."""
    status = result.get("status", "unknown")
    lines = [f"Action: {action}", f"Job: {job_id}", f"Status: {status}"]

    if extra.get("feedback"):
        lines.append(f"Feedback: {extra['feedback'][:200]}")
    if extra.get("agent_id"):
        lines.append(f"Agent: {extra['agent_id']}")

    # Include any extra fields from the response
    for key, value in result.items():
        if key == "status":
            continue
        if isinstance(value, str) and len(value) > 300:
            value = value[:300] + "..."
        lines.append(f"{key}: {value}")

    return "\n".join(lines)


def _format_created_job(result: dict[str, Any], config_name: str) -> str:
    """Format the result of creating a new job."""
    job_id = result.get("id", "unknown")
    lines = [
        "Job created successfully.",
        f"Job ID: {job_id}",
        f"Config: {config_name}",
        f"Status: {result.get('status', 'created')}",
    ]

    if result.get("description"):
        desc = result["description"]
        if len(desc) > 200:
            desc = desc[:200] + "..."
        lines.append(f"Description: {desc}")

    lines.append(
        "\nNext step: Use assign_job(job_id, agent_id) to assign this job to an agent."
    )
    return "\n".join(lines)


def _format_datasource_test(datasource_id: str, result: dict[str, Any]) -> str:
    """Format datasource test result."""
    status = result.get("status", "unknown")
    message = result.get("message", "")
    icon = "OK" if status == "ok" else "FAILED"
    lines = [
        f"Datasource test: {icon}",
        f"Datasource ID: {datasource_id}",
        f"Status: {status}",
    ]
    if message:
        lines.append(f"Message: {message}")
    return "\n".join(lines)


def _format_action_error(action: str, job_id: str, error: Exception) -> str:
    """Format an action error."""
    error_msg = str(error)

    # Extract detail from httpx HTTPStatusError
    if hasattr(error, "response"):
        try:
            detail = error.response.json().get("detail", error_msg)  # type: ignore[union-attr]
            error_msg = detail
        except Exception:
            error_msg = f"HTTP {error.response.status_code}: {error_msg}"  # type: ignore[union-attr]

    return f"Action '{action}' failed for job {job_id}:\n{error_msg}"


# =============================================================================
# Git History Formatters
# =============================================================================


def _format_commits(
    job_id: str,
    result: dict[str, Any],
    ref: str = "main",
    since_ref: str | None = None,
) -> str:
    """Format commit list."""
    commits = result.get("commits", [])
    total = result.get("total_commits", len(commits))

    if not commits:
        if since_ref:
            return f"No commits found between {since_ref} and {ref} for job {job_id}."
        return f"No commits found for job {job_id}."

    header = f"Commits for job {job_id}"
    if since_ref:
        header += f" ({since_ref}...{ref})"
    else:
        header += f" (ref: {ref})"
    lines = [f"{header} — {total} commit(s):\n"]

    for c in commits:
        sha_short = c.get("sha", "")[:8]
        message = c.get("message", "").split("\n")[0]  # First line only
        author = c.get("author", "unknown")
        date = c.get("date", "")
        lines.append(f"  {sha_short} {message}")
        if author or date:
            lines.append(f"           {author} — {date}")

    return "\n".join(lines)


def _format_diff(
    job_id: str,
    base: str,
    head: str,
    diff_text: str,
    max_chars: int = 50000,
) -> str:
    """Format diff output with optional truncation."""
    if not diff_text:
        return f"No differences between {base} and {head} for job {job_id}."

    # Count changed files from diff headers
    file_count = diff_text.count("\ndiff --git ")
    if diff_text.startswith("diff --git "):
        file_count += 1

    lines = [
        f"Diff for job {job_id}: {base}...{head}",
        f"Files changed: {file_count}",
        "---",
    ]

    header = "\n".join(lines) + "\n"

    if max_chars > 0 and len(diff_text) > max_chars:
        truncated = diff_text[:max_chars]
        return header + truncated + f"\n\n[truncated — diff exceeds {max_chars} characters]"

    return header + diff_text


def _filter_diff_by_file(diff_text: str, file_path: str) -> str:
    """Extract diff sections for a specific file."""
    sections = diff_text.split("\ndiff --git ")
    if diff_text.startswith("diff --git "):
        # First section doesn't have leading newline
        first = sections[0]
        rest = sections[1:]
    else:
        first = ""
        rest = sections[1:] if len(sections) > 1 else sections

    matching = []
    # Check the first section
    if first and file_path in first.split("\n")[0]:
        matching.append(first)
    # Check remaining sections
    for section in rest:
        header_line = section.split("\n")[0]
        if file_path in header_line:
            matching.append("diff --git " + section)

    if not matching:
        return f"No changes to '{file_path}' in this diff."

    return "\n".join(matching)


def _format_file_listing(
    job_id: str,
    path: str,
    entries: list[dict[str, Any]],
    ref: str | None = None,
) -> str:
    """Format directory listing."""
    if not entries:
        return f"No files found at '{path or '/'}' for job {job_id}."

    ref_label = ref or "HEAD"
    lines = [f"Files in job {job_id} at '{path or '/'}' (ref: {ref_label}):\n"]

    # Sort: directories first, then files
    dirs = [e for e in entries if e.get("type") == "dir"]
    files = [e for e in entries if e.get("type") != "dir"]

    for d in sorted(dirs, key=lambda x: x.get("name", "")):
        lines.append(f"  [dir]  {d['name']}/")

    for f in sorted(files, key=lambda x: x.get("name", "")):
        size = f.get("size", 0)
        if size >= 1024:
            size_str = f"{size / 1024:.1f} KB"
        else:
            size_str = f"{size} B"
        lines.append(f"  [file] {f['name']}  ({size_str})")

    lines.append(f"\nTotal: {len(dirs)} directories, {len(files)} files")
    return "\n".join(lines)


def _format_tags(job_id: str, tags: list[dict[str, Any]]) -> str:
    """Format tag list."""
    if not tags:
        return f"No tags found for job {job_id}."

    lines = [f"Tags for job {job_id} ({len(tags)}):\n"]
    for t in tags:
        name = t.get("name", "")
        sha = t.get("sha", "")[:8]
        message = t.get("message", "")
        line = f"  {name} ({sha})"
        if message:
            line += f" — {message.split(chr(10))[0]}"
        lines.append(line)

    return "\n".join(lines)


def _format_git_error(operation: str, job_id: str, error: Exception) -> str:
    """Format a git operation error."""
    error_msg = str(error)

    if hasattr(error, "response"):
        try:
            detail = error.response.json().get("detail", error_msg)  # type: ignore[union-attr]
            error_msg = detail
        except Exception:
            status = error.response.status_code  # type: ignore[union-attr]
            if status == 503:
                error_msg = "Gitea is not available — git history tools require a running Gitea instance."
            else:
                error_msg = f"HTTP {status}: {error_msg}"

    return f"Failed to {operation} for job {job_id}:\n{error_msg}"


# =============================================================================
# Workspace & Job Context Formatters
# =============================================================================


def _format_frozen_job(job_id: str, data: dict[str, Any]) -> str:
    """Format frozen job review data."""
    lines = [f"Frozen job review: {job_id}\n"]

    if data.get("summary"):
        lines.append(f"Summary:\n{data['summary']}\n")

    if data.get("confidence") is not None:
        conf = data["confidence"]
        if isinstance(conf, (int, float)):
            lines.append(f"Confidence: {conf:.0%}" if conf <= 1 else f"Confidence: {conf}")
        else:
            lines.append(f"Confidence: {conf}")

    if data.get("deliverables"):
        lines.append("Deliverables:")
        for d in data["deliverables"]:
            if isinstance(d, dict):
                name = d.get("name", d.get("path", "unknown"))
                desc = d.get("description", "")
                lines.append(f"  - {name}: {desc}" if desc else f"  - {name}")
            else:
                lines.append(f"  - {d}")
        lines.append("")

    if data.get("notes"):
        lines.append(f"Agent notes:\n{data['notes']}")

    # Include any other top-level fields
    skip = {"summary", "confidence", "deliverables", "notes"}
    for key, value in data.items():
        if key not in skip:
            if isinstance(value, str) and len(value) > 300:
                value = value[:300] + "..."
            lines.append(f"{key}: {value}")

    return "\n".join(lines)


def _format_workspace_overview(job_id: str, data: dict[str, Any]) -> str:
    """Format workspace overview."""
    if not data.get("has_workspace"):
        return f"No workspace found for job {job_id}."

    lines = [f"Workspace overview for job {job_id}\n"]

    # File listing
    files = data.get("files", [])
    if files:
        lines.append(f"Files ({len(files)}):")
        for f in files:
            name = f.get("name", "unknown")
            size = f.get("size", 0)
            if size >= 1024:
                size_str = f"{size / 1024:.1f} KB"
            else:
                size_str = f"{size} B"
            lines.append(f"  {name} ({size_str})")
        lines.append("")

    # workspace.md preview
    ws_md = data.get("workspace_md")
    if ws_md:
        lines.append("=== workspace.md (preview) ===")
        lines.append(ws_md[:1000])
        if len(ws_md) > 1000:
            lines.append("... (truncated)")
        lines.append("")

    # plan.md preview
    plan_md = data.get("plan_md")
    if plan_md:
        lines.append("=== plan.md (preview) ===")
        lines.append(plan_md[:1000])
        if len(plan_md) > 1000:
            lines.append("... (truncated)")
        lines.append("")

    # Todo summary
    todos = data.get("todos")
    if todos:
        todo_list = todos.get("todos", [])
        completed = sum(1 for t in todo_list if t.get("status") == "completed")
        total = len(todo_list)
        lines.append(f"Todos: {completed}/{total} completed")

    archive_count = data.get("archive_count", 0)
    if archive_count:
        lines.append(f"Archived phases: {archive_count}")

    return "\n".join(lines)


def _format_job_progress(job_id: str, data: dict[str, Any]) -> str:
    """Format job progress data."""
    lines = [f"Progress for job {job_id}\n"]

    lines.append(f"Status: {data.get('status', 'unknown')}")

    if data.get("creator_status"):
        lines.append(f"Creator: {data['creator_status']}")
    if data.get("validator_status"):
        lines.append(f"Validator: {data['validator_status']}")

    # Requirements progress
    reqs = data.get("requirements", {})
    if reqs:
        total = reqs.get("total", 0)
        integrated = reqs.get("integrated", 0)
        rejected = reqs.get("rejected", 0)
        pending = reqs.get("pending", 0)
        failed = reqs.get("failed", 0)
        lines.append(f"\nRequirements: {total} total")
        lines.append(f"  Integrated: {integrated}")
        if rejected:
            lines.append(f"  Rejected: {rejected}")
        if pending:
            lines.append(f"  Pending: {pending}")
        if failed:
            lines.append(f"  Failed: {failed}")

    progress_pct = data.get("progress_percent")
    if progress_pct is not None:
        lines.append(f"\nProgress: {progress_pct:.1f}%")

    elapsed = data.get("elapsed_seconds")
    if elapsed is not None:
        mins = int(elapsed) // 60
        secs = int(elapsed) % 60
        lines.append(f"Elapsed: {mins}m {secs}s")

    eta = data.get("eta_seconds")
    if eta is not None:
        eta_mins = int(eta) // 60
        eta_secs = int(eta) % 60
        lines.append(f"ETA: ~{eta_mins}m {eta_secs}s remaining")

    return "\n".join(lines)


def _format_requirements(job_id: str, data: dict[str, Any]) -> str:
    """Format requirements list."""
    reqs = data.get("requirements", [])
    total = data.get("total", len(reqs))

    if not reqs:
        return f"No requirements found for job {job_id}."

    lines = [f"Requirements for job {job_id} ({len(reqs)} of {total}):\n"]

    for r in reqs:
        name = r.get("name", r.get("requirement_id", "unknown"))
        status = r.get("status", "unknown")
        priority = r.get("priority", "")
        text = r.get("text", "")
        if len(text) > 150:
            text = text[:150] + "..."

        status_icon = {
            "integrated": "+",
            "pending": "?",
            "validating": "~",
            "rejected": "x",
            "failed": "!",
        }.get(status, " ")

        line = f"  [{status_icon}] {name}"
        if priority:
            line += f" (priority: {priority})"
        lines.append(line)
        if text:
            lines.append(f"      {text}")

    return "\n".join(lines)


def _format_workspace_error(operation: str, job_id: str, error: Exception) -> str:
    """Format a workspace operation error."""
    error_msg = str(error)

    if hasattr(error, "response"):
        try:
            detail = error.response.json().get("detail", error_msg)  # type: ignore[union-attr]
            error_msg = detail
        except Exception:
            error_msg = f"HTTP {error.response.status_code}: {error_msg}"  # type: ignore[union-attr]

    return f"Failed to {operation} for job {job_id}:\n{error_msg}"


# =============================================================================
# Monitoring & Database Formatters
# =============================================================================


def _format_job_stats(data: dict[str, Any]) -> str:
    """Format job queue statistics."""
    total = data.get("total", 0)
    lines = [f"Job Statistics (total: {total})\n"]

    for status in ("created", "processing", "pending_review", "completed", "failed", "cancelled"):
        count = data.get(status, 0)
        if count or status in ("created", "processing", "completed"):
            lines.append(f"  {status}: {count}")

    return "\n".join(lines)


def _format_agent_stats(data: dict[str, Any]) -> str:
    """Format agent workforce summary."""
    total = data.get("total", 0)
    lines = [f"Agent Statistics (total: {total})\n"]

    for status in ("ready", "working", "booting", "completed", "offline", "failed"):
        count = data.get(status, 0)
        if count or status in ("ready", "working", "offline"):
            lines.append(f"  {status}: {count}")

    return "\n".join(lines)


def _format_stuck_jobs(data: list[dict[str, Any]], threshold: int) -> str:
    """Format stuck jobs list."""
    if not data:
        return f"No stuck jobs found (threshold: {threshold} minutes)."

    lines = [f"Stuck jobs ({len(data)}, threshold: {threshold} min):\n"]
    for job in data:
        job_id = job.get("id", job.get("job_id", "unknown"))
        status = job.get("status", "unknown")
        updated = job.get("updated_at", "unknown")
        component = job.get("component", "")
        reason = job.get("reason", "")

        lines.append(f"  {job_id}")
        lines.append(f"    Status: {status}")
        lines.append(f"    Last update: {updated}")
        if component:
            lines.append(f"    Component: {component}")
        if reason:
            lines.append(f"    Reason: {reason}")

    return "\n".join(lines)


def _format_agents(agents: list[dict[str, Any]], status_filter: str | None = None) -> str:
    """Format agent list."""
    if not agents:
        filter_msg = f" with status '{status_filter}'" if status_filter else ""
        return f"No agents found{filter_msg}."

    lines = [f"Agents ({len(agents)}):\n"]
    for a in agents:
        agent_id = str(a.get("id", "unknown"))[:8]
        status = a.get("status", "unknown")
        config = a.get("config_name", a.get("config", "N/A"))
        hostname = a.get("hostname", "")
        current_job = a.get("current_job_id", "")
        heartbeat = a.get("last_heartbeat", "")

        icon = {
            "ready": "OK", "working": ">>", "booting": "..",
            "offline": "--", "failed": "!!", "completed": "++",
        }.get(status, "??")

        lines.append(f"  [{icon}] {agent_id}  ({status})")
        if config:
            lines.append(f"       Config: {config}")
        if hostname:
            lines.append(f"       Host: {hostname}")
        if current_job:
            lines.append(f"       Job: {current_job}")
        if heartbeat:
            lines.append(f"       Heartbeat: {heartbeat}")

    return "\n".join(lines)


def _format_experts(experts: list[dict[str, Any]]) -> str:
    """Format expert config list."""
    if not experts:
        return "No expert configurations found."

    lines = [f"Expert Configurations ({len(experts)}):\n"]
    for e in experts:
        eid = e.get("id", "unknown")
        name = e.get("display_name", eid)
        desc = e.get("description", "")
        tags = e.get("tags", [])

        lines.append(f"  {eid}: {name}")
        if desc:
            lines.append(f"    {desc}")
        if tags:
            lines.append(f"    Tags: {', '.join(tags)}")

    return "\n".join(lines)


def _format_expert_detail(expert_id: str, data: dict[str, Any]) -> str:
    """Format expert config detail."""
    lines = [f"Expert: {expert_id}\n"]

    if data.get("display_name"):
        lines.append(f"Name: {data['display_name']}")
    if data.get("description"):
        lines.append(f"Description: {data['description']}")
    if data.get("tags"):
        lines.append(f"Tags: {', '.join(data['tags'])}")

    # Config section
    config = data.get("config", {})
    if config:
        llm = config.get("llm")
        if llm:
            lines.append(f"\nLLM: {llm.get('model', 'N/A')}")
            if llm.get("base_url"):
                lines.append(f"  Base URL: {llm['base_url']}")

        tools = config.get("tools", {})
        if tools:
            lines.append("\nTools:")
            for category, tool_list in tools.items():
                if tool_list:
                    lines.append(f"  {category}: {', '.join(tool_list)}")

    # Instructions
    instructions = data.get("instructions")
    if instructions:
        preview = instructions[:1000]
        if len(instructions) > 1000:
            preview += "\n... (truncated)"
        lines.append(f"\n=== Instructions ===\n{preview}")

    return "\n".join(lines)


def _format_datasources(
    datasources: list[dict[str, Any]], type_filter: str | None = None
) -> str:
    """Format datasource list."""
    if not datasources:
        filter_msg = f" of type '{type_filter}'" if type_filter else ""
        return f"No datasources found{filter_msg}."

    lines = [f"Datasources ({len(datasources)}):\n"]
    for ds in datasources:
        ds_id = str(ds.get("id", "unknown"))[:8]
        name = ds.get("name", "unknown")
        ds_type = ds.get("type", "unknown")
        read_only = ds.get("read_only", True)
        job_id = ds.get("job_id")
        scope = "job-scoped" if job_id else "global"

        lines.append(f"  {ds_id}  {name} ({ds_type})")
        lines.append(f"    Scope: {scope}  Read-only: {read_only}")
        if job_id:
            lines.append(f"    Job: {job_id}")

    return "\n".join(lines)


def _format_tables(tables: list[dict[str, Any]]) -> str:
    """Format database table list."""
    if not tables:
        return "No tables found."

    lines = ["Database Tables:\n"]
    for t in tables:
        name = t.get("table_name", t.get("name", "unknown"))
        count = t.get("row_count", t.get("count", "?"))
        lines.append(f"  {name}: {count} rows")

    return "\n".join(lines)


def _format_table_data(table_name: str, data: dict[str, Any]) -> str:
    """Format paginated table data."""
    rows = data.get("data", data.get("rows", []))
    total = data.get("total", len(rows))
    page = data.get("page", 1)
    has_more = data.get("hasMore", False)

    if not rows:
        return f"No data in table '{table_name}'."

    lines = [f"Table: {table_name} (page {page}, {len(rows)} of {total} rows)\n"]

    for row in rows[:20]:  # Cap display at 20 rows
        row_parts = []
        for key, value in row.items():
            val_str = str(value)
            if len(val_str) > 80:
                val_str = val_str[:80] + "..."
            row_parts.append(f"{key}={val_str}")
        lines.append("  " + ", ".join(row_parts))

    if len(rows) > 20:
        lines.append(f"  ... ({len(rows) - 20} more rows)")
    if has_more:
        lines.append(f"\n... more rows available (total: {total})")

    return "\n".join(lines)


def _format_table_schema(table_name: str, columns: list[dict[str, Any]]) -> str:
    """Format table schema."""
    if not columns:
        return f"No schema found for table '{table_name}'."

    lines = [f"Schema for table '{table_name}':\n"]
    for col in columns:
        name = col.get("column_name", col.get("name", "unknown"))
        dtype = col.get("data_type", col.get("type", "unknown"))
        nullable = col.get("is_nullable", col.get("nullable", ""))
        default = col.get("column_default", col.get("default", ""))

        line = f"  {name}: {dtype}"
        if nullable == "NO" or nullable is False:
            line += " NOT NULL"
        if default:
            line += f" DEFAULT {default}"
        lines.append(line)

    return "\n".join(lines)


def _format_monitoring_error(operation: str, error: Exception) -> str:
    """Format a monitoring operation error."""
    error_msg = str(error)

    if hasattr(error, "response"):
        try:
            detail = error.response.json().get("detail", error_msg)  # type: ignore[union-attr]
            error_msg = detail
        except Exception:
            error_msg = f"HTTP {error.response.status_code}: {error_msg}"  # type: ignore[union-attr]

    return f"Failed to {operation}:\n{error_msg}"


# =============================================================================
# Citation & Source Library Formatters
# =============================================================================


def _format_sources(
    data: dict[str, Any],
    job_id: str | None = None,
    type_filter: str | None = None,
) -> str:
    """Format source list."""
    sources = data.get("sources", [])
    total = data.get("total", len(sources))

    if not sources:
        scope = f" for job {job_id}" if job_id else ""
        filter_msg = f" of type '{type_filter}'" if type_filter else ""
        return f"No sources found{scope}{filter_msg}."

    scope = f" for job {job_id}" if job_id else " (all jobs)"
    lines = [f"Sources{scope} ({len(sources)} of {total}):\n"]

    for s in sources:
        sid = s.get("id", "?")
        stype = s.get("type", "unknown")
        name = s.get("name", "unknown")
        identifier = s.get("identifier", "")
        preview = s.get("content_preview", "")
        if preview and len(preview) > 100:
            preview = preview[:100] + "..."

        lines.append(f"  [{sid}] {name} ({stype})")
        if identifier:
            lines.append(f"      Identifier: {identifier}")
        if preview:
            lines.append(f"      Preview: {preview}")
        if s.get("job_ids"):
            lines.append(f"      Jobs: {', '.join(s['job_ids'][:5])}")

    return "\n".join(lines)


def _format_source_detail(data: dict[str, Any]) -> str:
    """Format single source detail."""
    sid = data.get("id", "?")
    lines = [f"Source #{sid}\n"]

    lines.append(f"Type: {data.get('type', 'unknown')}")
    lines.append(f"Name: {data.get('name', 'unknown')}")
    lines.append(f"Identifier: {data.get('identifier', 'N/A')}")

    if data.get("version"):
        lines.append(f"Version: {data['version']}")
    if data.get("content_hash"):
        lines.append(f"Content hash: {data['content_hash'][:16]}...")

    full_len = data.get("full_content_length", 0)
    lines.append(f"Content length: {full_len} chars")

    if data.get("content_truncated"):
        lines.append("(content truncated)")

    if data.get("job_ids"):
        lines.append(f"Linked jobs: {', '.join(data['job_ids'])}")

    if data.get("metadata"):
        meta = data["metadata"]
        if isinstance(meta, dict):
            meta_str = json.dumps(meta, indent=2, default=str)
            if len(meta_str) > 500:
                meta_str = meta_str[:500] + "\n..."
            lines.append(f"\nMetadata:\n{meta_str}")

    content = data.get("content", "")
    if content:
        lines.append(f"\n=== Content ===\n{content}")

    return "\n".join(lines)


def _format_citations(
    job_id: str,
    data: dict[str, Any],
    status_filter: str | None = None,
) -> str:
    """Format citation list."""
    citations = data.get("citations", [])
    total = data.get("total", len(citations))

    if not citations:
        filter_msg = f" with status '{status_filter}'" if status_filter else ""
        return f"No citations found for job {job_id}{filter_msg}."

    lines = [f"Citations for job {job_id} ({len(citations)} of {total}):\n"]

    for c in citations:
        cid = c.get("id", "?")
        claim = c.get("claim", "")
        status = c.get("verification_status", "unknown")
        confidence = c.get("confidence", "")
        source_name = c.get("source_name", "unknown")
        source_id = c.get("source_id", "?")
        method = c.get("extraction_method", "")
        score = c.get("similarity_score")

        status_icon = {
            "verified": "+",
            "pending": "?",
            "failed": "x",
            "unverified": "~",
        }.get(status, " ")

        lines.append(f"  [{status_icon}] #{cid}: {claim}")
        lines.append(f"      Source: [{source_id}] {source_name}")
        parts = [f"Status: {status}", f"Confidence: {confidence}"]
        if method:
            parts.append(f"Method: {method}")
        if score is not None:
            parts.append(f"Score: {score:.3f}")
        lines.append(f"      {', '.join(parts)}")

    return "\n".join(lines)


def _format_citation_detail(data: dict[str, Any]) -> str:
    """Format full citation detail."""
    cid = data.get("id", "?")
    lines = [f"Citation #{cid}\n"]

    lines.append(f"Job: {data.get('job_id', 'N/A')}")
    lines.append(f"Status: {data.get('verification_status', 'unknown')}")
    lines.append(f"Confidence: {data.get('confidence', 'N/A')}")
    lines.append(f"Extraction method: {data.get('extraction_method', 'N/A')}")

    if data.get("similarity_score") is not None:
        lines.append(f"Similarity score: {data['similarity_score']:.3f}")

    # Source info
    lines.append(f"\nSource: [{data.get('source_id', '?')}] {data.get('source_name', 'unknown')}")
    lines.append(f"Source type: {data.get('source_type', 'N/A')}")
    if data.get("source_identifier"):
        lines.append(f"Source identifier: {data['source_identifier']}")

    # Claim and quote
    lines.append(f"\n=== Claim ===\n{data.get('claim', 'N/A')}")

    if data.get("verbatim_quote"):
        lines.append(f"\n=== Verbatim Quote ===\n{data['verbatim_quote']}")

    if data.get("quote_context"):
        ctx = data["quote_context"]
        if len(ctx) > 500:
            ctx = ctx[:500] + "..."
        lines.append(f"\n=== Quote Context ===\n{ctx}")

    if data.get("quote_language"):
        lines.append(f"Language: {data['quote_language']}")

    # Locator
    locator = data.get("locator")
    if locator and isinstance(locator, dict):
        loc_parts = []
        for key in ("page", "section", "paragraph", "line", "marginal_number"):
            if locator.get(key) is not None:
                loc_parts.append(f"{key}: {locator[key]}")
        if loc_parts:
            lines.append(f"\nLocator: {', '.join(loc_parts)}")

    # Verification details
    if data.get("verification_notes"):
        lines.append(f"\n=== Verification Notes ===\n{data['verification_notes']}")

    if data.get("matched_location") and isinstance(data["matched_location"], dict):
        lines.append(f"Matched location: {json.dumps(data['matched_location'], default=str)}")

    if data.get("relevance_reasoning"):
        reasoning = data["relevance_reasoning"]
        if len(reasoning) > 300:
            reasoning = reasoning[:300] + "..."
        lines.append(f"\nRelevance reasoning: {reasoning}")

    if data.get("created_by"):
        lines.append(f"\nCreated by: {data['created_by']}")

    return "\n".join(lines)


def _format_source_search(data: dict[str, Any]) -> str:
    """Format source search results."""
    results = data.get("results", [])
    query = data.get("query", "")
    mode = data.get("mode", "keyword")
    total = data.get("total", len(results))

    if not results:
        return f"No sources matching '{query}' found."

    lines = [
        f"Search results for '{query}' (mode: {mode}, {total} match(es)):\n"
    ]

    for r in results:
        sid = r.get("source_id", "?")
        name = r.get("source_name", "unknown")
        stype = r.get("source_type", "")
        evidence = r.get("evidence_label", "")
        rank = r.get("rank", 0)
        snippet = r.get("snippet", "")

        lines.append(f"  [{evidence}] [{sid}] {name} ({stype})")
        lines.append(f"      Rank: {rank:.4f}")
        if snippet:
            # Clean up HTML tags from ts_headline
            clean = snippet.replace("<b>", "**").replace("</b>", "**")
            lines.append(f"      Snippet: {clean}")

    return "\n".join(lines)


def _format_annotations(
    job_id: str,
    source_id: int,
    annotations: list[dict[str, Any]],
    type_filter: str | None = None,
) -> str:
    """Format source annotations."""
    if not annotations:
        filter_msg = f" of type '{type_filter}'" if type_filter else ""
        return f"No annotations found for source {source_id} in job {job_id}{filter_msg}."

    lines = [f"Annotations for source {source_id} in job {job_id} ({len(annotations)}):\n"]

    for a in annotations:
        atype = a.get("annotation_type", "note")
        content = a.get("content", "")
        page_ref = a.get("page_reference", "")

        icon = {
            "note": "N",
            "highlight": "H",
            "summary": "S",
            "question": "Q",
            "critique": "C",
        }.get(atype, "?")

        header = f"  [{icon}] {atype}"
        if page_ref:
            header += f" (page: {page_ref})"
        lines.append(header)

        if len(content) > 200:
            content = content[:200] + "..."
        lines.append(f"      {content}")

    return "\n".join(lines)


def _format_source_tags(job_id: str, source_id: int, tags: list[str]) -> str:
    """Format source tags."""
    if not tags:
        return f"No tags found for source {source_id} in job {job_id}."

    return f"Tags for source {source_id} in job {job_id}: {', '.join(tags)}"


def _format_citation_stats(job_id: str, data: dict[str, Any]) -> str:
    """Format citation statistics."""
    lines = [f"Citation Statistics for job {job_id}\n"]

    # Sources
    total_sources = data.get("total_sources", 0)
    lines.append(f"Sources: {total_sources} total")
    by_type = data.get("sources_by_type", {})
    for stype, count in sorted(by_type.items()):
        lines.append(f"  {stype}: {count}")

    # Citations
    total_citations = data.get("total_citations", 0)
    lines.append(f"\nCitations: {total_citations} total")

    by_status = data.get("citations_by_verification_status", {})
    if by_status:
        lines.append("  By verification status:")
        for status in ("verified", "pending", "failed", "unverified"):
            count = by_status.get(status, 0)
            if count:
                icon = {"verified": "+", "pending": "?", "failed": "x", "unverified": "~"}.get(
                    status, " "
                )
                lines.append(f"    [{icon}] {status}: {count}")

    by_confidence = data.get("citations_by_confidence", {})
    if by_confidence:
        lines.append("  By confidence:")
        for level in ("high", "medium", "low"):
            count = by_confidence.get(level, 0)
            if count:
                lines.append(f"    {level}: {count}")

    by_method = data.get("citations_by_extraction_method", {})
    if by_method:
        lines.append("  By extraction method:")
        for method, count in sorted(by_method.items()):
            lines.append(f"    {method}: {count}")

    return "\n".join(lines)


def _format_citation_error(
    operation: str, error: Exception, job_id: str | None = None
) -> str:
    """Format a citation operation error."""
    error_msg = str(error)

    if hasattr(error, "response"):
        try:
            detail = error.response.json().get("detail", error_msg)  # type: ignore[union-attr]
            error_msg = detail
        except Exception:
            error_msg = f"HTTP {error.response.status_code}: {error_msg}"  # type: ignore[union-attr]

    scope = f" for job {job_id}" if job_id else ""
    return f"Failed to {operation}{scope}:\n{error_msg}"
