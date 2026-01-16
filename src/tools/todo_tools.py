"""Todo tools - Claude Code TodoWrite pattern.

Provides LangGraph-compatible tools for managing short-term todos
within the two-tier planning model:

- Strategic planning: Long-term plans in workspace filesystem
- Tactical execution: Short-term todos managed by these tools

The primary tool is `todo_write` which follows the Claude Code TodoWrite pattern -
submitting the entire todo list as a single atomic operation.

In the phase alternation architecture:
- `todo_write` is strategic-only (writes todos.yaml for tactical phases)
- `todo_complete` is shared (marks tasks done, triggers phase transitions)
"""

import json
import logging
from typing import Any, Dict, List

import yaml
from langchain_core.tools import tool

from .context import ToolContext

logger = logging.getLogger(__name__)


def create_todo_tools(context: ToolContext) -> List:
    """Create todo tools bound to a specific context.

    Args:
        context: ToolContext with todo_manager

    Returns:
        List of LangChain tool functions

    Raises:
        ValueError: If context doesn't have a todo_manager
    """
    if not context.has_todo():
        raise ValueError("ToolContext must have a todo_manager for todo tools")

    todo_mgr = context.todo_manager

    @tool
    def todo_write(todos: str, phase: str = "", description: str = "") -> str:
        """Update the todo list with a complete list of tasks.

        This tool replaces the entire todo list with the provided tasks AND
        writes them to todos.yaml for phase transitions. In strategic mode,
        this prepares the tactical phase's work items.

        Use it to:
        - Add new tasks for the upcoming tactical phase
        - Mark tasks as in_progress or completed
        - Remove tasks (by omitting them)
        - Reorder or reprioritize tasks

        IMPORTANT: Submit the COMPLETE list every time. Tasks not included
        will be removed. This ensures the todo list always reflects your
        current understanding of what needs to be done.

        Args:
            todos: JSON array of todo objects. Each object must have:
                - id (int): Unique task identifier
                - content (str): Task description (min 10 chars)

                Optional fields:
                - status (str): "pending", "in_progress", or "completed" (default: "pending")
                - priority (str): "high", "medium", or "low" (default: "medium")

            phase: Optional phase name (e.g., "Phase 1: Extract requirements")
            description: Optional description of what this phase accomplishes

        Returns:
            Formatted summary showing:
            - Updated todo list grouped by status
            - Progress bar and statistics
            - Confirmation that todos.yaml was written

        Examples:
            # Create todos for first tactical phase
            todo_write(
                todos='[
                    {"id": 1, "content": "Extract document text from uploaded PDF"},
                    {"id": 2, "content": "Chunk document into processable segments"},
                    {"id": 3, "content": "Identify requirements in each chunk"},
                    {"id": 4, "content": "Validate extracted requirements"},
                    {"id": 5, "content": "Write requirements to database"}
                ]',
                phase="Phase 1: Document Processing",
                description="Extract and process requirements from the source document"
            )

        Best practices:
        - Create 5-20 todos per phase (validated on phase transition)
        - Each todo should be concrete and actionable
        - Use descriptive phase names for tracking
        - Have at most ONE task "in_progress" at a time
        """
        try:
            # Parse JSON input
            if isinstance(todos, str):
                todo_list = json.loads(todos)
            else:
                todo_list = todos

            if not isinstance(todo_list, list):
                return "Error: todos must be a JSON array of todo objects"

            # Validate and normalize each todo
            normalized_todos: List[Dict[str, Any]] = []
            for i, todo in enumerate(todo_list):
                if not isinstance(todo, dict):
                    return f"Error: todo at index {i} must be an object, got {type(todo).__name__}"

                # Require id and content
                if "id" not in todo:
                    return f"Error: todo at index {i} missing required 'id' field"
                if "content" not in todo:
                    return f"Error: todo at index {i} missing required 'content' field"

                # Validate content length
                content = str(todo["content"]).strip()
                if len(content) < 10:
                    return f"Error: todo at index {i} content too short (min 10 chars)"

                # Normalize todo
                normalized_todos.append({
                    "id": int(todo["id"]),
                    "content": content,
                    "status": todo.get("status", "pending"),
                    "priority": todo.get("priority", "medium"),
                })

            # Validate count (5-20 todos)
            if len(normalized_todos) < 5:
                return f"Error: Need at least 5 todos for a phase, got {len(normalized_todos)}"
            if len(normalized_todos) > 20:
                return f"Error: Maximum 20 todos per phase, got {len(normalized_todos)}. Split into multiple phases."

            # Write todos.yaml to workspace
            yaml_content: Dict[str, Any] = {
                "todos": [{"id": t["id"], "content": t["content"]} for t in normalized_todos]
            }
            if phase:
                yaml_content["phase"] = phase
            if description:
                yaml_content["description"] = description

            yaml_str = yaml.dump(yaml_content, default_flow_style=False, allow_unicode=True)

            # Write to workspace
            if context.has_workspace():
                context.workspace_manager.write_file("todos.yaml", yaml_str)
                logger.info(f"Wrote {len(normalized_todos)} todos to todos.yaml")

            # Also update in-memory TodoManager
            result = todo_mgr.set_todos_from_list(normalized_todos)

            # Return combined result
            return f"{result}\n\n✓ Wrote todos.yaml ({len(normalized_todos)} todos)"

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

    @tool
    def todo_complete() -> str:
        """Mark the current task as complete.

        Call this tool AFTER you have finished working on a task. It finds the
        first incomplete task (in_progress first, then highest-priority pending)
        and marks it as completed.

        This is the primary rhythm of work:
        1. Work on the current task
        2. Call todo_complete() when done
        3. Read the response to see what's next
        4. Repeat

        Returns:
            Status message including:
            - Which task was completed
            - How many tasks remain
            - What the next task is (if any)
            - Phase transition signal when all tasks complete

        Phase Transitions:
            When the last task is completed, this tool signals a phase transition.
            - In tactical mode: System will transition to strategic mode
            - In strategic mode: System will validate todos.yaml and transition to tactical

        Note:
            The response includes "PHASE_COMPLETE" when all tasks are done.
            This is detected by the graph to trigger phase transitions.
        """
        try:
            result = todo_mgr.complete_first_pending_sync()
            message = result["message"]

            # Add phase transition signal if this was the last task
            if result.get("is_last_task", False):
                message += "\n\n[PHASE_COMPLETE] All tasks in this phase are done."
                logger.info("Last task completed - phase transition signal sent")

            return message

        except Exception as e:
            logger.error(f"todo_complete error: {e}")
            return f"Error completing task: {str(e)}"

    @tool
    def todo_rewind(issue: str) -> str:
        """Panic button: Abandon current approach and re-plan.

        Use this tool when you realize the current approach isn't working and
        you need to reconsider your strategy. This is NOT for normal task
        completion - use todo_complete() for that.

        When to use todo_rewind:
        - You've hit a dead end that makes the current tasks impossible
        - You discovered the approach is fundamentally flawed
        - You need to try a completely different strategy
        - An external constraint makes the current plan invalid

        This tool will:
        1. Archive your current todos with the failure reason
        2. Clear the todo list

        After calling this, you should:
        1. Read plan.md to review overall strategy
        2. Update plan.md if needed
        3. Create new todos with todo_write()

        Args:
            issue: Description of WHY the current approach isn't working.
                   Be specific - this helps when reviewing failed approaches later.
                   Example: "The API doesn't support batch operations >100 items"

        Returns:
            Confirmation message with instructions for re-planning
        """
        if not issue or not issue.strip():
            return "Error: You must provide an 'issue' describing why the rewind is needed."

        try:
            # Archive with failure note
            result = todo_mgr.archive_with_failure_note(issue.strip())

            # Return with re-planning instructions
            return (
                f"{result}\n\n"
                "To recover, please:\n"
                "1. Read plan.md to review the overall strategy\n"
                "2. Update plan.md if the approach needs to change\n"
                "3. Create new todos with todo_write()"
            )

        except ValueError as e:
            return f"Error: {str(e)}"
        except Exception as e:
            logger.error(f"todo_rewind error: {e}")
            return f"Error during rewind: {str(e)}"

    # Return all todo tools
    return [
        todo_write,
        archive_and_reset,
        todo_complete,
        todo_rewind,
    ]


# Tool metadata for registry
# Phase availability:
#   - "strategic": Available only in strategic mode (planning)
#   - "tactical": Available only in tactical mode (execution)
#   - "both": Available in both modes
TODO_TOOLS_METADATA = {
    "todo_write": {
        "module": "todo_tools",
        "function": "todo_write",
        "description": "Write todos for the next tactical phase to todos.yaml",
        "category": "todo",
        "phases": ["strategic"],  # Strategic-only: creates work for tactical phase
    },
    "archive_and_reset": {
        "module": "todo_tools",
        "function": "archive_and_reset",
        "description": "Archive todos and reset for next phase",
        "category": "todo",
        "phases": ["strategic"],  # Strategic-only: used during phase transitions
    },
    "todo_complete": {
        "module": "todo_tools",
        "function": "todo_complete",
        "description": "Mark the current task as complete and show next task",
        "category": "todo",
        "phases": ["strategic", "tactical"],  # Both: used in all phases
    },
    "todo_rewind": {
        "module": "todo_tools",
        "function": "todo_rewind",
        "description": "Panic button - abandon current approach and re-plan",
        "category": "todo",
        "phases": ["tactical"],  # Tactical-only: escape hatch when stuck
    },
}
