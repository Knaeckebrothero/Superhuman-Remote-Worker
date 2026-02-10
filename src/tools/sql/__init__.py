"""SQL toolkit - PostgreSQL query operations.

Provides PostgreSQL tools when a PostgreSQL datasource is attached to a job:
- SQL query execution (read-only)
- Schema inspection
- SQL statement execution (write)

See docs/datasources.md for the datasource connector system.
"""

from typing import Any, Dict, List

from ..context import ToolContext


def create_sql_tools(context: ToolContext) -> List[Any]:
    """Create all SQL tools with injected context.

    Args:
        context: ToolContext with postgresql datasource

    Returns:
        List of LangChain tool functions

    Raises:
        ValueError: If PostgreSQL datasource not available in context
    """
    from .postgresql import create_postgresql_tools

    return create_postgresql_tools(context)


def get_sql_metadata() -> Dict[str, Dict[str, Any]]:
    """Get metadata for all SQL tools."""
    from .postgresql import SQL_TOOLS_METADATA

    return SQL_TOOLS_METADATA
