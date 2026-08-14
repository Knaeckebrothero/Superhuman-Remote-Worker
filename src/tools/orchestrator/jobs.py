"""Orchestrator job management tools for persistent agents.

These tools call the orchestrator REST API to create, monitor, and manage
worker jobs. They enable the persistent agent to delegate heavy work to
the autonomous worker pool.

The orchestrator URL is read from the ORCHESTRATOR_URL environment variable
(same as the worker's orchestrator_client.py).
"""

import logging
import os
from uuid import UUID
from typing import Any, Dict, List, Optional

import httpx
from langchain_core.tools import tool

from src.shared.orch_surface.client import AsyncCockpitClient
from src.shared.orch_surface.jobs import (
    CallerCtx,
    JOB_DESCRIPTORS,
    make_bound_handler,
    registry_metadata,
)

from ..context import ToolContext

logger = logging.getLogger(__name__)

ORCHESTRATOR_TOOLS_METADATA: Dict[str, Dict[str, Any]] = {
    "get_session_context": {
        "module": "orchestrator.jobs",
        "function": "get_session_context",
        "description": (
            "Summarize the current persistent session context: thread ID, user "
            "ID, project scope, workspace availability, backend capabilities, "
            "cloud mount status, knowledge/connector availability, the chat "
            "models this deployment routes, and the caller's effective grants."
        ),
        "category": "orchestrator",
        "short_description": "Show current session/project/workspace context.",
        "phases": ["strategic", "tactical"],
    },
    **registry_metadata(),
}


def _get_orchestrator_url() -> str:
    """Get orchestrator base URL from environment."""
    url = os.getenv("ORCHESTRATOR_URL", "http://localhost:8085")
    return url.rstrip("/")


def _get_client(*, user_id: Optional[str] = None) -> httpx.AsyncClient:
    """Create an httpx client for orchestrator calls.

    Attaches ``X-Internal-Key`` when ``MCP_INTERNAL_KEY`` is set so the
    orchestrator's Track B (P4b) gates accept agent-tool calls. When
    ``user_id`` is supplied the client also sends ``X-MCP-User-Id`` so
    the orchestrator's ``_get_user_from_mcp_headers`` path can resolve
    the originating user — required by ``GET /api/jobs``,
    ``GET /api/jobs/{id}`` and any other endpoint guarded by
    ``require_approved_user`` / ``require_job_access``. Without the
    user header those endpoints 401 even with a valid internal key.

    Worker-mode callers (no session, no user identity) pass
    ``user_id=None`` and continue to authenticate as anonymous internal
    against the dual-callable / require_internal endpoints.
    """
    headers: dict[str, str] = {}
    internal_key = os.getenv("MCP_INTERNAL_KEY", "")
    if internal_key:
        headers["X-Internal-Key"] = internal_key
    if user_id:
        headers["X-MCP-User-Id"] = user_id
    return httpx.AsyncClient(timeout=30.0, headers=headers)


_surface_client: AsyncCockpitClient | None = None


def _get_surface_client() -> AsyncCockpitClient:
    """Return the shared pooled client used by descriptor-backed tools."""
    global _surface_client
    if _surface_client is None:
        _surface_client = AsyncCockpitClient(base_url=_get_orchestrator_url())
    return _surface_client


def _caller_ctx(context: ToolContext) -> CallerCtx:
    """Translate trusted ToolContext lineage without adding public arguments."""
    parent_job_id: str | None = None
    candidate = context._job_metadata.get("job_id")
    try:
        if candidate:
            parent_job_id = str(UUID(str(candidate)))
    except (TypeError, ValueError, AttributeError):
        pass

    project_ids = tuple(str(project_id) for project_id in context.project_ids)
    officer = context.config.get("officer") or {}
    caller_kind = (
        "officer" if isinstance(officer, dict) and officer.get("enabled") else "session"
    )
    return CallerCtx(
        kind=caller_kind,
        user_id=context.user_id,
        project_ids=project_ids,
        lineage_project_id=context.project_id,
        thread_id=context.thread_id,
        parent_job_id=parent_job_id,
        resolve_job_id_prefixes=True,
    )


def _short_id(value: Any) -> str:
    text = str(value or "")
    return text[:8] if text else "unknown"


def _truncate(value: Any, *, limit: int = 140) -> str:
    text = str(value or "").strip()
    if not text:
        return "N/A"
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _compact_dict_value(data: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = data.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def _format_freeze_data(job: Dict[str, Any]) -> List[str]:
    freeze_data = job.get("freeze_data")
    if not isinstance(freeze_data, dict):
        context = job.get("context")
        if isinstance(context, dict):
            candidate = context.get("freeze_data")
            if isinstance(candidate, dict):
                freeze_data = candidate
    if not isinstance(freeze_data, dict):
        return []

    lines: List[str] = []
    freeze_type = _compact_dict_value(freeze_data, "freeze_type", "type")
    reason = _compact_dict_value(
        freeze_data,
        "reason",
        "message",
        "status_message",
        "review_reason",
        "pause_reason",
    )
    if freeze_type:
        lines.append(f"Freeze type: {freeze_type}")
    if reason:
        lines.append(f"Freeze reason: {_truncate(reason, limit=240)}")
    if freeze_data.get("requires_review") is not None:
        lines.append(f"Requires review: {freeze_data.get('requires_review')}")
    return lines


def _format_job_list_item(job: Dict[str, Any]) -> List[str]:
    job_id = job.get("id", "unknown")
    lines = [
        f"--- {job_id} (short: {_short_id(job_id)}) ---",
        f"  Status: {job.get('status', '?')}",
        f"  Description: {_truncate(job.get('description'), limit=140)}",
    ]
    if job.get("config_name"):
        lines.append(f"  Config: {job['config_name']}")
    if job.get("project_id"):
        lines.append(f"  Project ID: {job['project_id']}")
    if job.get("parent_job_id"):
        lines.append(f"  Parent job ID: {job['parent_job_id']}")
    if job.get("assigned_agent_id"):
        lines.append(f"  Agent: {job['assigned_agent_id']}")
    if job.get("updated_at"):
        lines.append(f"  Updated: {job['updated_at']}")
    elif job.get("created_at"):
        lines.append(f"  Created: {job['created_at']}")
    for freeze_line in _format_freeze_data(job):
        lines.append(f"  {freeze_line}")
    if job.get("error_message"):
        lines.append(f"  Error: {_truncate(job['error_message'], limit=180)}")
    return lines


def _format_grants(capabilities: Dict[str, Any]) -> List[str]:
    """Render the caller's effective grants for the session-context report.

    Shape comes from ``GET /api/users/me/capabilities``: admins get
    ``grants: None`` (unrestricted). Only the capabilities that decide whether a
    ``create_job`` override will be accepted are surfaced — the full
    catalog would bloat the tool result for no decision value.
    """
    if capabilities.get("is_admin"):
        return ["  Grants: admin (unrestricted)"]
    grants = capabilities.get("grants")
    if not isinstance(grants, dict):
        return []
    lines = ["  Grants:"]
    for key in ("vm_workspace", "shell_tools", "delegation", "datasource_tools"):
        if key in grants:
            lines.append(f"    {key}: {grants[key]}")
    allowed_models = grants.get("model_selection")
    if allowed_models is not None:
        lines.append(
            f"    model_selection: {', '.join(str(m) for m in allowed_models)}"
        )
    return lines


def _format_session_context(
    context: ToolContext,
    *,
    chat_models: Optional[List[str]] = None,
    capabilities: Optional[Dict[str, Any]] = None,
) -> str:
    workspace = context.workspace_manager
    backend = getattr(workspace, "backend", None) if workspace else None
    cloud_mount = context.config.get("cloud_mount") or {}
    datasource_keys = sorted(context.datasources.keys())

    lines = [
        "Session context:",
        f"  Thread ID: {context.thread_id or 'none'}",
        f"  User ID: {context.user_id or 'none'}",
        f"  Primary project ID: {context.project_id or 'none'}",
        f"  Project IDs: {', '.join(context.project_ids) if context.project_ids else 'none'}",
        f"  Job/context ID: {context.job_id or 'none'}",
        f"  Workspace available: {bool(workspace)}",
    ]
    if workspace:
        lines.extend(
            [
                f"  Workspace path: {workspace.path}",
                f"  Workspace backend: {type(backend).__name__ if backend else 'unknown'}",
                f"  Supports shell: {bool(getattr(backend, 'supports_shell', False))}",
                f"  Git available: {context.has_git()}",
            ]
        )
    lines.extend(
        [
            f"  Shell manager available: {context.has_shell()}",
            f"  Knowledge available: {context.has_knowledge()}",
            f"  Connectors: {', '.join(datasource_keys) if datasource_keys else 'none'}",
            f"  Cloud mount active: {bool(cloud_mount.get('active'))}",
        ]
    )
    if cloud_mount.get("active"):
        lines.append(
            f"  Cloud workspace entry: {cloud_mount.get('workspace_entry', '/workspace/cloud')}"
        )
    # Everything a create_job config_override needs to be written without
    # guessing: the model IDs this deployment actually routes, and the grants
    # that decide whether an override is accepted. Both are omitted (not faked)
    # when the lookup failed — see get_session_context.
    if chat_models:
        lines.append(f"  Available chat models: {', '.join(chat_models)}")
    if capabilities:
        lines.extend(_format_grants(capabilities))
    return "\n".join(lines)


def create_orchestrator_tools(context: ToolContext) -> List[Any]:
    """Create the session-context tool and every descriptor-backed job tool."""
    base_url = _get_orchestrator_url()

    @tool
    async def get_session_context() -> str:
        """Show the current session, project, workspace, and capability context.

        Use this before project/job/repository actions when you need to know
        which thread, user, project, and workspace backend this session is using.
        Also reports the chat models this deployment routes and your effective
        grants — read both before pinning a model or workspace backend in a
        create_job config_override.
        """
        chat_models: Optional[List[str]] = None
        capabilities: Optional[Dict[str, Any]] = None
        try:
            async with _get_client(user_id=context.user_id) as client:
                try:
                    resp = await client.get(f"{base_url}/api/models")
                    resp.raise_for_status()
                    groups = resp.json().get("groups") or []
                    models = [
                        str(model_id)
                        for group in groups
                        for model_id in (group.get("models") or [])
                    ]
                    chat_models = sorted(dict.fromkeys(models)) or None
                except Exception as error:
                    logger.debug("get_session_context: model lookup failed: %s", error)
                try:
                    resp = await client.get(f"{base_url}/api/users/me/capabilities")
                    resp.raise_for_status()
                    payload = resp.json()
                    if isinstance(payload, dict):
                        capabilities = payload
                except Exception as error:
                    logger.debug(
                        "get_session_context: capability lookup failed: %s", error
                    )
        except Exception as error:
            logger.debug("get_session_context: orchestrator unreachable: %s", error)

        return _format_session_context(
            context, chat_models=chat_models, capabilities=capabilities
        )

    caller = _caller_ctx(context)
    job_tools = [
        tool(
            make_bound_handler(
                item,
                client_provider=_get_surface_client,
                caller_provider=lambda caller=caller: caller,
            )
        )
        for item in JOB_DESCRIPTORS
    ]
    return [get_session_context, *job_tools]
