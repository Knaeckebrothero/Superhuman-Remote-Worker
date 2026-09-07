"""Project context tools for persistent sessions.

These tools are intentionally thin wrappers around orchestrator REST routes.
The orchestrator remains the authority for project visibility and job access.
"""

import logging
from typing import Any, Dict, List, Optional

import httpx
from langchain_core.tools import tool

from agent.tools.context import ToolContext
from shared.orch_surface.formatters import (
    format_job_list_item as _format_job_list_item,
    truncate_text as _truncate,
)

from agent.tools.orchestrator.jobs import _get_client, _get_orchestrator_url

from shared.tool_catalog.definitions import (
    PROJECT_TOOLS_METADATA as PROJECT_TOOLS_METADATA,
)

logger = logging.getLogger(__name__)


def _current_project_id(context: ToolContext) -> Optional[str]:
    return context.project_id or (
        context.project_ids[0] if context.project_ids else None
    )


def _format_project(project: Dict[str, Any], *, include_cloud_link: bool = True) -> str:
    project_id = project.get("id", "unknown")
    lines = [
        f"Project ID: {project_id}",
        f"Name: {_truncate(project.get('name'), limit=160)}",
    ]
    if project.get("status"):
        lines.append(f"Status: {project['status']}")
    if project.get("goal"):
        lines.append(f"Goal: {_truncate(project.get('goal'), limit=300)}")
    if project.get("description"):
        lines.append(f"Description: {_truncate(project.get('description'), limit=300)}")
    if project.get("default_config_name"):
        lines.append(f"Default config: {project['default_config_name']}")
    elif project.get("default_config"):
        lines.append(f"Default config: {project['default_config']}")
    if project.get("is_default") is not None:
        lines.append(f"Default project: {project['is_default']}")
    if project.get("repo_count") is not None:
        lines.append(f"Repositories: {project['repo_count']}")
    if project.get("datasource_count") is not None:
        lines.append(f"Connectors: {project['datasource_count']}")
    if project.get("job_count") is not None:
        lines.append(f"Jobs: {project['job_count']}")
    if include_cloud_link and project.get("cloud_storage_url"):
        lines.append(f"Cloud storage: {project['cloud_storage_url']}")
    if project.get("created_at"):
        lines.append(f"Created: {project['created_at']}")
    if project.get("updated_at"):
        lines.append(f"Updated: {project['updated_at']}")
    return "\n".join(lines)


def create_project_tools(context: ToolContext) -> List[Any]:
    """Create project tools with injected session context."""
    base_url = _get_orchestrator_url()
    # Background officer (officer_knowledge_plane.md §4): get_current_project
    # stays as identity metadata, but must not carry an object-plane pointer —
    # the project cloud-folder link is trimmed. Strict `is True`: the config
    # dict may be a MagicMock in tests.
    tool_cfg = getattr(context, "config", None)
    officer_session = (
        isinstance(tool_cfg, dict) and tool_cfg.get("officer_session") is True
    )

    @tool
    async def get_current_project() -> str:
        """Get details for the project associated with this session.

        If this session is not scoped to a project, the tool returns a clear
        message instead of falling back to arbitrary user-visible projects.
        """
        project_id = _current_project_id(context)
        if not project_id:
            return (
                "This session is not scoped to a current project. "
                "Use a project-scoped session or create/list jobs directly."
            )

        async with _get_client(user_id=context.user_id) as client:
            try:
                resp = await client.get(f"{base_url}/api/projects/{project_id}")
                resp.raise_for_status()
                return _format_project(
                    resp.json(), include_cloud_link=not officer_session
                )
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:
                    return f"Project '{project_id}' not found or not visible."
                return f"Failed to get current project: HTTP {e.response.status_code}"
            except httpx.RequestError as e:
                return f"Failed to connect to orchestrator: {e}"

    @tool
    async def list_project_jobs(
        project_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 20,
    ) -> str:
        """List jobs in the current project or a specified project.

        Args:
            project_id: Optional project UUID. Defaults to this session's project.
            status: Optional status filter.
            limit: Maximum jobs to return (default: 20).

        Returns:
            Formatted project job list with full job IDs.
        """
        effective_project_id = project_id or _current_project_id(context)
        if not effective_project_id:
            return (
                "No project_id was provided and this session is not scoped to "
                "a current project."
            )

        params: Dict[str, Any] = {"limit": limit}
        if status:
            params["status"] = status

        async with _get_client(user_id=context.user_id) as client:
            try:
                resp = await client.get(
                    f"{base_url}/api/projects/{effective_project_id}/jobs",
                    params=params,
                )
                resp.raise_for_status()
                data = resp.json()
                jobs = data if isinstance(data, list) else data.get("jobs", [])

                if not jobs:
                    filter_msg = f" with status='{status}'" if status else ""
                    return (
                        f"No jobs found for project {effective_project_id}{filter_msg}."
                    )

                lines = [
                    f"Found {len(jobs)} job(s) for project {effective_project_id}:\n"
                ]
                for job in jobs:
                    lines.extend(_format_job_list_item(job))
                    lines.append("")
                return "\n".join(lines)
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:
                    return f"Project '{effective_project_id}' not found or not visible."
                return f"Failed to list project jobs: HTTP {e.response.status_code}"
            except httpx.RequestError as e:
                return f"Failed to connect to orchestrator: {e}"

    return [get_current_project, list_project_jobs]
