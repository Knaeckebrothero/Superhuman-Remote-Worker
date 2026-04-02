"""Core utilities for Universal Agent.

Contains state management, context handling, workspace operations,
and supporting infrastructure.

Exports are lazy-loaded via ``__getattr__`` so that importing a submodule
(e.g. ``from src.core.loader import create_llm``) does NOT pull in heavy
dependencies like langgraph that only ``state.py`` needs.

NOTE: For TodoManager, use src.agent.managers.TodoManager instead.
"""

# ---------------------------------------------------------------------------
# Lazy attribute mapping: name → (module, attribute)
# ---------------------------------------------------------------------------
_LAZY_IMPORTS: dict[str, tuple[str, str]] = {
    # State
    "UniversalAgentState": (".state", "UniversalAgentState"),
    "create_initial_state": (".state", "create_initial_state"),
    # Loader
    "AgentConfig": (".loader", "AgentConfig"),
    "load_agent_config": (".loader", "load_agent_config"),
    "create_llm": (".loader", "create_llm"),
    "load_instructions": (".loader", "load_instructions"),
    "get_all_tool_names": (".loader", "get_all_tool_names"),
    "resolve_config_path": (".loader", "resolve_config_path"),
    # Context
    "ContextConfig": (".context", "ContextConfig"),
    "ContextManager": (".context", "ContextManager"),
    "ToolRetryManager": (".context", "ToolRetryManager"),
    # Workspace
    "WorkspaceManager": (".workspace", "WorkspaceManager"),
    "WorkspaceManagerConfig": (".workspace", "WorkspaceManagerConfig"),
    # Archiver
    "get_archiver": (".archiver", "get_archiver"),
    "LLMArchiver": (".archiver", "LLMArchiver"),
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
