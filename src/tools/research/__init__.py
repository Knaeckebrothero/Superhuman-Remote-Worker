"""Research toolkit - web search and information retrieval.

This toolkit provides research capabilities for gathering external
information, integrating with search APIs like Tavily.
"""

from typing import Any, Dict, List

from ..context import ToolContext


def create_research_tools(context: ToolContext) -> List[Any]:
    """Create all research tools with injected context.

    Args:
        context: ToolContext with workspace_manager (optional but recommended)

    Returns:
        List of LangChain tool functions
    """
    from .web import create_web_tools

    return create_web_tools(context)


def get_research_metadata() -> Dict[str, Dict[str, Any]]:
    """Get metadata for all research tools."""
    from .web import RESEARCH_TOOLS_METADATA

    return RESEARCH_TOOLS_METADATA
