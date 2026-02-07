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
        lines.append(f"\n=== Model Parameters ===")
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
