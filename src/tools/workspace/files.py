"""File operations tools for the Universal Agent.

Provides core file read/write/edit operations within the workspace.
These are the most commonly used tools for interacting with workspace files.
"""

import logging
from typing import Any, Dict, List, Optional

from langchain_core.tools import tool

from ..context import ToolContext
from src.utils.pdf import PDFReader, format_document_info, format_read_info

logger = logging.getLogger(__name__)

# Tool metadata for registry
# Phase availability: file tools are available in both strategic and tactical modes
FILE_TOOLS_METADATA: Dict[str, Dict[str, Any]] = {
    "read_file": {
        "module": "workspace.files",
        "function": "read_file",
        "description": "Read content from a file in the workspace",
        "category": "workspace",
        "phases": ["strategic", "tactical"],
    },
    "write_file": {
        "module": "workspace.files",
        "function": "write_file",
        "description": "Write content to a file (requires read_file first for existing files)",
        "category": "workspace",
        "phases": ["strategic", "tactical"],
    },
    "edit_file": {
        "module": "workspace.files",
        "function": "edit_file",
        "description": "Edit a file: replace text, or use position='end'/'start' to append/prepend (requires read_file first)",
        "category": "workspace",
        "phases": ["strategic", "tactical"],
    },
}


def create_file_tools(context: ToolContext) -> List[Any]:
    """Create file operation tools with injected context.

    Args:
        context: ToolContext with workspace_manager

    Returns:
        List of LangChain tool functions

    Raises:
        ValueError: If context doesn't have a workspace_manager
    """
    if not context.has_workspace():
        raise ValueError("ToolContext must have a workspace_manager for file tools")

    workspace = context.workspace_manager

    # Get word limit (with backward compatibility fallback)
    max_read_words = context.get_config("max_read_words")
    if max_read_words is None:
        # Fall back to legacy bytes limit, convert to words
        max_read_size_legacy = context.get_config("max_read_size", 137_500)  # ~25k words
        max_read_words = int(max_read_size_legacy / 5.5)

    # Initialize PDF reader with word limit
    pdf_reader = PDFReader(max_words_per_read=max_read_words)

    # Line-based reading constants (matching Claude Code behavior)
    DEFAULT_LINE_LIMIT = 2000
    MAX_LINE_LIMIT = 2000
    MAX_LINE_LENGTH = 2000  # Truncate lines longer than this

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
    def read_file(
        path: str,
        offset: Optional[int] = None,
        limit: Optional[int] = None,
        page_start: Optional[int] = None,
        page_end: Optional[int] = None
    ) -> str:
        """Read content from a file in the workspace.

        For text files, supports line-based access:
        - read_file("doc.txt") - reads first 2000 lines
        - read_file("doc.txt", offset=500, limit=100) - lines 500-599

        For PDF files, supports page-based access:
        - read_file("doc.pdf") - reads first pages within word limit
        - read_file("doc.pdf", page_start=5, page_end=10) - specific pages

        Args:
            path: Relative path to the file (e.g., "workspace.md")
            offset: For text files: starting line number (1-indexed, default: 1)
            limit: For text files: number of lines to read (default/max: 2000)
            page_start: For PDFs: first page to read (1-indexed)
            page_end: For PDFs: last page to read

        Returns:
            File content with line numbers, or error message.
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
                result = _read_pdf_file(full_path, path, page_start, page_end)
                # Record successful PDF read for read-before-write tracking
                if not result.startswith("Error:"):
                    context.record_file_read(path)
                return result

            # For non-PDF files, page parameters are ignored
            if page_start is not None or page_end is not None:
                logger.warning(f"page_start/page_end ignored for non-PDF file: {path}")

            # Apply line-based reading defaults
            start_line = offset if offset is not None else 1
            line_count = limit if limit is not None else DEFAULT_LINE_LIMIT
            line_count = min(line_count, MAX_LINE_LIMIT)  # Cap at max

            if start_line < 1:
                return "Error: offset must be >= 1 (line numbers are 1-indexed)"

            # Read file content
            content = workspace.read_file(path)
            lines = content.splitlines()
            total_lines = len(lines)

            # Validate offset
            if start_line > total_lines:
                return f"Error: offset ({start_line}) exceeds total lines ({total_lines})"

            # Extract requested range (convert to 0-indexed internally)
            end_line = min(start_line + line_count - 1, total_lines)
            selected_lines = lines[start_line - 1:end_line]

            # Format with line numbers (cat -n style) and truncate long lines
            output_lines = []
            for i, line in enumerate(selected_lines, start=start_line):
                if len(line) > MAX_LINE_LENGTH:
                    line = line[:MAX_LINE_LENGTH] + "..."
                output_lines.append(f"{i:6}\t{line}")

            result = "\n".join(output_lines)

            # Add continuation hint if there are more lines
            if end_line < total_lines:
                result += f"\n\n[Lines {start_line}-{end_line} of {total_lines}. "
                result += f"Use offset={end_line + 1} to continue.]"

            # Record successful read for read-before-write tracking
            context.record_file_read(path)
            return result

        except FileNotFoundError:
            return f"Error: File not found: {path}"
        except ValueError as e:
            return f"Error: {str(e)}"
        except Exception as e:
            logger.error(f"read_file error for {path}: {e}")
            return f"Error reading file: {str(e)}"

    @tool
    def write_file(path: str, content: str) -> str:
        """Write content to a file in the workspace.

        Creates parent directories automatically if they don't exist.
        Overwrites the file if it already exists.

        IMPORTANT: If the file already exists, you must read_file() first to
        understand its current contents before overwriting. This prevents
        accidental data loss from blind overwrites.

        Use this to:
        - Create new files (plan.md, research.md)
        - Save research notes (document_analysis.md)
        - Write intermediate results (candidates/candidates.md)
        - Store processed data (chunks/chunk_001.md)

        Args:
            path: Relative path for the file (e.g., "research.md")
            content: Content to write

        Returns:
            Confirmation message with file path and size
        """
        try:
            # Enforce read-before-write for existing files
            if workspace.exists(path) and not context.was_recently_read(path):
                return (
                    f"Error: You must read_file('{path}') before overwriting an existing file. "
                    f"This ensures you understand the current contents before replacing them. "
                    f"Read the file first, then call write_file again."
                )

            result_path = workspace.write_file(path, content)
            size = len(content.encode('utf-8'))

            return f"Written: {path} ({size:,} bytes)"

        except ValueError as e:
            return f"Error: {str(e)}"
        except Exception as e:
            logger.error(f"write_file error for {path}: {e}")
            return f"Error writing file: {str(e)}"

    @tool
    def edit_file(
        path: str,
        old_string: str = "",
        new_string: str = "",
        position: Optional[str] = None
    ) -> str:
        """Edit a file by replacing text or inserting at start/end.

        IMPORTANT: You must read_file() before editing. This ensures you
        understand the file's current contents before modifying it.

        **Modes:**

        1. **Replace mode** (default): Set `old_string` and `new_string` to find
           and replace text. The `old_string` must appear exactly once.

        2. **Append mode**: Set `position="end"` to add `new_string` at the end
           of the file. The `old_string` parameter is ignored.

        3. **Prepend mode**: Set `position="start"` to add `new_string` at the
           beginning of the file. The `old_string` parameter is ignored.

        Args:
            path: Relative path to the file (e.g., "plan.md")
            old_string: Text to find and replace (required for replace mode)
            new_string: Replacement text or content to insert
            position: Insert position - "start", "end", or None for replace mode

        Returns:
            Confirmation message or error with guidance

        Examples:
            # Replace mode (default)
            edit_file("plan.md", old_string="## Draft", new_string="## Final")

            # Append mode - add to end of file
            edit_file("notes.md", new_string="\\n## New Section", position="end")

            # Prepend mode - add to start of file
            edit_file("log.md", new_string="# Header\\n\\n", position="start")
        """
        try:
            if not workspace.exists(path):
                return f"Error: File not found: {path}"

            full_path = workspace.get_path(path)
            if full_path.is_dir():
                return f"Error: '{path}' is a directory, not a file."

            # Enforce read-before-write discipline
            if not context.was_recently_read(path):
                return (
                    f"Error: You must read_file('{path}') before editing. "
                    f"This ensures you understand the file's current contents. "
                    f"Read the file first, then call edit_file again."
                )

            # Validate position parameter
            if position is not None and position not in ("start", "end"):
                return (
                    f"Error: Invalid position '{position}'. "
                    f"Use 'start' to prepend, 'end' to append, or omit for replace mode."
                )

            content = workspace.read_file(path)

            # Position-based insert modes
            if position == "end":
                new_content = content + new_string
                workspace.write_file(path, new_content)
                size = len(new_content.encode("utf-8"))
                return f"Appended to: {path} ({size:,} bytes)"

            if position == "start":
                new_content = new_string + content
                workspace.write_file(path, new_content)
                size = len(new_content.encode("utf-8"))
                return f"Prepended to: {path} ({size:,} bytes)"

            # Replace mode (default) - requires old_string
            if not old_string:
                return (
                    "Error: old_string is required for replace mode. "
                    "To append, use position='end'. To prepend, use position='start'."
                )

            count = content.count(old_string)

            if count == 0:
                # Show a short snippet of the file to help the caller orient
                preview = content[:200].replace("\n", "\\n")
                return (
                    f"Error: old_string not found in {path}. "
                    f"Make sure the string matches exactly (including whitespace and newlines). "
                    f"File starts with: {preview!r}"
                )

            if count > 1:
                return (
                    f"Error: old_string appears {count} times in {path}. "
                    f"Include more surrounding context to make the match unique."
                )

            new_content = content.replace(old_string, new_string, 1)
            workspace.write_file(path, new_content)

            size = len(new_content.encode("utf-8"))
            return f"Edited: {path} ({size:,} bytes)"

        except ValueError as e:
            return f"Error: {str(e)}"
        except Exception as e:
            logger.error(f"edit_file error for {path}: {e}")
            return f"Error editing file: {str(e)}"

    return [read_file, write_file, edit_file]
