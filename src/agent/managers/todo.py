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

    def __init__(self, workspace: "WorkspaceManager"):
        """Initialize todo manager.

        Args:
            workspace: WorkspaceManager for archive operations
        """
        self._workspace = workspace
        self._todos: List[TodoItem] = []
        self._next_id = 1

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
            True if all todos are completed (or list is empty)
        """
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
                lines.append(f"  - {todo.content}")

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
                marker = "[!]" if todo.priority == "high" else ""
                lines.append(f"  - {marker}{todo.content}")

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
