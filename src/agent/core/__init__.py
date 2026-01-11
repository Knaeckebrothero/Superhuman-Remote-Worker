"""Core utilities for Universal Agent.

Contains state management, context handling, workspace operations,
todo management, and supporting infrastructure.
"""

from .state import UniversalAgentState, create_initial_state
from .loader import (
    AgentConfig,
    load_agent_config,
    create_llm,
    load_system_prompt,
    load_instructions,
    get_all_tool_names,
    resolve_config_path,
)
from .context import (
    ContextConfig,
    ContextManager,
    ProtectedContextConfig,
    ProtectedContextProvider,
    ToolRetryManager,
)
from .workspace import WorkspaceManager, WorkspaceConfig
from .todo import TodoManager, TodoItem, TodoStatus
from .archiver import get_archiver, LLMArchiver
from .transitions import (
    TransitionType,
    TransitionResult,
    PhaseInfo,
    PhaseTransitionManager,
    get_bootstrap_todos,
    BOOTSTRAP_PROMPT,
)

__all__ = [
    # State
    "UniversalAgentState",
    "create_initial_state",
    # Loader
    "AgentConfig",
    "load_agent_config",
    "create_llm",
    "load_system_prompt",
    "load_instructions",
    "get_all_tool_names",
    "resolve_config_path",
    # Context
    "ContextConfig",
    "ContextManager",
    "ProtectedContextConfig",
    "ProtectedContextProvider",
    "ToolRetryManager",
    # Workspace
    "WorkspaceManager",
    "WorkspaceConfig",
    # Todo
    "TodoManager",
    "TodoItem",
    "TodoStatus",
    # Archiver
    "get_archiver",
    "LLMArchiver",
    # Transitions
    "TransitionType",
    "TransitionResult",
    "PhaseInfo",
    "PhaseTransitionManager",
    "get_bootstrap_todos",
    "BOOTSTRAP_PROMPT",
]
