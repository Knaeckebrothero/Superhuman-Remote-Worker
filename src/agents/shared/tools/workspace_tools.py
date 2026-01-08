"""Workspace tools for filesystem operations.

Provides LangGraph-compatible tools for reading, writing, and managing
files within an agent's workspace. These tools enable the strategic
planning tier of the workspace-centric agent architecture.

All paths are relative to the job workspace root. Path traversal
attempts (e.g., '../') are blocked by the underlying WorkspaceManager.
"""

import logging
from typing import List, Optional

from langchain_core.tools import tool

from .context import ToolContext

logger = logging.getLogger(__name__)


def create_workspace_tools(context: ToolContext) -> List:
    """Create workspace tools bound to a specific context.

    Args:
        context: ToolContext with workspace_manager

    Returns:
        List of LangChain tool functions

    Raises:
        ValueError: If context doesn't have a workspace_manager
    """
    if not context.has_workspace():
        raise ValueError("ToolContext must have a workspace_manager for workspace tools")

    workspace = context.workspace_manager
    max_read_size = context.get_config("max_read_size", 100_000)  # 100KB default
    max_search_results = context.get_config("max_search_results", 50)

    @tool
    def read_file(path: str) -> str:
        """Read content from a file in the workspace.

        Use this to retrieve previously written files, read instructions,
        or access any file in your workspace.

        Args:
            path: Relative path to the file (e.g., "plans/main_plan.md")

        Returns:
            File content or error message
        """
        try:
            content = workspace.read_file(path)

            # Truncate very large files to prevent context overflow
            if len(content) > max_read_size:
                truncated = content[:max_read_size]
                return (
                    f"{truncated}\n\n"
                    f"[TRUNCATED: File is {len(content):,} bytes, showing first {max_read_size:,}. "
                    f"Use search_files to find specific content.]"
                )

            return content

        except FileNotFoundError:
            return f"Error: File not found: {path}"
        except ValueError as e:
            # Path validation errors (e.g., path traversal attempts)
            return f"Error: {str(e)}"
        except Exception as e:
            logger.error(f"read_file error for {path}: {e}")
            return f"Error reading file: {str(e)}"

    @tool
    def write_file(path: str, content: str) -> str:
        """Write content to a file in the workspace.

        Creates parent directories automatically if they don't exist.
        Overwrites the file if it already exists.

        Use this to:
        - Create plans (plans/main_plan.md)
        - Save research notes (notes/research.md)
        - Write intermediate results (candidates/candidates.md)
        - Store processed data (chunks/chunk_001.md)

        Args:
            path: Relative path for the file (e.g., "notes/research.md")
            content: Content to write

        Returns:
            Confirmation message with file path and size
        """
        try:
            result_path = workspace.write_file(path, content)
            size = len(content.encode('utf-8'))

            return f"Written: {path} ({size:,} bytes)"

        except ValueError as e:
            return f"Error: {str(e)}"
        except Exception as e:
            logger.error(f"write_file error for {path}: {e}")
            return f"Error writing file: {str(e)}"

    @tool
    def append_file(path: str, content: str) -> str:
        """Append content to an existing file in the workspace.

        Creates the file if it doesn't exist.
        Use this for log-style files or incremental updates.

        Args:
            path: Relative path to the file
            content: Content to append

        Returns:
            Confirmation message
        """
        try:
            workspace.append_file(path, content)
            size = len(content.encode('utf-8'))

            return f"Appended to: {path} ({size:,} bytes added)"

        except ValueError as e:
            return f"Error: {str(e)}"
        except Exception as e:
            logger.error(f"append_file error for {path}: {e}")
            return f"Error appending to file: {str(e)}"

    @tool
    def list_files(path: str = "", pattern: str = "*") -> str:
        """List files and directories in a workspace path.

        Directories are shown with a trailing slash.
        Use this to explore your workspace structure.

        Args:
            path: Relative directory path (empty for workspace root)
            pattern: Glob pattern to filter files (e.g., "*.md", "*.json")

        Returns:
            List of files and directories, or message if empty
        """
        try:
            items = workspace.list_files(path, pattern=pattern)

            if not items:
                return f"No files found in: {path or '/'}" + (
                    f" matching '{pattern}'" if pattern != "*" else ""
                )

            # Format output
            header = f"Contents of {path or '/'}:"
            if pattern != "*":
                header += f" (pattern: {pattern})"

            # Separate directories and files
            dirs = sorted([i for i in items if i.endswith("/")])
            files = sorted([i for i in items if not i.endswith("/")])

            lines = [header, ""]
            if dirs:
                lines.append("Directories:")
                for d in dirs:
                    lines.append(f"  {d}")
            if files:
                if dirs:
                    lines.append("")
                lines.append("Files:")
                for f in files:
                    lines.append(f"  {f}")

            return "\n".join(lines)

        except ValueError as e:
            return f"Error: {str(e)}"
        except Exception as e:
            logger.error(f"list_files error for {path}: {e}")
            return f"Error listing files: {str(e)}"

    @tool
    def delete_file(path: str) -> str:
        """Delete a file or empty directory from the workspace.

        Cannot delete non-empty directories (delete contents first).

        Args:
            path: Relative path to delete

        Returns:
            Confirmation or error message
        """
        try:
            success = workspace.delete_file(path)

            if success:
                return f"Deleted: {path}"
            else:
                return f"Not found: {path}"

        except ValueError as e:
            return f"Error: {str(e)}"
        except Exception as e:
            logger.error(f"delete_file error for {path}: {e}")
            return f"Error deleting: {str(e)}"

    @tool
    def search_files(
        query: str,
        path: str = "",
        case_sensitive: bool = False
    ) -> str:
        """Search for text content in workspace files.

        Searches through all text files and returns matching lines
        with file paths and line numbers.

        Args:
            query: Text or pattern to search for
            path: Directory to search in (empty for entire workspace)
            case_sensitive: Whether to match case exactly

        Returns:
            Search results with file paths, line numbers, and matching lines
        """
        try:
            results = workspace.search_files(
                query,
                path=path,
                case_sensitive=case_sensitive
            )

            if not results:
                return f"No matches found for: {query}"

            # Limit results
            total = len(results)
            results = results[:max_search_results]

            lines = [f"Search results for '{query}':", ""]

            # Group by file
            current_file = None
            for result in results:
                file_path = result["path"]
                if file_path != current_file:
                    if current_file is not None:
                        lines.append("")
                    lines.append(f"  {file_path}:")
                    current_file = file_path

                line_num = result.get("line_number", "?")
                line_text = result.get("line", "").strip()
                # Truncate long lines
                if len(line_text) > 100:
                    line_text = line_text[:100] + "..."
                lines.append(f"    L{line_num}: {line_text}")

            if total > max_search_results:
                lines.append("")
                lines.append(f"[Showing {max_search_results} of {total} matches]")

            return "\n".join(lines)

        except ValueError as e:
            return f"Error: {str(e)}"
        except Exception as e:
            logger.error(f"search_files error: {e}")
            return f"Error searching: {str(e)}"

    @tool
    def file_exists(path: str) -> str:
        """Check if a file or directory exists in the workspace.

        Args:
            path: Relative path to check

        Returns:
            "exists" or "not found" message
        """
        try:
            if workspace.exists(path):
                full_path = workspace.get_path(path)
                if full_path.is_dir():
                    return f"Exists (directory): {path}"
                else:
                    size = workspace.get_size(path)
                    return f"Exists (file): {path} ({size:,} bytes)"
            else:
                return f"Not found: {path}"

        except ValueError as e:
            return f"Error: {str(e)}"
        except Exception as e:
            logger.error(f"file_exists error for {path}: {e}")
            return f"Error: {str(e)}"

    @tool
    def get_workspace_summary() -> str:
        """Get a summary of the current workspace state.

        Returns information about the workspace including:
        - Job ID
        - Directory structure
        - File counts per directory
        - Total size

        Use this to understand what's in your workspace.

        Returns:
            Workspace summary
        """
        try:
            summary = workspace.get_summary()

            lines = [
                f"Workspace Summary",
                f"================",
                f"Job ID: {summary['job_id']}",
                f"Path: {summary['path']}",
                f"Total Files: {summary['total_files']}",
                f"Total Size: {summary['total_size_bytes']:,} bytes",
                "",
                "Directories:",
            ]

            for dir_name, info in sorted(summary["directories"].items()):
                count = info.get("file_count", 0)
                size = info.get("size_bytes", 0)
                lines.append(f"  {dir_name}/: {count} files ({size:,} bytes)")

            return "\n".join(lines)

        except Exception as e:
            logger.error(f"get_workspace_summary error: {e}")
            return f"Error getting summary: {str(e)}"

    # Return all workspace tools
    return [
        read_file,
        write_file,
        append_file,
        list_files,
        delete_file,
        search_files,
        file_exists,
        get_workspace_summary,
    ]


# Tool metadata for registry
WORKSPACE_TOOLS_METADATA = {
    "read_file": {
        "module": "workspace_tools",
        "function": "read_file",
        "description": "Read content from a file in the workspace",
        "category": "workspace",
    },
    "write_file": {
        "module": "workspace_tools",
        "function": "write_file",
        "description": "Write content to a file in the workspace",
        "category": "workspace",
    },
    "append_file": {
        "module": "workspace_tools",
        "function": "append_file",
        "description": "Append content to a file in the workspace",
        "category": "workspace",
    },
    "list_files": {
        "module": "workspace_tools",
        "function": "list_files",
        "description": "List files and directories in the workspace",
        "category": "workspace",
    },
    "delete_file": {
        "module": "workspace_tools",
        "function": "delete_file",
        "description": "Delete a file or empty directory",
        "category": "workspace",
    },
    "search_files": {
        "module": "workspace_tools",
        "function": "search_files",
        "description": "Search for text content in workspace files",
        "category": "workspace",
    },
    "file_exists": {
        "module": "workspace_tools",
        "function": "file_exists",
        "description": "Check if a file or directory exists",
        "category": "workspace",
    },
    "get_workspace_summary": {
        "module": "workspace_tools",
        "function": "get_workspace_summary",
        "description": "Get a summary of workspace contents",
        "category": "workspace",
    },
}
