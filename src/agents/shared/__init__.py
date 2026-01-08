"""Shared utilities for Creator and Validator agents.

This module provides common functionality used by both agents:
- ContextManager: Manages LLM context window for long-running operations
- CheckpointManager: Handles state persistence and recovery
- Workspace: Stores working data during task execution
- WorkspaceManager: Filesystem-based workspace for job data
- TodoManager: Tracks tasks and progress
- VectorStore: Semantic search over workspace files (Phase 8)
- Tools: Reusable LangGraph tools (workspace, todo, vector, registry)
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
from .workspace_manager import (
    WorkspaceManager,
    WorkspaceConfig,
    get_workspace_base_path,
)
from .todo_manager import (
    TodoManager,
    TodoItem,
    TodoStatus,
    create_todo_tool_functions,
)
from .vector import (
    VectorConfig,
    EmbeddingManager,
    WorkspaceVectorStore,
    VectorizedWorkspaceManager,
    create_workspace_vector_store,
)
from .tools import (
    ToolContext,
    create_workspace_tools,
    create_todo_tools,
    create_vector_tools,
    TOOL_REGISTRY,
    load_tools,
    get_available_tools,
)
from .llm_archiver import (
    LLMArchiver,
    get_archiver,
    archive_llm_request,
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
    # Workspace (legacy key-value storage)
    "Workspace",
    "WorkspaceEntry",
    # Workspace Manager (filesystem-based)
    "WorkspaceManager",
    "WorkspaceConfig",
    "get_workspace_base_path",
    # Todo Management
    "TodoManager",
    "TodoItem",
    "TodoStatus",
    "create_todo_tool_functions",
    # Vector Store (Phase 8)
    "VectorConfig",
    "EmbeddingManager",
    "WorkspaceVectorStore",
    "VectorizedWorkspaceManager",
    "create_workspace_vector_store",
    # Tool System
    "ToolContext",
    "create_workspace_tools",
    "create_todo_tools",
    "create_vector_tools",
    "TOOL_REGISTRY",
    "load_tools",
    "get_available_tools",
    # LLM Archiving
    "LLMArchiver",
    "get_archiver",
    "archive_llm_request",
]
