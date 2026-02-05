"""Git toolkit - workspace version control operations.

This toolkit provides git operations for agent workspaces:
- Commit history queries (log, show)
- Diff comparisons
- Status checking
- Phase tag listing

All tools are read-only and available in both strategic and tactical phases.
"""

from typing import Any, Dict, List

from ..context import ToolContext


def create_git_tools(context: ToolContext) -> List[Any]:
    """Create all git tools with injected context.

    Args:
        context: ToolContext with workspace_manager (which has git_manager)

    Returns:
        List of LangChain tool functions

    Raises:
        ValueError: If git manager not available in context
    """
    from .git_tools import create_git_tools as _create_git_tools

    return _create_git_tools(context)


def get_git_metadata() -> Dict[str, Dict[str, Any]]:
    """Get metadata for all git tools."""
    from .git_tools import GIT_TOOLS_METADATA

    return GIT_TOOLS_METADATA
