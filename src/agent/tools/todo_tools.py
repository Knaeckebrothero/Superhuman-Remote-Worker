"""Todo tools - Claude Code TodoWrite pattern.

Provides LangGraph-compatible tools for managing short-term todos
within the two-tier planning model:

- Strategic planning: Long-term plans in workspace filesystem
- Tactical execution: Short-term todos managed by these tools

The primary tool is `todo_write` which follows the Claude Code TodoWrite pattern -
submitting the entire todo list as a single atomic operation.

NOTE: The phase transition code in this module is legacy.
For the new nested loop graph (graph.py), phase transitions
are handled structurally via graph nodes, not via tool callbacks.
The transition code is kept for backwards compatibility with graph.py.
"""

import json
import logging
from typing import List, Optional

from langchain_core.tools import tool

from .context import ToolContext
from ..core.transitions import (
    PhaseTransitionManager,
    TransitionResult,
    TransitionType,
    get_job_completion_todos,
)

logger = logging.getLogger(__name__)


# LEGACY: Store the last transition result for the old graph.py to pick up
# Not used by the new nested loop graph (graph.py)
_last_transition_result: Optional[TransitionResult] = None


def get_last_transition_result() -> Optional[TransitionResult]:
    """Get the last phase transition result.

    LEGACY: Used by old graph.py. The new nested loop graph handles
    transitions via graph nodes, not this callback mechanism.

    This is called by the graph to check if a phase transition should occur.
    After retrieval, the result is cleared.

    Returns:
        TransitionResult if a transition should occur, None otherwise
    """
    global _last_transition_result
    result = _last_transition_result
    _last_transition_result = None
    return result


def _set_transition_result(result: TransitionResult) -> None:
    """Set the transition result for the graph to pick up.

    LEGACY: Used by old graph.py.
    """
    global _last_transition_result
    _last_transition_result = result


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
            - Phase transition instructions (if this was the last task)

        Note:
            When you complete the last task in a phase, this tool automatically
            triggers phase transition. You'll receive instructions to:
            1. Read main_plan.md
            2. Update the plan
            3. Create new todos for the next phase
        """
        try:
            result = todo_mgr.complete_first_pending_sync()
            message = result["message"]

            # Check if this was the last task in the phase
            if result.get("is_last_task", False):
                logger.info("Last task in phase completed, checking for phase transition")

                # Create phase transition manager
                transition_mgr = PhaseTransitionManager(
                    workspace_manager=context.workspace_manager if context.has_workspace() else None,
                    todo_manager=todo_mgr,
                )

                # Check for transition
                transition_result = transition_mgr.check_transition(is_last_task=True)

                if transition_result.should_transition:
                    # Execute the transition (archive todos, etc.)
                    execution = transition_mgr.execute_transition(transition_result)
                    logger.info(f"Phase transition executed: {execution}")

                    # Store the result for the graph to pick up
                    _set_transition_result(transition_result)

                    # If all phases are complete, automatically inject job completion todos
                    if transition_result.transition_type == TransitionType.JOB_COMPLETE:
                        logger.info("All phases complete, injecting job completion todos")
                        completion_todos = get_job_completion_todos()
                        todo_mgr.set_todos_from_list(completion_todos)
                        todo_mgr.set_phase_info(
                            phase_number=0,  # Special phase for job completion
                            total_phases=0,
                            phase_name="Job Completion",
                        )

                    # Append the transition prompt to the message
                    message += "\n\n" + transition_result.transition_prompt

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
        3. Trigger a rewind transition with recovery instructions

        After calling this, you will receive detailed instructions to:
        1. Read main_plan.md to review overall strategy
        2. Read workspace_summary.md for current state
        3. Update main_plan.md if needed
        4. Create new todos with todo_write()

        Args:
            issue: Description of WHY the current approach isn't working.
                   Be specific - this helps when reviewing failed approaches later.
                   Example: "The API doesn't support batch operations >100 items"

        Returns:
            Rewind transition instructions for recovery
        """
        if not issue or not issue.strip():
            return "Error: You must provide an 'issue' describing why the rewind is needed."

        try:
            # Archive with failure note
            result = todo_mgr.archive_with_failure_note(issue.strip())

            # Create phase transition manager for rewind transition
            transition_mgr = PhaseTransitionManager(
                workspace_manager=context.workspace_manager if context.has_workspace() else None,
                todo_manager=todo_mgr,
            )

            # Get rewind transition result
            transition_result = transition_mgr.check_rewind_transition(issue.strip())

            # Store the result for the graph to pick up
            _set_transition_result(transition_result)

            # Return the rewind transition prompt
            return transition_result.transition_prompt

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
    "todo_complete": {
        "module": "todo_tools",
        "function": "todo_complete",
        "description": "Mark the current task as complete and show next task",
        "category": "todo",
    },
    "todo_rewind": {
        "module": "todo_tools",
        "function": "todo_rewind",
        "description": "Panic button - abandon current approach and re-plan",
        "category": "todo",
    },
}
