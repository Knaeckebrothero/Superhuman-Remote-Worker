"""Coding toolkit - shell command execution for coding agents.

This toolkit provides command execution capabilities:
- run_command: Execute shell commands with timeout and output truncation

Available in both strategic and tactical phases.
"""

from typing import Any, Dict, List

from ..context import ToolContext


def create_coding_tools(context: ToolContext) -> List[Any]:
    """Create all coding tools with injected context.

    Args:
        context: ToolContext with workspace_manager

    Returns:
        List of LangChain tool functions

    Raises:
        ValueError: If workspace manager not available in context
    """
    from .coding_tools import create_coding_tools as _create_coding_tools

    return _create_coding_tools(context)


def get_coding_metadata() -> Dict[str, Dict[str, Any]]:
    """Get metadata for all coding tools."""
    from .coding_tools import CODING_TOOLS_METADATA

    return CODING_TOOLS_METADATA
