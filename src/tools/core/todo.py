"""Todo management tools for the Universal Agent.

Provides task tracking and phase control tools that enable
the strategic/tactical phase alternation pattern.

In the phase alternation architecture:
- `next_phase_todos` is strategic-only (stages todos for tactical phases)
- `todo_complete` is shared (marks tasks done, triggers phase transitions)
- `todo_list` is shared (helps see current state)
- `todo_rewind` is tactical-only (escape hatch when stuck)
"""

import logging
from typing import Any, Dict, List

from langchain_core.tools import tool

from ..context import ToolContext

logger = logging.getLogger(__name__)

# Tool metadata for registry
# Phase availability:
#   - "strategic": Available only in strategic mode (planning)
#   - "tactical": Available only in tactical mode (execution)
#   - Both: Available in both modes
TODO_TOOLS_METADATA: Dict[str, Dict[str, Any]] = {
    "next_phase_todos": {
        "module": "core.todo",
        "function": "next_phase_todos",
        "description": "Stage todos for the next tactical phase",
        "category": "core",
        "phases": ["strategic"],  # Strategic-only: creates work for tactical phase
    },
    "todo_complete": {
        "module": "core.todo",
        "function": "todo_complete",
        "description": "Mark a single task as complete (one at a time)",
        "category": "core",
        "phases": ["strategic", "tactical"],  # Both: used in all phases
    },
    "todo_list": {
        "module": "core.todo",
        "function": "todo_list",
        "description": "List all todos with IDs and status",
        "category": "core",
        "phases": ["strategic", "tactical"],  # Both: helps see current state
    },
    "todo_rewind": {
        "module": "core.todo",
        "function": "todo_rewind",
        "description": "Panic button - abandon current approach and re-plan",
        "category": "core",
        "phases": ["tactical"],  # Tactical-only: escape hatch when stuck
    },
}


def create_todo_tools(context: ToolContext) -> List[Any]:
    """Create todo management tools with injected context.

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
    def next_phase_todos(todos: List[str], phase_name: str = "") -> str:
        """Create todos for the next tactical execution phase.

        This tool stages todos for the upcoming tactical phase. It takes a simple
        list of task strings and stages them directly in the TodoManager.

        Use this to:
        - Create actionable tasks for the next execution phase
        - Prepare work items after strategic planning is complete

        The tool validates:
        - Todo count (5-20 items required)
        - Each todo content (minimum 10 characters)

        After calling this tool, complete your current strategic todo by
        invoking the `todo_complete` tool. The phase transition will then
        apply the staged todos.

        Args:
            todos: List of task descriptions. Each must be at least 10 characters.
                   Example: ["Extract document text from PDF", "Chunk document into segments"]
            phase_name: Optional name for the phase (e.g., "Phase 1: Document Processing")

        Returns:
            Success message confirming todos were staged, or error if validation fails.
        """
        try:
            # Enforcement of the todo guide read is now config-driven via
            # instruction_files triggers (apply_instruction_enforcement wrapper);
            # the guide is the bundled "todo-guide" skill (Slice 3). Fallback check
            # for backward compat when no instruction_files are configured:
            if not context._instruction_files and not context.was_recently_read(
                "skills/todo-guide/SKILL.md"
            ):
                from src.services.guardrails import format_nudge

                model = (
                    context._llm_config.model
                    if context._llm_config is not None
                    else None
                )
                return format_nudge(
                    "read_file_required_error",
                    model=model,
                    file_path="skills/todo-guide/SKILL.md",
                    tool_name="next_phase_todos",
                )

            # Convert to list if it's a string (JSON)
            if isinstance(todos, str):
                import json

                todos = json.loads(todos)

            if not isinstance(todos, list):
                return "Error: todos must be a list of task descriptions"

            # Validate each item is a string
            for i, item in enumerate(todos):
                if not isinstance(item, str):
                    return f"Error: todo at index {i} must be a string, got {type(item).__name__}"

            # Use TodoManager's staging method which handles validation
            result = todo_mgr.stage_tactical_todos(todos, phase_name)

            # Check if all other strategic todos are complete
            remaining = [t for t in todo_mgr.list_pending()]
            if len(remaining) <= 1:
                # Only the current todo (create todos) remains
                return f"{result}\n\nAll strategic todos complete. Invoke the `todo_complete` tool to transition to tactical phase."
            else:
                remaining_list = "\n".join(f"  - {t.content}" for t in remaining)
                return f"{result}\n\nRemaining strategic todos before transition:\n{remaining_list}"

        except ValueError as e:
            return f"Error: {str(e)}"
        except Exception as e:
            logger.error(f"next_phase_todos error: {e}")
            return f"Error staging todos: {str(e)}"

    @tool
    def todo_complete(todo_id: str = "") -> str:
        """Mark a single task as complete.

        Call this tool AFTER you have finished working on a task.
        Complete one todo at a time — each call should reflect verified work
        for that specific task.

        Args:
            todo_id: Optional. The ID of the todo to complete (e.g., "todo_1").
                     If not provided, completes the first incomplete task
                     (in_progress first, then highest-priority pending).

        This is the primary rhythm of work:
        1. Work on the current task
        2. Invoke this tool when done — either with no arguments (completes
           the first pending task) or with todo_id set to a specific id
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
            if todo_id and todo_id.strip():
                # Reject comma-separated IDs — complete one todo at a time
                ids = [id.strip() for id in todo_id.split(",") if id.strip()]

                if len(ids) > 1:
                    return (
                        "Error: Complete one todo at a time. Each todo_complete call should "
                        "reflect verified work for that specific task.\n"
                        f"Invoke `todo_complete` with todo_id set to `{ids[0]}` first, "
                        "then invoke it again for each subsequent task."
                    )

                if len(ids) == 1:
                    # Single ID - use existing logic
                    todo = todo_mgr.complete(ids[0])
                    if not todo:
                        # List available todos to help the agent
                        available = todo_mgr.list_all()
                        if available:
                            todo_list_str = ", ".join(t.id for t in available)
                            return f"Error: Todo '{ids[0]}' not found. Available todos: {todo_list_str}"
                        return (
                            f"Error: Todo '{ids[0]}' not found. No todos in the list."
                        )

                    # Queue memory for completed todo with notes (Memory Light)
                    # Only store if notes contain substantive content (>50 chars)
                    if context.recall_store and todo.notes:
                        combined_notes = "; ".join(todo.notes)
                        if len(combined_notes) > 50:
                            from src.services.recall_store import extract_keywords

                            context.queue_memory(
                                content=f"Completed: {todo.content}\nOutcome: {combined_notes}",
                                keywords=extract_keywords(todo.content),
                                importance=0.7,
                                source="todo",
                                memory_type="procedural",
                                source_phase=todo_mgr.phase_number,
                            )

                    # Build response message
                    remaining = todo_mgr.list_pending()
                    is_last = todo_mgr.all_complete()

                    if is_last:
                        message = (
                            f"Completed: {todo.content}\n"
                            f"All tasks complete! Ready for phase transition."
                        )
                    else:
                        next_task = remaining[0] if remaining else None
                        next_str = f"\nNext: {next_task.content}" if next_task else ""
                        message = (
                            f"Completed: {todo.content}\n"
                            f"Remaining: {len(remaining)} tasks{next_str}"
                        )
            else:
                # Original behavior: complete first pending task
                result = todo_mgr.complete_first_pending_sync()
                message = result["message"]
                is_last = result.get("is_last_task", False)

            # Add phase transition signal if this was the last task
            if is_last:
                message += "\n\n[PHASE_COMPLETE] All tasks in this phase are done."
                logger.info("Last task completed - phase transition signal sent")

            return message

        except Exception as e:
            logger.error(f"todo_complete error: {e}")
            return f"Error completing task: {str(e)}"

    @tool
    def todo_list() -> str:
        """List all current todos with their IDs and status.

        Use this tool to see:
        - All todo IDs (for use with todo_complete)
        - Current status of each todo
        - Overall progress

        Returns:
            Formatted list of all todos with IDs and status.

        Example output:
            Current Todos (2/5 complete):
            - [x] todo_1: Extract document text (completed)
            - [x] todo_2: Chunk document (completed)
            - [ ] todo_3: Identify requirements (pending)
            - [ ] todo_4: Validate requirements (pending)
            - [ ] todo_5: Write to database (pending)
        """
        try:
            todos = todo_mgr.list_all()
            if not todos:
                return "No todos in the current list."

            completed = sum(1 for t in todos if t.status.value == "completed")
            total = len(todos)

            lines = [f"Current Todos ({completed}/{total} complete):"]
            for todo in todos:
                status_mark = "x" if todo.status.value == "completed" else " "
                status_text = f"({todo.status.value})"
                priority_mark = " [!]" if todo.priority == "high" else ""
                lines.append(
                    f"- [{status_mark}] {todo.id}: {todo.content}{priority_mark} {status_text}"
                )

            return "\n".join(lines)

        except Exception as e:
            logger.error(f"todo_list error: {e}")
            return f"Error listing todos: {str(e)}"

    @tool
    def todo_rewind(issue: str) -> str:
        """Panic button: Abandon current approach and re-plan.

        Use this tool when you realize the current approach isn't working and
        you need to reconsider your strategy. This is NOT for normal task
        completion — use the `todo_complete` tool for that.

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
        3. Create new todos with next_phase_todos()

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

            # Clear any staged todos from a prior next_phase_todos call —
            # the agent is abandoning the current approach, stale staged
            # todos should not block job_complete or re-planning.
            if todo_mgr.has_staged_todos():
                todo_mgr.clear_staged_todos()
                result += "\n(Cleared previously staged todos.)"

            # Return with re-planning instructions
            return (
                f"{result}\n\n"
                "To recover, please:\n"
                "1. Read plan.md to review the overall strategy\n"
                "2. Update plan.md if the approach needs to change\n"
                "3. Create new todos with next_phase_todos()"
            )

        except ValueError as e:
            return f"Error: {str(e)}"
        except Exception as e:
            logger.error(f"todo_rewind error: {e}")
            return f"Error during rewind: {str(e)}"

    # Return all todo tools
    return [
        next_phase_todos,
        todo_complete,
        todo_list,
        todo_rewind,
    ]
