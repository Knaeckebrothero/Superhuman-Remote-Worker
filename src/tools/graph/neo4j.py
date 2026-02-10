"""Neo4j graph tools for the Universal Agent.

Provides Neo4j graph operations:
- Cypher query execution (read and write)
- Schema inspection

These tools are injected automatically when a Neo4j datasource is
attached to a job. See docs/datasources.md.
"""

import logging
from typing import Any, Dict, List, Optional

from langchain_core.tools import tool

from ..context import ToolContext

logger = logging.getLogger(__name__)


# Tool metadata for registry
# Phase availability: domain tools are tactical-only
GRAPH_TOOLS_METADATA: Dict[str, Dict[str, Any]] = {
    "execute_cypher_query": {
        "module": "graph.neo4j",
        "function": "execute_cypher_query",
        "description": "Execute a Cypher query against Neo4j",
        "category": "graph",
        "defer_to_workspace": True,
        "short_description": "Execute Cypher query against Neo4j database.",
        "phases": ["tactical"],
    },
    "get_database_schema": {
        "module": "graph.neo4j",
        "function": "get_database_schema",
        "description": "Get Neo4j database schema (labels, relationships, properties)",
        "category": "graph",
        "defer_to_workspace": True,
        "short_description": "Get Neo4j schema (labels, relationships, properties).",
        "phases": ["tactical"],
    },
}


def create_neo4j_tools(context: ToolContext) -> List[Any]:
    """Create Neo4j graph tools with injected context.

    Args:
        context: ToolContext with dependencies (must include neo4j datasource)

    Returns:
        List of LangChain tool functions

    Raises:
        ValueError: If Neo4j database not available in context
    """
    neo4j = context.get_datasource("neo4j")
    if not neo4j:
        raise ValueError("Neo4j datasource not available in context")

    # Schema cache
    _schema_cache: Optional[Dict] = None

    def get_schema() -> Dict:
        nonlocal _schema_cache
        if _schema_cache is None and neo4j:
            _schema_cache = neo4j.get_schema() if hasattr(neo4j, 'get_schema') else neo4j.get_database_schema()
        return _schema_cache or {}

    @tool
    def execute_cypher_query(query: str) -> str:
        """Execute a Cypher query against the Neo4j database.

        Args:
            query: A valid Cypher query string

        Returns:
            String representation of query results (up to 50 records)

        Use this to explore the graph, find entities, check relationships.
        """
        if not neo4j:
            return "Error: No Neo4j connection available"

        try:
            results = neo4j.execute_query(query)

            if not results:
                return "Query executed successfully but returned no results."

            limited_results = results[:50]
            formatted = []
            for i, record in enumerate(limited_results, 1):
                formatted.append(f"Record {i}: {record}")

            result_str = "\n".join(formatted)

            if len(results) > 50:
                result_str += f"\n\n... and {len(results) - 50} more results"

            return result_str

        except Exception as e:
            return f"Error executing query: {str(e)}"

    @tool
    def get_database_schema() -> str:
        """Get the Neo4j database schema.

        Returns:
            Schema information with node labels, relationship types, properties
        """
        schema = get_schema()
        if not schema:
            return "Error: Could not retrieve schema"

        schema_str = "Neo4j Database Schema:\n\n"
        schema_str += f"Node Labels ({len(schema.get('node_labels', []))}):\n"
        for label in schema.get('node_labels', []):
            schema_str += f"  - {label}\n"

        schema_str += f"\nRelationship Types ({len(schema.get('relationship_types', []))}):\n"
        for rel_type in schema.get('relationship_types', []):
            schema_str += f"  - {rel_type}\n"

        schema_str += f"\nProperty Keys ({len(schema.get('property_keys', []))}):\n"
        for prop in schema.get('property_keys', [])[:30]:
            schema_str += f"  - {prop}\n"

        if len(schema.get('property_keys', [])) > 30:
            schema_str += f"  ... and {len(schema['property_keys']) - 30} more\n"

        return schema_str

    return [
        execute_cypher_query,
        get_database_schema,
    ]
