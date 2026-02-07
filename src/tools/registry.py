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

import logging
from typing import Any, Dict, List, Optional

from .context import ToolContext

# Import workspace tools from new package
from .workspace import create_workspace_tools, get_workspace_metadata

# Import domain tools
from .document import create_document_tools, get_document_metadata
from .research import create_research_tools, get_research_metadata
from .citation import create_citation_tools, get_citation_metadata
from .graph import create_graph_tools, get_graph_metadata
from .git import create_git_tools, get_git_metadata
from .coding import create_coding_tools, get_coding_metadata

# Import from core toolkit package
from .core import create_core_tools, get_core_metadata
from .core.todo import create_todo_tools
from .core.job import create_job_tools

logger = logging.getLogger(__name__)


# Master registry mapping tool names to their metadata
# This is populated from individual tool modules
TOOL_REGISTRY: Dict[str, Dict[str, Any]] = {}

# Register workspace tools
TOOL_REGISTRY.update(get_workspace_metadata())

# Register core toolkit (todo + job/completion tools)
TOOL_REGISTRY.update(get_core_metadata())

# Register domain tools
TOOL_REGISTRY.update(get_document_metadata())
TOOL_REGISTRY.update(get_research_metadata())
TOOL_REGISTRY.update(get_citation_metadata())
TOOL_REGISTRY.update(get_graph_metadata())
TOOL_REGISTRY.update(get_git_metadata())
TOOL_REGISTRY.update(get_coding_metadata())


def get_available_tools() -> Dict[str, Dict[str, Any]]:
    """Get all registered tools with their metadata.

    Returns:
        Dictionary mapping tool names to metadata
    """
    return TOOL_REGISTRY.copy()


def get_tools_by_category(category: str) -> List[str]:
    """Get tool names in a specific category.

    Args:
        category: Category name (workspace, core, document, research, citation, graph)

    Returns:
        List of tool names in the category
    """
    return [
        name for name, meta in TOOL_REGISTRY.items()
        if meta.get("category") == category
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


def get_tools_for_phase(phase: str) -> List[str]:
    """Get all tool names available in a given phase.

    Args:
        phase: Phase to get tools for ("strategic" or "tactical")

    Returns:
        List of tool names available in the phase
    """
    return [
        name for name, meta in TOOL_REGISTRY.items()
        if phase in meta.get("phases", ["strategic", "tactical"])
        and not meta.get("placeholder", False)
    ]


def get_phase_tool_summary() -> Dict[str, Dict[str, List[str]]]:
    """Get a summary of tools by phase and category.

    Returns:
        Dictionary with structure:
        {
            "strategic": {"workspace": [...], "core": [...], ...},
            "tactical": {"workspace": [...], "document": [...], ...}
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
            ["read_file", "write_file", "next_phase_todos", "chunk_document"],
            phase="strategic",
            context=ctx
        )
        # Result: Only read_file, write_file, next_phase_todos (chunk_document is tactical-only)
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
        if not context.has_workspace():
            raise ValueError("Workspace tools require workspace_manager in ToolContext")
        workspace_tools = create_workspace_tools(context)
        requested = set(tools_by_category["workspace"])
        for tool in workspace_tools:
            if tool.name in requested:
                all_tools.append(tool)
                logger.debug(f"Loaded workspace tool: {tool.name}")

    # Core tools (todo + job)
    if "core" in tools_by_category:
        if not context.has_workspace():
            raise ValueError("Core tools require workspace_manager in ToolContext")
        if not context.has_todo():
            raise ValueError("Core tools require todo_manager in ToolContext")
        core_tools = create_core_tools(context)
        requested = set(tools_by_category["core"])
        for tool in core_tools:
            if tool.name in requested:
                all_tools.append(tool)
                logger.debug(f"Loaded core tool: {tool.name}")

    # Document tools
    if "document" in tools_by_category:
        try:
            doc_tools = create_document_tools(context)
            requested = set(tools_by_category["document"])
            for tool in doc_tools:
                if tool.name in requested:
                    all_tools.append(tool)
                    logger.debug(f"Loaded document tool: {tool.name}")
        except Exception as e:
            logger.warning(f"Could not load document tools: {e}")

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
        if not context.has_neo4j():
            logger.warning("Graph tools require neo4j_db in ToolContext")
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

    # Coding tools
    if "coding" in tools_by_category:
        if not context.has_workspace():
            logger.warning("Coding tools require workspace_manager in ToolContext")
        else:
            try:
                coding_tools = create_coding_tools(context)
                requested = set(tools_by_category["coding"])
                for tool in coding_tools:
                    if tool.name in requested:
                        all_tools.append(tool)
                        logger.debug(f"Loaded coding tool: {tool.name}")
            except Exception as e:
                logger.warning(f"Could not load coding tools: {e}")

    logger.info(f"Loaded {len(all_tools)} tools: {[t.name for t in all_tools]}")
    return all_tools


def load_tools_by_category(category: str, context: ToolContext) -> List[Any]:
    """Load all tools in a specific category.

    Args:
        category: Category name (workspace, core, document, research, citation, graph)
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
