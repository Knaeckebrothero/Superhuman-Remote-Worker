"""Lightweight session task manager for persistent interactive agents.

Unlike the phase-based TodoManager (used by worker agents), this manager
has no phase tracking, no archiving, and no workspace integration.
It's a simple in-memory task list for organizing work during a session.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

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
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }


class SessionTaskManager:
    """In-memory task list for persistent agent sessions."""

    def __init__(self):
        self._tasks: List[SessionTask] = []
        self._next_id = 1

    def add(self, description: str, priority: str = "medium") -> SessionTask:
        """Add a new task."""
        task = SessionTask(
            id=f"task_{self._next_id}",
            description=description,
            priority=priority,
        )
        self._tasks.append(task)
        self._next_id += 1
        return task

    def start(self, task_id: str) -> Optional[SessionTask]:
        """Mark a task as in-progress."""
        for task in self._tasks:
            if task.id == task_id and task.status == "pending":
                task.status = "in_progress"
                return task
        return None

    def complete(self, task_id: str, notes: str = "") -> Optional[SessionTask]:
        """Mark a task as completed."""
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
            lines.append(f"  {icon} {task.id}: {task.description}{priority_tag}{notes_tag}")

        return "\n".join(lines)
