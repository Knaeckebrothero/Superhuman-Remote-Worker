"""
Agent implementations for requirement analysis.

This module provides the Universal Agent - a config-driven agent that can
be deployed as Creator or Validator by changing its JSON configuration.
"""

# Universal Agent
from .agent import UniversalAgent
from .core.state import UniversalAgentState
from .api.app import create_app

# Shared utilities (legacy location - prefer managers package)
from .core.workspace import WorkspaceManager
from .core.todo import TodoManager as LegacyTodoManager
from .core.context import ContextManager

# New managers package (nested loop architecture)
from .managers import (
    TodoManager,
    TodoItem,
    TodoStatus,
    PlanManager,
    MemoryManager,
)

# Context management exports (for tests)
from .core.context import (
    ContextConfig,
    ContextManagementState,
    ToolRetryManager,
    ProtectedContextConfig,  # DEPRECATED - kept for backwards compat
    ProtectedContextProvider,  # DEPRECATED - kept for backwards compat
    count_tokens_tiktoken,
    count_tokens_approximate,
    get_token_counter,
    write_error_to_workspace,
)

# Graph exports
from .graph import (
    build_nested_loop_graph,
    run_graph_with_streaming,
    get_managers_from_workspace,
)

# Loader exports
from .core.loader import (
    load_summarization_prompt,
    get_all_tool_names,
    AgentConfig,
)

__all__ = [
    # Universal Agent
    'UniversalAgent',
    'UniversalAgentState',
    'create_app',
    # Managers (new architecture)
    'TodoManager',
    'TodoItem',
    'TodoStatus',
    'PlanManager',
    'MemoryManager',
    # Shared utilities (legacy)
    'WorkspaceManager',
    'LegacyTodoManager',
    'ContextManager',
    # Context management
    'ContextConfig',
    'ContextManagementState',
    'ToolRetryManager',
    'ProtectedContextConfig',  # DEPRECATED
    'ProtectedContextProvider',  # DEPRECATED
    'count_tokens_tiktoken',
    'count_tokens_approximate',
    'get_token_counter',
    'write_error_to_workspace',
    # Graph
    'build_nested_loop_graph',
    'run_graph_with_streaming',
    'get_managers_from_workspace',
    # Loader
    'load_summarization_prompt',
    'get_all_tool_names',
    'AgentConfig',
]
