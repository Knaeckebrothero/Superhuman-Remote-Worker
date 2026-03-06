"""Knowledge toolkit — Project knowledge base operations.

Provides tools for reading and writing to the project knowledge base
(Neo4j source of truth + pgvector search index). Write-through model:
every write updates both stores atomically.

See docs/features/project_knowledge_base.md for full architecture.
"""

from typing import Any, Dict, List

from ..context import ToolContext


def create_knowledge_tools(context: ToolContext) -> List[Any]:
    """Create all knowledge base tools with injected context.

    Args:
        context: ToolContext with knowledge_graph and knowledge_store

    Returns:
        List of LangChain tool functions
    """
    from .knowledge_tools import create_kb_tools

    return create_kb_tools(context)


def get_knowledge_metadata() -> Dict[str, Dict[str, Any]]:
    """Get metadata for all knowledge tools."""
    from .knowledge_tools import KNOWLEDGE_TOOLS_METADATA

    return KNOWLEDGE_TOOLS_METADATA
