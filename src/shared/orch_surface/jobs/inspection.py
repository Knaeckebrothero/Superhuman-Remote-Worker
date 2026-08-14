"""Canonical job-inspection handlers."""

from __future__ import annotations

import asyncio
import json
from typing import Literal

from .. import formatters as fmt
from ..client import AsyncCockpitClient
from ._utils import resolve_job_id
from .descriptors import CallerCtx, descriptor

_ALL = frozenset({"mcp", "session", "officer"})
_MCP = frozenset({"mcp"})
_MCP_OFFICER = frozenset({"mcp", "officer"})
_EXPLICIT_GATE = (
    "named explicitly by the caller configuration; broader S5 defaults remain pending"
)


@descriptor(group="job_inspection", plane="observability", caller_defaults=_ALL)
async def list_jobs(
    client: AsyncCockpitClient,
    caller: CallerCtx,
    status: Literal[
        "created",
        "processing",
        "completed",
        "failed",
        "cancelled",
        "pending_review",
        "paused",
    ]
    | None = None,
    limit: int = 20,
) -> str:
    """List agent jobs with optional status filter.

    Returns job ID, status, config name, timestamps, and audit entry count.
    Use this to find jobs to investigate.

    Args:
        status: Filter by lifecycle status, including pending_review and paused
        limit: Maximum jobs to return (1-100, default 20)

    Returns:
        Formatted list of jobs with ID, status, config, timestamps
    """
    limit = min(max(limit, 1), 100)
    jobs = await client.list_jobs(status=status, limit=limit)
    return fmt.format_jobs(jobs)


@descriptor(group="job_inspection", plane="observability", caller_defaults=_ALL)
async def get_job(client: AsyncCockpitClient, caller: CallerCtx, job_id: str) -> str:
    """Get detailed information about a specific job by ID.

    Returns full job details including description, config, status,
    timestamps, and audit count.

    Args:
        job_id: Job UUID to retrieve

    Returns:
        Formatted job details
    """
    resolved = await resolve_job_id(client, caller, job_id)
    job = await client.get_job(resolved)
    return fmt.format_job_detail(job)


@descriptor(
    group="job_inspection",
    plane="observability",
    caller_defaults=_MCP,
    grant="explicit",
    gate=_EXPLICIT_GATE,
)
async def get_audit_trail(
    client: AsyncCockpitClient,
    caller: CallerCtx,
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
    page_size = min(max(page_size, 1), 200)
    resolved = await resolve_job_id(client, caller, job_id)
    audit = await client.get_audit_trail(
        job_id=resolved,
        page=page,
        page_size=page_size,
        filter_category=filter,
    )
    return fmt.format_audit(audit)


@descriptor(
    group="job_inspection",
    plane="observability",
    caller_defaults=_MCP,
    grant="explicit",
    gate=_EXPLICIT_GATE,
)
async def get_audit_bulk(
    client: AsyncCockpitClient,
    caller: CallerCtx,
    job_id: str,
    offset: int = 0,
    limit: int = 200,
    filter: Literal["all", "messages", "tools", "errors"] = "all",
) -> str:
    """Get audit entries in chunks using offset/limit pagination.

    Better than page-based audit trail for scanning large histories. Returns a
    lean projection: LLM messages, tool calls with their results, and errors.
    Per-call tool arguments and full tracebacks are omitted to keep large scans
    cheap. Supports up to 200 entries per request.

    Args:
        job_id: Job UUID to get audit for
        offset: Number of entries to skip (default: 0)
        limit: Maximum entries to return (max 200, default 200)
        filter: Filter category (all, messages, tools, errors)

    Returns:
        Formatted audit entries with offset metadata
    """
    limit = min(max(limit, 1), 200)
    resolved = await resolve_job_id(client, caller, job_id)
    data = await client.get_audit_bulk(
        job_id=resolved,
        offset=offset,
        limit=limit,
        filter_category=filter,
    )
    return fmt.format_audit_bulk(data)


@descriptor(
    group="job_inspection",
    plane="observability",
    caller_defaults=_MCP,
    grant="explicit",
    gate=_EXPLICIT_GATE,
)
async def get_chat_history(
    client: AsyncCockpitClient,
    caller: CallerCtx,
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
    page_size = min(max(page_size, 1), 200)
    resolved = await resolve_job_id(client, caller, job_id)
    chat = await client.get_chat_history(
        job_id=resolved,
        page=page,
        page_size=page_size,
    )
    return fmt.format_chat_history(chat)


@descriptor(
    group="job_inspection",
    plane="observability",
    caller_defaults=_MCP,
    grant="explicit",
    gate=_EXPLICIT_GATE,
)
async def get_chat_bulk(
    client: AsyncCockpitClient,
    caller: CallerCtx,
    job_id: str,
    offset: int = 0,
    limit: int = 200,
) -> str:
    """Get chat history in chunks using offset/limit pagination.

    Better than page-based chat history for scanning large conversations.
    Supports up to 200 entries per request.

    Args:
        job_id: Job UUID to get chat history for
        offset: Number of entries to skip (default: 0)
        limit: Maximum entries to return (max 200, default 200)

    Returns:
        Formatted chat turns with offset metadata
    """
    limit = min(max(limit, 1), 200)
    resolved = await resolve_job_id(client, caller, job_id)
    data = await client.get_chat_bulk(job_id=resolved, offset=offset, limit=limit)
    return fmt.format_chat_bulk(data)


@descriptor(
    group="job_inspection",
    plane="observability",
    caller_defaults=_MCP,
    grant="explicit",
    gate=_EXPLICIT_GATE,
)
async def get_todos(client: AsyncCockpitClient, caller: CallerCtx, job_id: str) -> str:
    """Get all todos for a job including current active todos and archives.

    Shows task planning and execution progress across phases. Gitea-backed:
    committed state as of the worker's last phase-boundary push.

    Args:
        job_id: Job UUID to get todos for

    Returns:
        Formatted todos with current and archived phases
    """
    resolved = await resolve_job_id(client, caller, job_id)
    todos = await client.get_todos(resolved)
    return fmt.format_todos(todos)


@descriptor(
    group="job_inspection",
    plane="observability",
    caller_defaults=_MCP,
    grant="explicit",
    gate=_EXPLICIT_GATE,
)
async def get_llm_request(
    client: AsyncCockpitClient, caller: CallerCtx, doc_id: str
) -> str:
    """Get full LLM request/response by its audit-store request ID.

    Returns complete message history, model response, and token usage.
    Use document IDs from audit trail entries.

    Args:
        doc_id: Audit-store request ID (string)

    Returns:
        Formatted LLM request with messages and response
    """
    request = await client.get_llm_request(doc_id)
    return fmt.format_llm_request(request)


@descriptor(
    group="job_inspection",
    plane="observability",
    caller_defaults=_MCP,
    grant="explicit",
    gate=_EXPLICIT_GATE,
)
async def get_job_summary(
    client: AsyncCockpitClient, caller: CallerCtx, job_id: str
) -> str:
    """Get a comprehensive one-shot summary of a job.

    Fetches status, progress, todos, workspace overview, and recent tool
    calls in parallel. Returns everything in a single response — ideal for
    understanding a job's current state without multiple tool calls. The
    todos and workspace sections are Gitea-backed: committed state as of
    the worker's last phase-boundary push.

    Args:
        job_id: Job UUID to summarize

    Returns:
        Combined summary with status, progress, todos, workspace, and recent activity
    """
    resolved = await resolve_job_id(client, caller, job_id)
    results = await asyncio.gather(
        client.get_job(resolved),
        client.get_job_progress(resolved),
        client.get_todos(resolved),
        client.get_workspace_overview(resolved),
        client.get_audit_trail(
            resolved, page=-1, page_size=10, filter_category="tools"
        ),
        return_exceptions=True,
    )
    return fmt.format_job_summary(*results)


@descriptor(
    group="job_inspection",
    plane="observability",
    caller_defaults=_MCP,
    grant="explicit",
    gate=_EXPLICIT_GATE,
)
async def search_audit(
    client: AsyncCockpitClient,
    caller: CallerCtx,
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
    limit = min(max(limit, 1), 100)
    resolved = await resolve_job_id(client, caller, job_id)
    query_lower = query.lower()
    matches: list[dict] = []
    page = 1
    while len(matches) < limit:
        audit = await client.get_audit_trail(
            job_id=resolved,
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
            lines.append(f"[{step_num}] Tool: {tool.get('name', 'unknown')}")
            lines.append(f"    Args: {json.dumps(tool.get('arguments', {}))[:150]}")
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
            lines.append(f"[{step_num}] ERROR: {entry.get('error', 'Unknown error')}")
        else:
            lines.append(f"[{step_num}] {step_type}")
        lines.append("")
    return "\n".join(lines)


@descriptor(
    group="job_inspection",
    plane="object",
    caller_defaults=_MCP,
    grant="explicit",
    gate=_EXPLICIT_GATE,
)
async def list_job_commits(
    client: AsyncCockpitClient,
    caller: CallerCtx,
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
    limit = min(max(limit, 1), 100)
    resolved = await resolve_job_id(client, caller, job_id)
    try:
        result = await client.list_job_commits(
            resolved, sha=ref, since_ref=since_ref, page=page, limit=limit
        )
        return fmt.format_commits(resolved, result, ref=ref, since_ref=since_ref)
    except Exception as error:
        return fmt.format_git_error("list commits", resolved, error)


@descriptor(
    group="job_inspection",
    plane="object",
    caller_defaults=_MCP,
    grant="explicit",
    gate=_EXPLICIT_GATE,
)
async def get_job_diff(
    client: AsyncCockpitClient,
    caller: CallerCtx,
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
    resolved = await resolve_job_id(client, caller, job_id)
    try:
        result = await client.get_job_diff(resolved, base=base, head=head)
        diff_text = result.get("diff", "")
        if file_path and diff_text:
            diff_text = fmt.filter_diff_by_file(diff_text, file_path)
        return fmt.format_diff(resolved, base, head, diff_text, max_chars=max_chars)
    except Exception as error:
        return fmt.format_git_error("get diff", resolved, error)


@descriptor(group="job_inspection", plane="object", caller_defaults=_ALL)
async def get_job_file(
    client: AsyncCockpitClient,
    caller: CallerCtx,
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
    resolved = await resolve_job_id(client, caller, job_id)
    try:
        result = await client.get_job_file(resolved, path=file_path, ref=ref)
        content = result.get("content", "")
        ref_label = ref or "HEAD"
        size = result.get("size", len(content))
        return f"File: {file_path} (ref: {ref_label}, {size} bytes)\n---\n{content}"
    except Exception as error:
        return fmt.format_git_error(f"read file '{file_path}'", resolved, error)


@descriptor(group="job_inspection", plane="object", caller_defaults=_ALL)
async def list_job_files(
    client: AsyncCockpitClient,
    caller: CallerCtx,
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
    resolved = await resolve_job_id(client, caller, job_id)
    try:
        entries = await client.list_job_files(resolved, path=path, ref=ref)
        return fmt.format_file_listing(resolved, path, entries, ref=ref)
    except Exception as error:
        return fmt.format_git_error(f"list files at '{path or '/'}'", resolved, error)


@descriptor(
    group="job_inspection",
    plane="observability",
    caller_defaults=_MCP,
    grant="explicit",
    gate=_EXPLICIT_GATE,
)
async def get_frozen_job(
    client: AsyncCockpitClient, caller: CallerCtx, job_id: str
) -> str:
    """Get the frozen job review data including summary, confidence, and deliverables.

    Returns the agent's self-assessment when it froze the job for review.
    The job must be in 'pending_review' status (or the frozen data must still exist).

    Args:
        job_id: Job UUID

    Returns:
        Frozen job summary with confidence score, deliverables, and agent notes
    """
    resolved = await resolve_job_id(client, caller, job_id)
    try:
        data = await client.get_frozen_job(resolved)
        return fmt.format_frozen_job(resolved, data)
    except Exception as error:
        return fmt.format_workspace_error("get frozen job data", resolved, error)


@descriptor(
    group="job_inspection",
    plane="object",
    caller_defaults=_MCP,
    grant="explicit",
    gate=_EXPLICIT_GATE,
)
async def get_workspace_overview(
    client: AsyncCockpitClient, caller: CallerCtx, job_id: str
) -> str:
    """Get a summary of the workspace state from the job's Gitea repo.

    Returns the repo-root file listing, truncated workspace.md/plan.md
    previews when present, current todo counts, and archive count — all
    committed state as of the worker's last phase-boundary push (mid-phase
    edits are not visible yet).

    Args:
        job_id: Job UUID

    Returns:
        Workspace overview with file list, content previews, and statistics
    """
    resolved = await resolve_job_id(client, caller, job_id)
    try:
        data = await client.get_workspace_overview(resolved)
        return fmt.format_workspace_overview(resolved, data)
    except Exception as error:
        return fmt.format_workspace_error("get workspace overview", resolved, error)


@descriptor(
    group="job_inspection",
    plane="observability",
    caller_defaults=_MCP,
    grant="explicit",
    gate=_EXPLICIT_GATE,
)
async def get_job_progress(
    client: AsyncCockpitClient, caller: CallerCtx, job_id: str
) -> str:
    """Get detailed job progress including phase information and ETA.

    Shows current status, requirement completion stats, elapsed time,
    and estimated time remaining.

    Args:
        job_id: Job UUID

    Returns:
        Progress data with phase info and completion statistics
    """
    resolved = await resolve_job_id(client, caller, job_id)
    try:
        data = await client.get_job_progress(resolved)
        return fmt.format_job_progress(resolved, data)
    except Exception as error:
        return fmt.format_workspace_error("get job progress", resolved, error)


@descriptor(
    group="job_inspection",
    plane="observability",
    caller_defaults=_MCP_OFFICER,
    grant="explicit",
    gate=_EXPLICIT_GATE,
)
async def get_stuck_jobs(
    client: AsyncCockpitClient,
    caller: CallerCtx,
    threshold_minutes: int = 30,
) -> str:
    """Get jobs stuck in processing beyond a threshold.

    A job is considered stuck if it's in 'processing' status but hasn't
    been updated within the threshold period.

    Args:
        threshold_minutes: Minutes after which a job is considered stuck (default: 30)

    Returns:
        List of stuck jobs with details and last update time
    """
    threshold_minutes = min(max(threshold_minutes, 1), 1440)
    try:
        data = await client.get_stuck_jobs(threshold_minutes)
        return fmt.format_stuck_jobs(data, threshold_minutes)
    except Exception as error:
        return fmt.format_monitoring_error("get stuck jobs", error)


@descriptor(
    group="job_inspection",
    plane="observability",
    caller_defaults=_MCP,
    grant="explicit",
    gate=_EXPLICIT_GATE,
)
async def get_job_log(
    client: AsyncCockpitClient,
    caller: CallerCtx,
    job_id: str,
    lines: int = 100,
    grep: str | None = None,
    level: str | None = None,
) -> str:
    """Read the tail of a job's log file with optional filtering.

    Returns the last N lines of the log file, optionally filtered by log
    level and/or grep pattern. Useful for diagnosing agent errors. Works
    after the agent pod is gone too: falls back to the S3 log archive
    written at pod deletion, scoped to this job's lines.

    Args:
        job_id: Job UUID
        lines: Number of tail lines to return (max 1000, default 100)
        grep: Case-insensitive substring filter
        level: Log level filter (DEBUG, INFO, WARNING, ERROR)

    Returns:
        Formatted log output with line count and filter info
    """
    lines = min(max(lines, 1), 1000)
    resolved = await resolve_job_id(client, caller, job_id)
    try:
        data = await client.get_job_logs(
            job_id=resolved, lines=lines, grep=grep, level=level
        )
        return fmt.format_job_log(resolved, data)
    except Exception as error:
        return fmt.format_workspace_error("get job log", resolved, error)


@descriptor(
    group="job_inspection",
    plane="observability",
    caller_defaults=_MCP,
    grant="explicit",
    gate=_EXPLICIT_GATE,
)
async def list_llm_requests(
    client: AsyncCockpitClient,
    caller: CallerCtx,
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
    limit = min(max(limit, 1), 100)
    resolved = await resolve_job_id(client, caller, job_id)
    try:
        data = await client.list_llm_requests(
            job_id=resolved, limit=limit, offset=offset
        )
        return fmt.format_llm_requests(resolved, data)
    except Exception as error:
        return fmt.format_workspace_error("list LLM requests", resolved, error)


@descriptor(
    group="job_inspection",
    plane="object",
    caller_defaults=_MCP,
    grant="explicit",
    gate=_EXPLICIT_GATE,
)
async def get_shell_state(
    client: AsyncCockpitClient, caller: CallerCtx, job_id: str
) -> str:
    """Get shell tab state from the agent processing a job.

    Returns the list of open terminal tabs with their type (ssh, repl,
    claude-code, etc.) and recent output. Requires the job to be actively
    processing on an agent.

    Args:
        job_id: Job UUID (must be in 'processing' status)

    Returns:
        Shell state with tab names, types, and recent output
    """
    resolved = await resolve_job_id(client, caller, job_id)
    try:
        data = await client.get_shell_state(resolved)
        return fmt.format_shell_state(resolved, data)
    except Exception as error:
        error_msg = str(error)
        if hasattr(error, "response"):
            try:
                error_msg = error.response.json().get("detail", error_msg)
            except Exception:
                error_msg = f"HTTP {error.response.status_code}: {error_msg}"
        return f"Failed to get shell state for job {resolved}:\n{error_msg}"


@descriptor(
    group="job_inspection",
    plane="observability",
    caller_defaults=_MCP,
    grant="explicit",
    gate=_EXPLICIT_GATE,
)
async def get_current_todos(
    client: AsyncCockpitClient, caller: CallerCtx, job_id: str
) -> str:
    """Get only the current active todos from todos.yaml.

    Lighter than get_todos which includes all archives. Shows pending,
    in-progress, and completed items with a progress summary.

    Args:
        job_id: Job UUID to get current todos for

    Returns:
        Formatted current todos with progress count
    """
    resolved = await resolve_job_id(client, caller, job_id)
    data = await client.get_current_todos(resolved)
    if data is None:
        return f"No current todos found for job {resolved}."
    return fmt.format_current_todos(data)


@descriptor(
    group="job_inspection",
    plane="observability",
    caller_defaults=_MCP,
    grant="explicit",
    gate=_EXPLICIT_GATE,
)
async def list_todo_archives(
    client: AsyncCockpitClient, caller: CallerCtx, job_id: str
) -> str:
    """List all archived todo files for a job.

    Returns metadata for each phase archive (filename, phase name, timestamp).
    Use get_todo_archive to read the full content of a specific archive.

    Args:
        job_id: Job UUID to list archives for

    Returns:
        List of archived todo files with metadata
    """
    resolved = await resolve_job_id(client, caller, job_id)
    archives = await client.list_archived_todos(resolved)
    return fmt.format_todo_archives(resolved, archives)


@descriptor(
    group="job_inspection",
    plane="observability",
    caller_defaults=_MCP,
    grant="explicit",
    gate=_EXPLICIT_GATE,
)
async def get_todo_archive(
    client: AsyncCockpitClient,
    caller: CallerCtx,
    job_id: str,
    filename: str,
) -> str:
    """Get the full content of an archived todo file for a specific phase.

    Use list_todo_archives first to get available filenames.

    Args:
        job_id: Job UUID
        filename: Archive filename (e.g. 'todos_phase1_20260124_183618.md')

    Returns:
        Full archived todos with status and notes
    """
    resolved = await resolve_job_id(client, caller, job_id)
    data = await client.get_archived_todos(resolved, filename)
    if data is None:
        return f"Archive '{filename}' not found for job {resolved}."
    return fmt.format_todo_archive_detail(resolved, filename, data)


@descriptor(
    group="job_inspection",
    plane="observability",
    caller_defaults=_MCP,
    grant="explicit",
    gate=_EXPLICIT_GATE,
)
async def get_audit_timerange(
    client: AsyncCockpitClient, caller: CallerCtx, job_id: str
) -> str:
    """Get the first and last timestamps for a job's audit entries.

    Quick way to see when a job started and when it last had activity.
    Requires the audit store to be available.

    Args:
        job_id: Job UUID to get time range for

    Returns:
        Start and end timestamps, or error if the audit store is unavailable
    """
    resolved = await resolve_job_id(client, caller, job_id)
    data = await client.get_audit_time_range(resolved)
    if data is None:
        return f"No audit time range available for job {resolved} (audit store may be unavailable)."
    start = data.get("start", "unknown")
    end = data.get("end", "unknown")
    return f"Audit time range for job {resolved}:\n  Start: {start}\n  End:   {end}"


@descriptor(
    group="job_inspection",
    plane="observability",
    caller_defaults=_MCP,
    grant="explicit",
    gate=_EXPLICIT_GATE,
)
async def list_message_threads(
    client: AsyncCockpitClient, caller: CallerCtx, job_id: str
) -> str:
    """List message threads for an agent job.

    Shows all communication threads between the agent and humans,
    including thread ID, subject, message count, and status.

    Args:
        job_id: Job UUID to list threads for

    Returns:
        Formatted list of message threads
    """
    resolved = await resolve_job_id(client, caller, job_id)
    data = await client.list_message_threads(resolved)
    return fmt.format_message_threads(data.get("threads", []))


@descriptor(
    group="job_inspection",
    plane="observability",
    caller_defaults=_MCP,
    grant="explicit",
    gate=_EXPLICIT_GATE,
)
async def get_message_thread(
    client: AsyncCockpitClient,
    caller: CallerCtx,
    job_id: str,
    thread_id: str,
) -> str:
    """Get full message history for a specific thread.

    Shows all messages in chronological order with direction,
    timestamps, and content.

    Args:
        job_id: Job UUID
        thread_id: Thread ID to retrieve

    Returns:
        Formatted thread message history
    """
    resolved = await resolve_job_id(client, caller, job_id)
    data = await client.list_message_threads(resolved)
    target = next(
        (
            thread
            for thread in data.get("threads", [])
            if thread.get("thread_id") == thread_id
        ),
        None,
    )
    if not target:
        return f"Thread {thread_id} not found in job {resolved}."
    messages = target.get("messages", [])
    if not messages:
        return fmt.format_message_threads([target])
    return fmt.format_thread_messages(messages, thread_id)
