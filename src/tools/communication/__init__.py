"""Communication toolkit — messaging tools for agent-human interaction.

Provides tools for agents to send messages to humans (job owner, project
members) with async and blocking modes. Messages are stored as workspace
files and delivered via email through the orchestrator.
"""

from typing import Any, Dict, List

from ..context import ToolContext


def create_communication_tools(context: ToolContext) -> List[Any]:
    """Create all communication tools with injected context.

    Args:
        context: ToolContext (must have workspace_manager and job_id)

    Returns:
        List of LangChain tool functions
    """
    from .messaging import create_communication_tools as _create

    return _create(context)


def get_communication_metadata() -> Dict[str, Dict[str, Any]]:
    """Get metadata for all communication tools."""
    from .messaging import COMMUNICATION_TOOLS_METADATA

    return COMMUNICATION_TOOLS_METADATA
