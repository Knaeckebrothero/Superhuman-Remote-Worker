"""Shell toolkit - shell command execution.

This toolkit provides tools in two modes (configured via shell.mode):

Stateless mode (default):
- run_command: Simple command→output execution (hidden persistent tab underneath)
- shell_read: Read more output from scrollback when needed

Persistent mode (opt-in via shell.mode: persistent):
- shell_execute: Full tab management, keystrokes, async commands
- shell_read: Read output from any named terminal tab

Available in both strategic and tactical phases. Requires tmux + ShellManager.
"""

from typing import Any, Dict, List

from ..context import ToolContext


def create_shell_tools(context: ToolContext) -> List[Any]:
    """Create all shell tools with injected context.

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


def get_shell_metadata() -> Dict[str, Dict[str, Any]]:
    """Get metadata for all shell tools."""
    from .coding_tools import CODING_TOOLS_METADATA
    from .shell_tools import SHELL_TOOLS_METADATA

    return {**CODING_TOOLS_METADATA, **SHELL_TOOLS_METADATA}
