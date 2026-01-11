"""
Agent implementations for requirement analysis.

This module provides the Universal Agent - a config-driven agent that can
be deployed as Creator or Validator by changing its JSON configuration.
"""

# Universal Agent
from .agent import UniversalAgent
from .core.state import UniversalAgentState
from .api.app import create_app

# Shared utilities (managers)
from .core.workspace import WorkspaceManager
from .core.todo import TodoManager
from .core.context import ContextManager

# Context management exports (for tests)
from .core.context import (
    ContextConfig,
    ContextManagementState,
    ToolRetryManager,
    ProtectedContextConfig,
    ProtectedContextProvider,
    count_tokens_tiktoken,
    count_tokens_approximate,
    get_token_counter,
    write_error_to_workspace,
)

# Graph exports
from .graph import (
    build_agent_graph,
    run_graph_with_streaming,
    run_graph_with_summarization,
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
    # Shared utilities
    'WorkspaceManager',
    'TodoManager',
    'ContextManager',
    # Context management
    'ContextConfig',
    'ContextManagementState',
    'ToolRetryManager',
    'ProtectedContextConfig',
    'ProtectedContextProvider',
    'count_tokens_tiktoken',
    'count_tokens_approximate',
    'get_token_counter',
    'write_error_to_workspace',
    # Graph
    'build_agent_graph',
    'run_graph_with_streaming',
    'run_graph_with_summarization',
    # Loader
    'load_summarization_prompt',
    'get_all_tool_names',
    'AgentConfig',
]
