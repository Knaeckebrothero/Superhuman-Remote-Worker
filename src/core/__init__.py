"""Core utilities for Universal Agent.

Contains state management, context handling, workspace operations,
and supporting infrastructure.

NOTE: For TodoManager, use src.agent.managers.TodoManager instead.
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
    ToolRetryManager,
)
from .workspace import WorkspaceManager, WorkspaceManagerConfig
from .archiver import get_archiver, LLMArchiver

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
    "ToolRetryManager",
    # Workspace
    "WorkspaceManager",
    "WorkspaceManagerConfig",
    # Archiver
    "get_archiver",
    "LLMArchiver",
]
