"""WebDAV toolkit — WebDAV file operations.

Provides WebDAV tools when a WebDAV datasource is attached to a job:
- List files and folders
- Download files to workspace
- Get file metadata

See docs/datasources.md for the datasource connector system.
"""

from typing import Any, Dict, List

from ..context import ToolContext


def create_webdav_tools(context: ToolContext) -> List[Any]:
    """Create all WebDAV tools with injected context.

    Args:
        context: ToolContext with webdav datasource

    Returns:
        List of LangChain tool functions

    Raises:
        ValueError: If WebDAV datasource not available in context
    """
    from .tools import create_webdav_tools as _impl

    return _impl(context)


def get_webdav_metadata() -> Dict[str, Dict[str, Any]]:
    """Get metadata for all WebDAV tools."""
    from .tools import WEBDAV_TOOLS_METADATA

    return WEBDAV_TOOLS_METADATA
