"""
Superhuman Remote Worker — Universal Agent System

Universal Agent architecture for requirement extraction and validation.

Exports are lazy-loaded via ``__getattr__`` so that importing a submodule
(e.g. ``from shared.runtime.core.loader import create_llm``) does NOT pull in the
full agent dependency tree (aiosqlite, langgraph, etc.).  Direct imports
like ``from agent import UniversalAgent`` still work — they simply trigger
the relevant import on first access.
"""

__version__ = "2.0.0"

# ---------------------------------------------------------------------------
# Lazy attribute mapping: name → (module, attribute)
# ---------------------------------------------------------------------------
_LAZY_IMPORTS: dict[str, tuple[str, str]] = {
    # Universal Agent
    "UniversalAgent": ("agent.agent", "UniversalAgent"),
    "UniversalAgentState": ("agent.core.state", "UniversalAgentState"),
    "create_app": ("agent.api.app", "create_app"),
    # Shared utilities
    "WorkspaceManager": ("agent.core.workspace", "WorkspaceManager"),
    "ContextManager": ("agent.core.context", "ContextManager"),
    # Managers package (nested loop architecture)
    "TodoManager": ("agent.managers", "TodoManager"),
    "TodoItem": ("agent.managers", "TodoItem"),
    "TodoStatus": ("agent.managers", "TodoStatus"),
    "PlanManager": ("agent.managers", "PlanManager"),
    "MemoryManager": ("agent.managers", "MemoryManager"),
    # Context management exports (for tests)
    "ContextConfig": ("agent.core.context", "ContextConfig"),
    "ContextManagementState": ("agent.core.context", "ContextManagementState"),
    "ToolRetryManager": ("agent.core.context", "ToolRetryManager"),
    "count_tokens_tiktoken": ("agent.core.context", "count_tokens_tiktoken"),
    "count_tokens_approximate": ("agent.core.context", "count_tokens_approximate"),
    "get_token_counter": ("agent.core.context", "get_token_counter"),
    "write_error_to_workspace": ("agent.core.context", "write_error_to_workspace"),
    # Graph exports
    "build_nested_loop_graph": ("agent.graph", "build_nested_loop_graph"),
    "run_graph_with_streaming": ("agent.graph", "run_graph_with_streaming"),
    "get_managers_from_workspace": ("agent.graph", "get_managers_from_workspace"),
    # Persistent agent exports
    "run_persistent_loop": ("agent.persistent_graph", "run_persistent_loop"),
    "PersistentLoopCallbacks": ("agent.persistent_graph", "PersistentLoopCallbacks"),
    "create_persistent_app": ("agent.api.persistent_app", "create_persistent_app"),
    "create_dual_app": ("agent.api.dual_app", "create_dual_app"),
    "PersistentSession": ("agent.api.persistent_session", "PersistentSession"),
    # Loader exports
    "load_summarization_prompt": (
        "shared.runtime.core.loader",
        "load_summarization_prompt",
    ),
    "load_auxiliary_prompt": ("shared.runtime.core.loader", "load_auxiliary_prompt"),
    "get_all_tool_names": ("shared.runtime.core.loader", "get_all_tool_names"),
    "AgentConfig": ("shared.runtime.core.loader", "AgentConfig"),
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
