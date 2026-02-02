"""Core toolkit - task management and job lifecycle.

This toolkit is always loaded as it provides essential agent functionality.
It includes:
- Todo tools: Task tracking (next_phase_todos, todo_complete, todo_list, todo_rewind)
- Job tools: Completion signaling (mark_complete, job_complete)
"""

from typing import Any, Dict, List

from ..context import ToolContext


def create_core_tools(context: ToolContext) -> List[Any]:
    """Create all core tools with injected context.

    Args:
        context: ToolContext with dependencies

    Returns:
        List of LangChain tool functions
    """
    from .todo import create_todo_tools
    from .job import create_job_tools

    tools = []

    if context.has_todo():
        tools.extend(create_todo_tools(context))

    if context.has_workspace():
        tools.extend(create_job_tools(context))

    return tools


def get_core_metadata() -> Dict[str, Dict[str, Any]]:
    """Get metadata for all core tools."""
    from .todo import TODO_TOOLS_METADATA
    from .job import JOB_TOOLS_METADATA

    return {**TODO_TOOLS_METADATA, **JOB_TOOLS_METADATA}


__all__ = [
    "create_core_tools",
    "get_core_metadata",
]
