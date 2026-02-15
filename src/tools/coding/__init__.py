"""Coding toolkit - shell command execution and Claude Code delegation.

This toolkit provides:
- run_command: Execute shell commands with timeout and output truncation
- claude_code: Delegate tasks to Claude Code CLI sessions (requires `claude` in PATH)

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
    from .claude_code import create_claude_code_tools as _create_claude_code_tools

    return _create_coding_tools(context) + _create_claude_code_tools(context)


def get_coding_metadata() -> Dict[str, Dict[str, Any]]:
    """Get metadata for all coding tools."""
    from .coding_tools import CODING_TOOLS_METADATA
    from .claude_code import CLAUDE_CODE_METADATA

    return {**CODING_TOOLS_METADATA, **CLAUDE_CODE_METADATA}
