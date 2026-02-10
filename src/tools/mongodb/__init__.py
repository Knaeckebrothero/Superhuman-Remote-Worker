"""MongoDB toolkit - document database operations.

Provides MongoDB tools when a MongoDB datasource is attached to a job:
- Document querying with filters
- Aggregation pipelines
- Schema inspection (collections, fields, indexes)
- Document insertion
- Document updates

See docs/datasources.md for the datasource connector system.
"""

from typing import Any, Dict, List

from ..context import ToolContext


def create_mongodb_tools(context: ToolContext) -> List[Any]:
    """Create all MongoDB tools with injected context.

    Args:
        context: ToolContext with mongodb datasource

    Returns:
        List of LangChain tool functions

    Raises:
        ValueError: If MongoDB datasource not available in context
    """
    from .mongo import create_mongo_tools

    return create_mongo_tools(context)


def get_mongodb_metadata() -> Dict[str, Dict[str, Any]]:
    """Get metadata for all MongoDB tools."""
    from .mongo import MONGODB_TOOLS_METADATA

    return MONGODB_TOOLS_METADATA
