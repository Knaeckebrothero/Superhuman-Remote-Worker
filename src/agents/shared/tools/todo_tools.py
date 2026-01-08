"""Todo tools for tactical execution tracking.

Provides LangGraph-compatible tools for managing short-term todos
within the two-tier planning model:

- Strategic planning: Long-term plans in workspace filesystem
- Tactical execution: Short-term todos managed by these tools

The tools wrap TodoManager operations and integrate with the workspace
for archive persistence.
"""

import logging
from typing import List

from langchain_core.tools import tool

from .context import ToolContext

logger = logging.getLogger(__name__)


def create_todo_tools(context: ToolContext) -> List:
    """Create todo tools bound to a specific context.

    Args:
        context: ToolContext with todo_manager (and optionally workspace_manager for archives)

    Returns:
        List of LangChain tool functions

    Raises:
        ValueError: If context doesn't have a todo_manager
    """
    if not context.has_todo():
        raise ValueError("ToolContext must have a todo_manager for todo tools")

    todo_mgr = context.todo_manager

    # If workspace manager is available, set it for archive operations
    if context.has_workspace():
        todo_mgr.set_workspace_manager(context.workspace_manager)

    @tool
    def add_todo(content: str, priority: int = 0) -> str:
        """Add a task to the current todo list.

        Use this to track concrete, actionable steps for the current phase.
        Break complex work into 10-20 small, specific tasks.

        Args:
            content: Task description (be specific and actionable)
            priority: Higher number = more important (default 0)

        Returns:
            Confirmation with todo ID

        Examples:
            add_todo("Extract text from document.pdf")
            add_todo("Validate requirement REQ-001 against metamodel", priority=1)
        """
        try:
            item = todo_mgr.add_sync(content, priority=priority)
            return f"Added: {item.id} - {content}"
        except Exception as e:
            logger.error(f"add_todo error: {e}")
            return f"Error adding todo: {str(e)}"

    @tool
    def complete_todo(todo_id: str, notes: str = "") -> str:
        """Mark a todo as complete.

        Call this immediately after finishing a task.
        Optionally add notes about what was accomplished or discovered.

        Args:
            todo_id: The todo ID (e.g., "todo_1")
            notes: Optional completion notes (findings, decisions, etc.)

        Returns:
            Confirmation or error message
        """
        try:
            item = todo_mgr.complete_sync(todo_id, notes=notes if notes else None)
            if item:
                return f"Completed: {todo_id}"
            return f"Todo not found: {todo_id}"
        except Exception as e:
            logger.error(f"complete_todo error: {e}")
            return f"Error completing todo: {str(e)}"

    @tool
    def start_todo(todo_id: str) -> str:
        """Mark a todo as in-progress.

        Use this when you begin working on a task to track what's active.

        Args:
            todo_id: The todo ID to start (e.g., "todo_1")

        Returns:
            Confirmation or error message
        """
        try:
            item = todo_mgr.start_sync(todo_id)
            if item:
                return f"Started: {todo_id} - {item.content}"
            return f"Todo not found: {todo_id}"
        except Exception as e:
            logger.error(f"start_todo error: {e}")
            return f"Error starting todo: {str(e)}"

    @tool
    def list_todos() -> str:
        """List all current todos with their status.

        Shows todos organized by status with visual indicators:
        - ○ pending
        - ◐ in progress
        - ● completed
        - ✗ blocked
        - − skipped

        Returns:
            Formatted list of all todos
        """
        try:
            items = todo_mgr.list_all_sync()
            return todo_mgr.format_list(items)
        except Exception as e:
            logger.error(f"list_todos error: {e}")
            return f"Error listing todos: {str(e)}"

    @tool
    def get_progress() -> str:
        """Get progress summary for current todos.

        Shows completion statistics to track phase progress.

        Returns:
            Progress summary with counts and percentage
        """
        try:
            progress = todo_mgr.get_progress_sync()
            return (
                f"Progress: {progress['completed']}/{progress['total']} "
                f"({progress['completion_percentage']}% complete)\n"
                f"In progress: {progress['in_progress']}, "
                f"Pending: {progress['pending']}, "
                f"Blocked: {progress['blocked']}"
            )
        except Exception as e:
            logger.error(f"get_progress error: {e}")
            return f"Error getting progress: {str(e)}"

    @tool
    def archive_and_reset(phase_name: str = "") -> str:
        """Archive completed todos and reset for next phase.

        This tool:
        1. Saves all current todos to workspace/archive/todos_<phase>_<timestamp>.md
        2. Clears the todo list
        3. Returns confirmation prompting you to add new todos

        Use this when:
        - Completing a phase of work
        - Before transitioning to a different type of task
        - When instructed to archive progress

        Args:
            phase_name: Optional name for the archived phase (e.g., "phase_1_extraction")

        Returns:
            Confirmation with archive path or error message
        """
        try:
            result = todo_mgr.archive_and_reset(phase_name=phase_name)
            return result
        except ValueError as e:
            return f"Error: {str(e)}"
        except Exception as e:
            logger.error(f"archive_and_reset error: {e}")
            return f"Error archiving todos: {str(e)}"

    @tool
    def get_next_todo() -> str:
        """Get the next pending task to work on.

        Returns the highest priority pending todo.

        Returns:
            Next todo to work on or message if none pending
        """
        try:
            item = todo_mgr.next_sync()
            if item:
                priority_note = f" [priority: {item.priority}]" if item.priority > 0 else ""
                return f"Next: {item.id} - {item.content}{priority_note}"
            return "No pending todos. All tasks complete or add new todos."
        except Exception as e:
            logger.error(f"get_next_todo error: {e}")
            return f"Error getting next todo: {str(e)}"

    # Return all todo tools
    return [
        add_todo,
        complete_todo,
        start_todo,
        list_todos,
        get_progress,
        archive_and_reset,
        get_next_todo,
    ]


# Tool metadata for registry
TODO_TOOLS_METADATA = {
    "add_todo": {
        "module": "todo_tools",
        "function": "add_todo",
        "description": "Add a task to the todo list",
        "category": "todo",
    },
    "complete_todo": {
        "module": "todo_tools",
        "function": "complete_todo",
        "description": "Mark a todo as complete",
        "category": "todo",
    },
    "start_todo": {
        "module": "todo_tools",
        "function": "start_todo",
        "description": "Mark a todo as in-progress",
        "category": "todo",
    },
    "list_todos": {
        "module": "todo_tools",
        "function": "list_todos",
        "description": "List all current todos",
        "category": "todo",
    },
    "get_progress": {
        "module": "todo_tools",
        "function": "get_progress",
        "description": "Get progress summary",
        "category": "todo",
    },
    "archive_and_reset": {
        "module": "todo_tools",
        "function": "archive_and_reset",
        "description": "Archive todos and reset for next phase",
        "category": "todo",
    },
    "get_next_todo": {
        "module": "todo_tools",
        "function": "get_next_todo",
        "description": "Get the next task to work on",
        "category": "todo",
    },
}
