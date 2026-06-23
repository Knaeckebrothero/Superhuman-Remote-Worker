"""Tool registry for dynamic tool loading.

Provides a centralized registry of available tools and functions
to load them based on configuration. This enables the Universal Agent
to load different tool sets based on its config file.

Usage:
    from src.tools import load_tools, ToolContext

    context = ToolContext(workspace_manager=ws)
    tools = load_tools(["read_file", "write_file", "list_files"], context)

    # Or load all tools in a category
    tools = load_tools_by_category("workspace", context)
"""

import functools
import logging
from typing import Any, Dict, List

from .citation import create_citation_tools, get_citation_metadata
from .webdav import create_webdav_tools, get_webdav_metadata
from .communication import create_communication_tools, get_communication_metadata
from .context import ToolContext

# Import from core toolkit package
from .core import create_core_tools, get_core_metadata
from .core.session_task_tools import (
    create_session_task_tools,
    get_session_task_metadata,
)
from .delegation import create_delegation_tools, get_delegation_metadata

# Import domain tools
from .evaluation import create_evaluation_tools, get_evaluation_metadata
from .git import create_git_tools, get_git_metadata
from .graph import create_graph_tools, get_graph_metadata
from .knowledge import create_knowledge_tools, get_knowledge_metadata
from .mongodb import create_mongodb_tools, get_mongodb_metadata
from .orchestrator import create_orchestrator_tools, get_orchestrator_metadata
from .research import (
    create_browser_direct_tools,
    create_research_tools,
    get_browser_direct_metadata,
    get_research_metadata,
)
from .shell import create_shell_tools, get_shell_metadata
from .sql import create_sql_tools, get_sql_metadata

# Import workspace tools from new package
from .workspace import create_workspace_tools, get_workspace_metadata

logger = logging.getLogger(__name__)


# Master registry mapping tool names to their metadata
# This is populated from individual tool modules
TOOL_REGISTRY: Dict[str, Dict[str, Any]] = {}

# Register workspace tools
TOOL_REGISTRY.update(get_workspace_metadata())

# Register core toolkit (todo + job/completion tools)
TOOL_REGISTRY.update(get_core_metadata())

# Register domain tools
TOOL_REGISTRY.update(get_research_metadata())
TOOL_REGISTRY.update(get_browser_direct_metadata())
TOOL_REGISTRY.update(get_citation_metadata())
TOOL_REGISTRY.update(get_graph_metadata())
TOOL_REGISTRY.update(get_sql_metadata())
TOOL_REGISTRY.update(get_mongodb_metadata())
TOOL_REGISTRY.update(get_webdav_metadata())
TOOL_REGISTRY.update(get_git_metadata())
TOOL_REGISTRY.update(get_shell_metadata())
TOOL_REGISTRY.update(get_evaluation_metadata())
TOOL_REGISTRY.update(get_knowledge_metadata())
TOOL_REGISTRY.update(get_communication_metadata())
TOOL_REGISTRY.update(get_orchestrator_metadata())

# Register session task tools (lightweight todos for persistent sessions)
TOOL_REGISTRY.update(get_session_task_metadata())

# Register delegation tools (subagent spawning)
TOOL_REGISTRY.update(get_delegation_metadata())


def get_available_tools() -> Dict[str, Dict[str, Any]]:
    """Get all registered tools with their metadata.

    Returns:
        Dictionary mapping tool names to metadata
    """
    return TOOL_REGISTRY.copy()


def get_tools_by_category(category: str) -> List[str]:
    """Get tool names in a specific category.

    Args:
        category: Category name (workspace, core, research, citation, graph)

    Returns:
        List of tool names in the category
    """
    return [
        name for name, meta in TOOL_REGISTRY.items() if meta.get("category") == category
    ]


def get_categories() -> set[str]:
    """Get all available tool categories.

    Returns:
        Set of category names
    """
    return {meta.get("category", "unknown") for meta in TOOL_REGISTRY.values()}


def filter_tools_by_phase(tool_names: List[str], phase: str) -> List[str]:
    """Filter tool names to only those available in the given phase.

    Tools without a 'phases' field are assumed to be available in both phases.

    Args:
        tool_names: List of tool names to filter
        phase: Phase to filter for ("strategic" or "tactical")

    Returns:
        Filtered list of tool names available in the phase
    """
    filtered = []
    for name in tool_names:
        if name not in TOOL_REGISTRY:
            continue
        meta = TOOL_REGISTRY[name]
        phases = meta.get("phases", ["strategic", "tactical"])  # Default: both
        if phase in phases:
            filtered.append(name)
    return filtered


# Tool categories that need a workspace-backed execution environment. The lite
# tiers (virtual/none) declare supports_shell=False — there is no workspace
# pod — so none of these can run there.
_EXECUTION_CATEGORIES = ("shell", "browser_direct", "git")


def filter_tools_by_backend(tool_names: List[str], backend: Any) -> List[str]:
    """Drop tools the workspace backend can't support (capability gate).

    Enforcement-by-construction for the lite tiers
    (``docs/features/no_workspace_agent_mode.md`` §3.2/§7): instead of trusting
    each config's tool lists to omit them, tools are removed whenever the
    backend doesn't declare the matching capability —

    - ``not backend.supports_shell`` → drop ``shell``, ``browser_direct``,
      ``git`` (all need a workspace-backed execution environment). This is what
      ``WorkspaceBackend.supports_shell`` already promises: it "gates
      ShellManager construction *and* shell tool registration".
    - ``not backend.supports_file_tools`` → drop ``workspace`` (file) tools —
      the ``none`` tier, whose ScratchBackend is internal-only (§6).

    Everything else (web research, datasource SQL/graph/Mongo/WebDAV, knowledge,
    core, communication, delegation, citation) passes through. ``backend=None``
    is a no-op, so non-agent callers (docs, cockpit display) see the full set.
    """
    if backend is None:
        return tool_names
    drop_categories: set[str] = set()
    if not getattr(backend, "supports_shell", False):
        drop_categories.update(_EXECUTION_CATEGORIES)
    if not getattr(backend, "supports_file_tools", True):
        drop_categories.add("workspace")
    if not drop_categories:
        return tool_names
    kept: List[str] = []
    dropped: List[str] = []
    for name in tool_names:
        category = TOOL_REGISTRY.get(name, {}).get("category")
        (dropped if category in drop_categories else kept).append(name)
    if dropped:
        logger.info(
            "Backend capability gate dropped %d tool(s) %s "
            "(supports_shell=%s, supports_file_tools=%s)",
            len(dropped),
            sorted(dropped),
            getattr(backend, "supports_shell", False),
            getattr(backend, "supports_file_tools", True),
        )
    return kept


def get_tools_for_phase(phase: str) -> List[str]:
    """Get all tool names available in a given phase.

    Args:
        phase: Phase to get tools for ("strategic" or "tactical")

    Returns:
        List of tool names available in the phase
    """
    return [
        name
        for name, meta in TOOL_REGISTRY.items()
        if phase in meta.get("phases", ["strategic", "tactical"])
        and not meta.get("placeholder", False)
    ]


def get_phase_tool_summary() -> Dict[str, Dict[str, List[str]]]:
    """Get a summary of tools by phase and category.

    Returns:
        Dictionary with structure:
        {
            "strategic": {"workspace": [...], "core": [...], ...},
            "tactical": {"workspace": [...], "research": [...], ...}
        }
    """
    summary = {
        "strategic": {},
        "tactical": {},
    }

    for name, meta in TOOL_REGISTRY.items():
        if meta.get("placeholder", False):
            continue

        phases = meta.get("phases", ["strategic", "tactical"])
        category = meta.get("category", "unknown")

        for phase in phases:
            if phase not in summary:
                continue
            if category not in summary[phase]:
                summary[phase][category] = []
            summary[phase][category].append(name)

    return summary


def load_tools_for_phase(
    tool_names: List[str],
    phase: str,
    context: ToolContext,
) -> List[Any]:
    """Load tools filtered by phase availability.

    Convenience function that filters tools by phase and then loads them.

    Args:
        tool_names: List of tool names to potentially load
        phase: Phase to filter for ("strategic" or "tactical")
        context: ToolContext with dependencies

    Returns:
        List of loaded tools available in the specified phase

    Example:
        ```python
        # Load all configured tools, but only those available in strategic phase
        tools = load_tools_for_phase(
            ["read_file", "write_file", "next_phase_todos", "web_search"],
            phase="strategic",
            context=ctx
        )
        # Result: Only tools available in the strategic phase
        ```
    """
    filtered_names = filter_tools_by_phase(tool_names, phase)
    if not filtered_names:
        logger.warning(f"No tools available for phase '{phase}' from: {tool_names}")
        return []
    return load_tools(filtered_names, context)


def load_tools(tool_names: List[str], context: ToolContext) -> List[Any]:
    """Load tools by name from the registry.

    This function creates tool instances with the provided context,
    enabling dependency injection of workspace managers, database
    connections, and other resources.

    Args:
        tool_names: List of tool names to load
        context: ToolContext with dependencies

    Returns:
        List of LangChain Tool objects ready to bind to LLM

    Raises:
        ValueError: If a tool is not found or not implemented

    Example:
        ```python
        context = ToolContext(workspace_manager=ws)
        tools = load_tools(["read_file", "write_file"], context)
        ```
    """
    # Validate all tool names first
    unknown_tools = [name for name in tool_names if name not in TOOL_REGISTRY]
    if unknown_tools:
        available = ", ".join(sorted(TOOL_REGISTRY.keys()))
        raise ValueError(
            f"Unknown tools: {unknown_tools}. Available tools: {available}"
        )

    # Check for placeholder tools
    placeholder_tools = [
        name for name in tool_names if TOOL_REGISTRY[name].get("placeholder", False)
    ]
    if placeholder_tools:
        raise ValueError(
            f"Tools not yet implemented: {placeholder_tools}. "
            f"These will be available in later phases."
        )

    # Group tools by category for efficient loading
    tools_by_category: Dict[str, List[str]] = {}
    for name in tool_names:
        category = TOOL_REGISTRY[name].get("category", "unknown")
        if category not in tools_by_category:
            tools_by_category[category] = []
        tools_by_category[category].append(name)

    # Load tools by category
    all_tools = []

    # Workspace tools
    if "workspace" in tools_by_category:
        if not context.has_workspace():
            raise ValueError("Workspace tools require workspace_manager in ToolContext")
        workspace_tools = create_workspace_tools(context)
        requested = set(tools_by_category["workspace"])
        for tool in workspace_tools:
            if tool.name in requested:
                all_tools.append(tool)
                logger.debug(f"Loaded workspace tool: {tool.name}")

    # Core tools (todo + job + control). Todo/job tools need a workspace (todos
    # persist to workspace files); todo tools additionally need a TodoManager.
    # The lone exception is the control tool request_workspace_upgrade, which
    # needs neither manager — so a lite session running with todo_manager=None
    # can still expose it (workspace_tier_upgrade.md §4.2 S5). Require each
    # manager only when a tool that actually depends on it was requested.
    if "core" in tools_by_category:
        from .core.todo import TODO_TOOLS_METADATA
        from .core.upgrade import WORKSPACE_UPGRADE_TOOLS_METADATA

        requested = set(tools_by_category["core"])
        needs_workspace = requested - set(WORKSPACE_UPGRADE_TOOLS_METADATA)
        if needs_workspace and not context.has_workspace():
            raise ValueError("Core tools require workspace_manager in ToolContext")
        if requested & set(TODO_TOOLS_METADATA) and not context.has_todo():
            raise ValueError("Core tools require todo_manager in ToolContext")
        core_tools = create_core_tools(context)
        for tool in core_tools:
            if tool.name in requested:
                all_tools.append(tool)
                logger.debug(f"Loaded core tool: {tool.name}")

    # Session task tools (lightweight todos for persistent sessions)
    if "session_task" in tools_by_category:
        try:
            st_tools = create_session_task_tools(context)
            requested = set(tools_by_category["session_task"])
            for tool in st_tools:
                if tool.name in requested:
                    all_tools.append(tool)
                    logger.debug(f"Loaded session task tool: {tool.name}")
        except Exception as e:
            logger.debug(f"Session task tools not available: {e}")

    # Research tools
    if "research" in tools_by_category:
        try:
            research_tools = create_research_tools(context)
            requested = set(tools_by_category["research"])
            for tool in research_tools:
                if tool.name in requested:
                    all_tools.append(tool)
                    logger.debug(f"Loaded research tool: {tool.name}")
        except Exception as e:
            logger.warning(f"Could not load research tools: {e}")

    # Direct browser control tools
    if "browser_direct" in tools_by_category:
        try:
            bd_tools = create_browser_direct_tools(context)
            requested = set(tools_by_category["browser_direct"])
            for tool in bd_tools:
                if tool.name in requested:
                    all_tools.append(tool)
                    logger.debug(f"Loaded browser_direct tool: {tool.name}")
        except Exception as e:
            logger.warning(f"Could not load browser_direct tools: {e}")

    # Citation tools
    if "citation" in tools_by_category:
        try:
            cite_tools = create_citation_tools(context)
            requested = set(tools_by_category["citation"])
            for tool in cite_tools:
                if tool.name in requested:
                    all_tools.append(tool)
                    logger.debug(f"Loaded citation tool: {tool.name}")
        except Exception as e:
            logger.warning(f"Could not load citation tools: {e}")

    # Graph tools
    if "graph" in tools_by_category:
        if not context.has_datasource("neo4j"):
            logger.warning("Graph tools require a neo4j datasource in ToolContext")
        else:
            try:
                graph_tools = create_graph_tools(context)
                requested = set(tools_by_category["graph"])
                for tool in graph_tools:
                    if tool.name in requested:
                        all_tools.append(tool)
                        logger.debug(f"Loaded graph tool: {tool.name}")
            except Exception as e:
                logger.warning(f"Could not load graph tools: {e}")

    # SQL tools
    if "sql" in tools_by_category:
        if not context.has_datasource("postgresql"):
            logger.warning("SQL tools require a postgresql datasource in ToolContext")
        else:
            try:
                sql_tools = create_sql_tools(context)
                requested = set(tools_by_category["sql"])
                for tool in sql_tools:
                    if tool.name in requested:
                        all_tools.append(tool)
                        logger.debug(f"Loaded sql tool: {tool.name}")
            except Exception as e:
                logger.warning(f"Could not load sql tools: {e}")

    # MongoDB tools
    if "mongodb" in tools_by_category:
        if not context.has_datasource("mongodb"):
            logger.warning("MongoDB tools require a mongodb datasource in ToolContext")
        else:
            try:
                mongo_tools = create_mongodb_tools(context)
                requested = set(tools_by_category["mongodb"])
                for tool in mongo_tools:
                    if tool.name in requested:
                        all_tools.append(tool)
                        logger.debug(f"Loaded mongodb tool: {tool.name}")
            except Exception as e:
                logger.warning(f"Could not load mongodb tools: {e}")

    # WebDAV datasource tools
    if "webdav" in tools_by_category:
        if not context.has_datasource("webdav"):
            logger.warning("WebDAV tools require a webdav datasource in ToolContext")
        else:
            try:
                webdav_tools = create_webdav_tools(context)
                requested = set(tools_by_category["webdav"])
                for tool in webdav_tools:
                    if tool.name in requested:
                        all_tools.append(tool)
                        logger.debug(f"Loaded webdav tool: {tool.name}")
            except Exception as e:
                logger.warning(f"Could not load webdav tools: {e}")

    # Git tools
    if "git" in tools_by_category:
        if not context.has_workspace():
            logger.warning("Git tools require workspace_manager in ToolContext")
        elif context.workspace_manager.git_manager is None:
            logger.warning("Git tools require git_manager on workspace_manager")
        else:
            try:
                git_tools = create_git_tools(context)
                requested = set(tools_by_category["git"])
                for tool in git_tools:
                    if tool.name in requested:
                        all_tools.append(tool)
                        logger.debug(f"Loaded git tool: {tool.name}")
            except Exception as e:
                logger.warning(f"Could not load git tools: {e}")

    # Shell tools
    if "shell" in tools_by_category:
        if not context.has_workspace():
            logger.warning("Shell tools require workspace_manager in ToolContext")
        else:
            try:
                shell_tools = create_shell_tools(context)
                requested = set(tools_by_category["shell"])
                for tool in shell_tools:
                    if tool.name in requested:
                        all_tools.append(tool)
                        logger.debug(f"Loaded shell tool: {tool.name}")
            except Exception as e:
                logger.warning(f"Could not load shell tools: {e}")

    # Evaluation tools
    if "evaluation" in tools_by_category:
        try:
            eval_tools = create_evaluation_tools(context)
            requested = set(tools_by_category["evaluation"])
            for tool in eval_tools:
                if tool.name in requested:
                    all_tools.append(tool)
                    logger.debug(f"Loaded evaluation tool: {tool.name}")
        except Exception as e:
            logger.warning(f"Could not load evaluation tools: {e}")

    # Knowledge tools
    if "knowledge" in tools_by_category:
        if not context.has_knowledge():
            logger.warning(
                "Knowledge tools require knowledge_graph and knowledge_store in ToolContext"
            )
        else:
            try:
                knowledge_tools = create_knowledge_tools(context)
                requested = set(tools_by_category["knowledge"])
                for tool in knowledge_tools:
                    if tool.name in requested:
                        all_tools.append(tool)
                        logger.debug(f"Loaded knowledge tool: {tool.name}")
            except Exception as e:
                logger.warning(f"Could not load knowledge tools: {e}")

    # Communication tools
    if "communication" in tools_by_category:
        try:
            comm_tools = create_communication_tools(context)
            requested = set(tools_by_category["communication"])
            for tool in comm_tools:
                if tool.name in requested:
                    all_tools.append(tool)
                    logger.debug(f"Loaded communication tool: {tool.name}")
        except Exception as e:
            logger.warning(f"Could not load communication tools: {e}")

    # Delegation tools (subagent spawning)
    if "delegation" in tools_by_category:
        try:
            delegation_tools = create_delegation_tools(context)
            requested = set(tools_by_category["delegation"])
            for tool in delegation_tools:
                if tool.name in requested:
                    all_tools.append(tool)
                    logger.debug(f"Loaded delegation tool: {tool.name}")
        except Exception as e:
            logger.warning(f"Could not load delegation tools: {e}")

    # Orchestrator tools (job delegation for persistent agents)
    if "orchestrator" in tools_by_category:
        try:
            orch_tools = create_orchestrator_tools(context)
            requested = set(tools_by_category["orchestrator"])
            for tool in orch_tools:
                if tool.name in requested:
                    all_tools.append(tool)
                    logger.debug(f"Loaded orchestrator tool: {tool.name}")
        except Exception as e:
            logger.warning(f"Could not load orchestrator tools: {e}")

    logger.info(f"Loaded {len(all_tools)} tools: {[t.name for t in all_tools]}")
    return all_tools


def load_tools_by_category(category: str, context: ToolContext) -> List[Any]:
    """Load all tools in a specific category.

    Args:
        category: Category name (workspace, core, research, citation, graph)
        context: ToolContext with dependencies

    Returns:
        List of LangChain Tool objects

    Example:
        ```python
        tools = load_tools_by_category("workspace", context)
        ```
    """
    tool_names = get_tools_by_category(category)
    # Filter out placeholder tools
    tool_names = [
        name for name in tool_names if not TOOL_REGISTRY[name].get("placeholder", False)
    ]
    return load_tools(tool_names, context)


def register_tool(
    name: str,
    module: str,
    function: str,
    description: str,
    category: str = "custom",
    **kwargs,
) -> None:
    """Register a custom tool in the registry.

    Use this to add tools that aren't part of the standard tool set.

    Args:
        name: Unique tool name
        module: Module containing the tool
        function: Function name in the module
        description: Tool description
        category: Tool category
        **kwargs: Additional metadata
    """
    if name in TOOL_REGISTRY:
        logger.warning(f"Overwriting existing tool registration: {name}")

    TOOL_REGISTRY[name] = {
        "module": module,
        "function": function,
        "description": description,
        "category": category,
        **kwargs,
    }
    logger.info(f"Registered tool: {name} ({category})")


def unregister_tool(name: str) -> bool:
    """Remove a tool from the registry.

    Args:
        name: Tool name to remove

    Returns:
        True if tool was removed, False if not found
    """
    if name in TOOL_REGISTRY:
        del TOOL_REGISTRY[name]
        logger.info(f"Unregistered tool: {name}")
        return True
    return False


def apply_instruction_enforcement(
    tools: List[Any],
    context: ToolContext,
) -> List[Any]:
    """Apply instruction file enforcement wrappers to tools.

    For each tool that has a 'before_tool' trigger with enforce=True,
    wraps the tool's func to check was_recently_read() before executing.
    The agent must read the instruction file before the tool will work.

    This generalizes the pattern from todo.py's hardcoded todo_guide.md
    check into a config-driven system.

    Args:
        tools: List of loaded LangChain tool objects
        context: ToolContext with instruction_files configured

    Returns:
        Same list of tools (modified in-place with enforcement wrappers)
    """
    if not context._instruction_files:
        return tools

    # Build lookup: tool_name -> list of enforced file paths
    enforcement_map: Dict[str, List[str]] = {}
    for entry in context._instruction_files:
        if entry.trigger_type == "before_tool" and entry.enforce:
            enforcement_map.setdefault(entry.trigger_target, []).append(entry.path)

    if not enforcement_map:
        return tools

    def _enforcement_block(tool_name, files):
        """Return a nudge string if a required instruction file is still unread
        (gate closed), else None."""
        for file_path in files:
            if not context.was_recently_read(file_path):
                from src.services.guardrails import format_nudge

                model = (
                    context._llm_config.model
                    if context._llm_config is not None
                    else None
                )
                return format_nudge(
                    "read_file_required_error",
                    model=model,
                    file_path=file_path,
                    tool_name=tool_name,
                )
        return None

    def _make_sync_wrapper(orig, tool_name, files):
        @functools.wraps(orig)
        def wrapper(*args, **kwargs):
            blocked = _enforcement_block(tool_name, files)
            if blocked is not None:
                return blocked
            return orig(*args, **kwargs)

        return wrapper

    def _make_async_wrapper(orig, tool_name, files):
        @functools.wraps(orig)
        async def wrapper(*args, **kwargs):
            blocked = _enforcement_block(tool_name, files)
            if blocked is not None:
                return blocked
            return await orig(*args, **kwargs)

        return wrapper

    for tool in tools:
        if tool.name not in enforcement_map:
            continue

        required_files = enforcement_map[tool.name]
        tool_name = tool.name

        # Sync tools (e.g. next_phase_todos) expose .func; async @tool functions
        # (e.g. cite_web / cite_document) expose .coroutine and are invoked via
        # .ainvoke(), which bypasses .func. Wrap whichever the tool actually has —
        # wrapping only .func makes enforcement a silent no-op for async tools.
        wrapped = False
        if getattr(tool, "func", None) is not None:
            tool.func = _make_sync_wrapper(tool.func, tool_name, required_files)
            wrapped = True
        if getattr(tool, "coroutine", None) is not None:
            tool.coroutine = _make_async_wrapper(
                tool.coroutine, tool_name, required_files
            )
            wrapped = True

        if wrapped:
            logger.debug(
                f"Applied instruction enforcement to {tool_name}: "
                f"requires {required_files}"
            )

    wrapped_count = sum(1 for t in tools if t.name in enforcement_map)
    if wrapped_count:
        logger.info(f"Applied instruction enforcement to {wrapped_count} tools")

    return tools
