"""Delegation toolkit — subagent spawning tools.

Provides the `delegate_work` tool that allows agents to spawn 1-5 child jobs
as git worktree branches. Children run in parallel; the parent suspends
(checkpoint + wake) and resumes to review/merge results.

See docs/features/subagent_delegation.md for the full design.
"""

from typing import Any, Dict, List

from ..context import ToolContext


def create_delegation_tools(context: ToolContext) -> List[Any]:
    """Create all delegation tools with injected context.

    Args:
        context: ToolContext (must have workspace_manager, orchestrator_client, job_metadata)

    Returns:
        List of LangChain tool functions
    """
    from .delegate_work import create_delegation_tools as _create

    return _create(context)


def get_delegation_metadata() -> Dict[str, Dict[str, Any]]:
    """Get metadata for all delegation tools."""
    from .delegate_work import DELEGATION_TOOLS_METADATA

    return DELEGATION_TOOLS_METADATA
