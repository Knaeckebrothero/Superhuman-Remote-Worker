"""Todo manager for nested loop graph architecture.

This module provides the TodoManager for tracking task execution
within the graph's inner loop. The manager is stateful - it holds
todos in memory until explicitly archived.

The TodoManager is used by graph nodes to:
- Create todos from plan phases
- Track completion during execution
- Archive completed todos at phase transitions
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING, Any, Dict, List, Optional

import yaml

if TYPE_CHECKING:
    from ..core.workspace import WorkspaceManager

logger = logging.getLogger(__name__)


class TodoStatus(Enum):
    """Status values for todo items."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


@dataclass
class TodoItem:
    """A single todo item.

    Attributes:
        id: Unique identifier (e.g., "todo_1")
        content: Task description
        status: Current status (pending, in_progress, completed)
        priority: Priority level ("high", "medium", "low")
        notes: Completion notes or comments
        created_at: When the todo was created
    """

    id: str
    content: str
    status: TodoStatus = TodoStatus.PENDING
    priority: str = "medium"
    notes: List[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> Dict[str, Any]:
        """Serialize todo item to dictionary."""
        return {
            "id": self.id,
            "content": self.content,
            "status": self.status.value,
            "priority": self.priority,
            "notes": self.notes,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TodoItem":
        """Deserialize todo item from dictionary."""
        return cls(
            id=data["id"],
            content=data["content"],
            status=TodoStatus(data.get("status", "pending")),
            priority=data.get("priority", "medium"),
            notes=data.get("notes", []),
            created_at=(
                datetime.fromisoformat(data["created_at"])
                if data.get("created_at")
                else datetime.now(timezone.utc)
            ),
        )


class TodoManager:
    """Stateful manager for todo list operations.

    The TodoManager holds todos in memory and provides operations for
    the graph's inner execution loop. Todos are created when entering
    a plan phase and archived when the phase completes.

    Key design principles:
    - Stateful: Todos live in memory until archived
    - Graph-focused: API matches what graph nodes need
    - Workspace-backed: Archives write to workspace filesystem

    Example:
        ```python
        # Create manager with workspace
        todo_mgr = TodoManager(workspace)

        # Add todos for current phase
        todo_mgr.add("Extract document chunks")
        todo_mgr.add("Identify requirements", priority="high")

        # Track progress
        pending = todo_mgr.list_pending()
        todo_mgr.complete("todo_1", notes=["Processed 15 chunks"])

        # Archive at phase end
        todo_mgr.archive("extraction")
        ```
    """

    def __init__(
        self,
        workspace: "WorkspaceManager",
        min_todos: int = 5,
        max_todos: int = 20,
    ):
        """Initialize todo manager.

        Args:
            workspace: WorkspaceManager for archive operations
            min_todos: Minimum todos required for tactical phases (default: 5)
            max_todos: Maximum todos allowed for tactical phases (default: 20)
        """
        self._workspace = workspace
        self._todos: List[TodoItem] = []
        self._next_id = 1
        self._min_todos = min_todos
        self._max_todos = max_todos
        # Staging for next phase todos
        self._staged_todos: List[TodoItem] = []
        self._staged_phase_name: str = ""

    def add(self, content: str, priority: str = "medium") -> TodoItem:
        """Add a new todo item.

        Args:
            content: Task description
            priority: Priority level ("high", "medium", "low")

        Returns:
            Created TodoItem
        """
        item = TodoItem(
            id=f"todo_{self._next_id}",
            content=content,
            priority=priority,
        )
        self._todos.append(item)
        self._next_id += 1

        logger.info(f"Added todo: {item.id} - {content} [{priority}]")
        return item

    def complete(
        self, todo_id: str, notes: Optional[List[str]] = None
    ) -> Optional[TodoItem]:
        """Mark a todo as completed.

        Args:
            todo_id: Todo ID to complete
            notes: Optional completion notes

        Returns:
            Updated TodoItem or None if not found
        """
        for todo in self._todos:
            if todo.id == todo_id:
                todo.status = TodoStatus.COMPLETED
                if notes:
                    todo.notes.extend(notes)
                logger.info(f"Completed todo: {todo_id}")
                return todo

        logger.warning(f"Todo not found: {todo_id}")
        return None

    def start(self, todo_id: str) -> Optional[TodoItem]:
        """Mark a todo as in progress.

        Args:
            todo_id: Todo ID to start

        Returns:
            Updated TodoItem or None if not found
        """
        for todo in self._todos:
            if todo.id == todo_id:
                todo.status = TodoStatus.IN_PROGRESS
                logger.info(f"Started todo: {todo_id}")
                return todo

        logger.warning(f"Todo not found: {todo_id}")
        return None

    def get(self, todo_id: str) -> Optional[TodoItem]:
        """Get a todo by ID.

        Args:
            todo_id: Todo ID

        Returns:
            TodoItem or None if not found
        """
        for todo in self._todos:
            if todo.id == todo_id:
                return todo
        return None

    def list_all(self) -> List[TodoItem]:
        """List all todo items.

        Returns:
            List of all TodoItems
        """
        return self._todos.copy()

    def list_pending(self) -> List[TodoItem]:
        """List pending todo items (not completed).

        Includes both PENDING and IN_PROGRESS items, sorted by:
        1. Priority (high > medium > low)
        2. Creation time (earlier first)

        Returns:
            List of pending TodoItems
        """
        priority_order = {"high": 0, "medium": 1, "low": 2}
        pending = [t for t in self._todos if t.status != TodoStatus.COMPLETED]
        return sorted(
            pending, key=lambda t: (priority_order.get(t.priority, 1), t.created_at)
        )

    def all_complete(self) -> bool:
        """Check if all todos are complete.

        Returns:
            True if all todos are completed, False if empty or incomplete
        """
        if not self._todos:
            return False
        return all(t.status == TodoStatus.COMPLETED for t in self._todos)

    def format_for_display(self) -> str:
        """Format todos for Layer 2 injection.

        Creates a compact, readable format suitable for injecting
        into the agent's context as a status reminder.

        Returns:
            Formatted string for display
        """
        if not self._todos:
            return "No active todos."

        lines = ["## Current Todos"]

        # In progress
        in_progress = [t for t in self._todos if t.status == TodoStatus.IN_PROGRESS]
        if in_progress:
            lines.append("")
            lines.append("**In Progress:**")
            for todo in in_progress:
                lines.append(f"  - [{todo.id}] {todo.content}")

        # Pending
        pending = [t for t in self._todos if t.status == TodoStatus.PENDING]
        if pending:
            lines.append("")
            lines.append("**Pending:**")
            # Sort by priority
            priority_order = {"high": 0, "medium": 1, "low": 2}
            pending_sorted = sorted(
                pending, key=lambda t: priority_order.get(t.priority, 1)
            )
            for todo in pending_sorted:
                marker = "[!] " if todo.priority == "high" else ""
                lines.append(f"  - [{todo.id}] {marker}{todo.content}")

        # Completed count
        completed = [t for t in self._todos if t.status == TodoStatus.COMPLETED]
        if completed:
            lines.append("")
            lines.append(f"**Completed:** {len(completed)}/{len(self._todos)}")

        return "\n".join(lines)

    def archive(self, phase_name: str = "") -> str:
        """Archive todos to workspace and clear the list.

        Writes completed todos as markdown to archive/ directory,
        then clears the internal list for the next phase.

        Args:
            phase_name: Optional name for the archived phase

        Returns:
            Path to archive file
        """
        if not self._todos:
            logger.info("No todos to archive")
            return ""

        # Generate archive content
        lines = []
        timestamp = datetime.now(timezone.utc)

        # Header
        if phase_name:
            lines.append(f"# Archived Todos: {phase_name}")
        else:
            lines.append("# Archived Todos")
        lines.append(f"Archived: {timestamp.isoformat()}")
        lines.append("")

        # Completed
        completed = [t for t in self._todos if t.status == TodoStatus.COMPLETED]
        if completed:
            lines.append(f"## Completed ({len(completed)})")
            for todo in completed:
                lines.append(f"- [x] {todo.content}")
                for note in todo.notes:
                    lines.append(f"  - {note}")
            lines.append("")

        # Not completed
        not_completed = [t for t in self._todos if t.status != TodoStatus.COMPLETED]
        if not_completed:
            lines.append(f"## Not Completed ({len(not_completed)})")
            for todo in not_completed:
                status_mark = "~" if todo.status == TodoStatus.IN_PROGRESS else " "
                lines.append(f"- [{status_mark}] {todo.content}")
            lines.append("")

        # Summary
        lines.append("## Summary")
        lines.append(f"- Total: {len(self._todos)}")
        lines.append(f"- Completed: {len(completed)}")
        lines.append(f"- Not completed: {len(not_completed)}")

        content = "\n".join(lines)

        # Generate filename
        ts_str = timestamp.strftime("%Y%m%d_%H%M%S")
        if phase_name:
            safe_name = "".join(c if c.isalnum() or c in "_-" else "_" for c in phase_name)
            filename = f"todos_{safe_name}_{ts_str}.md"
        else:
            filename = f"todos_{ts_str}.md"

        archive_path = f"archive/{filename}"

        # Write to workspace
        self._workspace.write_file(archive_path, content)
        logger.info(f"Archived {len(self._todos)} todos to {archive_path}")

        # Clear the list
        self._todos = []
        self._next_id = 1

        return archive_path

    def clear(self) -> None:
        """Clear all todos without archiving."""
        count = len(self._todos)
        self._todos = []
        self._next_id = 1
        logger.info(f"Cleared {count} todos without archiving")

    # =========================================================================
    # Staging methods for next phase todos
    # =========================================================================

    def stage_tactical_todos(
        self,
        todos: List[str],
        phase_name: str = "",
    ) -> str:
        """Stage todos for the next tactical phase.

        This method validates the todos and stores them in a staging area.
        The staged todos will be applied when transitioning to tactical phase.

        Args:
            todos: List of task descriptions (min 10 chars each)
            phase_name: Optional name for the phase

        Returns:
            Success message or error message if validation fails

        Raises:
            ValueError: If validation fails
        """
        # Validate count
        if len(todos) < self._min_todos:
            raise ValueError(
                f"Too few todos: {len(todos)} < {self._min_todos}. "
                f"Create more detailed, actionable tasks."
            )
        if len(todos) > self._max_todos:
            raise ValueError(
                f"Too many todos: {len(todos)} > {self._max_todos}. "
                f"Split into multiple phases."
            )

        # Validate each todo content
        for i, content in enumerate(todos):
            if not isinstance(content, str):
                raise ValueError(f"Todo #{i + 1}: content must be a string")
            if len(content.strip()) < 10:
                raise ValueError(
                    f"Todo #{i + 1}: content too short ({len(content.strip())} chars). "
                    f"Provide a meaningful task description."
                )

        # Create staged todo items
        self._staged_todos = []
        for i, content in enumerate(todos):
            item = TodoItem(
                id=f"todo_{i + 1}",
                content=content.strip(),
                priority="medium",
                status=TodoStatus.PENDING,
            )
            self._staged_todos.append(item)

        self._staged_phase_name = phase_name

        logger.info(f"Staged {len(self._staged_todos)} todos for next phase")
        return (
            f"Staged {len(self._staged_todos)} todos for the next tactical phase"
            + (f" ({phase_name})" if phase_name else "")
            + "."
        )

    def has_staged_todos(self) -> bool:
        """Check if there are staged todos for the next phase.

        Returns:
            True if there are staged todos waiting to be applied
        """
        return len(self._staged_todos) > 0

    def get_staged_phase_name(self) -> str:
        """Get the name of the staged phase.

        Returns:
            Phase name or empty string if not set
        """
        return self._staged_phase_name

    def apply_staged_todos(self) -> None:
        """Apply staged todos to the active todo list.

        Moves todos from the staging area to the active list,
        clearing the current todos and staging area.
        """
        if not self._staged_todos:
            logger.warning("No staged todos to apply")
            return

        # Clear current todos and apply staged
        self._todos = self._staged_todos.copy()
        self._next_id = len(self._todos) + 1

        count = len(self._todos)
        phase_name = self._staged_phase_name

        # Clear staging
        self._staged_todos = []
        self._staged_phase_name = ""

        logger.info(f"Applied {count} staged todos" + (f" ({phase_name})" if phase_name else ""))

    def clear_staged_todos(self) -> None:
        """Clear staged todos without applying them."""
        count = len(self._staged_todos)
        self._staged_todos = []
        self._staged_phase_name = ""
        if count > 0:
            logger.info(f"Cleared {count} staged todos")

    def log_state(self) -> None:
        """Log current todo state for monitoring."""
        total = len(self._todos)
        completed = len([t for t in self._todos if t.status == TodoStatus.COMPLETED])
        in_progress = len([t for t in self._todos if t.status == TodoStatus.IN_PROGRESS])
        pending = len([t for t in self._todos if t.status == TodoStatus.PENDING])

        logger.info(
            f"Todo state: total={total}, completed={completed}, "
            f"in_progress={in_progress}, pending={pending}"
        )

    def get_progress(self) -> Dict[str, Any]:
        """Get progress statistics.

        Returns:
            Dictionary with progress metrics
        """
        total = len(self._todos)
        completed = len([t for t in self._todos if t.status == TodoStatus.COMPLETED])

        return {
            "total": total,
            "completed": completed,
            "pending": total - completed,
            "percentage": round((completed / total * 100) if total > 0 else 0, 1),
        }

    # =========================================================================
    # Compatibility methods for todo_tools.py
    # =========================================================================

    def set_todos_from_list(self, todo_list: List[Dict[str, Any]]) -> str:
        """Replace all todos with items from a list of dictionaries.

        This is used by the next_phase_todos tool to atomically replace the
        entire todo list.

        Args:
            todo_list: List of todo dictionaries with keys:
                - content (str): Task description (required)
                - status (str): "pending", "in_progress", or "completed"
                - priority (str): "high", "medium", or "low" (optional)
                - id (str): Todo ID (optional, auto-generated if missing)

        Returns:
            Formatted summary of the updated todo list
        """
        # Clear existing todos
        self._todos = []
        self._next_id = 1

        # Add each todo from the list
        for item in todo_list:
            content = item.get("content", "")
            status_str = item.get("status", "pending")
            priority = item.get("priority", "medium")
            todo_id = item.get("id")

            # Create todo item
            if todo_id:
                todo = TodoItem(
                    id=todo_id,
                    content=content,
                    priority=priority,
                    status=TodoStatus(status_str),
                )
            else:
                todo = TodoItem(
                    id=f"todo_{self._next_id}",
                    content=content,
                    priority=priority,
                    status=TodoStatus(status_str),
                )
                self._next_id += 1

            self._todos.append(todo)

        logger.info(f"Set {len(self._todos)} todos from list")
        return self._format_todo_summary()

    def archive_and_reset(self, phase_name: str = "") -> str:
        """Archive todos and reset for next phase.

        Convenience method that calls archive() and returns a
        user-friendly message.

        Args:
            phase_name: Optional name for the archived phase

        Returns:
            Confirmation message with archive path
        """
        if not self._todos:
            return "No todos to archive. The todo list is already empty."

        count = len(self._todos)
        archive_path = self.archive(phase_name)

        return (
            f"Archived {count} todos to {archive_path}.\n"
            f"Todo list cleared. Ready for new phase.\n"
            f"Use next_phase_todos to add new tasks."
        )

    def complete_first_pending_sync(self) -> Dict[str, Any]:
        """Find and complete the first pending or in-progress task.

        Looks for tasks in this order:
        1. First in_progress task
        2. First pending task (by priority)

        Returns:
            Dictionary with:
                - message (str): Status message
                - completed_id (str): ID of completed task (if any)
                - is_last_task (bool): True if this was the last task
        """
        # Find first in_progress task
        in_progress = [t for t in self._todos if t.status == TodoStatus.IN_PROGRESS]
        if in_progress:
            target = in_progress[0]
        else:
            # Find first pending task
            pending = self.list_pending()
            if not pending:
                return {
                    "message": "No pending tasks to complete.",
                    "completed_id": None,
                    "is_last_task": self.all_complete(),  # False if empty, True only if todos exist and all complete
                }
            target = pending[0]

        # Complete it
        self.complete(target.id)

        # Check if all complete now
        is_last = self.all_complete()

        # Build message
        remaining = len(self.list_pending())
        if is_last:
            message = (
                f"Completed: {target.content}\n"
                f"All tasks complete! Ready for phase transition."
            )
        else:
            next_task = self.list_pending()[0] if remaining > 0 else None
            next_str = f"\nNext: {next_task.content}" if next_task else ""
            message = (
                f"Completed: {target.content}\n"
                f"Remaining: {remaining} tasks{next_str}"
            )

        return {
            "message": message,
            "completed_id": target.id,
            "is_last_task": is_last,
        }

    def _format_todo_summary(self) -> str:
        """Format a summary of the current todo list.

        Returns:
            Formatted summary string
        """
        if not self._todos:
            return "Todo list is empty."

        lines = []
        progress = self.get_progress()
        bar_len = 20
        filled = int(bar_len * progress["percentage"] / 100)
        bar = "█" * filled + "░" * (bar_len - filled)

        lines.append(f"Progress: [{bar}] {progress['percentage']}%")
        lines.append(f"Total: {progress['total']} | Completed: {progress['completed']} | Pending: {progress['pending']}")
        lines.append("")

        # Group by status
        in_progress = [t for t in self._todos if t.status == TodoStatus.IN_PROGRESS]
        pending = [t for t in self._todos if t.status == TodoStatus.PENDING]
        completed = [t for t in self._todos if t.status == TodoStatus.COMPLETED]

        if in_progress:
            lines.append("IN PROGRESS:")
            for t in in_progress:
                lines.append(f"  → {t.content}")

        if pending:
            lines.append("PENDING:")
            priority_order = {"high": 0, "medium": 1, "low": 2}
            for t in sorted(pending, key=lambda x: priority_order.get(x.priority, 1)):
                marker = "[!]" if t.priority == "high" else "[ ]"
                lines.append(f"  {marker} {t.content}")

        if completed:
            lines.append(f"COMPLETED: ({len(completed)} tasks)")
            for t in completed[:3]:  # Show last 3
                lines.append(f"  ✓ {t.content}")
            if len(completed) > 3:
                lines.append(f"  ... and {len(completed) - 3} more")

        return "\n".join(lines)

    def set_phase_info(
        self,
        phase_number: int = 0,
        total_phases: int = 0,
        phase_name: str = "",
    ) -> None:
        """Legacy compatibility stub - phase tracking is no longer used.

        In the new nested loop architecture, phase transitions are handled
        structurally by graph nodes, not via TodoManager state.

        Args:
            phase_number: Ignored
            total_phases: Ignored
            phase_name: Ignored
        """
        logger.debug(
            f"set_phase_info called (ignored): phase={phase_number}/{total_phases}, name={phase_name}"
        )

    def archive_with_failure_note(self, issue: str) -> str:
        """Archive todos with a failure note.

        Used by todo_rewind when the current approach isn't working.

        Args:
            issue: Description of why the approach failed

        Returns:
            Confirmation message
        """
        if not self._todos:
            return "No todos to archive."

        # Add failure note to archive content
        count = len(self._todos)
        phase_name = f"failed_{datetime.now(timezone.utc).strftime('%H%M%S')}"

        # Archive with phase name indicating failure
        archive_path = self.archive(phase_name)

        # Append failure note to the archive file
        note_content = f"\n\n## Failure Note\n\n{issue}\n"
        try:
            existing = self._workspace.read_file(archive_path)
            self._workspace.write_file(archive_path, existing + note_content)
        except Exception as e:
            logger.warning(f"Could not append failure note: {e}")

        return (
            f"Archived {count} todos with failure note to {archive_path}.\n"
            f"Issue: {issue}\n"
            f"Todo list cleared for re-planning."
        )

    # =========================================================================
    # State Persistence Methods (for checkpoint/resume support)
    # =========================================================================

    STATE_FILENAME = "todos_state.yaml"

    def save_state(self) -> str:
        """Save current todo state to workspace for resume support.

        Saves the current todos, next_id, and staged todos to a YAML file
        in the workspace. This allows resuming from exactly where execution
        left off after a crash or restart.

        Returns:
            Path to the saved state file
        """
        state = {
            "version": 1,
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "next_id": self._next_id,
            "todos": [todo.to_dict() for todo in self._todos],
            "staged_todos": [todo.to_dict() for todo in self._staged_todos],
            "staged_phase_name": self._staged_phase_name,
        }

        content = yaml.dump(state, default_flow_style=False, allow_unicode=True)
        self._workspace.write_file(self.STATE_FILENAME, content)

        logger.info(
            f"Saved todo state: {len(self._todos)} active, "
            f"{len(self._staged_todos)} staged"
        )
        return self.STATE_FILENAME

    def load_state(self) -> bool:
        """Load todo state from workspace if it exists.

        Restores todos, next_id, and staged todos from a previously saved
        state file. This is called during resume to restore the exact
        execution state.

        Returns:
            True if state was loaded, False if no state file exists
        """
        try:
            if not self._workspace.exists(self.STATE_FILENAME):
                logger.debug(f"No saved state file: {self.STATE_FILENAME}")
                return False

            content = self._workspace.read_file(self.STATE_FILENAME)
            state = yaml.safe_load(content)

            if not state or not isinstance(state, dict):
                logger.warning(f"Invalid state file format: {self.STATE_FILENAME}")
                return False

            # Restore active todos
            self._todos = []
            for todo_dict in state.get("todos", []):
                self._todos.append(TodoItem.from_dict(todo_dict))

            # Restore next_id
            self._next_id = state.get("next_id", len(self._todos) + 1)

            # Restore staged todos
            self._staged_todos = []
            for todo_dict in state.get("staged_todos", []):
                self._staged_todos.append(TodoItem.from_dict(todo_dict))
            self._staged_phase_name = state.get("staged_phase_name", "")

            logger.info(
                f"Loaded todo state: {len(self._todos)} active, "
                f"{len(self._staged_todos)} staged, next_id={self._next_id}"
            )
            return True

        except Exception as e:
            logger.warning(f"Failed to load todo state: {e}")
            return False

    def clear_saved_state(self) -> None:
        """Remove the saved state file.

        Called after successful archival or when starting a fresh phase
        to ensure stale state doesn't persist.
        """
        try:
            if self._workspace.exists(self.STATE_FILENAME):
                self._workspace.delete_file(self.STATE_FILENAME)
                logger.debug(f"Cleared saved state file: {self.STATE_FILENAME}")
        except Exception as e:
            logger.warning(f"Failed to clear state file: {e}")
