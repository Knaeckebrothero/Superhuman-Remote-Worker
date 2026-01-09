"""Shared utilities for Creator and Validator agents.

This module provides common functionality used by both agents:
- ContextManager: Manages LLM context window for long-running operations
- WorkspaceManager: Filesystem-based workspace for job data
- TodoManager: Tracks tasks and progress
- Tools: Reusable LangGraph tools (workspace, todo, registry)
- LLMArchiver: Archives LLM requests and agent audit trails to MongoDB
"""

from .context_manager import (
    ContextManager,
    SummarizingContextManager,
    ContextConfig,
    count_tokens_approximately,
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
from .tools import (
    ToolContext,
    create_workspace_tools,
    create_todo_tools,
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
    # Workspace Manager (filesystem-based)
    "WorkspaceManager",
    "WorkspaceConfig",
    "get_workspace_base_path",
    # Todo Management
    "TodoManager",
    "TodoItem",
    "TodoStatus",
    "create_todo_tool_functions",
    # Tool System
    "ToolContext",
    "create_workspace_tools",
    "create_todo_tools",
    "TOOL_REGISTRY",
    "load_tools",
    "get_available_tools",
    # LLM Archiving
    "LLMArchiver",
    "get_archiver",
    "archive_llm_request",
]
