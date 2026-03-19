"""WebDAV tools.

Provides file access to WebDAV (Nextcloud, ownCloud, or any WebDAV server)
attached as a datasource. Read tools (list, read, info) are always available.
Write tools (write, delete) are only injected when the datasource is not
marked read-only.

Connection is established by the agent's _create_datasource_connection() and
injected via ToolContext.get_datasource("webdav").
"""

import logging
import os
from typing import Any, Dict, List

from langchain_core.tools import tool

from ..context import ToolContext

logger = logging.getLogger(__name__)


CLOUD_TOOLS_METADATA: Dict[str, Dict[str, Any]] = {
    "cloud_list": {
        "module": "cloud.webdav",
        "function": "cloud_list",
        "category": "cloud",
        "phases": ["strategic", "tactical"],
    },
    "cloud_read": {
        "module": "cloud.webdav",
        "function": "cloud_read",
        "category": "cloud",
        "phases": ["strategic", "tactical"],
    },
    "cloud_info": {
        "module": "cloud.webdav",
        "function": "cloud_info",
        "category": "cloud",
        "phases": ["strategic", "tactical"],
    },
    "cloud_write": {
        "module": "cloud.webdav",
        "function": "cloud_write",
        "category": "cloud",
        "phases": ["tactical"],
    },
    "cloud_delete": {
        "module": "cloud.webdav",
        "function": "cloud_delete",
        "category": "cloud",
        "phases": ["tactical"],
    },
}


def create_webdav_tools(context: ToolContext) -> List[Any]:
    """Create WebDAV tools with injected context.

    Args:
        context: ToolContext with webdav datasource (webdav3.client.Client)

    Returns:
        List of LangChain tool functions
    """
    client = context.get_datasource("webdav")
    if not client:
        raise ValueError("WebDAV datasource not available in context")

    workspace = context.workspace_manager

    @tool
    def cloud_list(path: str = "/", recursive: bool = False) -> str:
        """List files and folders in WebDAV.

        Args:
            path: Directory path to list (default: root "/")
            recursive: If True, list all files recursively

        Returns:
            Formatted list of files with sizes and types
        """
        try:
            items = client.list(path, get_info=True)
            if not items:
                return f"No files found at {path}"

            lines = []
            for item in items:
                name = item.get("path", "").rstrip("/").split("/")[-1]
                if not name:
                    continue  # Skip the directory itself
                is_dir = item.get("isdir", False)
                size = item.get("size", "")
                type_icon = "[DIR]" if is_dir else "[FILE]"
                size_str = f"  {_human_size(int(size))}" if size and not is_dir else ""
                lines.append(f"  {type_icon} {name}{size_str}")

                if recursive and is_dir:
                    sub_path = path.rstrip("/") + "/" + name
                    try:
                        sub_items = client.list(sub_path, get_info=True)
                        for sub in sub_items:
                            sub_name = sub.get("path", "").rstrip("/").split("/")[-1]
                            if not sub_name:
                                continue
                            sub_is_dir = sub.get("isdir", False)
                            sub_size = sub.get("size", "")
                            sub_icon = "[DIR]" if sub_is_dir else "[FILE]"
                            sub_size_str = f"  {_human_size(int(sub_size))}" if sub_size and not sub_is_dir else ""
                            lines.append(f"    {sub_icon} {name}/{sub_name}{sub_size_str}")
                    except Exception:
                        lines.append(f"    (could not list {name}/)")

            if not lines:
                return f"Empty directory: {path}"
            return f"WebDAV — {path}:\n" + "\n".join(lines)
        except Exception as e:
            return f"Error listing {path}: {e}"

    @tool
    def cloud_read(path: str, target: str = "") -> str:
        """Download a file from WebDAV into the workspace.

        Args:
            path: File path in WebDAV (e.g. "/documents/report.pdf")
            target: Local filename in workspace (default: same as source filename)

        Returns:
            Confirmation with local path, or error message
        """
        try:
            filename = target or os.path.basename(path.rstrip("/"))
            if not filename:
                return "Error: could not determine filename from path"

            documents_dir = str(workspace.get_path("documents"))
            os.makedirs(documents_dir, exist_ok=True)
            local_path = os.path.join(documents_dir, filename)

            client.download_sync(remote_path=path, local_path=local_path)

            size = os.path.getsize(local_path)
            return f"Downloaded {path} → documents/{filename} ({_human_size(size)})"
        except Exception as e:
            return f"Error downloading {path}: {e}"

    @tool
    def cloud_info(path: str) -> str:
        """Get metadata about a file or folder in WebDAV.

        Args:
            path: File or folder path in WebDAV

        Returns:
            File metadata (size, modified date, content type)
        """
        try:
            info = client.info(path)
            if not info:
                return f"No info available for {path}"

            lines = [f"WebDAV — {path}:"]
            if "size" in info:
                lines.append(f"  Size: {_human_size(int(info['size']))}")
            if "modified" in info:
                lines.append(f"  Modified: {info['modified']}")
            if "content_type" in info:
                lines.append(f"  Type: {info['content_type']}")
            if "isdir" in info:
                lines.append(f"  Directory: {info['isdir']}")
            return "\n".join(lines)
        except Exception as e:
            return f"Error getting info for {path}: {e}"

    @tool
    def cloud_write(source: str, remote_path: str) -> str:
        """Upload a file from the workspace to WebDAV.

        Args:
            source: Relative path within workspace (e.g. "output/report.pdf")
            remote_path: Target path in WebDAV (e.g. "/project-files/report.pdf")

        Returns:
            Confirmation with file size, or error message
        """
        try:
            local_path = str(workspace.get_path(source))
            if not os.path.isfile(local_path):
                return f"Error: file not found in workspace: {source}"

            # Create parent directories on WebDAV if needed
            parent = "/".join(remote_path.rstrip("/").split("/")[:-1])
            if parent and parent != "/":
                try:
                    client.mkdir(parent)
                except Exception:
                    pass  # Directory may already exist

            client.upload_sync(remote_path=remote_path, local_path=local_path)
            size = os.path.getsize(local_path)
            return f"Uploaded {source} → {remote_path} ({_human_size(size)})"
        except Exception as e:
            return f"Error uploading {source} to {remote_path}: {e}"

    @tool
    def cloud_delete(path: str) -> str:
        """Delete a file or folder from WebDAV.

        Args:
            path: Path in WebDAV to delete (e.g. "/project-files/old-report.pdf")

        Returns:
            Confirmation message, or error message
        """
        try:
            client.clean(path)
            return f"Deleted {path} from WebDAV"
        except Exception as e:
            return f"Error deleting {path}: {e}"

    return [cloud_list, cloud_read, cloud_info, cloud_write, cloud_delete]


def _human_size(size_bytes: int) -> str:
    """Format bytes as human-readable size."""
    for unit in ("B", "KB", "MB", "GB"):
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}" if unit != "B" else f"{size_bytes} B"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"
