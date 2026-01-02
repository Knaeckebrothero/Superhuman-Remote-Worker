"""Todo list management for autonomous agents.

This module provides todo list functionality for agents to track their tasks,
breaking down complex jobs into manageable steps and monitoring progress.
"""

import logging
from typing import Optional, List, Dict, Any
from datetime import datetime
from dataclasses import dataclass, field
from enum import Enum

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
    created_at: datetime = field(default_factory=datetime.utcnow)
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
            created_at=datetime.fromisoformat(data["created_at"]) if data.get("created_at") else datetime.utcnow(),
            started_at=datetime.fromisoformat(data["started_at"]) if data.get("started_at") else None,
            completed_at=datetime.fromisoformat(data["completed_at"]) if data.get("completed_at") else None,
            notes=data.get("notes", []),
        )


class TodoManager:
    """Manages a todo list for an autonomous agent.

    Provides methods for creating, updating, and querying todos.
    Todos are persisted via a Workspace instance.

    Example:
        ```python
        workspace = Workspace(job_id="123", agent="creator")
        todo_mgr = TodoManager(workspace)

        # Add tasks
        await todo_mgr.add("Process document chunks")
        await todo_mgr.add("Extract requirement candidates")
        await todo_mgr.add("Research each candidate", priority=1)

        # Work through tasks
        task = await todo_mgr.next()
        await todo_mgr.start(task.id)
        # ... do work ...
        await todo_mgr.complete(task.id, notes=["Processed 15 chunks"])

        # Check progress
        progress = await todo_mgr.get_progress()
        print(f"{progress['completed']}/{progress['total']} tasks complete")
        ```
    """

    def __init__(self, workspace: Any):
        """Initialize todo manager.

        Args:
            workspace: Workspace instance for persistence
        """
        self.workspace = workspace
        self._todos: List[TodoItem] = []
        self._next_id = 1
        self._loaded = False

    async def _ensure_loaded(self) -> None:
        """Ensure todos are loaded from workspace."""
        if not self._loaded:
            await self.load()
            self._loaded = True

    async def load(self) -> None:
        """Load todos from workspace."""
        data = await self.workspace.load_todo()
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
        """Save todos to workspace."""
        await self.workspace.save_todo([t.to_dict() for t in self._todos])
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
            todo.started_at = datetime.utcnow()
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
            todo.completed_at = datetime.utcnow()
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
