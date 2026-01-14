"""Tools for Universal Agent.

This package provides tools for the workspace-centric agent architecture:
- Workspace tools: File operations (read, write, list, delete, search)
- To do tools: Task management with archiving
- Domain tools: Document processing, search, citation, cache, graph operations
- Registry: Dynamic tool loading based on configuration

Usage:
    from src.tools import (
        ToolContext,
        load_tools,
        create_workspace_tools,
        create_todo_tools,
    )

    # Create context with required dependencies
    context = ToolContext(
        workspace_manager=workspace_mgr,
        todo_manager=todo_mgr,
        postgres_conn=pg_conn,
        neo4j_conn=neo4j_conn,
    )

    # Load tools by name
    tools = load_tools(["read_file", "write_file", "add_todo", "complete_todo"], context)

    # Or load all domain tools
    tools = load_tools(["extract_document_text", "execute_cypher_query"], context)

    # Or create tool sets directly
    workspace_tools = create_workspace_tools(context)
    todo_tools = create_todo_tools(context)
"""

from .context import ToolContext
from .workspace_tools import create_workspace_tools
from .todo_tools import create_todo_tools
from .document_tools import create_document_tools
from .search_tools import create_search_tools
from .citation_tools import create_citation_tools
from .cache_tools import create_cache_tools
from .graph_tools import create_graph_tools
from .registry import (
    TOOL_REGISTRY,
    load_tools,
    get_available_tools,
    get_tools_by_category,
    load_tools_by_category,
)
from .description_generator import (
    generate_workspace_tool_docs,
    generate_tool_description,
    generate_tool_index,
)
from .description_override import (
    apply_description_overrides,
    get_deferred_tools,
    get_core_tools,
)

__all__ = [
    # Context
    "ToolContext",
    # Workspace tools
    "create_workspace_tools",
    # Todo tools
    "create_todo_tools",
    # Domain tools
    "create_document_tools",
    "create_search_tools",
    "create_citation_tools",
    "create_cache_tools",
    "create_graph_tools",
    # Registry
    "TOOL_REGISTRY",
    "load_tools",
    "get_available_tools",
    "get_tools_by_category",
    "load_tools_by_category",
    # Description generator
    "generate_workspace_tool_docs",
    "generate_tool_description",
    "generate_tool_index",
    # Description override
    "apply_description_overrides",
    "get_deferred_tools",
    "get_core_tools",
]
