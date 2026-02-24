"""Coding toolkit - persistent shell sessions and Claude Code delegation.

This toolkit provides:
- shell: Execute commands in persistent tmux-backed terminal tabs
- shell_open/send/read/close/list: Manage persistent terminal tabs
- claude_code: Delegate tasks to Claude Code CLI sessions (requires `claude` in PATH)

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

    # Claude Code print-mode tool disabled — replaced by persistent tmux tab.
    # The agent interacts with Claude Code via shell_send/shell_read on the
    # "claude-code" tab instead. See shell_manager.py auto_start_claude_code.
    # from .claude_code import create_claude_code_tools as _create_claude_code_tools
    # tools += _create_claude_code_tools(context)

    # Conditionally include shell tools when ShellManager is available
    if context.shell_manager is not None:
        from .shell_tools import create_shell_tools as _create_shell_tools
        tools.extend(_create_shell_tools(context))

    return tools


def get_coding_metadata() -> Dict[str, Dict[str, Any]]:
    """Get metadata for all coding tools."""
    from .coding_tools import CODING_TOOLS_METADATA
    from .shell_tools import SHELL_TOOLS_METADATA

    # claude_code print-mode metadata excluded — tool replaced by persistent shell tab.
    # from .claude_code import CLAUDE_CODE_METADATA
    return {**CODING_TOOLS_METADATA, **SHELL_TOOLS_METADATA}
