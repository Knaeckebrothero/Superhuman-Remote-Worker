"""Coding toolkit - persistent shell sessions.

This toolkit provides:
- shell_execute: Execute commands or send keystrokes in persistent tmux-backed terminal tabs
- shell_read: Read output from persistent terminal tabs with offset support

Available in both strategic and tactical phases. Requires tmux + ShellManager.
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

    tools = _create_coding_tools(context)

    # Include shell tools when ShellManager is available
    if context.shell_manager is not None:
        from .shell_tools import create_shell_tools as _create_shell_tools
        tools.extend(_create_shell_tools(context))

    return tools


def get_coding_metadata() -> Dict[str, Dict[str, Any]]:
    """Get metadata for all coding tools."""
    from .coding_tools import CODING_TOOLS_METADATA
    from .shell_tools import SHELL_TOOLS_METADATA

    return {**CODING_TOOLS_METADATA, **SHELL_TOOLS_METADATA}
