"""Citation toolkit - document and web citation capabilities.

This toolkit provides citation tools for requirement traceability,
integrating with CitationEngine for verified, persistent citations.
"""

from typing import Any, Dict, List

from ..context import ToolContext


def create_citation_tools(context: ToolContext) -> List[Any]:
    """Create all citation tools with injected context.

    Args:
        context: ToolContext with workspace_manager (optional but recommended)

    Returns:
        List of LangChain tool functions
    """
    from .sources import create_source_tools

    return create_source_tools(context)


def get_citation_metadata() -> Dict[str, Dict[str, Any]]:
    """Get metadata for all citation tools."""
    from .sources import CITATION_TOOLS_METADATA

    return CITATION_TOOLS_METADATA
