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
from .pdf_utils import PDFReader, format_document_info, format_read_info

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
    max_read_size = context.get_config("max_read_size", 25_000)  # 25KB default (~7,500 tokens)
    max_search_results = context.get_config("max_search_results", 50)

    # Initialize PDF reader with same size limit
    pdf_reader = PDFReader(max_chars_per_read=max_read_size)

    @tool
    def read_file(
        path: str,
        page_start: Optional[int] = None,
        page_end: Optional[int] = None
    ) -> str:
        """Read content from a file in the workspace.

        For PDF files, supports page-based access:
        - read_file("doc.pdf") - reads first pages within size limit
        - read_file("doc.pdf", page_start=5, page_end=10) - specific page range

        Args:
            path: Relative path to the file (e.g., "documents/GoBD.pdf")
            page_start: For PDFs: first page to read (1-indexed, default: 1)
            page_end: For PDFs: last page to read (default: auto-limit by size)

        Returns:
            File content or error message. For PDFs, includes page info.
        """
        try:
            # Check file exists
            if not workspace.exists(path):
                return f"Error: File not found: {path}"

            full_path = workspace.get_path(path)
            if full_path.is_dir():
                return f"Error: '{path}' is a directory, not a file. Use list_files to see its contents."

            # Handle PDF files with page-based reading
            if full_path.suffix.lower() == '.pdf':
                return _read_pdf_file(full_path, path, page_start, page_end)

            # For non-PDF files, page parameters are ignored
            if page_start is not None or page_end is not None:
                logger.warning(f"page_start/page_end ignored for non-PDF file: {path}")

            file_size = full_path.stat().st_size

            # Reject files that are too large
            if file_size > max_read_size:
                estimated_tokens = file_size // 4
                max_tokens = max_read_size // 4
                return (
                    f"Error: File '{path}' ({file_size:,} bytes, ~{estimated_tokens:,} tokens) "
                    f"exceeds maximum allowed size ({max_read_size:,} bytes, ~{max_tokens:,} tokens).\n\n"
                    f"Alternatives:\n"
                    f"- Use search_files('{path.split('/')[-1].split('.')[0]}') to find specific content\n"
                    f"- Read individual chunks from the chunks/ directory instead\n"
                    f"- For document content, always prefer reading from chunks/chunk_XXX.txt files"
                )

            content = workspace.read_file(path)
            return content

        except FileNotFoundError:
            return f"Error: File not found: {path}"
        except ValueError as e:
            return f"Error: {str(e)}"
        except Exception as e:
            logger.error(f"read_file error for {path}: {e}")
            return f"Error reading file: {str(e)}"

    def _read_pdf_file(
        full_path,
        relative_path: str,
        page_start: Optional[int],
        page_end: Optional[int]
    ) -> str:
        """Internal helper to read PDF files with page support."""
        if not pdf_reader.is_available():
            return "Error: PDF reading requires pdfplumber. Install with: pip install pdfplumber"

        try:
            text, read_info = pdf_reader.read_pages(
                full_path,
                page_start=page_start,
                page_end=page_end
            )

            # Build header showing what was read
            pages = read_info["pages_read"]
            if len(pages) == 1:
                header = f"[Page {pages[0]} of {read_info['total_pages']}]"
            elif len(pages) > 1:
                header = f"[Pages {pages[0]}-{pages[-1]} of {read_info['total_pages']}]"
            else:
                header = f"[No pages read - total pages: {read_info['total_pages']}]"

            # Build result
            result_parts = [header, "", text]

            # Add continuation guidance if truncated
            if read_info["was_truncated"]:
                result_parts.append("")
                result_parts.append(format_read_info(read_info, relative_path))

            return "\n".join(result_parts)

        except ValueError as e:
            return f"Error: {str(e)}"
        except Exception as e:
            logger.error(f"PDF read error for {relative_path}: {e}")
            return f"Error reading PDF: {str(e)}"

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

    @tool
    def get_document_info(path: str) -> str:
        """Get metadata about a document without reading its full content.

        Returns page count, estimated size, and available metadata.
        Use this before reading large PDFs to plan page-by-page access.

        Args:
            path: Relative path to the document (e.g., "documents/GoBD.pdf")

        Returns:
            Document metadata including page count, estimated tokens, file size.
            For non-PDF files, returns basic file information.
        """
        try:
            if not workspace.exists(path):
                return f"Error: File not found: {path}"

            full_path = workspace.get_path(path)
            if full_path.is_dir():
                return f"Error: '{path}' is a directory, not a file."

            # For PDF files, get detailed info
            if full_path.suffix.lower() == '.pdf':
                if not pdf_reader.is_available():
                    return "Error: PDF info requires pdfplumber. Install with: pip install pdfplumber"

                try:
                    info = pdf_reader.get_document_info(full_path)

                    # Format with reading suggestions
                    lines = [format_document_info(info), ""]

                    # Add reading suggestions based on size
                    total_chars = info.get("estimated_total_chars", 0)
                    page_count = info.get("page_count", 0)

                    if total_chars <= max_read_size:
                        lines.append("This document fits in a single read.")
                        lines.append(f'Use: read_file("{path}")')
                    else:
                        # Estimate how many pages fit in one read
                        chars_per_page = info.get("estimated_chars_per_page", 3000)
                        pages_per_read = max(1, max_read_size // chars_per_page)

                        lines.append(f"Suggested approach (document exceeds single-read limit):")
                        lines.append(f"- Read ~{pages_per_read} pages at a time")
                        lines.append(f'- Start with: read_file("{path}", page_start=1, page_end={min(pages_per_read, page_count)})')
                        if page_count > pages_per_read:
                            lines.append(f'- Continue with: read_file("{path}", page_start={pages_per_read + 1}, page_end={min(pages_per_read * 2, page_count)})')

                    return "\n".join(lines)

                except Exception as e:
                    logger.error(f"PDF info error for {path}: {e}")
                    return f"Error getting PDF info: {str(e)}"

            # For non-PDF files, return basic info
            file_size = full_path.stat().st_size
            estimated_tokens = file_size // 4

            lines = [
                f"Document: {full_path.name}",
                f"Type: {full_path.suffix or 'unknown'}",
                f"File size: {file_size:,} bytes (~{estimated_tokens:,} tokens)",
            ]

            if file_size <= max_read_size:
                lines.append(f"\nThis file fits in a single read.")
                lines.append(f'Use: read_file("{path}")')
            else:
                lines.append(f"\nFile exceeds single-read limit ({max_read_size:,} bytes).")
                lines.append("Consider using search_files() or chunking.")

            return "\n".join(lines)

        except ValueError as e:
            return f"Error: {str(e)}"
        except Exception as e:
            logger.error(f"get_document_info error for {path}: {e}")
            return f"Error: {str(e)}"

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
        get_document_info,
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
    "get_document_info": {
        "module": "workspace_tools",
        "function": "get_document_info",
        "description": "Get document metadata (page count, size) for planning access",
        "category": "workspace",
        "defer_to_workspace": True,
        "short_description": "Get PDF/document metadata (pages, size) for planning access.",
    },
}
