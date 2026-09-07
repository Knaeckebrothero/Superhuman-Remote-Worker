"""Core utilities for Universal Agent.

Contains state management, context handling, workspace operations,
and supporting infrastructure.

Exports are lazy-loaded via ``__getattr__`` so that importing a submodule
(e.g. ``from shared.runtime.core.loader import create_llm``) does NOT pull in heavy
dependencies like langgraph that only ``state.py`` needs.

NOTE: For TodoManager, use agent.agent.managers.TodoManager instead.
"""

# ---------------------------------------------------------------------------
# Lazy attribute mapping: name → (module, attribute)
# ---------------------------------------------------------------------------
_LAZY_IMPORTS: dict[str, tuple[str, str]] = {
    # State
    "UniversalAgentState": ("agent.core.state", "UniversalAgentState"),
    "create_initial_state": ("agent.core.state", "create_initial_state"),
    # Loader
    "AgentConfig": ("shared.runtime.core.loader", "AgentConfig"),
    "load_agent_config": ("shared.runtime.core.loader", "load_agent_config"),
    "create_llm": ("shared.runtime.core.loader", "create_llm"),
    "load_instructions": ("shared.runtime.core.loader", "load_instructions"),
    "get_all_tool_names": ("shared.runtime.core.loader", "get_all_tool_names"),
    "resolve_config_path": ("shared.runtime.core.loader", "resolve_config_path"),
    # Context
    "ContextConfig": ("agent.core.context", "ContextConfig"),
    "ContextManager": ("agent.core.context", "ContextManager"),
    "ToolRetryManager": ("agent.core.context", "ToolRetryManager"),
    # Workspace
    "WorkspaceManager": ("agent.core.workspace", "WorkspaceManager"),
    "WorkspaceManagerConfig": ("agent.core.workspace", "WorkspaceManagerConfig"),
    # Archiver
    "get_archiver": ("agent.core.archiver", "get_archiver"),
    "LLMArchiver": ("agent.core.archiver", "LLMArchiver"),
}

__all__ = list(_LAZY_IMPORTS.keys())


def __getattr__(name: str):
    if name in _LAZY_IMPORTS:
        module_path, attr = _LAZY_IMPORTS[name]
        import importlib

        mod = importlib.import_module(module_path, package=__name__)
        value = getattr(mod, attr)
        # Cache on the module so __getattr__ is only called once per name
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
