"""Graph toolkit - Neo4j graph operations.

Provides Neo4j graph tools when a Neo4j datasource is attached to a job:
- Cypher query execution
- Schema inspection

See docs/datasources.md for the datasource connector system.
"""

from typing import Any, Dict, List

from ..context import ToolContext


def create_graph_tools(context: ToolContext) -> List[Any]:
    """Create all graph tools with injected context.

    Args:
        context: ToolContext with neo4j datasource

    Returns:
        List of LangChain tool functions

    Raises:
        ValueError: If Neo4j datasource not available in context
    """
    from .neo4j import create_neo4j_tools

    return create_neo4j_tools(context)


def get_graph_metadata() -> Dict[str, Dict[str, Any]]:
    """Get metadata for all graph tools."""
    from .neo4j import GRAPH_TOOLS_METADATA

    return GRAPH_TOOLS_METADATA
