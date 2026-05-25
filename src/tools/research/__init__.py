"""Research toolkit - web search, academic papers, browser automation.

This toolkit provides research capabilities for gathering external
information, integrating with search APIs like Tavily, accessing
academic paper databases (arXiv, Unpaywall, Semantic Scholar),
and browser automation for navigating websites.
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
    from .papers import create_paper_tools
    from .web import create_web_tools
    from .workflow import create_workflow_tools

    # Autonomous browser tools (browse_website / download_from_website) were
    # deprecated; the main agent drives the browser via the direct browser_*
    # tools instead. See docs/features/browser_workspace_executor.md.
    tools = []
    tools.extend(create_web_tools(context))
    tools.extend(create_paper_tools(context))
    tools.extend(create_workflow_tools(context))
    return tools


def create_browser_direct_tools(context: ToolContext) -> List[Any]:
    """Create direct browser control tools with injected context."""
    from .browser_direct import create_browser_direct_tools as _create

    return _create(context)


def get_research_metadata() -> Dict[str, Dict[str, Any]]:
    """Get metadata for all research tools."""
    from .browser import BROWSER_TOOLS_METADATA
    from .papers import PAPER_TOOLS_METADATA
    from .web import RESEARCH_TOOLS_METADATA
    from .workflow import WORKFLOW_TOOLS_METADATA

    metadata = {}
    metadata.update(RESEARCH_TOOLS_METADATA)
    metadata.update(PAPER_TOOLS_METADATA)
    metadata.update(BROWSER_TOOLS_METADATA)
    metadata.update(WORKFLOW_TOOLS_METADATA)
    return metadata


def get_browser_direct_metadata() -> Dict[str, Dict[str, Any]]:
    """Get metadata for direct browser control tools."""
    from .browser_direct import BROWSER_DIRECT_TOOLS_METADATA

    return BROWSER_DIRECT_TOOLS_METADATA
