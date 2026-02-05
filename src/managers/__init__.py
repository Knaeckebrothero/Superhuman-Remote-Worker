"""Managers package for nested loop graph architecture.

This package provides focused manager classes for the agent's
nested loop workflow:

- **TodoManager**: Stateful manager for task tracking within the
  inner execution loop. Holds todos in memory until archived.

- **PlanManager**: Service for plan.md operations. Reads the
  strategic plan at phase transitions.

- **MemoryManager**: Service for workspace.md operations. Provides
  long-term memory that's always in the system prompt.

All managers are built on top of WorkspaceManager and operate on
files in the job workspace.

Example:
    ```python
    from src.core.workspace import WorkspaceManager
    from src.managers import TodoManager, PlanManager, MemoryManager

    # Create workspace
    workspace = WorkspaceManager(job_id="abc123")
    workspace.initialize()

    # Create managers
    todo_mgr = TodoManager(workspace)
    plan_mgr = PlanManager(workspace)
    memory_mgr = MemoryManager(workspace)

    # Use managers
    if not plan_mgr.exists():
        plan_mgr.write("# Plan\\n\\n## Phase 1...")

    todo_mgr.add("First task", priority="high")
    memory_mgr.set_state("Phase", "1")
    ```
"""

from .git_manager import GitManager
from .memory import MemoryManager
from .plan import PlanManager
from .todo import TodoItem, TodoManager, TodoStatus

__all__ = [
    "GitManager",
    "TodoManager",
    "TodoItem",
    "TodoStatus",
    "PlanManager",
    "MemoryManager",
]
