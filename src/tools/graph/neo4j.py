"""Neo4j graph tools for the Universal Agent.

Provides Neo4j graph operations for the Validator Agent:
- Cypher query execution
- Schema inspection
- Metamodel validation
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
        "category": "domain",
        "defer_to_workspace": True,
        "short_description": "Execute Cypher query against Neo4j database.",
        "phases": ["tactical"],
    },
    "get_database_schema": {
        "module": "graph.neo4j",
        "function": "get_database_schema",
        "description": "Get Neo4j database schema (labels, relationships, properties)",
        "category": "domain",
        "defer_to_workspace": True,
        "short_description": "Get Neo4j schema (labels, relationships, properties).",
        "phases": ["tactical"],
    },
    "validate_schema_compliance": {
        "module": "graph.neo4j",
        "function": "validate_schema_compliance",
        "description": "Run metamodel compliance checks",
        "category": "domain",
        "defer_to_workspace": True,
        "short_description": "Run metamodel compliance checks (structural/relationships/quality).",
        "phases": ["tactical"],
    },
}


def create_neo4j_tools(context: ToolContext) -> List[Any]:
    """Create Neo4j graph tools with injected context.

    Args:
        context: ToolContext with dependencies (must include neo4j connection)

    Returns:
        List of LangChain tool functions

    Raises:
        ValueError: If Neo4j database not available in context
    """
    neo4j = context.graph
    if not neo4j:
        raise ValueError("Neo4j database not available in context")

    # Schema cache
    _schema_cache: Optional[Dict] = None

    def get_schema() -> Dict:
        nonlocal _schema_cache
        if _schema_cache is None and neo4j:
            # Neo4jDB uses get_schema(), old interface uses get_database_schema()
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

    @tool
    def validate_schema_compliance(check_type: str = "all") -> str:
        """Run metamodel compliance checks against the graph.

        Args:
            check_type: Type of checks to run:
                - "all": All checks (default)
                - "structural": Node labels and properties (A1-A3)
                - "relationships": Relationship types and directions (B1-B3)
                - "quality": Quality gates (C1-C5)

        Returns:
            Compliance report with pass/fail status and violations
        """
        if not neo4j:
            return "Error: No Neo4j connection available"

        try:
            from src.utils.metamodel_validator import MetamodelValidator, Severity
            validator = MetamodelValidator(neo4j)

            if check_type == "all":
                report = validator.run_all_checks()
            elif check_type == "structural":
                report = validator.run_structural_checks()
            elif check_type == "relationships":
                report = validator.run_relationship_checks()
            elif check_type == "quality":
                report = validator.run_quality_gate_checks()
            else:
                return "Invalid check_type. Use 'all', 'structural', 'relationships', or 'quality'."

            lines = [
                "## Metamodel Compliance Report",
                f"**Status:** {'PASSED' if report.passed else 'FAILED'}",
                f"**Errors:** {report.error_count} | **Warnings:** {report.warning_count}",
                "",
            ]

            if report.error_count > 0:
                lines.append("### Errors (Must Fix)")
                for result in report.results:
                    if not result.passed and result.severity == Severity.ERROR:
                        lines.append(f"\n**{result.check_id}: {result.check_name}**")
                        lines.append(f"- {result.message}")
                        if result.violations:
                            for v in result.violations[:3]:
                                lines.append(f"  - {v}")

            if report.warning_count > 0:
                lines.append("\n### Warnings")
                for result in report.results:
                    if not result.passed and result.severity == Severity.WARNING:
                        lines.append(f"\n**{result.check_id}: {result.check_name}**")
                        lines.append(f"- {result.message}")

            return "\n".join(lines)

        except ImportError:
            return "Error: MetamodelValidator not available"
        except Exception as e:
            return f"Error running compliance check: {str(e)}"

    return [
        execute_cypher_query,
        get_database_schema,
        validate_schema_compliance,
    ]
