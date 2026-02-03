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

# Workspace toolkit (new package structure)
from .workspace import create_workspace_tools, get_workspace_metadata

# Domain tools
from .document import create_document_tools, get_document_metadata
from .research import create_research_tools, get_research_metadata

# Backward compatibility alias
create_search_tools = create_research_tools
from .citation import create_citation_tools, get_citation_metadata
# Note: cache_tools removed (deprecated, not used in configs)
from .graph import create_graph_tools, get_graph_metadata

# Core toolkit exports (todo + job tools)
from .core import create_core_tools, get_core_metadata
from .core.todo import create_todo_tools
from .core.job import create_job_tools, get_final_phase_data, clear_final_phase_data
from .registry import (
    TOOL_REGISTRY,
    load_tools,
    load_tools_for_phase,
    get_available_tools,
    get_tools_by_category,
    load_tools_by_category,
    filter_tools_by_phase,
    get_tools_for_phase,
    get_phase_tool_summary,
)
from .description_manager import (
    DescriptionManager,
    generate_workspace_tool_docs,
    generate_tool_description,
    generate_tool_index,
    apply_description_overrides,
    get_deferred_tools,
    get_core_tools,
)

__all__ = [
    # Context
    "ToolContext",
    # Workspace toolkit
    "create_workspace_tools",
    "get_workspace_metadata",
    # Core toolkit (todo + job)
    "create_core_tools",
    "get_core_metadata",
    "create_todo_tools",
    "create_job_tools",
    "get_final_phase_data",
    "clear_final_phase_data",
    # Domain tools
    "create_document_tools",
    "get_document_metadata",
    "create_search_tools",
    "create_research_tools",
    "get_research_metadata",
    "create_citation_tools",
    "get_citation_metadata",
    "create_graph_tools",
    "get_graph_metadata",
    # Registry
    "TOOL_REGISTRY",
    "load_tools",
    "load_tools_for_phase",
    "get_available_tools",
    "get_tools_by_category",
    "load_tools_by_category",
    # Phase-aware tool filtering
    "filter_tools_by_phase",
    "get_tools_for_phase",
    "get_phase_tool_summary",
    # Description manager
    "DescriptionManager",
    "generate_workspace_tool_docs",
    "generate_tool_description",
    "generate_tool_index",
    "apply_description_overrides",
    "get_deferred_tools",
    "get_core_tools",
]
