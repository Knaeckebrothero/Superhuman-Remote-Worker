"""File operations tools for the Universal Agent.

Provides core file read/write/edit operations within the workspace.
These are the most commonly used tools for interacting with workspace files.

Enhanced with visual content support:
- Multimodal models receive rendered page screenshots (base64)
- Text-only models receive AI-generated descriptions of visual content
- Configurable via `llm.multimodal` in agent config
"""

import base64
import logging
import mimetypes
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from langchain_core.tools import tool

from ..context import ToolContext
from src.utils.pdf import PDFReader, format_document_info, format_read_info

logger = logging.getLogger(__name__)

# Supported image file extensions
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".tif"}

# Document extensions that support visual rendering
VISUAL_DOCUMENT_EXTENSIONS = {".pdf", ".pptx", ".docx"}

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


def _get_mime_type(file_path: Path) -> str:
    """Get MIME type for a file based on extension."""
    mime_type, _ = mimetypes.guess_type(str(file_path))
    return mime_type or "application/octet-stream"


def _is_image_file(file_path: Path) -> bool:
    """Check if file is a supported image format."""
    return file_path.suffix.lower() in IMAGE_EXTENSIONS


def _is_visual_document(file_path: Path) -> bool:
    """Check if file is a document that supports visual rendering."""
    return file_path.suffix.lower() in VISUAL_DOCUMENT_EXTENSIONS


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

    # Check if model is multimodal (from agent config)
    is_multimodal = context.get_config("multimodal", False)

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

    def _handle_image_file(
        full_path: Path,
        describe: Optional[str],
    ) -> str:
        """Handle standalone image files.

        For multimodal models: Returns base64-encoded image data.
        For text-only models: Returns AI-generated description.
        """
        if is_multimodal:
            # Return image for multimodal model to see directly
            try:
                image_data = full_path.read_bytes()
                base64_image = base64.b64encode(image_data).decode()
                mime_type = _get_mime_type(full_path)

                # Return in a format that can be parsed by the agent
                # The LLM will receive this as text, but we format it clearly
                return (
                    f"[IMAGE: {full_path.name}]\n"
                    f"Type: {mime_type}\n"
                    f"Size: {len(image_data):,} bytes\n\n"
                    f"<image_data mime_type=\"{mime_type}\">\n"
                    f"{base64_image}\n"
                    f"</image_data>"
                )
            except Exception as e:
                logger.error(f"Error reading image {full_path}: {e}")
                return f"Error reading image: {str(e)}"
        else:
            # Get AI description for text-only model
            try:
                from src.services.vision_helper import get_vision_helper
                from src.services.description_cache import get_description_cache

                cache = get_description_cache()
                vision = get_vision_helper()

                # Check cache first
                cached = cache.get(full_path, query=describe)
                if cached:
                    logger.debug(f"Cache hit for image: {full_path.name}")
                    return f"[IMAGE: {full_path.name}]\n\n{cached}"

                # Generate description
                image_data = full_path.read_bytes()
                description = vision.describe_image_sync(
                    image_data,
                    mime_type=_get_mime_type(full_path),
                    query=describe,
                )

                # Cache for future use
                cache.set(full_path, description, query=describe)

                return f"[IMAGE: {full_path.name}]\n\n{description}"

            except ImportError as e:
                logger.warning(f"Vision services not available: {e}")
                return (
                    f"[IMAGE: {full_path.name}]\n"
                    f"(Visual description not available - vision services not configured)"
                )
            except Exception as e:
                logger.error(f"Error describing image {full_path}: {e}")
                return f"[IMAGE: {full_path.name}]\n(Error generating description: {str(e)})"

    def _get_visual_content(
        full_path: Path,
        page_num: int,
        describe: Optional[str],
    ) -> str:
        """Get visual content description for a document page.

        For multimodal models: Returns base64-encoded page screenshot.
        For text-only models: Returns AI-generated description.
        """
        try:
            from src.services.document_renderer import get_document_renderer
            from src.services.vision_helper import get_vision_helper
            from src.services.description_cache import get_description_cache

            renderer = get_document_renderer()

            # Render the page as PNG
            try:
                page_image = renderer.render_page(full_path, page_num)
            except Exception as e:
                logger.warning(f"Could not render page {page_num} of {full_path.name}: {e}")
                return ""  # No visual content available

            if is_multimodal:
                # Return base64 image for multimodal model
                base64_image = base64.b64encode(page_image).decode()
                return (
                    f"\n<page_image page=\"{page_num}\" mime_type=\"image/png\">\n"
                    f"{base64_image}\n"
                    f"</page_image>"
                )
            else:
                # Get AI description for text-only model
                cache = get_description_cache()
                vision = get_vision_helper()

                # Check cache first
                cached = cache.get(full_path, page=page_num, query=describe)
                if cached:
                    logger.debug(f"Cache hit for page {page_num} of {full_path.name}")
                    return f"\n[PAGE {page_num} - VISUAL CONTENT]\n{cached}"

                # Generate description
                description = vision.describe_document_page_sync(
                    page_image,
                    page_num=page_num,
                    query=describe,
                )

                # Cache for future use
                cache.set(full_path, description, page=page_num, query=describe)

                if describe:
                    return f"\n[PAGE {page_num} - VISUAL CONTENT (Query: \"{describe[:50]}...\")]\n{description}"
                else:
                    return f"\n[PAGE {page_num} - VISUAL CONTENT]\n{description}"

        except ImportError as e:
            logger.debug(f"Vision services not available: {e}")
            return ""  # Silently skip visual content if services not available
        except Exception as e:
            logger.warning(f"Error getting visual content for page {page_num}: {e}")
            return ""

    def _read_visual_document(
        full_path: Path,
        relative_path: str,
        page_start: Optional[int],
        page_end: Optional[int],
        describe: Optional[str],
    ) -> str:
        """Read a document with visual content (PDF, PPTX, DOCX).

        Combines text extraction with visual content based on multimodal setting.
        """
        suffix = full_path.suffix.lower()

        # For PDF, use existing reader for text
        if suffix == ".pdf":
            text_result = _read_pdf_file(full_path, relative_path, page_start, page_end)

            # If there was an error, return it
            if text_result.startswith("Error:"):
                return text_result

            # Get page range that was read
            try:
                from src.services.document_renderer import get_document_renderer
                renderer = get_document_renderer()
                total_pages = renderer.get_page_count(full_path)

                start = page_start or 1
                end = min(page_end or total_pages, total_pages)

                # Add visual content for each page read
                visual_parts = []
                for page_num in range(start, end + 1):
                    visual_content = _get_visual_content(full_path, page_num, describe)
                    if visual_content:
                        visual_parts.append(visual_content)

                if visual_parts:
                    return text_result + "\n" + "\n".join(visual_parts)
                return text_result

            except Exception as e:
                logger.debug(f"Could not add visual content: {e}")
                return text_result

        # For PPTX and DOCX, we need different text extraction
        elif suffix == ".pptx":
            return _read_pptx_file(full_path, relative_path, page_start, page_end, describe)
        elif suffix == ".docx":
            return _read_docx_file(full_path, relative_path, page_start, page_end, describe)
        else:
            return f"Error: Unsupported visual document type: {suffix}"

    def _read_pptx_file(
        full_path: Path,
        relative_path: str,
        slide_start: Optional[int],
        slide_end: Optional[int],
        describe: Optional[str],
    ) -> str:
        """Read a PowerPoint file with text and visual content."""
        try:
            from pptx import Presentation

            prs = Presentation(full_path)
            total_slides = len(prs.slides)

            start = slide_start or 1
            end = min(slide_end or total_slides, total_slides)

            if start > total_slides:
                return f"Error: slide_start ({start}) exceeds total slides ({total_slides})"

            result_parts = [f"[Slides {start}-{end} of {total_slides}]", ""]

            for slide_num in range(start, end + 1):
                slide = prs.slides[slide_num - 1]

                # Extract text from slide
                slide_text = []
                for shape in slide.shapes:
                    if hasattr(shape, "text") and shape.text.strip():
                        slide_text.append(shape.text.strip())

                result_parts.append(f"[SLIDE {slide_num}]")
                if slide_text:
                    result_parts.append("\n".join(slide_text))
                else:
                    result_parts.append("(No text content)")

                # Add visual content
                visual_content = _get_visual_content(full_path, slide_num, describe)
                if visual_content:
                    result_parts.append(visual_content)

                result_parts.append("")  # Blank line between slides

            return "\n".join(result_parts)

        except ImportError:
            return "Error: python-pptx not installed. Install with: pip install python-pptx"
        except Exception as e:
            logger.error(f"PPTX read error for {relative_path}: {e}")
            return f"Error reading PowerPoint: {str(e)}"

    def _read_docx_file(
        full_path: Path,
        relative_path: str,
        page_start: Optional[int],
        page_end: Optional[int],
        describe: Optional[str],
    ) -> str:
        """Read a Word document with text and visual content."""
        try:
            from docx import Document

            doc = Document(full_path)

            # Extract all text first
            text_parts = []
            for para in doc.paragraphs:
                if para.text.strip():
                    text_parts.append(para.text)

            # Also extract text from tables
            for table in doc.tables:
                for row in table.rows:
                    row_text = [cell.text.strip() for cell in row.cells if cell.text.strip()]
                    if row_text:
                        text_parts.append(" | ".join(row_text))

            text_content = "\n\n".join(text_parts)

            # Try to get page count and visual content
            try:
                from src.services.document_renderer import get_document_renderer
                renderer = get_document_renderer()
                total_pages = renderer.get_page_count(full_path)

                start = page_start or 1
                end = min(page_end or total_pages, total_pages)

                result_parts = [f"[Pages {start}-{end} of {total_pages}]", "", text_content]

                # Add visual content for requested pages
                visual_parts = []
                for page_num in range(start, end + 1):
                    visual_content = _get_visual_content(full_path, page_num, describe)
                    if visual_content:
                        visual_parts.append(visual_content)

                if visual_parts:
                    result_parts.append("\n" + "\n".join(visual_parts))

                return "\n".join(result_parts)

            except Exception as e:
                # If visual rendering fails, just return text
                logger.debug(f"Could not add visual content for DOCX: {e}")
                return f"[Document: {full_path.name}]\n\n{text_content}"

        except ImportError:
            return "Error: python-docx not installed. Install with: pip install python-docx"
        except Exception as e:
            logger.error(f"DOCX read error for {relative_path}: {e}")
            return f"Error reading Word document: {str(e)}"

    @tool
    def read_file(
        path: str,
        offset: Optional[int] = None,
        limit: Optional[int] = None,
        page_start: Optional[int] = None,
        page_end: Optional[int] = None,
        describe: Optional[str] = None,
    ) -> str:
        """Read content from a file in the workspace.

        For text files, supports line-based access:
        - read_file("doc.txt") - reads first 2000 lines
        - read_file("doc.txt", offset=500, limit=100) - lines 500-599

        For documents (PDF, PPTX, DOCX), supports page-based access:
        - read_file("doc.pdf") - reads first pages within word limit
        - read_file("doc.pdf", page_start=5, page_end=10) - specific pages
        - Visual content (charts, diagrams) is automatically included

        For image files (PNG, JPG, etc.):
        - Returns image data or AI-generated description

        Args:
            path: Relative path to the file (e.g., "workspace.md")
            offset: For text files: starting line number (1-indexed, default: 1)
            limit: For text files: number of lines to read (default/max: 2000)
            page_start: For documents: first page/slide to read (1-indexed)
            page_end: For documents: last page/slide to read
            describe: Optional query for visual analysis (e.g., "What values are in this chart?")

        Returns:
            File content with line numbers, or error message.
            For documents: includes text + visual content descriptions.
            For images: includes image data or description.
        """
        try:
            # Check file exists
            if not workspace.exists(path):
                return f"Error: File not found: {path}"

            full_path = workspace.get_path(path)
            if full_path.is_dir():
                return f"Error: '{path}' is a directory, not a file. Use list_files to see its contents."

            # Handle image files
            if _is_image_file(full_path):
                result = _handle_image_file(full_path, describe)
                if not result.startswith("Error:"):
                    context.record_file_read(path)
                return result

            # Handle visual documents (PDF, PPTX, DOCX) with page-based reading + visual content
            if _is_visual_document(full_path):
                result = _read_visual_document(full_path, path, page_start, page_end, describe)
                if not result.startswith("Error:"):
                    context.record_file_read(path)
                return result

            # For non-document files, page parameters are ignored
            if page_start is not None or page_end is not None:
                logger.warning(f"page_start/page_end ignored for non-document file: {path}")

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
