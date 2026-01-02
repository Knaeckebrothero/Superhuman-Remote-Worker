"""Shared utilities for Creator and Validator agents.

This module provides common functionality used by both agents:
- ContextManager: Manages LLM context window for long-running operations
- CheckpointManager: Handles state persistence and recovery
- Workspace: Stores working data during task execution
- TodoManager: Tracks tasks and progress
"""

from .context_manager import (
    ContextManager,
    SummarizingContextManager,
    ContextConfig,
    count_tokens_approximately,
)
from .checkpoint import (
    CheckpointManager,
    CheckpointData,
)
from .workspace import (
    Workspace,
    WorkspaceEntry,
)
from .todo_manager import (
    TodoManager,
    TodoItem,
    TodoStatus,
    create_todo_tool_functions,
)

__all__ = [
    # Context Management
    "ContextManager",
    "SummarizingContextManager",
    "ContextConfig",
    "count_tokens_approximately",
    # Checkpointing
    "CheckpointManager",
    "CheckpointData",
    # Workspace
    "Workspace",
    "WorkspaceEntry",
    # Todo Management
    "TodoManager",
    "TodoItem",
    "TodoStatus",
    "create_todo_tool_functions",
]
