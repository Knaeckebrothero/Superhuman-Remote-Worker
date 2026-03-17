"""WebDAV toolkit — WebDAV file operations.

Provides WebDAV tools when a WebDAV datasource is attached to a job:
- List files and folders
- Download files to workspace
- Get file metadata

See docs/datasources.md for the datasource connector system.
"""

from typing import Any, Dict, List

from ..context import ToolContext


def create_cloud_tools(context: ToolContext) -> List[Any]:
    """Create all WebDAV tools with injected context.

    Args:
        context: ToolContext with webdav datasource

    Returns:
        List of LangChain tool functions

    Raises:
        ValueError: If WebDAV datasource not available in context
    """
    from .webdav import create_webdav_tools

    return create_webdav_tools(context)


def get_cloud_metadata() -> Dict[str, Dict[str, Any]]:
    """Get metadata for all WebDAV tools."""
    from .webdav import CLOUD_TOOLS_METADATA

    return CLOUD_TOOLS_METADATA
