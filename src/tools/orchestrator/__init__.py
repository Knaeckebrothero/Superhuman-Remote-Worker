"""Orchestrator toolkit — application tools for persistent agents.

Provides tools for persistent agents to inspect and steer SRW application
state through the orchestrator REST API.
"""

from typing import Any, Dict, List

from ..context import ToolContext


def create_orchestrator_tools(context: ToolContext) -> List[Any]:
    """Create orchestrator application tools.

    Args:
        context: ToolContext (needs config with orchestrator_url)

    Returns:
        List of LangChain tool functions
    """
    from .catalog import create_catalog_tools
    from .jobs import create_orchestrator_tools as _create_jobs
    from .projects import create_project_tools
    from .repositories import create_repository_tools
    from .workflows import create_workflow_tools

    return [
        *_create_jobs(context),
        *create_project_tools(context),
        *create_repository_tools(context),
        *create_catalog_tools(context),
        *create_workflow_tools(context),
    ]


def get_orchestrator_metadata() -> Dict[str, Dict[str, Any]]:
    """Get metadata for all orchestrator tools."""
    from .catalog import CATALOG_TOOLS_METADATA
    from .jobs import ORCHESTRATOR_TOOLS_METADATA
    from .projects import PROJECT_TOOLS_METADATA
    from .repositories import REPOSITORY_TOOLS_METADATA
    from .workflows import WORKFLOW_TOOLS_METADATA

    return {
        **ORCHESTRATOR_TOOLS_METADATA,
        **PROJECT_TOOLS_METADATA,
        **REPOSITORY_TOOLS_METADATA,
        **CATALOG_TOOLS_METADATA,
        **WORKFLOW_TOOLS_METADATA,
    }
