"""Todo tools - Claude Code TodoWrite pattern.

Provides LangGraph-compatible tools for managing short-term todos
within the two-tier planning model:

- Strategic planning: Long-term plans in workspace filesystem
- Tactical execution: Short-term todos managed by these tools

The primary tool is `todo_write` which follows the Claude Code TodoWrite pattern -
submitting the entire todo list as a single atomic operation.
"""

import json
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
    def todo_write(todos: str) -> str:
        """Update the todo list with a complete list of tasks.

        This tool replaces the entire todo list with the provided tasks.
        Use it to:
        - Add new tasks
        - Mark tasks as in_progress or completed
        - Remove tasks (by omitting them)
        - Reorder or reprioritize tasks

        IMPORTANT: Submit the COMPLETE list every time. Tasks not included
        will be removed. This ensures the todo list always reflects your
        current understanding of what needs to be done.

        Args:
            todos: JSON array of todo objects. Each object must have:
                - content (str): Task description
                - status (str): "pending", "in_progress", or "completed"

                Optional fields:
                - priority (str): "high", "medium", or "low" (default: "medium")
                - id (str): Todo ID to preserve (auto-generated if omitted)

        Returns:
            Formatted summary showing:
            - Updated todo list grouped by status
            - Progress bar and statistics
            - Hint about what to work on next

        Examples:
            # Start with initial tasks
            todo_write('[
                {"content": "Extract document text", "status": "pending", "priority": "high"},
                {"content": "Chunk document", "status": "pending"},
                {"content": "Identify requirements", "status": "pending"}
            ]')

            # After completing first task and starting second
            todo_write('[
                {"content": "Extract document text", "status": "completed"},
                {"content": "Chunk document", "status": "in_progress"},
                {"content": "Identify requirements", "status": "pending"}
            ]')

        Best practices:
        - Have at most ONE task "in_progress" at a time
        - Mark tasks "completed" ONLY when fully done
        - Use "high" priority for blocking/critical tasks
        - Keep 10-20 tasks for the current phase
        - Archive with `archive_and_reset` when changing phases
        """
        try:
            # Parse JSON input
            if isinstance(todos, str):
                todo_list = json.loads(todos)
            else:
                todo_list = todos

            if not isinstance(todo_list, list):
                return "Error: todos must be a JSON array of todo objects"

            # Validate each todo has required fields
            for i, todo in enumerate(todo_list):
                if not isinstance(todo, dict):
                    return f"Error: todo at index {i} must be an object, got {type(todo).__name__}"
                if "content" not in todo:
                    return f"Error: todo at index {i} missing required 'content' field"
                if "status" not in todo:
                    return f"Error: todo at index {i} missing required 'status' field"

            # Use the new set_todos_from_list method
            result = todo_mgr.set_todos_from_list(todo_list)
            return result

        except json.JSONDecodeError as e:
            return f"Error parsing JSON: {str(e)}. Ensure todos is a valid JSON array."
        except ValueError as e:
            return f"Error: {str(e)}"
        except Exception as e:
            logger.error(f"todo_write error: {e}")
            return f"Error updating todos: {str(e)}"

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

    # Return the two todo tools
    return [
        todo_write,
        archive_and_reset,
    ]


# Tool metadata for registry
TODO_TOOLS_METADATA = {
    "todo_write": {
        "module": "todo_tools",
        "function": "todo_write",
        "description": "Update the complete todo list with tasks and their statuses",
        "category": "todo",
    },
    "archive_and_reset": {
        "module": "todo_tools",
        "function": "archive_and_reset",
        "description": "Archive todos and reset for next phase",
        "category": "todo",
    },
}
