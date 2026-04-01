"""
Superhuman Remote Worker — Universal Agent System

Universal Agent architecture for requirement extraction and validation.

Exports are lazy-loaded via ``__getattr__`` so that importing a submodule
(e.g. ``from src.core.loader import create_llm``) does NOT pull in the
full agent dependency tree (aiosqlite, langgraph, etc.).  Direct imports
like ``from src import UniversalAgent`` still work — they simply trigger
the relevant import on first access.
"""

__version__ = "2.0.0"

# ---------------------------------------------------------------------------
# Lazy attribute mapping: name → (module, attribute)
# ---------------------------------------------------------------------------
_LAZY_IMPORTS: dict[str, tuple[str, str]] = {
    # Universal Agent
    "UniversalAgent": (".agent", "UniversalAgent"),
    "UniversalAgentState": (".core.state", "UniversalAgentState"),
    "create_app": (".api.app", "create_app"),
    # Shared utilities
    "WorkspaceManager": (".core.workspace", "WorkspaceManager"),
    "ContextManager": (".core.context", "ContextManager"),
    # Managers package (nested loop architecture)
    "TodoManager": (".managers", "TodoManager"),
    "TodoItem": (".managers", "TodoItem"),
    "TodoStatus": (".managers", "TodoStatus"),
    "PlanManager": (".managers", "PlanManager"),
    "MemoryManager": (".managers", "MemoryManager"),
    # Context management exports (for tests)
    "ContextConfig": (".core.context", "ContextConfig"),
    "ContextManagementState": (".core.context", "ContextManagementState"),
    "ToolRetryManager": (".core.context", "ToolRetryManager"),
    "count_tokens_tiktoken": (".core.context", "count_tokens_tiktoken"),
    "count_tokens_approximate": (".core.context", "count_tokens_approximate"),
    "get_token_counter": (".core.context", "get_token_counter"),
    "write_error_to_workspace": (".core.context", "write_error_to_workspace"),
    # Graph exports
    "build_nested_loop_graph": (".graph", "build_nested_loop_graph"),
    "run_graph_with_streaming": (".graph", "run_graph_with_streaming"),
    "get_managers_from_workspace": (".graph", "get_managers_from_workspace"),
    # Persistent agent exports
    "run_persistent_loop": (".persistent_graph", "run_persistent_loop"),
    "PersistentLoopCallbacks": (".persistent_graph", "PersistentLoopCallbacks"),
    "create_persistent_app": (".api.persistent_app", "create_persistent_app"),
    "PersistentSession": (".api.persistent_session", "PersistentSession"),
    # Loader exports
    "load_summarization_prompt": (".core.loader", "load_summarization_prompt"),
    "load_auxiliary_prompt": (".core.loader", "load_auxiliary_prompt"),
    "get_all_tool_names": (".core.loader", "get_all_tool_names"),
    "AgentConfig": (".core.loader", "AgentConfig"),
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
