"""MCP server exposing debug cockpit tools.

Provides tools to inspect agent jobs, audit trails, todos, and graph changes
via the Model Context Protocol.
"""

from __future__ import annotations

import json
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

try:
    from .client import CockpitClient
except ImportError:
    from client import CockpitClient  # type: ignore[no-redef]


def create_server() -> Server:
    """Create and configure the MCP server with all tools."""
    app = Server("cockpit-debug")
    client = CockpitClient()

    @app.list_tools()
    async def list_tools() -> list[Tool]:
        """Return the list of available tools."""
        return [
            Tool(
                name="list_jobs",
                description=(
                    "List agent jobs with optional status filter. "
                    "Returns job ID, status, config name, timestamps, and audit entry count. "
                    "Use this to find jobs to investigate."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "status": {
                            "type": "string",
                            "description": "Filter by status: pending, running, completed, failed",
                            "enum": ["pending", "running", "completed", "failed"],
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Maximum number of jobs to return",
                            "default": 20,
                            "minimum": 1,
                            "maximum": 100,
                        },
                    },
                },
            ),
            Tool(
                name="get_job",
                description=(
                    "Get detailed information about a specific job by ID. "
                    "Returns full job details including prompt, config, status, "
                    "timestamps, and audit count."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "job_id": {
                            "type": "string",
                            "description": "Job UUID to retrieve",
                        },
                    },
                    "required": ["job_id"],
                },
            ),
            Tool(
                name="get_audit_trail",
                description=(
                    "Get paginated audit entries for a job's execution. "
                    "Shows LLM messages, tool calls, and errors. "
                    "Use filter to narrow results. Page -1 returns the last page."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "job_id": {
                            "type": "string",
                            "description": "Job UUID to get audit for",
                        },
                        "page": {
                            "type": "integer",
                            "description": "Page number (1-indexed, -1 for last page)",
                            "default": 1,
                        },
                        "page_size": {
                            "type": "integer",
                            "description": "Entries per page (max 200)",
                            "default": 20,
                            "minimum": 1,
                            "maximum": 200,
                        },
                        "filter": {
                            "type": "string",
                            "description": "Filter category",
                            "enum": ["all", "messages", "tools", "errors"],
                            "default": "all",
                        },
                    },
                    "required": ["job_id"],
                },
            ),
            Tool(
                name="get_chat_history",
                description=(
                    "Get paginated chat history for a job showing conversation turns. "
                    "Returns clean sequential view of input/response pairs without duplicates. "
                    "Use this to understand the agent's reasoning flow."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "job_id": {
                            "type": "string",
                            "description": "Job UUID to get chat history for",
                        },
                        "page": {
                            "type": "integer",
                            "description": "Page number (1-indexed, -1 for last page)",
                            "default": 1,
                        },
                        "page_size": {
                            "type": "integer",
                            "description": "Entries per page (max 200)",
                            "default": 20,
                            "minimum": 1,
                            "maximum": 200,
                        },
                    },
                    "required": ["job_id"],
                },
            ),
            Tool(
                name="get_todos",
                description=(
                    "Get all todos for a job including current active todos and archives. "
                    "Shows task planning and execution progress across phases."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "job_id": {
                            "type": "string",
                            "description": "Job UUID to get todos for",
                        },
                    },
                    "required": ["job_id"],
                },
            ),
            Tool(
                name="get_graph_changes",
                description=(
                    "Get timeline of Neo4j graph mutations for a job. "
                    "Returns parsed Cypher queries showing nodes/relationships "
                    "created, modified, and deleted. Includes summary statistics."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "job_id": {
                            "type": "string",
                            "description": "Job UUID to get graph changes for",
                        },
                    },
                    "required": ["job_id"],
                },
            ),
            Tool(
                name="get_llm_request",
                description=(
                    "Get full LLM request/response by MongoDB document ID. "
                    "Returns complete message history, model response, and token usage. "
                    "Use document IDs from audit trail entries."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "doc_id": {
                            "type": "string",
                            "description": "MongoDB ObjectId (24 hex characters)",
                        },
                    },
                    "required": ["doc_id"],
                },
            ),
            Tool(
                name="search_audit",
                description=(
                    "Search audit entries by content pattern. "
                    "Searches across message content, tool names, and arguments. "
                    "Returns matching entries with context."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "job_id": {
                            "type": "string",
                            "description": "Job UUID to search within",
                        },
                        "query": {
                            "type": "string",
                            "description": "Search string (case-insensitive substring match)",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Maximum results to return",
                            "default": 20,
                            "minimum": 1,
                            "maximum": 100,
                        },
                    },
                    "required": ["job_id", "query"],
                },
            ),
        ]

    @app.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
        """Handle tool calls by dispatching to appropriate client methods."""
        try:
            result = _handle_tool(client, name, arguments)
            return [TextContent(type="text", text=result)]
        except Exception as e:
            error_msg = f"Error calling {name}: {e}"
            return [TextContent(type="text", text=error_msg)]

    return app


def _handle_tool(client: CockpitClient, name: str, args: dict[str, Any]) -> str:
    """Dispatch tool call to client and format result."""
    if name == "list_jobs":
        jobs = client.list_jobs(
            status=args.get("status"),
            limit=args.get("limit", 20),
        )
        return _format_jobs(jobs)

    elif name == "get_job":
        job = client.get_job(args["job_id"])
        return _format_job_detail(job)

    elif name == "get_audit_trail":
        audit = client.get_audit_trail(
            job_id=args["job_id"],
            page=args.get("page", 1),
            page_size=args.get("page_size", 20),
            filter_category=args.get("filter", "all"),
        )
        return _format_audit(audit)

    elif name == "get_chat_history":
        chat = client.get_chat_history(
            job_id=args["job_id"],
            page=args.get("page", 1),
            page_size=args.get("page_size", 20),
        )
        return _format_chat_history(chat)

    elif name == "get_todos":
        todos = client.get_todos(args["job_id"])
        return _format_todos(todos)

    elif name == "get_graph_changes":
        changes = client.get_graph_changes(args["job_id"])
        return _format_graph_changes(changes)

    elif name == "get_llm_request":
        request = client.get_llm_request(args["doc_id"])
        return _format_llm_request(request)

    elif name == "search_audit":
        return _search_audit(
            client,
            job_id=args["job_id"],
            query=args["query"],
            limit=args.get("limit", 20),
        )

    else:
        return f"Unknown tool: {name}"


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

    if job.get("prompt"):
        prompt = job["prompt"]
        if len(prompt) > 500:
            prompt = prompt[:500] + "..."
        lines.append(f"\nPrompt:\n{prompt}")

    if job.get("error"):
        lines.append(f"\nError: {job['error']}")

    return "\n".join(lines)


def _format_audit(audit: dict[str, Any]) -> str:
    """Format audit trail entries."""
    entries = audit.get("entries", [])
    total = audit.get("total", 0)
    page = audit.get("page", 1)
    page_size = audit.get("pageSize", 50)
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
    """Format LLM request/response."""
    lines = [f"LLM Request: {request.get('_id', 'unknown')}\n"]

    lines.append(f"Job: {request.get('job_id', 'N/A')}")
    lines.append(f"Model: {request.get('model', 'N/A')}")
    lines.append(f"Timestamp: {request.get('timestamp', 'N/A')}")

    # Token usage
    usage = request.get("usage", {})
    if usage:
        lines.append(
            f"Tokens: {usage.get('prompt_tokens', 0)} prompt, "
            f"{usage.get('completion_tokens', 0)} completion"
        )

    # Messages
    messages = request.get("messages", [])
    if messages:
        lines.append(f"\n=== Messages ({len(messages)}) ===")
        for msg in messages[-5:]:  # Last 5 messages
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            if isinstance(content, str):
                preview = content[:200]
                if len(content) > 200:
                    preview += "..."
            else:
                preview = str(content)[:200]
            lines.append(f"[{role}]: {preview}")

    # Response
    response = request.get("response", {})
    if response:
        lines.append("\n=== Response ===")
        choices = response.get("choices", [])
        for choice in choices[:1]:  # First choice
            message = choice.get("message", {})
            content = message.get("content", "")
            if content:
                preview = content[:500]
                if len(content) > 500:
                    preview += "..."
                lines.append(preview)

    return "\n".join(lines)


def _search_audit(
    client: CockpitClient,
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
        audit = client.get_audit_trail(
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
    if query in entry.get("error", "").lower():
        return True

    return False


async def main() -> None:
    """Run the MCP server."""
    app = create_server()
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())
