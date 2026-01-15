"""
Graph-RAG Requirement Analysis System

Universal Agent architecture for requirement extraction and validation.
"""

__version__ = "2.0.0"

# Universal Agent
from .agent import UniversalAgent
from .core.state import UniversalAgentState
from .api.app import create_app

# Shared utilities
from .core.workspace import WorkspaceManager
from .core.context import ContextManager

# Managers package (nested loop architecture)
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
    # Managers (nested loop architecture)
    'TodoManager',
    'TodoItem',
    'TodoStatus',
    'PlanManager',
    'MemoryManager',
    # Shared utilities
    'WorkspaceManager',
    'ContextManager',
    # Context management
    'ContextConfig',
    'ContextManagementState',
    'ToolRetryManager',
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
