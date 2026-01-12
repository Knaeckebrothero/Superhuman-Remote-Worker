"""Todo list management for autonomous agents.

DEPRECATION NOTICE:
This module is legacy code. For new development, use:
- src/agent/managers/todo.py - New TodoManager for nested loop graph

The legacy TodoManager in this file has additional features like phase tracking
and hierarchical todos that the todo_tools.py depends on. It is kept for
backwards compatibility with graph.py and todo_tools.py.

The new TodoManager in managers/ is simpler and designed for the
nested loop graph architecture (graph.py).

---

This module provides todo list functionality for agents to track their tasks,
breaking down complex jobs into manageable steps and monitoring progress.

The TodoManager supports a two-tier planning model:
- Strategic planning: Long-term plans stored as markdown files in the workspace
- Tactical execution: Short-term todos managed by this class, archived when complete

Todos are session-scoped (in-memory) until explicitly archived via archive_and_reset().
This creates natural checkpoints for context compaction.
"""

import logging
from typing import Optional, List, Dict, Any, TYPE_CHECKING
from datetime import datetime, timezone
from dataclasses import dataclass, field
from enum import Enum

if TYPE_CHECKING:
    from .workspace import WorkspaceManager

logger = logging.getLogger(__name__)


class TodoStatus(Enum):
    """Status values for todo items."""
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    SKIPPED = "skipped"


@dataclass
class TodoItem:
    """A single todo item."""
    id: str
    content: str
    status: TodoStatus = TodoStatus.PENDING
    priority: int = 0  # Higher = more important
    parent_id: Optional[str] = None  # For hierarchical todos
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize todo item."""
        return {
            "id": self.id,
            "content": self.content,
            "status": self.status.value,
            "priority": self.priority,
            "parent_id": self.parent_id,
            "metadata": self.metadata,
            "created_at": self.created_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TodoItem":
        """Deserialize todo item."""
        return cls(
            id=data["id"],
            content=data["content"],
            status=TodoStatus(data.get("status", "pending")),
            priority=data.get("priority", 0),
            parent_id=data.get("parent_id"),
            metadata=data.get("metadata", {}),
            created_at=datetime.fromisoformat(data["created_at"]) if data.get("created_at") else datetime.now(timezone.utc),
            started_at=datetime.fromisoformat(data["started_at"]) if data.get("started_at") else None,
            completed_at=datetime.fromisoformat(data["completed_at"]) if data.get("completed_at") else None,
            notes=data.get("notes", []),
        )


class TodoManager:
    """Manages a todo list for an autonomous agent.

    Provides methods for creating, updating, and querying todos.
    Supports two modes:

    1. Session-scoped (new, recommended for workspace-centric architecture):
       - Todos are in-memory until archived
       - Use archive_and_reset() to persist to workspace and clear
       - Natural checkpoint boundaries for context compaction

    2. Legacy persistence mode (for backward compatibility):
       - Pass a legacy Workspace instance with load_todo/save_todo methods
       - Todos are saved after each operation

    Example (session-scoped with archive):
        ```python
        # Create in-memory manager
        todo_mgr = TodoManager()

        # Add tasks for current phase
        todo_mgr.add_sync("Process document chunks")
        todo_mgr.add_sync("Extract requirement candidates")

        # Work through tasks
        task = todo_mgr.next_sync()
        todo_mgr.start_sync(task.id)
        # ... do work ...
        todo_mgr.complete_sync(task.id, notes=["Processed 15 chunks"])

        # Archive and reset for next phase
        workspace_manager = WorkspaceManager(job_id="123", base_path=Path("./workspace"))
        result = todo_mgr.archive_and_reset("phase_1", workspace_manager)
        # Saves to workspace/archive/todos_phase_1_20250108_143000.md
        ```
    """

    def __init__(
        self,
        workspace: Any = None,
        workspace_manager: Optional["WorkspaceManager"] = None,
        auto_reflection: bool = True,
        reflection_task_content: str = "Review plan, update progress, create next todos",
    ):
        """Initialize todo manager.

        Args:
            workspace: Legacy Workspace instance for persistence (optional)
            workspace_manager: WorkspaceManager for archive operations (optional)
            auto_reflection: If True, automatically append a reflection task to todo lists
            reflection_task_content: Content of the auto-appended reflection task
        """
        self._legacy_workspace = workspace
        self._workspace_manager = workspace_manager
        self._todos: List[TodoItem] = []
        self._next_id = 1
        self._loaded = False
        self._auto_reflection = auto_reflection
        self._reflection_task_content = reflection_task_content

        # Phase tracking for guardrails system
        self._phase_number: int = 0  # Current phase (0 = bootstrap/not set)
        self._total_phases: int = 0  # Total phases (0 = unknown)
        self._phase_name: str = ""   # Optional descriptive name for current phase

    async def _ensure_loaded(self) -> None:
        """Ensure todos are loaded from legacy workspace (async mode only)."""
        if not self._loaded:
            if self._legacy_workspace is not None:
                await self.load()
            self._loaded = True

    async def load(self) -> None:
        """Load todos from legacy workspace (async mode only)."""
        if self._legacy_workspace is None:
            logger.debug("No legacy workspace - using session-scoped mode")
            return

        data = await self._legacy_workspace.load_todo()
        if data:
            self._todos = [TodoItem.from_dict(item) for item in data]
            # Update next ID
            if self._todos:
                max_id = max(int(t.id.split("_")[1]) for t in self._todos if "_" in t.id)
                self._next_id = max_id + 1
        else:
            self._todos = []

        logger.debug(f"Loaded {len(self._todos)} todo items")

    async def save(self) -> None:
        """Save todos to legacy workspace (async mode only)."""
        if self._legacy_workspace is None:
            logger.debug("No legacy workspace - skipping save (session-scoped mode)")
            return

        await self._legacy_workspace.save_todo([t.to_dict() for t in self._todos])
        logger.debug(f"Saved {len(self._todos)} todo items")

    async def add(
        self,
        content: str,
        priority: int = 0,
        parent_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> TodoItem:
        """Add a new todo item.

        Args:
            content: Task description
            priority: Priority level (higher = more important)
            parent_id: Optional parent task ID for hierarchical todos
            metadata: Optional additional metadata

        Returns:
            Created TodoItem
        """
        await self._ensure_loaded()

        item = TodoItem(
            id=f"todo_{self._next_id}",
            content=content,
            priority=priority,
            parent_id=parent_id,
            metadata=metadata or {},
        )

        self._todos.append(item)
        self._next_id += 1
        await self.save()

        logger.info(f"Added todo: {item.id} - {content}")
        return item

    async def add_batch(self, items: List[Dict[str, Any]]) -> List[TodoItem]:
        """Add multiple todo items at once.

        Args:
            items: List of dicts with 'content' and optional 'priority', 'parent_id', 'metadata'

        Returns:
            List of created TodoItems
        """
        await self._ensure_loaded()

        created = []
        for item_data in items:
            item = TodoItem(
                id=f"todo_{self._next_id}",
                content=item_data["content"],
                priority=item_data.get("priority", 0),
                parent_id=item_data.get("parent_id"),
                metadata=item_data.get("metadata", {}),
            )
            self._todos.append(item)
            self._next_id += 1
            created.append(item)

        await self.save()
        logger.info(f"Added {len(created)} todo items")
        return created

    async def get(self, todo_id: str) -> Optional[TodoItem]:
        """Get a todo item by ID.

        Args:
            todo_id: Todo ID

        Returns:
            TodoItem or None if not found
        """
        await self._ensure_loaded()

        for todo in self._todos:
            if todo.id == todo_id:
                return todo
        return None

    async def next(self) -> Optional[TodoItem]:
        """Get the next pending todo item to work on.

        Returns the highest priority pending item.

        Returns:
            Next TodoItem or None if no pending items
        """
        await self._ensure_loaded()

        pending = [t for t in self._todos if t.status == TodoStatus.PENDING]
        if not pending:
            return None

        # Sort by priority (descending) then by creation time
        pending.sort(key=lambda t: (-t.priority, t.created_at))
        return pending[0]

    async def start(self, todo_id: str) -> Optional[TodoItem]:
        """Mark a todo as in progress.

        Args:
            todo_id: Todo ID

        Returns:
            Updated TodoItem or None if not found
        """
        await self._ensure_loaded()

        todo = await self.get(todo_id)
        if todo:
            todo.status = TodoStatus.IN_PROGRESS
            todo.started_at = datetime.now(timezone.utc)
            await self.save()
            logger.info(f"Started todo: {todo_id}")

        return todo

    async def complete(
        self,
        todo_id: str,
        notes: Optional[List[str]] = None
    ) -> Optional[TodoItem]:
        """Mark a todo as completed.

        Args:
            todo_id: Todo ID
            notes: Optional completion notes

        Returns:
            Updated TodoItem or None if not found
        """
        await self._ensure_loaded()

        todo = await self.get(todo_id)
        if todo:
            todo.status = TodoStatus.COMPLETED
            todo.completed_at = datetime.now(timezone.utc)
            if notes:
                todo.notes.extend(notes)
            await self.save()
            logger.info(f"Completed todo: {todo_id}")

        return todo

    async def block(
        self,
        todo_id: str,
        reason: str
    ) -> Optional[TodoItem]:
        """Mark a todo as blocked.

        Args:
            todo_id: Todo ID
            reason: Reason for blocking

        Returns:
            Updated TodoItem or None if not found
        """
        await self._ensure_loaded()

        todo = await self.get(todo_id)
        if todo:
            todo.status = TodoStatus.BLOCKED
            todo.notes.append(f"Blocked: {reason}")
            await self.save()
            logger.info(f"Blocked todo: {todo_id} - {reason}")

        return todo

    async def skip(
        self,
        todo_id: str,
        reason: str
    ) -> Optional[TodoItem]:
        """Mark a todo as skipped.

        Args:
            todo_id: Todo ID
            reason: Reason for skipping

        Returns:
            Updated TodoItem or None if not found
        """
        await self._ensure_loaded()

        todo = await self.get(todo_id)
        if todo:
            todo.status = TodoStatus.SKIPPED
            todo.notes.append(f"Skipped: {reason}")
            await self.save()
            logger.info(f"Skipped todo: {todo_id} - {reason}")

        return todo

    async def add_note(self, todo_id: str, note: str) -> Optional[TodoItem]:
        """Add a note to a todo item.

        Args:
            todo_id: Todo ID
            note: Note to add

        Returns:
            Updated TodoItem or None if not found
        """
        await self._ensure_loaded()

        todo = await self.get(todo_id)
        if todo:
            todo.notes.append(note)
            await self.save()

        return todo

    async def list_all(self) -> List[TodoItem]:
        """List all todo items.

        Returns:
            List of all TodoItems
        """
        await self._ensure_loaded()
        return self._todos.copy()

    async def list_by_status(self, status: TodoStatus) -> List[TodoItem]:
        """List todo items by status.

        Args:
            status: Status to filter by

        Returns:
            List of matching TodoItems
        """
        await self._ensure_loaded()
        return [t for t in self._todos if t.status == status]

    async def list_pending(self) -> List[TodoItem]:
        """List all pending todo items."""
        return await self.list_by_status(TodoStatus.PENDING)

    async def list_in_progress(self) -> List[TodoItem]:
        """List all in-progress todo items."""
        return await self.list_by_status(TodoStatus.IN_PROGRESS)

    async def list_completed(self) -> List[TodoItem]:
        """List all completed todo items."""
        return await self.list_by_status(TodoStatus.COMPLETED)

    async def get_progress(self) -> Dict[str, Any]:
        """Get progress summary.

        Returns:
            Dictionary with progress statistics
        """
        await self._ensure_loaded()

        total = len(self._todos)
        completed = len([t for t in self._todos if t.status == TodoStatus.COMPLETED])
        in_progress = len([t for t in self._todos if t.status == TodoStatus.IN_PROGRESS])
        pending = len([t for t in self._todos if t.status == TodoStatus.PENDING])
        blocked = len([t for t in self._todos if t.status == TodoStatus.BLOCKED])
        skipped = len([t for t in self._todos if t.status == TodoStatus.SKIPPED])

        return {
            "total": total,
            "completed": completed,
            "in_progress": in_progress,
            "pending": pending,
            "blocked": blocked,
            "skipped": skipped,
            "completion_percentage": round((completed / total * 100) if total > 0 else 0, 1),
        }

    async def get_current(self) -> Optional[TodoItem]:
        """Get the current in-progress todo item.

        Returns:
            In-progress TodoItem or None
        """
        in_progress = await self.list_in_progress()
        return in_progress[0] if in_progress else None

    async def clear_completed(self) -> int:
        """Remove all completed todo items.

        Returns:
            Number of items removed
        """
        await self._ensure_loaded()

        before = len(self._todos)
        self._todos = [t for t in self._todos if t.status != TodoStatus.COMPLETED]
        removed = before - len(self._todos)

        if removed > 0:
            await self.save()
            logger.info(f"Cleared {removed} completed todo items")

        return removed

    async def reset(self) -> None:
        """Reset all todos to pending state."""
        await self._ensure_loaded()

        for todo in self._todos:
            todo.status = TodoStatus.PENDING
            todo.started_at = None
            todo.completed_at = None

        await self.save()
        logger.info("Reset all todos to pending")

    # -------------------------------------------------------------------------
    # Synchronous methods for session-scoped operation (no persistence)
    # -------------------------------------------------------------------------

    def add_sync(
        self,
        content: str,
        priority: int = 0,
        parent_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> TodoItem:
        """Add a new todo item (synchronous, session-scoped).

        Args:
            content: Task description
            priority: Priority level (higher = more important)
            parent_id: Optional parent task ID for hierarchical todos
            metadata: Optional additional metadata

        Returns:
            Created TodoItem
        """
        item = TodoItem(
            id=f"todo_{self._next_id}",
            content=content,
            priority=priority,
            parent_id=parent_id,
            metadata=metadata or {},
        )

        self._todos.append(item)
        self._next_id += 1

        logger.info(f"Added todo: {item.id} - {content}")
        return item

    def get_sync(self, todo_id: str) -> Optional[TodoItem]:
        """Get a todo item by ID (synchronous).

        Args:
            todo_id: Todo ID

        Returns:
            TodoItem or None if not found
        """
        for todo in self._todos:
            if todo.id == todo_id:
                return todo
        return None

    def start_sync(self, todo_id: str) -> Optional[TodoItem]:
        """Mark a todo as in progress (synchronous).

        Args:
            todo_id: Todo ID

        Returns:
            Updated TodoItem or None if not found
        """
        todo = self.get_sync(todo_id)
        if todo:
            todo.status = TodoStatus.IN_PROGRESS
            todo.started_at = datetime.now(timezone.utc)
            logger.info(f"Started todo: {todo_id}")
        return todo

    def complete_sync(
        self,
        todo_id: str,
        notes: Optional[str] = None
    ) -> Optional[TodoItem]:
        """Mark a todo as completed (synchronous).

        Args:
            todo_id: Todo ID
            notes: Optional completion note

        Returns:
            Updated TodoItem or None if not found
        """
        todo = self.get_sync(todo_id)
        if todo:
            todo.status = TodoStatus.COMPLETED
            todo.completed_at = datetime.now(timezone.utc)
            if notes:
                todo.notes.append(notes)
            logger.info(f"Completed todo: {todo_id}")
        return todo

    def next_sync(self) -> Optional[TodoItem]:
        """Get the next pending todo item (synchronous).

        Returns the highest priority pending item.

        Returns:
            Next TodoItem or None if no pending items
        """
        pending = [t for t in self._todos if t.status == TodoStatus.PENDING]
        if not pending:
            return None

        # Sort by priority (descending) then by creation time
        pending.sort(key=lambda t: (-t.priority, t.created_at))
        return pending[0]

    def list_all_sync(self) -> List[TodoItem]:
        """List all todo items (synchronous).

        Returns:
            List of all TodoItems
        """
        return self._todos.copy()

    def list_by_status_sync(self, status: TodoStatus) -> List[TodoItem]:
        """List todo items by status (synchronous).

        Args:
            status: Status to filter by

        Returns:
            List of matching TodoItems
        """
        return [t for t in self._todos if t.status == status]

    def complete_first_pending_sync(self, notes: Optional[str] = None) -> Dict[str, Any]:
        """Mark the first incomplete task as complete (synchronous).

        This is the core method for the `todo_complete` tool. It finds the first
        task that is either in_progress or pending (in that order) and marks it
        as completed.

        Args:
            notes: Optional completion note to add

        Returns:
            Dictionary with:
                - completed: The completed TodoItem (or None)
                - remaining: Number of remaining incomplete tasks
                - is_last_task: True if this was the last task in the phase
                - next_task: The next pending task (or None)
                - message: Human-readable status message
        """
        # First look for in_progress tasks
        in_progress = [t for t in self._todos if t.status == TodoStatus.IN_PROGRESS]
        pending = [t for t in self._todos if t.status == TodoStatus.PENDING]

        task_to_complete = None
        if in_progress:
            task_to_complete = in_progress[0]
        elif pending:
            # Sort by priority (descending) then creation time
            pending.sort(key=lambda t: (-t.priority, t.created_at))
            task_to_complete = pending[0]

        if not task_to_complete:
            return {
                "completed": None,
                "remaining": 0,
                "is_last_task": True,
                "next_task": None,
                "message": "No incomplete tasks to complete. Todo list is empty or all tasks are done.",
            }

        # Mark as complete
        task_to_complete.status = TodoStatus.COMPLETED
        task_to_complete.completed_at = datetime.now(timezone.utc)
        if notes:
            task_to_complete.notes.append(notes)

        logger.info(f"Completed todo: {task_to_complete.id} - {task_to_complete.content}")

        # Calculate remaining
        remaining_in_progress = [t for t in self._todos if t.status == TodoStatus.IN_PROGRESS]
        remaining_pending = [t for t in self._todos if t.status == TodoStatus.PENDING]
        remaining_count = len(remaining_in_progress) + len(remaining_pending)

        # Find next task
        next_task = None
        if remaining_in_progress:
            next_task = remaining_in_progress[0]
        elif remaining_pending:
            remaining_pending.sort(key=lambda t: (-t.priority, t.created_at))
            next_task = remaining_pending[0]

        # Build message
        phase_info = self._format_phase_indicator()
        phase_prefix = f"[{phase_info}] " if phase_info else ""

        if remaining_count == 0:
            message = (
                f"{phase_prefix}Task '{task_to_complete.content}' marked complete. "
                f"All tasks in this phase are done!"
            )
        else:
            message = (
                f"{phase_prefix}Task '{task_to_complete.content}' marked complete. "
                f"{remaining_count} task{'s' if remaining_count != 1 else ''} remaining."
            )
            if next_task:
                message += f"\nNext up: {next_task.content}"

        return {
            "completed": task_to_complete,
            "remaining": remaining_count,
            "is_last_task": remaining_count == 0,
            "next_task": next_task,
            "message": message,
        }

    def get_progress_sync(self) -> Dict[str, Any]:
        """Get progress summary (synchronous).

        Returns:
            Dictionary with progress statistics
        """
        total = len(self._todos)
        completed = len([t for t in self._todos if t.status == TodoStatus.COMPLETED])
        in_progress = len([t for t in self._todos if t.status == TodoStatus.IN_PROGRESS])
        pending = len([t for t in self._todos if t.status == TodoStatus.PENDING])
        blocked = len([t for t in self._todos if t.status == TodoStatus.BLOCKED])
        skipped = len([t for t in self._todos if t.status == TodoStatus.SKIPPED])

        return {
            "total": total,
            "completed": completed,
            "in_progress": in_progress,
            "pending": pending,
            "blocked": blocked,
            "skipped": skipped,
            "completion_percentage": round((completed / total * 100) if total > 0 else 0, 1),
        }

    # -------------------------------------------------------------------------
    # Archive functionality for two-tier planning model
    # -------------------------------------------------------------------------

    def set_workspace_manager(self, workspace_manager: "WorkspaceManager") -> None:
        """Set the workspace manager for archive operations.

        Args:
            workspace_manager: Initialized WorkspaceManager instance
        """
        self._workspace_manager = workspace_manager

    # -------------------------------------------------------------------------
    # Phase tracking for guardrails system
    # -------------------------------------------------------------------------

    def set_phase_info(
        self,
        phase_number: int,
        total_phases: int = 0,
        phase_name: str = ""
    ) -> None:
        """Set phase information for guardrails system.

        Args:
            phase_number: Current phase number (1-indexed)
            total_phases: Total number of phases (0 = unknown)
            phase_name: Optional descriptive name for the phase
        """
        self._phase_number = phase_number
        self._total_phases = total_phases
        self._phase_name = phase_name
        logger.info(f"Set phase info: {phase_number}/{total_phases} - {phase_name or 'unnamed'}")

    def get_phase_info(self) -> Dict[str, Any]:
        """Get current phase information.

        Returns:
            Dictionary with phase_number, total_phases, phase_name
        """
        return {
            "phase_number": self._phase_number,
            "total_phases": self._total_phases,
            "phase_name": self._phase_name,
        }

    def _format_phase_indicator(self) -> str:
        """Format phase indicator for display.

        Returns:
            Formatted phase string like "Phase 2 of 4: Extraction" or empty string
        """
        if self._phase_number == 0:
            return ""

        if self._total_phases > 0:
            base = f"Phase {self._phase_number} of {self._total_phases}"
        else:
            base = f"Phase {self._phase_number}"

        if self._phase_name:
            return f"{base}: {self._phase_name}"
        return base

    def _format_archive_content(self, phase_name: str = "") -> str:
        """Format todos as markdown for archiving.

        Args:
            phase_name: Optional name for the phase being archived

        Returns:
            Markdown-formatted archive content
        """
        lines = []

        # Header with phase info
        phase_indicator = self._format_phase_indicator()
        if phase_name and phase_indicator:
            title = f"Archived Todos: {phase_indicator} - {phase_name}"
        elif phase_indicator:
            title = f"Archived Todos: {phase_indicator}"
        elif phase_name:
            title = f"Archived Todos: {phase_name}"
        else:
            title = "Archived Todos"

        lines.append(f"# {title}")
        lines.append(f"Archived: {datetime.now(timezone.utc).isoformat()}")
        lines.append("")

        # Group by status
        completed = [t for t in self._todos if t.status == TodoStatus.COMPLETED]
        in_progress = [t for t in self._todos if t.status == TodoStatus.IN_PROGRESS]
        pending = [t for t in self._todos if t.status == TodoStatus.PENDING]
        blocked = [t for t in self._todos if t.status == TodoStatus.BLOCKED]
        skipped = [t for t in self._todos if t.status == TodoStatus.SKIPPED]

        # Completed section
        if completed:
            lines.append(f"## Completed ({len(completed)})")
            for todo in completed:
                lines.append(f"- [x] {todo.content}")
                if todo.notes:
                    for note in todo.notes:
                        lines.append(f"  - {note}")
            lines.append("")

        # In progress section
        if in_progress:
            lines.append(f"## Was In Progress ({len(in_progress)})")
            for todo in in_progress:
                lines.append(f"- [ ] {todo.content} (stopped mid-task)")
                if todo.notes:
                    for note in todo.notes:
                        lines.append(f"  - {note}")
            lines.append("")

        # Pending section
        if pending:
            lines.append(f"## Was Pending ({len(pending)})")
            for todo in pending:
                priority_marker = f" [P{todo.priority}]" if todo.priority > 0 else ""
                lines.append(f"- [ ] {todo.content}{priority_marker}")
            lines.append("")

        # Blocked section
        if blocked:
            lines.append(f"## Was Blocked ({len(blocked)})")
            for todo in blocked:
                lines.append(f"- [ ] {todo.content}")
                if todo.notes:
                    for note in todo.notes:
                        lines.append(f"  - {note}")
            lines.append("")

        # Skipped section
        if skipped:
            lines.append(f"## Skipped ({len(skipped)})")
            for todo in skipped:
                lines.append(f"- [-] {todo.content}")
                if todo.notes:
                    for note in todo.notes:
                        lines.append(f"  - {note}")
            lines.append("")

        # Summary
        progress = self.get_progress_sync()
        lines.append("## Summary")
        lines.append(f"- Total: {progress['total']}")
        lines.append(f"- Completed: {progress['completed']} ({progress['completion_percentage']}%)")
        lines.append(f"- In Progress: {progress['in_progress']}")
        lines.append(f"- Pending: {progress['pending']}")
        lines.append(f"- Blocked: {progress['blocked']}")
        lines.append(f"- Skipped: {progress['skipped']}")

        return "\n".join(lines)

    def archive_and_reset(
        self,
        phase_name: str = "",
        workspace_manager: Optional["WorkspaceManager"] = None
    ) -> str:
        """Archive current todos to workspace and reset for next phase.

        This method:
        1. Formats todos as human-readable markdown
        2. Writes to workspace/archive/todos_<phase>_<timestamp>.md
        3. Clears the internal todo list
        4. Returns confirmation message

        This creates a natural checkpoint for context compaction.

        Args:
            phase_name: Optional name for the phase being archived
            workspace_manager: Optional WorkspaceManager (uses instance default if not provided)

        Returns:
            Confirmation message with archive path

        Raises:
            ValueError: If no workspace manager is available
        """
        ws = workspace_manager or self._workspace_manager
        if ws is None:
            raise ValueError(
                "No workspace manager available. Pass one to archive_and_reset() "
                "or set it via set_workspace_manager()."
            )

        # Count items before clearing
        total_count = len(self._todos)

        if total_count == 0:
            return "No todos to archive. Todo list is empty."

        # Generate archive content
        archive_content = self._format_archive_content(phase_name)

        # Generate filename with phase number if available
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

        # Build filename parts
        parts = ["todos"]

        # Add phase number if set
        if self._phase_number > 0:
            parts.append(f"phase{self._phase_number}")

        # Add custom phase name if provided
        if phase_name:
            # Sanitize phase name for filename
            safe_name = "".join(c if c.isalnum() or c in "_-" else "_" for c in phase_name)
            parts.append(safe_name)

        # Add timestamp
        parts.append(timestamp)

        filename = "_".join(parts) + ".md"
        archive_path = f"archive/{filename}"

        # Write to workspace
        ws.write_file(archive_path, archive_content)

        # Clear todos
        self._todos = []
        self._next_id = 1

        logger.info(f"Archived {total_count} todos to {archive_path}")
        return f"Archived {total_count} todos to {archive_path}. Todo list cleared. Ready for new todos."

    def clear(self) -> int:
        """Clear all todos without archiving.

        Returns:
            Number of todos cleared
        """
        count = len(self._todos)
        self._todos = []
        self._next_id = 1
        logger.info(f"Cleared {count} todos without archiving")
        return count

    def archive_with_failure_note(
        self,
        issue: str,
        workspace_manager: Optional["WorkspaceManager"] = None
    ) -> str:
        """Archive current todos with a failure note and reset for re-planning.

        This is the core method for the `todo_rewind` panic button. It archives
        the current state with a failure note explaining why the approach didn't
        work, then clears the todo list to allow for re-planning.

        Args:
            issue: Description of the issue that caused the rewind
            workspace_manager: Optional WorkspaceManager (uses instance default if not provided)

        Returns:
            Message prompting the agent to re-read the plan and re-plan

        Raises:
            ValueError: If no workspace manager is available
        """
        ws = workspace_manager or self._workspace_manager
        if ws is None:
            raise ValueError(
                "No workspace manager available. Pass one to archive_with_failure_note() "
                "or set it via set_workspace_manager()."
            )

        # Build failure archive content
        lines = []

        # Header with failure notice
        phase_info = self._format_phase_indicator()
        if phase_info:
            lines.append(f"# REWIND: {phase_info}")
        else:
            lines.append("# REWIND: Todo Archive (Failed Approach)")

        lines.append(f"Archived: {datetime.now(timezone.utc).isoformat()}")
        lines.append("")
        lines.append("## Failure Reason")
        lines.append("")
        lines.append(f"> {issue}")
        lines.append("")

        # Include the standard archive content
        lines.append("## State at Time of Rewind")
        lines.append("")

        # Group by status
        completed = [t for t in self._todos if t.status == TodoStatus.COMPLETED]
        in_progress = [t for t in self._todos if t.status == TodoStatus.IN_PROGRESS]
        pending = [t for t in self._todos if t.status == TodoStatus.PENDING]

        if completed:
            lines.append(f"### Completed Before Rewind ({len(completed)})")
            for todo in completed:
                lines.append(f"- [x] {todo.content}")
            lines.append("")

        if in_progress:
            lines.append(f"### Was In Progress ({len(in_progress)})")
            for todo in in_progress:
                lines.append(f"- [ ] {todo.content} (stopped)")
            lines.append("")

        if pending:
            lines.append(f"### Was Pending ({len(pending)})")
            for todo in pending:
                lines.append(f"- [ ] {todo.content}")
            lines.append("")

        # Summary
        total = len(self._todos)
        completed_count = len(completed)
        lines.append("## Summary")
        lines.append(f"- Total tasks: {total}")
        lines.append(f"- Completed before rewind: {completed_count}")
        lines.append(f"- Tasks abandoned: {len(in_progress) + len(pending)}")

        archive_content = "\n".join(lines)

        # Generate filename with REWIND marker
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        if self._phase_number > 0:
            filename = f"todos_REWIND_phase{self._phase_number}_{timestamp}.md"
        else:
            filename = f"todos_REWIND_{timestamp}.md"

        archive_path = f"archive/{filename}"

        # Write to workspace
        ws.write_file(archive_path, archive_content)

        # Clear todos
        cleared_count = len(self._todos)
        self._todos = []
        self._next_id = 1

        logger.warning(f"REWIND: Archived {cleared_count} todos to {archive_path} - Issue: {issue}")

        # Build response message
        response_lines = [
            f"REWIND triggered: {issue}",
            "",
            f"Archived {cleared_count} todos to {archive_path}",
            "",
            "To recover, you should:",
            "1. Read main_plan.md to review the overall strategy",
            "2. Read workspace_summary.md to understand current state",
            "3. Reconsider your approach based on the issue",
            "4. Update main_plan.md if the strategy needs to change",
            "5. Create new todos with todo_write for the revised approach",
        ]

        return "\n".join(response_lines)

    # -------------------------------------------------------------------------
    # TodoWrite pattern - atomic list replacement
    # -------------------------------------------------------------------------

    def _inject_reflection_task(self, todos: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Inject a reflection task if not already present.

        This ensures the agent maintains a "roter Faden" (red thread) by always
        having a task to review their plan and update progress after completing
        the current batch of work.

        Args:
            todos: List of todo dictionaries

        Returns:
            Modified list with reflection task appended if needed
        """
        if not self._auto_reflection:
            return todos

        # Check for existing reflection task using keywords
        reflection_keywords = ["review plan", "update plan", "review progress", "update progress"]
        has_reflection = any(
            any(kw in t.get("content", "").lower() for kw in reflection_keywords)
            for t in todos
        )

        if has_reflection:
            return todos

        # Skip if no pending tasks (nothing to reflect on after)
        pending = [t for t in todos if t.get("status") == "pending"]
        if not pending:
            return todos

        # Append reflection task with lowest priority so it's always last
        reflection_task = {
            "content": self._reflection_task_content,
            "status": "pending",
            "priority": "low",
        }
        return todos + [reflection_task]

    def set_todos_from_list(self, todos: List[Dict[str, Any]]) -> str:
        """Set the entire todo list from a list of todo dictionaries.

        This is the primary method for the Claude Code-style TodoWrite pattern.
        It replaces the entire todo list atomically.

        Args:
            todos: List of todo dicts, each with:
                - content (str, required): Task description
                - status (str, required): "pending", "in_progress", or "completed"
                - priority (str, optional): "high", "medium", or "low" (default: "medium")
                - id (str, optional): Todo ID (auto-generated if not provided)

        Returns:
            Formatted summary of the updated todo list with progress and hints

        Raises:
            ValueError: If a todo dict is missing required fields or has invalid status

        Example:
            ```python
            todos = [
                {"content": "Extract document text", "status": "completed"},
                {"content": "Chunk document", "status": "in_progress"},
                {"content": "Identify requirements", "status": "pending", "priority": "high"},
            ]
            result = todo_mgr.set_todos_from_list(todos)
            ```
        """
        # Inject reflection task if enabled (maintains "roter Faden")
        todos = self._inject_reflection_task(todos)

        VALID_STATUSES = {"pending", "in_progress", "completed"}
        PRIORITY_MAP = {"high": 2, "medium": 1, "low": 0}

        # Build mapping of existing IDs to preserve timestamps
        existing_by_id = {t.id: t for t in self._todos}

        new_todos = []
        for i, todo_dict in enumerate(todos):
            # Validate required fields
            if "content" not in todo_dict:
                raise ValueError(f"Todo at index {i} missing required 'content' field")
            if "status" not in todo_dict:
                raise ValueError(f"Todo at index {i} missing required 'status' field")

            status_str = todo_dict["status"].lower()
            if status_str not in VALID_STATUSES:
                raise ValueError(
                    f"Todo at index {i} has invalid status '{status_str}'. "
                    f"Valid values: {VALID_STATUSES}"
                )

            # Convert status string to enum
            status = TodoStatus(status_str)

            # Convert priority string to int
            priority_str = todo_dict.get("priority", "medium").lower()
            priority = PRIORITY_MAP.get(priority_str, 1)

            # Use provided ID or generate new one
            todo_id = todo_dict.get("id")
            if not todo_id:
                todo_id = f"todo_{self._next_id}"
                self._next_id += 1

            # Check if we're updating an existing todo (preserve timestamps)
            existing = existing_by_id.get(todo_id)
            if existing:
                item = TodoItem(
                    id=todo_id,
                    content=todo_dict["content"],
                    status=status,
                    priority=priority,
                    parent_id=existing.parent_id,
                    metadata=existing.metadata,
                    created_at=existing.created_at,
                    started_at=existing.started_at if status in (TodoStatus.IN_PROGRESS, TodoStatus.COMPLETED) else None,
                    completed_at=existing.completed_at if status == TodoStatus.COMPLETED else None,
                    notes=existing.notes,
                )
                # Update timestamps based on status transitions
                if status == TodoStatus.IN_PROGRESS and existing.status != TodoStatus.IN_PROGRESS:
                    item.started_at = datetime.now(timezone.utc)
                if status == TodoStatus.COMPLETED and existing.status != TodoStatus.COMPLETED:
                    item.completed_at = datetime.now(timezone.utc)
            else:
                item = TodoItem(
                    id=todo_id,
                    content=todo_dict["content"],
                    status=status,
                    priority=priority,
                )
                if status == TodoStatus.IN_PROGRESS:
                    item.started_at = datetime.now(timezone.utc)
                if status == TodoStatus.COMPLETED:
                    item.completed_at = datetime.now(timezone.utc)

            new_todos.append(item)

        # Replace the todo list
        self._todos = new_todos

        # Update next_id to be higher than any existing ID
        max_id = 0
        for todo in self._todos:
            if todo.id.startswith("todo_"):
                try:
                    id_num = int(todo.id.split("_")[1])
                    max_id = max(max_id, id_num)
                except (ValueError, IndexError):
                    pass
        self._next_id = max(self._next_id, max_id + 1)

        logger.info(f"Set {len(self._todos)} todos via set_todos_from_list")

        # Return formatted summary with progress and hints
        return self._format_todo_write_response()

    def _format_todo_write_response(self) -> str:
        """Format the response for todo_write tool.

        Returns a structured response with:
        1. Current todo list with status icons
        2. Progress summary
        3. Hint about what to do next
        """
        lines = []

        # Group by status for display
        in_progress = [t for t in self._todos if t.status == TodoStatus.IN_PROGRESS]
        pending = [t for t in self._todos if t.status == TodoStatus.PENDING]
        completed = [t for t in self._todos if t.status == TodoStatus.COMPLETED]

        # Progress summary
        total = len(self._todos)

        lines.append("## Todo List Updated")
        lines.append("")

        if total == 0:
            lines.append("No todos in list. Add tasks with todo_write.")
            return "\n".join(lines)

        # In progress section
        if in_progress:
            lines.append(f"### In Progress ({len(in_progress)})")
            for todo in in_progress:
                priority_marker = " [HIGH]" if todo.priority >= 2 else ""
                lines.append(f"  - [{todo.id}] {todo.content}{priority_marker}")
            lines.append("")

        # Pending section
        if pending:
            lines.append(f"### Pending ({len(pending)})")
            # Sort by priority (descending)
            pending_sorted = sorted(pending, key=lambda t: -t.priority)
            for todo in pending_sorted:
                priority_marker = " [HIGH]" if todo.priority >= 2 else ""
                lines.append(f"  - [{todo.id}] {todo.content}{priority_marker}")
            lines.append("")

        # Completed section (abbreviated)
        if completed:
            lines.append(f"### Completed ({len(completed)})")
            # Only show last 3 completed
            for todo in completed[-3:]:
                lines.append(f"  - [x] {todo.content}")
            if len(completed) > 3:
                lines.append(f"  - ... and {len(completed) - 3} more")
            lines.append("")

        # Progress bar
        completed_count = len(completed)
        pct = round(completed_count / total * 100) if total > 0 else 0
        bar_filled = pct // 10
        bar = "█" * bar_filled + "░" * (10 - bar_filled)
        lines.append(f"**Progress:** [{bar}] {completed_count}/{total} ({pct}%)")
        lines.append("")

        # Hint about next action
        if in_progress:
            current = in_progress[0]
            lines.append(f"**Currently working on:** {current.content}")
        elif pending:
            # Find highest priority pending
            next_todo = sorted(pending, key=lambda t: -t.priority)[0]
            lines.append(f"**Next up:** {next_todo.content}")
        else:
            lines.append("**All tasks complete!** Consider using `archive_and_reset` if moving to a new phase.")

        return "\n".join(lines)

    # -------------------------------------------------------------------------
    # Formatting methods
    # -------------------------------------------------------------------------

    def format_list(self, items: Optional[List[TodoItem]] = None) -> str:
        """Format todo items as a readable string.

        Args:
            items: Items to format (defaults to all)

        Returns:
            Formatted string
        """
        items = items or self._todos

        if not items:
            return "No todos."

        status_icons = {
            TodoStatus.PENDING: "○",
            TodoStatus.IN_PROGRESS: "◐",
            TodoStatus.COMPLETED: "●",
            TodoStatus.BLOCKED: "✗",
            TodoStatus.SKIPPED: "−",
        }

        lines = []
        for todo in items:
            icon = status_icons.get(todo.status, "?")
            priority_marker = "!" * min(todo.priority, 3) if todo.priority > 0 else ""
            lines.append(f"{icon} [{todo.id}] {priority_marker}{todo.content}")
            if todo.notes:
                for note in todo.notes[-2:]:  # Show last 2 notes
                    lines.append(f"    → {note}")

        return "\n".join(lines)


def create_todo_tool_functions(todo_manager: TodoManager) -> List[Dict[str, Any]]:
    """Create tool function definitions for LangGraph agents.

    Returns tool definitions that can be added to an agent's toolkit
    for managing its todo list.

    Args:
        todo_manager: TodoManager instance

    Returns:
        List of tool function definitions
    """
    async def add_todo(content: str, priority: int = 0) -> str:
        """Add a new todo item to the task list."""
        item = await todo_manager.add(content, priority=priority)
        return f"Added: {item.id} - {content}"

    async def start_todo(todo_id: str) -> str:
        """Start working on a todo item."""
        item = await todo_manager.start(todo_id)
        if item:
            return f"Started: {todo_id}"
        return f"Todo not found: {todo_id}"

    async def complete_todo(todo_id: str, notes: str = "") -> str:
        """Complete a todo item."""
        note_list = [notes] if notes else None
        item = await todo_manager.complete(todo_id, notes=note_list)
        if item:
            return f"Completed: {todo_id}"
        return f"Todo not found: {todo_id}"

    async def list_todos() -> str:
        """List all current todos."""
        items = await todo_manager.list_all()
        return todo_manager.format_list(items)

    async def get_next_todo() -> str:
        """Get the next task to work on."""
        item = await todo_manager.next()
        if item:
            return f"Next: {item.id} - {item.content}"
        return "No pending todos."

    async def get_progress() -> str:
        """Get progress summary."""
        progress = await todo_manager.get_progress()
        return (
            f"Progress: {progress['completed']}/{progress['total']} "
            f"({progress['completion_percentage']}% complete)\n"
            f"In progress: {progress['in_progress']}, "
            f"Pending: {progress['pending']}, "
            f"Blocked: {progress['blocked']}"
        )

    return [
        {"func": add_todo, "name": "add_todo", "description": "Add a new task to the todo list"},
        {"func": start_todo, "name": "start_todo", "description": "Start working on a todo item"},
        {"func": complete_todo, "name": "complete_todo", "description": "Mark a todo item as complete"},
        {"func": list_todos, "name": "list_todos", "description": "List all current todo items"},
        {"func": get_next_todo, "name": "get_next_todo", "description": "Get the next task to work on"},
        {"func": get_progress, "name": "get_progress", "description": "Get progress summary"},
    ]
