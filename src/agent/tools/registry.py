"""Tool registry for dynamic tool loading.

Provides a centralized registry of available tools and functions
to load them based on configuration. This enables the Universal Agent
to load different tool sets based on its config file.

Usage:
    from src.agents.tools import load_tools, ToolContext

    context = ToolContext(workspace_manager=ws)
    tools = load_tools(["read_file", "write_file", "list_files"], context)

    # Or load all tools in a category
    tools = load_tools_by_category("workspace", context)
"""

import logging
from typing import Any, Dict, List, Optional, Set

from .context import ToolContext
from .workspace_tools import create_workspace_tools, WORKSPACE_TOOLS_METADATA
from .todo_tools import create_todo_tools, TODO_TOOLS_METADATA
from .document_tools import create_document_tools, DOCUMENT_TOOLS_METADATA
from .search_tools import create_search_tools, SEARCH_TOOLS_METADATA
from .citation_tools import create_citation_tools, CITATION_TOOLS_METADATA
from .cache_tools import create_cache_tools, CACHE_TOOLS_METADATA
from .graph_tools import create_graph_tools, GRAPH_TOOLS_METADATA
from .completion_tools import create_completion_tools

# Completion tools metadata
COMPLETION_TOOLS_METADATA = {
    "mark_complete": {
        "module": "completion_tools",
        "function": "mark_complete",
        "description": "Signal task/phase completion with structured report",
        "category": "completion",
    },
    "job_complete": {
        "module": "completion_tools",
        "function": "job_complete",
        "description": "Signal FINAL job completion - call when all phases are done",
        "category": "completion",
    },
}

logger = logging.getLogger(__name__)


# Master registry mapping tool names to their metadata
# This is populated from individual tool modules
TOOL_REGISTRY: Dict[str, Dict[str, Any]] = {}

# Register workspace tools (Phase 2)
TOOL_REGISTRY.update(WORKSPACE_TOOLS_METADATA)

# Register todo tools (Phase 3)
TOOL_REGISTRY.update(TODO_TOOLS_METADATA)

# Register domain tools (Phase 5)
TOOL_REGISTRY.update(DOCUMENT_TOOLS_METADATA)
TOOL_REGISTRY.update(SEARCH_TOOLS_METADATA)
TOOL_REGISTRY.update(CITATION_TOOLS_METADATA)
TOOL_REGISTRY.update(CACHE_TOOLS_METADATA)
TOOL_REGISTRY.update(GRAPH_TOOLS_METADATA)

# Register completion tools
TOOL_REGISTRY.update(COMPLETION_TOOLS_METADATA)


def get_available_tools() -> Dict[str, Dict[str, Any]]:
    """Get all registered tools with their metadata.

    Returns:
        Dictionary mapping tool names to metadata
    """
    return TOOL_REGISTRY.copy()


def get_tools_by_category(category: str) -> List[str]:
    """Get tool names in a specific category.

    Args:
        category: Category name ("workspace", "todo", "domain")

    Returns:
        List of tool names in the category
    """
    return [
        name for name, meta in TOOL_REGISTRY.items()
        if meta.get("category") == category
    ]


def get_categories() -> Set[str]:
    """Get all available tool categories.

    Returns:
        Set of category names
    """
    return {meta.get("category", "unknown") for meta in TOOL_REGISTRY.values()}


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
            f"Unknown tools: {unknown_tools}. "
            f"Available tools: {available}"
        )

    # Check for placeholder tools
    placeholder_tools = [
        name for name in tool_names
        if TOOL_REGISTRY[name].get("placeholder", False)
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
        workspace_tool_names = set(tools_by_category["workspace"])
        if not context.has_workspace():
            raise ValueError(
                "Workspace tools require a workspace_manager in ToolContext"
            )
        workspace_tools = create_workspace_tools(context)
        for tool in workspace_tools:
            if tool.name in workspace_tool_names:
                all_tools.append(tool)
                logger.debug(f"Loaded workspace tool: {tool.name}")

    # Todo tools
    if "todo" in tools_by_category:
        if not context.has_todo():
            raise ValueError(
                "Todo tools require a todo_manager in ToolContext"
            )
        todo_tools = create_todo_tools(context)
        todo_tool_names = set(tools_by_category["todo"])
        for tool in todo_tools:
            if tool.name in todo_tool_names:
                all_tools.append(tool)
                logger.debug(f"Loaded todo tool: {tool.name}")

    # Domain tools (Phase 5)
    if "domain" in tools_by_category:
        domain_tool_names = set(tools_by_category["domain"])
        loaded_domain_tools = _load_domain_tools(domain_tool_names, context)
        all_tools.extend(loaded_domain_tools)

    # Completion tools
    if "completion" in tools_by_category:
        if not context.has_workspace():
            raise ValueError(
                "Completion tools require a workspace_manager in ToolContext"
            )
        completion_tools = create_completion_tools(context)
        completion_tool_names = set(tools_by_category["completion"])
        for tool in completion_tools:
            if tool.name in completion_tool_names:
                all_tools.append(tool)
                logger.debug(f"Loaded completion tool: {tool.name}")

    logger.info(f"Loaded {len(all_tools)} tools: {[t.name for t in all_tools]}")
    return all_tools


def _load_domain_tools(tool_names: Set[str], context: ToolContext) -> List[Any]:
    """Load domain-specific tools based on requested names.

    Args:
        tool_names: Set of domain tool names to load
        context: ToolContext with dependencies

    Returns:
        List of domain tool objects
    """
    loaded_tools = []

    # Group by module for efficient loading
    document_tools_needed = tool_names & set(DOCUMENT_TOOLS_METADATA.keys())
    search_tools_needed = tool_names & set(SEARCH_TOOLS_METADATA.keys())
    citation_tools_needed = tool_names & set(CITATION_TOOLS_METADATA.keys())
    cache_tools_needed = tool_names & set(CACHE_TOOLS_METADATA.keys())
    graph_tools_needed = tool_names & set(GRAPH_TOOLS_METADATA.keys())

    # Load document tools if needed
    if document_tools_needed:
        try:
            doc_tools = create_document_tools(context)
            for tool in doc_tools:
                if tool.name in document_tools_needed:
                    loaded_tools.append(tool)
                    logger.debug(f"Loaded document tool: {tool.name}")
        except Exception as e:
            logger.warning(f"Could not load document tools: {e}")

    # Load search tools if needed
    if search_tools_needed:
        try:
            srch_tools = create_search_tools(context)
            for tool in srch_tools:
                if tool.name in search_tools_needed:
                    loaded_tools.append(tool)
                    logger.debug(f"Loaded search tool: {tool.name}")
        except Exception as e:
            logger.warning(f"Could not load search tools: {e}")

    # Load citation tools if needed
    if citation_tools_needed:
        try:
            cite_tools = create_citation_tools(context)
            for tool in cite_tools:
                if tool.name in citation_tools_needed:
                    loaded_tools.append(tool)
                    logger.debug(f"Loaded citation tool: {tool.name}")
        except Exception as e:
            logger.warning(f"Could not load citation tools: {e}")

    # Load cache tools if needed
    if cache_tools_needed:
        try:
            cche_tools = create_cache_tools(context)
            for tool in cche_tools:
                if tool.name in cache_tools_needed:
                    loaded_tools.append(tool)
                    logger.debug(f"Loaded cache tool: {tool.name}")
        except Exception as e:
            logger.warning(f"Could not load cache tools: {e}")

    # Load graph tools if needed
    if graph_tools_needed:
        if not context.neo4j_conn:
            logger.warning("Graph tools require neo4j_conn in ToolContext")
        else:
            try:
                grph_tools = create_graph_tools(context)
                for tool in grph_tools:
                    if tool.name in graph_tools_needed:
                        loaded_tools.append(tool)
                        logger.debug(f"Loaded graph tool: {tool.name}")
            except Exception as e:
                logger.warning(f"Could not load graph tools: {e}")

    return loaded_tools


def load_tools_by_category(category: str, context: ToolContext) -> List[Any]:
    """Load all tools in a specific category.

    Args:
        category: Category name ("workspace", "todo", "domain")
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
        name for name in tool_names
        if not TOOL_REGISTRY[name].get("placeholder", False)
    ]
    return load_tools(tool_names, context)


def register_tool(
    name: str,
    module: str,
    function: str,
    description: str,
    category: str = "custom",
    **kwargs
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
        **kwargs
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
