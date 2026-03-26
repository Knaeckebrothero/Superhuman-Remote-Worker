"""Server-side tool dispatch for the instruction builder.

Handles execution of all server-side builder tools (job inspection, git history,
monitoring, database, citations, execution debug, and actions) by delegating to
AsyncCockpitClient for API calls and shared formatters for output formatting.

Replaces the inline _execute_server_tool() in main.py with a clean dispatch table.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from . import formatters as fmt

try:
    from ..mcp.client import AsyncCockpitClient
except ImportError:
    try:
        from mcp.client import AsyncCockpitClient  # type: ignore[no-redef]
    except ImportError:
        import importlib

        _mod = importlib.import_module("orchestrator.mcp.client")
        AsyncCockpitClient = _mod.AsyncCockpitClient  # type: ignore[misc]

logger = logging.getLogger(__name__)

# Singleton client instance
_client: AsyncCockpitClient | None = None


def _get_client() -> AsyncCockpitClient:
    """Get or create the async client instance (loopback to orchestrator API)."""
    global _client
    if _client is None:
        _client = AsyncCockpitClient(base_url="http://localhost:8085")
    return _client


# =============================================================================
# Tool Handlers
# =============================================================================
# Each handler is an async function: (args: dict) -> (result: str, full_content: str | None)
#
# result: formatted text shown to the builder LLM
# full_content: untruncated content for inspection tools (sent as SSE tool_result
#               with full=true for the frontend request viewer), or None for mutations


async def _list_jobs(args: dict) -> tuple[str, str | None]:
    client = _get_client()
    jobs = await client.list_jobs(
        status=args.get("status"),
        limit=args.get("limit", 20),
    )
    formatted = fmt.format_jobs(jobs)
    return formatted, formatted


async def _get_job(args: dict) -> tuple[str, str | None]:
    client = _get_client()
    job = await client.get_job(args["job_id"])
    formatted = fmt.format_job_detail(job)
    return formatted, formatted


async def _get_job_progress(args: dict) -> tuple[str, str | None]:
    client = _get_client()
    data = await client.get_job_progress(args["job_id"])
    formatted = fmt.format_job_progress(args["job_id"], data)
    return formatted, formatted


async def _get_workspace_file(args: dict) -> tuple[str, str | None]:
    client = _get_client()
    result = await client.get_workspace_file(args["job_id"], args["path"])
    content = result.get("content", "")
    formatted = f"Workspace file: {args['path']} (job {args['job_id']})\n---\n{content}"
    return formatted, formatted


async def _get_workspace_overview(args: dict) -> tuple[str, str | None]:
    client = _get_client()
    data = await client.get_workspace_overview(args["job_id"])
    formatted = fmt.format_workspace_overview(args["job_id"], data)
    return formatted, formatted


async def _get_frozen_job(args: dict) -> tuple[str, str | None]:
    client = _get_client()
    data = await client.get_frozen_job(args["job_id"])
    formatted = fmt.format_frozen_job(args["job_id"], data)
    return formatted, formatted


async def _get_todos(args: dict) -> tuple[str, str | None]:
    client = _get_client()
    data = await client.get_todos(args["job_id"])
    formatted = fmt.format_todos(data)
    return formatted, formatted


async def _get_chat_history(args: dict) -> tuple[str, str | None]:
    client = _get_client()
    data = await client.get_chat_history(
        job_id=args["job_id"],
        page=args.get("page", -1),
        page_size=args.get("page_size", 20),
    )
    formatted = fmt.format_chat_history(data)
    return formatted, formatted


# ---- Git History ----


async def _list_job_commits(args: dict) -> tuple[str, str | None]:
    client = _get_client()
    ref = args.get("ref", "main")
    result = await client.list_job_commits(
        job_id=args["job_id"],
        sha=ref,
        since_ref=args.get("since_ref"),
        page=args.get("page", 1),
        limit=args.get("limit", 20),
    )
    formatted = fmt.format_commits(
        args["job_id"], result, ref=ref, since_ref=args.get("since_ref")
    )
    return formatted, formatted


async def _get_job_diff(args: dict) -> tuple[str, str | None]:
    client = _get_client()
    result = await client.get_job_diff(
        job_id=args["job_id"],
        base=args["base"],
        head=args.get("head", "HEAD"),
    )
    diff_text = result.get("diff", "")
    if args.get("file_path") and diff_text:
        diff_text = fmt.filter_diff_by_file(diff_text, args["file_path"])
    formatted = fmt.format_diff(
        args["job_id"],
        args["base"],
        args.get("head", "HEAD"),
        diff_text,
        max_chars=args.get("max_chars", 50000),
    )
    return formatted, formatted


async def _get_job_file(args: dict) -> tuple[str, str | None]:
    client = _get_client()
    result = await client.get_job_file(
        job_id=args["job_id"],
        path=args["file_path"],
        ref=args.get("ref"),
    )
    content = result.get("content", "")
    ref_label = args.get("ref") or "HEAD"
    size = result.get("size", len(content))
    formatted = (
        f"File: {args['file_path']} (ref: {ref_label}, {size} bytes)\n---\n{content}"
    )
    return formatted, formatted


async def _list_job_files(args: dict) -> tuple[str, str | None]:
    client = _get_client()
    entries = await client.list_job_files(
        job_id=args["job_id"],
        path=args.get("path", ""),
        ref=args.get("ref"),
    )
    formatted = fmt.format_file_listing(
        args["job_id"],
        args.get("path", ""),
        entries,
        ref=args.get("ref"),
    )
    return formatted, formatted


async def _list_job_tags(args: dict) -> tuple[str, str | None]:
    client = _get_client()
    tags = await client.list_job_tags(args["job_id"])
    formatted = fmt.format_tags(args["job_id"], tags)
    return formatted, formatted


# ---- Monitoring ----


async def _get_job_stats(args: dict) -> tuple[str, str | None]:
    client = _get_client()
    data = await client.get_job_stats()
    formatted = fmt.format_job_stats(data)
    return formatted, formatted


async def _get_agent_stats(args: dict) -> tuple[str, str | None]:
    client = _get_client()
    data = await client.get_agent_stats()
    formatted = fmt.format_agent_stats(data)
    return formatted, formatted


async def _get_stuck_jobs(args: dict) -> tuple[str, str | None]:
    client = _get_client()
    data = await client.get_stuck_jobs(
        threshold_minutes=args.get("threshold_minutes", 30),
    )
    formatted = fmt.format_stuck_jobs(data, args.get("threshold_minutes", 30))
    return formatted, formatted


async def _list_agents(args: dict) -> tuple[str, str | None]:
    client = _get_client()
    agents = await client.list_agents(status=args.get("status"))
    formatted = fmt.format_agents(agents, status_filter=args.get("status"))
    return formatted, formatted


async def _list_experts(args: dict) -> tuple[str, str | None]:
    client = _get_client()
    experts = await client.list_experts()
    formatted = fmt.format_experts(experts)
    return formatted, formatted


async def _get_expert(args: dict) -> tuple[str, str | None]:
    client = _get_client()
    data = await client.get_expert(args["expert_id"])
    formatted = fmt.format_expert_detail(args["expert_id"], data)
    return formatted, formatted


async def _list_datasources(args: dict) -> tuple[str, str | None]:
    client = _get_client()
    datasources = await client.list_datasources(ds_type=args.get("ds_type"))
    formatted = fmt.format_datasources(datasources, type_filter=args.get("ds_type"))
    return formatted, formatted


# ---- Database Inspection ----


async def _list_tables(args: dict) -> tuple[str, str | None]:
    client = _get_client()
    tables = await client.list_tables()
    formatted = fmt.format_tables(tables)
    return formatted, formatted


async def _query_table(args: dict) -> tuple[str, str | None]:
    client = _get_client()
    limit = args.get("limit", 50)
    offset = args.get("offset", 0)
    page = (offset // max(limit, 1)) + 1
    data = await client.get_table_data(
        table_name=args["table_name"],
        page=page,
        page_size=limit,
    )
    formatted = fmt.format_table_data(args["table_name"], data)
    return formatted, formatted


async def _get_table_schema(args: dict) -> tuple[str, str | None]:
    client = _get_client()
    columns = await client.get_table_schema(args["table_name"])
    formatted = fmt.format_table_schema(args["table_name"], columns)
    return formatted, formatted


# ---- Execution Debug ----


async def _get_audit_trail(args: dict) -> tuple[str, str | None]:
    client = _get_client()
    data = await client.get_audit_trail(
        job_id=args["job_id"],
        page=args.get("page", 1),
        page_size=args.get("page_size", 20),
        filter_category=args.get("filter", "all"),
    )
    formatted = fmt.format_audit(data)
    return formatted, formatted


async def _get_graph_changes(args: dict) -> tuple[str, str | None]:
    client = _get_client()
    data = await client.get_graph_changes(args["job_id"])
    formatted = fmt.format_graph_changes(data)
    return formatted, formatted


async def _get_llm_request(args: dict) -> tuple[str, str | None]:
    client = _get_client()
    data = await client.get_llm_request(args["doc_id"])
    formatted = fmt.format_llm_request(data)
    return formatted, formatted


async def _search_audit(args: dict) -> tuple[str, str | None]:
    client = _get_client()
    query = args["query"]
    limit = min(max(args.get("limit", 20), 1), 100)
    query_lower = query.lower()
    matches: list[dict] = []

    page = 1
    while len(matches) < limit:
        audit = await client.get_audit_trail(
            job_id=args["job_id"],
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
        return f"No audit entries matching '{query}' found.", None

    lines = [f"Found {len(matches)} entries matching '{query}':\n"]
    for entry in matches:
        step_num = entry.get("step_number", "?")
        step_type = entry.get("step_type", "unknown")
        if step_type == "tool":
            tool = entry.get("tool", {})
            tool_name = tool.get("name", "unknown")
            lines.append(f"[{step_num}] Tool: {tool_name}")
            tool_args = json.dumps(tool.get("arguments", {}))[:150]
            lines.append(f"    Args: {tool_args}")
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

    formatted = "\n".join(lines)
    return formatted, formatted


# ---- Citation & Source Library ----


async def _list_job_sources(args: dict) -> tuple[str, str | None]:
    client = _get_client()
    data = await client.list_job_sources(
        job_id=args.get("job_id"),
        source_type=args.get("source_type"),
        limit=args.get("limit", 50),
        offset=args.get("offset", 0),
    )
    formatted = fmt.format_sources(
        data, job_id=args.get("job_id"), type_filter=args.get("source_type")
    )
    return formatted, formatted


async def _get_source_detail(args: dict) -> tuple[str, str | None]:
    client = _get_client()
    data = await client.get_source_detail(
        source_id=args["source_id"],
        content_limit=args.get("content_limit", 2000),
    )
    formatted = fmt.format_source_detail(data)
    return formatted, formatted


async def _list_job_citations(args: dict) -> tuple[str, str | None]:
    client = _get_client()
    data = await client.list_job_citations(
        job_id=args["job_id"],
        source_id=args.get("source_id"),
        verification_status=args.get("verification_status"),
        limit=args.get("limit", 50),
        offset=args.get("offset", 0),
    )
    formatted = fmt.format_citations(
        args["job_id"],
        data,
        status_filter=args.get("verification_status"),
    )
    return formatted, formatted


async def _get_citation_detail(args: dict) -> tuple[str, str | None]:
    client = _get_client()
    data = await client.get_citation_detail(args["citation_id"])
    formatted = fmt.format_citation_detail(data)
    return formatted, formatted


async def _search_job_sources(args: dict) -> tuple[str, str | None]:
    client = _get_client()
    data = await client.search_job_sources(
        job_id=args["job_id"],
        query=args["query"],
        mode=args.get("mode", "keyword"),
        source_type=args.get("source_type"),
        tags=args.get("tags"),
        top_k=args.get("top_k", 10),
    )
    formatted = fmt.format_source_search(data)
    return formatted, formatted


async def _get_source_annotations(args: dict) -> tuple[str, str | None]:
    client = _get_client()
    annotations = await client.get_source_annotations(
        job_id=args["job_id"],
        source_id=args["source_id"],
        annotation_type=args.get("annotation_type"),
    )
    formatted = fmt.format_annotations(
        args["job_id"],
        args["source_id"],
        annotations,
        type_filter=args.get("annotation_type"),
    )
    return formatted, formatted


async def _get_source_tags(args: dict) -> tuple[str, str | None]:
    client = _get_client()
    tags = await client.get_source_tags(args["job_id"], args["source_id"])
    formatted = fmt.format_source_tags(args["job_id"], args["source_id"], tags)
    return formatted, formatted


async def _get_citation_stats(args: dict) -> tuple[str, str | None]:
    client = _get_client()
    data = await client.get_citation_stats(args["job_id"])
    formatted = fmt.format_citation_stats(args["job_id"], data)
    return formatted, formatted


async def _get_memory_stats(args: dict) -> tuple[str, str | None]:
    client = _get_client()
    data = await client.get_memory_stats(args["job_id"])
    formatted = fmt.format_memory_stats(args["job_id"], data)
    return formatted, formatted


# ---- Actions (mutations) ----


async def _approve_job(args: dict) -> tuple[str, str | None]:
    client = _get_client()
    result = await client.approve_job(args["job_id"])
    return fmt.format_action_result("approve", args["job_id"], result), None


async def _resume_job_with_feedback(args: dict) -> tuple[str, str | None]:
    client = _get_client()
    result = await client.resume_job(args["job_id"], feedback=args.get("feedback"))
    return fmt.format_action_result(
        "resume",
        args["job_id"],
        result,
        feedback=args.get("feedback"),
    ), None


async def _cancel_job(args: dict) -> tuple[str, str | None]:
    client = _get_client()
    result = await client.cancel_job(args["job_id"])
    return fmt.format_action_result("cancel", args["job_id"], result), None


async def _delete_job(args: dict) -> tuple[str, str | None]:
    client = _get_client()
    result = await client.delete_job(args["job_id"])
    return fmt.format_action_result("delete", args["job_id"], result), None


async def _assign_job(args: dict) -> tuple[str, str | None]:
    client = _get_client()
    result = await client.assign_job(args["job_id"], args["agent_id"])
    return fmt.format_action_result(
        "assign",
        args["job_id"],
        result,
        agent_id=args["agent_id"],
    ), None


async def _create_job(
    args: dict,
    *,
    user_id: str | None = None,
    project_id: str | None = None,
) -> tuple[str, str | None]:
    client = _get_client()
    result = await client.create_job(
        description=args["description"],
        config_name=args.get("config_name", "default"),
        datasource_ids=args.get("datasource_ids"),
        instructions=args.get("instructions"),
        config_override=args.get("config_override"),
        context=args.get("context"),
        project_id=args.get("project_id", project_id),
        user_id=user_id,
    )
    return fmt.format_created_job(result, args.get("config_name", "default")), None


async def _test_datasource(args: dict) -> tuple[str, str | None]:
    client = _get_client()
    result = await client.test_datasource(args["datasource_id"])
    return fmt.format_datasource_test(args["datasource_id"], result), None


async def _get_agent_system_info(args: dict) -> tuple[str, str | None]:
    client = _get_client()
    data = await client.get_agent_system_info(args["agent_id"])
    formatted = fmt.format_system_info(args["agent_id"], data)
    return formatted, formatted


async def _pause_job(args: dict) -> tuple[str, str | None]:
    client = _get_client()
    result = await client.pause_job(args["job_id"])
    return fmt.format_action_result("pause", args["job_id"], result), None


async def _get_audit_bulk(args: dict) -> tuple[str, str | None]:
    client = _get_client()
    data = await client.get_audit_bulk(
        job_id=args["job_id"],
        offset=args.get("offset", 0),
        limit=min(args.get("limit", 500), 500),
    )
    formatted = fmt.format_audit_bulk(data)
    return formatted, formatted


async def _get_chat_bulk(args: dict) -> tuple[str, str | None]:
    client = _get_client()
    data = await client.get_chat_bulk(
        job_id=args["job_id"],
        offset=args.get("offset", 0),
        limit=min(args.get("limit", 500), 500),
    )
    formatted = fmt.format_chat_bulk(data)
    return formatted, formatted


async def _get_job_summary(args: dict) -> tuple[str, str | None]:
    import asyncio

    client = _get_client()
    job_id = args["job_id"]

    results = await asyncio.gather(
        client.get_job(job_id),
        client.get_job_progress(job_id),
        client.get_todos(job_id),
        client.get_workspace_overview(job_id),
        client.get_audit_trail(job_id, page=-1, page_size=10, filter_category="tools"),
        return_exceptions=True,
    )

    formatted = fmt.format_job_summary(*results)
    return formatted, formatted


async def _get_job_log(args: dict) -> tuple[str, str | None]:
    client = _get_client()
    job_id = args["job_id"]
    data = await client.get_job_logs(
        job_id=job_id,
        lines=min(args.get("lines", 100), 1000),
        grep=args.get("grep"),
        level=args.get("level"),
    )
    formatted = fmt.format_job_log(job_id, data)
    return formatted, formatted


async def _list_llm_requests(args: dict) -> tuple[str, str | None]:
    client = _get_client()
    job_id = args["job_id"]
    data = await client.list_llm_requests(
        job_id=job_id,
        limit=min(args.get("limit", 20), 100),
        offset=args.get("offset", 0),
    )
    formatted = fmt.format_llm_requests(job_id, data)
    return formatted, formatted


async def _get_shell_state(args: dict) -> tuple[str, str | None]:
    client = _get_client()
    job_id = args["job_id"]
    data = await client.get_shell_state(job_id)
    formatted = fmt.format_shell_state(job_id, data)
    return formatted, formatted


# ---- Todo Archives & Current Todos ----


async def _get_current_todos(args: dict) -> tuple[str, str | None]:
    client = _get_client()
    data = await client.get_current_todos(args["job_id"])
    if data is None:
        return f"No current todos found for job {args['job_id']}.", None
    formatted = fmt.format_current_todos(data)
    return formatted, formatted


async def _list_todo_archives(args: dict) -> tuple[str, str | None]:
    client = _get_client()
    archives = await client.list_archived_todos(args["job_id"])
    formatted = fmt.format_todo_archives(args["job_id"], archives)
    return formatted, formatted


async def _get_todo_archive(args: dict) -> tuple[str, str | None]:
    client = _get_client()
    data = await client.get_archived_todos(args["job_id"], args["filename"])
    if data is None:
        return f"Archive '{args['filename']}' not found for job {args['job_id']}.", None
    formatted = fmt.format_todo_archive_detail(args["job_id"], args["filename"], data)
    return formatted, formatted


# ---- Audit Time Range ----


async def _get_audit_timerange(args: dict) -> tuple[str, str | None]:
    client = _get_client()
    data = await client.get_audit_time_range(args["job_id"])
    if data is None:
        return (
            f"No audit time range available for job {args['job_id']} (MongoDB may be unavailable).",
            None,
        )
    start = data.get("start", "unknown")
    end = data.get("end", "unknown")
    formatted = (
        f"Audit time range for job {args['job_id']}:\n  Start: {start}\n  End:   {end}"
    )
    return formatted, formatted


# ---- Knowledge Base ----


async def _get_knowledge_summary(args: dict) -> tuple[str, str | None]:
    client = _get_client()
    data = await client.get_knowledge_summary(args["project_id"])
    formatted = fmt.format_knowledge_summary(args["project_id"], data)
    return formatted, formatted


async def _list_knowledge_notes(args: dict) -> tuple[str, str | None]:
    client = _get_client()
    data = await client.list_knowledge_notes(
        project_id=args["project_id"],
        note_type=args.get("note_type"),
        status=args.get("status"),
        tag=args.get("tag"),
        job_id=args.get("job_id"),
        limit=args.get("limit", 50),
        offset=args.get("offset", 0),
    )
    formatted = fmt.format_knowledge_notes(data)
    return formatted, formatted


async def _get_knowledge_note(args: dict) -> tuple[str, str | None]:
    client = _get_client()
    data = await client.get_knowledge_note(args["project_id"], args["note_id"])
    formatted = fmt.format_knowledge_note_detail(data)
    return formatted, formatted


async def _search_knowledge(args: dict) -> tuple[str, str | None]:
    client = _get_client()
    data = await client.search_knowledge(
        project_id=args["project_id"],
        query=args["query"],
        limit=args.get("limit", 10),
    )
    formatted = fmt.format_knowledge_search(data)
    return formatted, formatted


# ---- Projects ----


async def _list_projects(args: dict) -> tuple[str, str | None]:
    client = _get_client()
    projects = await client.list_projects(user_id=args.get("user_id"))
    formatted = fmt.format_projects(projects)
    return formatted, formatted


async def _get_project(args: dict) -> tuple[str, str | None]:
    client = _get_client()
    project = await client.get_project(args["project_id"])
    formatted = fmt.format_project_detail(project)
    return formatted, formatted


async def _create_project(args: dict) -> tuple[str, str | None]:
    client = _get_client()
    result = await client.create_project(
        name=args["name"],
        user_id=args["user_id"],
        description=args.get("description"),
        goal=args.get("goal"),
        default_config_name=args.get("default_config_name"),
        default_config_override=args.get("default_config_override"),
    )
    return fmt.format_created_project(result), None


async def _list_project_jobs(args: dict) -> tuple[str, str | None]:
    client = _get_client()
    jobs = await client.list_project_jobs(
        project_id=args["project_id"],
        status=args.get("status"),
        limit=args.get("limit", 100),
    )
    formatted = fmt.format_jobs(jobs)
    return formatted, formatted


async def _create_project_job(args: dict) -> tuple[str, str | None]:
    client = _get_client()
    result = await client.create_project_job(
        project_id=args["project_id"],
        description=args["description"],
        config_name=args.get("config_name", "default"),
        datasource_ids=args.get("datasource_ids"),
        instructions=args.get("instructions"),
        config_override=args.get("config_override"),
        context=args.get("context"),
    )
    return fmt.format_created_job(result, args.get("config_name", "default")), None


# ---- Datasource CRUD ----


async def _create_datasource(args: dict) -> tuple[str, str | None]:
    client = _get_client()
    result = await client.create_datasource(
        name=args["name"],
        ds_type=args["type"],
        connection_url=args["connection_url"],
        description=args.get("description"),
        credentials=args.get("credentials"),
        read_only=args.get("read_only", True),
        job_id=args.get("job_id"),
    )
    return fmt.format_created_datasource(result), None


async def _update_datasource(args: dict) -> tuple[str, str | None]:
    client = _get_client()
    result = await client.update_datasource(
        datasource_id=args["datasource_id"],
        name=args.get("name"),
        description=args.get("description"),
        connection_url=args.get("connection_url"),
        credentials=args.get("credentials"),
        read_only=args.get("read_only"),
    )
    status = result.get("status", "unknown")
    return f"Datasource {args['datasource_id']} updated ({status}).", None


async def _delete_datasource(args: dict) -> tuple[str, str | None]:
    client = _get_client()
    result = await client.delete_datasource(args["datasource_id"])
    status = result.get("status", "unknown")
    return f"Datasource {args['datasource_id']} deleted ({status}).", None


# ---- Knowledge Mutations ----


async def _update_knowledge_note(args: dict) -> tuple[str, str | None]:
    client = _get_client()
    result = await client.update_knowledge_note(
        project_id=args["project_id"],
        note_id=args["note_id"],
        status=args.get("status"),
        add_tags=args.get("add_tags"),
        remove_tags=args.get("remove_tags"),
    )
    status = result.get("status", "unknown")
    return f"Knowledge note {args['note_id']} updated ({status}).", None


async def _delete_knowledge_note(args: dict) -> tuple[str, str | None]:
    client = _get_client()
    result = await client.delete_knowledge_note(args["project_id"], args["note_id"])
    status = result.get("status", "unknown")
    return f"Knowledge note {args['note_id']} deleted ({status}).", None


async def _export_knowledge(args: dict) -> tuple[str, str | None]:
    client = _get_client()
    result = await client.export_knowledge(args["project_id"])
    path = result.get("path", "unknown")
    count = result.get("note_count", 0)
    name = result.get("project_name", "")
    formatted = (
        f"Knowledge base exported for project '{name}'.\n"
        f"  Notes exported: {count}\n"
        f"  Export path: {path}\n"
        f"  Format: Obsidian-compatible markdown with YAML frontmatter"
    )
    return formatted, None


# ---- Job Promotion ----


async def _promote_job(args: dict) -> tuple[str, str | None]:
    client = _get_client()
    result = await client.promote_job(
        job_id=args["job_id"],
        name=args["name"],
        user_id=args["user_id"],
        description=args.get("description"),
        goal=args.get("goal"),
    )
    project_id = result.get("project_id", "unknown")
    project_name = result.get("project_name", args["name"])
    formatted = (
        f"Job {args['job_id']} promoted to project '{project_name}'.\n"
        f"  New project ID: {project_id}\n"
        f"  Git history preserved from job branch."
    )
    return formatted, None


# ---- Minor Tools ----


async def _get_daily_stats(args: dict) -> tuple[str, str | None]:
    client = _get_client()
    days = args.get("days", 7)
    data = await client.get_daily_stats(days=days)
    formatted = fmt.format_daily_stats(data, days)
    return formatted, formatted


async def _reload_experts(args: dict) -> tuple[str, str | None]:
    client = _get_client()
    result = await client.reload_experts()
    count = result.get("count", 0)
    return f"Expert configurations reloaded ({count} experts loaded).", None


async def _deregister_agent(args: dict) -> tuple[str, str | None]:
    client = _get_client()
    result = await client.deregister_agent(args["agent_id"])
    status = result.get("status", "unknown")
    return f"Agent {args['agent_id']} deregistered ({status}).", None


# ---- Project Management (Extended) ----


async def _update_project(args: dict) -> tuple[str, str | None]:
    client = _get_client()
    result = await client.update_project(
        project_id=args["project_id"],
        name=args.get("name"),
        description=args.get("description"),
        goal=args.get("goal"),
        status=args.get("status"),
        default_config_name=args.get("default_config_name"),
        default_config_override=args.get("default_config_override"),
    )
    status = result.get("status", "unknown")
    return f"Project {args['project_id']} updated ({status}).", None


async def _delete_project(args: dict) -> tuple[str, str | None]:
    client = _get_client()
    result = await client.delete_project(args["project_id"])
    status = result.get("status", "unknown")
    return f"Project {args['project_id']} deleted ({status}).", None


async def _list_project_members(args: dict) -> tuple[str, str | None]:
    client = _get_client()
    members = await client.list_project_members(args["project_id"])
    formatted = fmt.format_project_members(args["project_id"], members)
    return formatted, formatted


async def _add_project_member(args: dict) -> tuple[str, str | None]:
    client = _get_client()
    result = await client.add_project_member(
        project_id=args["project_id"],
        user_id=args["user_id"],
        role=args.get("role", "editor"),
    )
    name = result.get("display_name", args["user_id"])
    role = result.get("role", args.get("role", "editor"))
    return f"Added {name} to project {args['project_id']} as {role}.", None


async def _update_project_member(args: dict) -> tuple[str, str | None]:
    client = _get_client()
    result = await client.update_project_member(
        project_id=args["project_id"],
        user_id=args["user_id"],
        role=args["role"],
    )
    status = result.get("status", "unknown")
    return (
        f"Updated member {args['user_id']} role to {args['role']} "
        f"in project {args['project_id']} ({status})."
    ), None


async def _remove_project_member(args: dict) -> tuple[str, str | None]:
    client = _get_client()
    result = await client.remove_project_member(
        project_id=args["project_id"],
        user_id=args["user_id"],
    )
    status = result.get("status", "unknown")
    return (
        f"Removed member {args['user_id']} from project "
        f"{args['project_id']} ({status})."
    ), None


async def _list_project_experts(args: dict) -> tuple[str, str | None]:
    client = _get_client()
    experts = await client.list_project_experts(args["project_id"])
    formatted = fmt.format_project_experts(args["project_id"], experts)
    return formatted, formatted


async def _get_project_expert(args: dict) -> tuple[str, str | None]:
    client = _get_client()
    data = await client.get_project_expert(args["project_id"], args["expert_name"])
    formatted = fmt.format_project_expert_detail(args["project_id"], data)
    return formatted, formatted


# =============================================================================
# Dispatch Table
# =============================================================================

_DISPATCH: dict[str, Any] = {
    # Job inspection
    "list_jobs": _list_jobs,
    "get_job": _get_job,
    "get_job_progress": _get_job_progress,
    "get_workspace_file": _get_workspace_file,
    "get_workspace_overview": _get_workspace_overview,
    "get_frozen_job": _get_frozen_job,
    "get_todos": _get_todos,
    "get_chat_history": _get_chat_history,
    # Git history
    "list_job_commits": _list_job_commits,
    "get_job_diff": _get_job_diff,
    "get_job_file": _get_job_file,
    "list_job_files": _list_job_files,
    "list_job_tags": _list_job_tags,
    # Monitoring
    "get_job_stats": _get_job_stats,
    "get_agent_stats": _get_agent_stats,
    "get_stuck_jobs": _get_stuck_jobs,
    "list_agents": _list_agents,
    "list_experts": _list_experts,
    "get_expert": _get_expert,
    "list_datasources": _list_datasources,
    # Database inspection
    "list_tables": _list_tables,
    "query_table": _query_table,
    "get_table_schema": _get_table_schema,
    # Execution debug
    "get_audit_trail": _get_audit_trail,
    "get_audit_bulk": _get_audit_bulk,
    "get_chat_bulk": _get_chat_bulk,
    "get_graph_changes": _get_graph_changes,
    "get_llm_request": _get_llm_request,
    "list_llm_requests": _list_llm_requests,
    "search_audit": _search_audit,
    "get_job_log": _get_job_log,
    "get_job_summary": _get_job_summary,
    "get_shell_state": _get_shell_state,
    "get_audit_timerange": _get_audit_timerange,
    # Todo archives
    "get_current_todos": _get_current_todos,
    "list_todo_archives": _list_todo_archives,
    "get_todo_archive": _get_todo_archive,
    # Knowledge base
    "get_knowledge_summary": _get_knowledge_summary,
    "list_knowledge_notes": _list_knowledge_notes,
    "get_knowledge_note": _get_knowledge_note,
    "search_knowledge": _search_knowledge,
    # Projects
    "list_projects": _list_projects,
    "get_project": _get_project,
    "create_project": _create_project,
    "list_project_jobs": _list_project_jobs,
    "create_project_job": _create_project_job,
    # Minor tools
    "get_daily_stats": _get_daily_stats,
    "reload_experts": _reload_experts,
    "deregister_agent": _deregister_agent,
    # Project management (extended)
    "update_project": _update_project,
    "delete_project": _delete_project,
    "list_project_members": _list_project_members,
    "add_project_member": _add_project_member,
    "update_project_member": _update_project_member,
    "remove_project_member": _remove_project_member,
    "list_project_experts": _list_project_experts,
    "get_project_expert": _get_project_expert,
    # Datasource CRUD
    "create_datasource": _create_datasource,
    "update_datasource": _update_datasource,
    "delete_datasource": _delete_datasource,
    # Knowledge mutations
    "update_knowledge_note": _update_knowledge_note,
    "delete_knowledge_note": _delete_knowledge_note,
    "export_knowledge": _export_knowledge,
    # Job promotion
    "promote_job": _promote_job,
    # Citation & source library
    "list_job_sources": _list_job_sources,
    "get_source_detail": _get_source_detail,
    "list_job_citations": _list_job_citations,
    "get_citation_detail": _get_citation_detail,
    "search_job_sources": _search_job_sources,
    "get_source_annotations": _get_source_annotations,
    "get_source_tags": _get_source_tags,
    "get_citation_stats": _get_citation_stats,
    "get_memory_stats": _get_memory_stats,
    # Actions
    "approve_job": _approve_job,
    "resume_job_with_feedback": _resume_job_with_feedback,
    "cancel_job": _cancel_job,
    "pause_job": _pause_job,
    "delete_job": _delete_job,
    "assign_job": _assign_job,
    "create_job": _create_job,
    "create_follow_up_job": _create_job,  # alias for backward compat
    "test_datasource": _test_datasource,
    "get_agent_system_info": _get_agent_system_info,
}


async def execute_server_tool(
    tool_name: str,
    args: dict,
    *,
    tavily_search_fn: Any | None = None,
    user_id: str | None = None,
    active_project_id: str | None = None,
) -> tuple[str, str | None]:
    """Execute a server-side builder tool.

    Args:
        tool_name: Name of the tool to execute
        args: Tool arguments dict
        tavily_search_fn: Async callable for web_search (injected from main.py
                         to avoid circular imports with builder_search)
        user_id: Session user ID (used for default project fallback on job creation)
        active_project_id: Active project context from the cockpit UI

    Returns:
        Tuple of (result_text, full_content).
        full_content is the untruncated result for inspection tools, None for mutations.
    """
    # Special case: web_search uses the tavily search function
    if tool_name == "web_search":
        if tavily_search_fn is None:
            return (
                "Error: web_search not available (no search function configured)",
                None,
            )
        result = await tavily_search_fn(
            query=args.get("query", ""),
            max_results=args.get("max_results", 5),
        )
        return result, None

    handler = _DISPATCH.get(tool_name)
    if handler is None:
        return f"Error: Unknown server tool: {tool_name}", None

    try:
        # Pass user/project context to job-creation handlers
        if tool_name in ("create_job", "create_follow_up_job"):
            return await handler(
                args,
                user_id=user_id,
                project_id=active_project_id,
            )
        return await handler(args)
    except Exception as e:
        error_msg = str(e)[:300]
        if hasattr(e, "response") and hasattr(e.response, "status_code"):
            error_detail = (
                e.response.text[:200] if hasattr(e.response, "text") else error_msg
            )
            return f"Error ({e.response.status_code}): {error_detail}", None
        return f"Error: {error_msg}", None
