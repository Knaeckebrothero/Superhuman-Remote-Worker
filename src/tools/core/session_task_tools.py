"""Session task tools for persistent interactive agents.

Provides lightweight task tracking during interactive sessions.
Unlike the phase-based todo tools, these have no phase coupling,
no minimum/maximum constraints, and no archiving.
"""

import logging
from typing import Any, Dict, List

from langchain_core.tools import tool

from ..context import ToolContext

logger = logging.getLogger(__name__)

SESSION_TASK_METADATA: Dict[str, Dict[str, Any]] = {
    "task_add": {
        "module": "core.session_task_tools",
        "function": "task_add",
        "description": "Add a task to the session task list",
        "category": "session_task",
        "phases": ["strategic", "tactical"],
    },
    "task_complete": {
        "module": "core.session_task_tools",
        "function": "task_complete",
        "description": "Mark a session task as completed",
        "category": "session_task",
        "phases": ["strategic", "tactical"],
    },
    "task_list": {
        "module": "core.session_task_tools",
        "function": "task_list",
        "description": "List all session tasks with status",
        "category": "session_task",
        "phases": ["strategic", "tactical"],
    },
}


def get_session_task_metadata() -> Dict[str, Dict[str, Any]]:
    """Return tool metadata for registry registration."""
    return SESSION_TASK_METADATA


def create_session_task_tools(context: ToolContext) -> List[Any]:
    """Create session task tools with injected context."""
    task_mgr = context.session_task_manager
    if not task_mgr:
        raise ValueError("ToolContext must have a session_task_manager for task tools")

    @tool
    def task_add(description: str, priority: str = "medium") -> str:
        """Add a task to track during this session.

        Use this to organize your work into discrete steps the user can
        follow. Tasks appear in the UI as a checklist.

        Args:
            description: What needs to be done (e.g., "Refactor auth middleware")
            priority: "high", "medium", or "low" (default: "medium")

        Returns:
            Confirmation with task ID and current task list.
        """
        if not description.strip():
            return "Error: description cannot be empty."
        if priority not in ("high", "medium", "low"):
            priority = "medium"
        task = task_mgr.add(description.strip(), priority)
        return f"Added {task.id}: {task.description}\n\n{task_mgr.format_for_display()}"

    @tool
    def task_complete(task_id: str, notes: str = "") -> str:
        """Mark a session task as completed.

        Args:
            task_id: The task ID (e.g., "task_1")
            notes: Optional completion notes

        Returns:
            Updated task list, or error if task not found.
        """
        task = task_mgr.complete(task_id.strip(), notes.strip())
        if not task:
            return f"Error: task '{task_id}' not found or already completed."
        return f"Completed {task.id}: {task.description}\n\n{task_mgr.format_for_display()}"

    @tool
    def task_list() -> str:
        """List all session tasks with their status.

        Returns:
            Formatted task list showing pending, in-progress, and completed items.
        """
        return task_mgr.format_for_display()

    return [task_add, task_complete, task_list]
