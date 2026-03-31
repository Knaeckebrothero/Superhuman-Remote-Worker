"""Orchestrator toolkit — job delegation tools for persistent agents.

Provides tools for persistent agents to create, monitor, and manage
worker jobs via the orchestrator REST API. These tools enable the
persistent agent to delegate heavy work to the worker pool while
maintaining an interactive session with the user.
"""

from typing import Any, Dict, List

from ..context import ToolContext


def create_orchestrator_tools(context: ToolContext) -> List[Any]:
    """Create orchestrator job management tools.

    Args:
        context: ToolContext (needs config with orchestrator_url)

    Returns:
        List of LangChain tool functions
    """
    from .jobs import create_orchestrator_tools as _create

    return _create(context)


def get_orchestrator_metadata() -> Dict[str, Dict[str, Any]]:
    """Get metadata for all orchestrator tools."""
    from .jobs import ORCHESTRATOR_TOOLS_METADATA

    return ORCHESTRATOR_TOOLS_METADATA
