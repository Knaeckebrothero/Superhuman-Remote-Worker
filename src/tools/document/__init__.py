"""Document toolkit - document processing and extraction.

This toolkit provides document handling tools for extracting text,
chunking documents, and identifying requirement candidates.
"""

from typing import Any, Dict, List

from ..context import ToolContext


def create_document_tools(context: ToolContext) -> List[Any]:
    """Create all document processing tools with injected context.

    Args:
        context: ToolContext with workspace_manager (optional but recommended)

    Returns:
        List of LangChain tool functions
    """
    from .processing import create_processing_tools

    return create_processing_tools(context)


def get_document_metadata() -> Dict[str, Dict[str, Any]]:
    """Get metadata for all document tools."""
    from .processing import DOCUMENT_TOOLS_METADATA

    return DOCUMENT_TOOLS_METADATA
