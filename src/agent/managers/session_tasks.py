"""Durable task manager for persistent interactive sessions.

Unlike the phase-based TodoManager (used by worker jobs), this manager has no
phase tracking or workspace integration.  PostgreSQL is authoritative when a
thread/database handle is supplied; the small local list is only the hydrated
view used for tool formatting and frontend broadcasts.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class SessionTask:
    """A single session task."""

    id: str
    description: str
    status: str = "pending"  # pending | in_progress | completed
    priority: str = "medium"  # high | medium | low
    notes: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: Optional[datetime] = None

    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "description": self.description,
            "status": self.status,
            "priority": self.priority,
            "notes": self.notes,
            "created_at": self.created_at.isoformat(),
            "completed_at": self.completed_at.isoformat()
            if self.completed_at
            else None,
        }

    @classmethod
    def from_row(cls, row: Dict[str, Any]) -> "SessionTask":
        """Build the public task shape from a migration-0133 row."""

        created_at = row.get("created_at")
        if isinstance(created_at, str):
            created_at = datetime.fromisoformat(created_at)
        completed_at = row.get("completed_at")
        if isinstance(completed_at, str):
            completed_at = datetime.fromisoformat(completed_at)
        return cls(
            id=f"task_{int(row['task_number'])}",
            description=str(row.get("description") or ""),
            status=str(row.get("status") or "pending"),
            priority=str(row.get("priority") or "medium"),
            notes=str(row.get("notes") or ""),
            created_at=created_at or datetime.now(timezone.utc),
            completed_at=completed_at,
        )


class SessionTaskManager:
    """Postgres-backed task list with a claim-local hydrated view."""

    def __init__(self, *, thread_id: str | None = None, postgres: Any = None):
        self.thread_id = thread_id
        self.postgres = postgres
        self._tasks: List[SessionTask] = []
        self._next_id = 1

    @property
    def durable(self) -> bool:
        return bool(self.thread_id and self.postgres is not None)

    @staticmethod
    def _task_number(task_id: str) -> int | None:
        prefix, separator, raw_number = str(task_id).partition("_")
        if prefix != "task" or separator != "_":
            return None
        try:
            value = int(raw_number)
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    def _replace(self, task: SessionTask) -> SessionTask:
        for index, existing in enumerate(self._tasks):
            if existing.id == task.id:
                self._tasks[index] = task
                return task
        self._tasks.append(task)
        try:
            number = int(task.id.removeprefix("task_"))
        except ValueError:
            number = 0
        self._next_id = max(self._next_id, number + 1)
        return task

    async def hydrate(self) -> None:
        """Replace the cache with durable thread state on every attach."""

        if not self.durable:
            return
        rows = await self.postgres.list_session_tasks(self.thread_id)
        self._tasks = [SessionTask.from_row(dict(row)) for row in rows]
        numbers = [self._task_number(task.id) or 0 for task in self._tasks]
        self._next_id = max(numbers, default=0) + 1
        logger.info(
            "Restored SessionTaskManager: thread=%s tasks=%d next_id=%d",
            self.thread_id,
            len(self._tasks),
            self._next_id,
        )

    async def add(self, description: str, priority: str = "medium") -> SessionTask:
        """Add a task durably before returning its tool result."""

        if self.durable:
            row = await self.postgres.create_session_task(
                self.thread_id,
                description,
                priority,
            )
            return self._replace(SessionTask.from_row(dict(row)))
        task = SessionTask(
            id=f"task_{self._next_id}",
            description=description,
            priority=priority,
        )
        self._tasks.append(task)
        self._next_id += 1
        return task

    async def start(self, task_id: str) -> Optional[SessionTask]:
        """Mark a task as in-progress."""

        if self.durable:
            number = self._task_number(task_id)
            if number is None:
                return None
            row = await self.postgres.start_session_task(self.thread_id, number)
            return self._replace(SessionTask.from_row(dict(row))) if row else None
        for task in self._tasks:
            if task.id == task_id and task.status == "pending":
                task.status = "in_progress"
                return task
        return None

    async def complete(self, task_id: str, notes: str = "") -> Optional[SessionTask]:
        """Mark a task as completed durably."""

        if self.durable:
            number = self._task_number(task_id)
            if number is None:
                return None
            row = await self.postgres.complete_session_task(
                self.thread_id,
                number,
                notes,
            )
            return self._replace(SessionTask.from_row(dict(row))) if row else None
        for task in self._tasks:
            if task.id == task_id and task.status != "completed":
                task.status = "completed"
                task.completed_at = datetime.now(timezone.utc)
                if notes:
                    task.notes = notes
                return task
        return None

    def list_all(self) -> List[SessionTask]:
        """Return all tasks, sorted by priority then creation time."""
        priority_order = {"high": 0, "medium": 1, "low": 2}
        return sorted(
            self._tasks,
            key=lambda t: (
                0 if t.status == "in_progress" else 1 if t.status == "pending" else 2,
                priority_order.get(t.priority, 1),
                t.created_at,
            ),
        )

    def to_dict_list(self) -> List[Dict]:
        """Serialize all tasks for WS transport."""
        return [t.to_dict() for t in self.list_all()]

    def format_for_display(self) -> str:
        """Format task list for LLM tool output."""
        tasks = self.list_all()
        if not tasks:
            return "No tasks yet."

        lines = []
        completed = sum(1 for t in tasks if t.status == "completed")
        lines.append(f"Tasks: {completed}/{len(tasks)} completed\n")

        for task in tasks:
            icon = {"pending": "○", "in_progress": "◑", "completed": "●"}[task.status]
            priority_tag = f" [{task.priority}]" if task.priority != "medium" else ""
            notes_tag = f"  — {task.notes}" if task.notes else ""
            lines.append(
                f"  {icon} {task.id}: {task.description}{priority_tag}{notes_tag}"
            )

        return "\n".join(lines)
