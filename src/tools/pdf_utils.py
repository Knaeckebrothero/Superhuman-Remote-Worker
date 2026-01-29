"""PDF utilities for workspace tools.

Provides page-based PDF reading capabilities using pdfplumber.
Designed for intelligent partial reading that respects context window limits.
"""

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

try:
    import pdfplumber
    PDF_AVAILABLE = True
except ImportError:
    PDF_AVAILABLE = False
    pdfplumber = None


class PDFReader:
    """Utility class for page-based PDF reading with auto-pagination."""

    def __init__(self, max_words_per_read: int = 25_000):
        """Initialize PDF reader with word limits.

        Args:
            max_words_per_read: Maximum words to return in a single read.
                               Default 25,000 (~25,000 tokens).
        """
        self.max_words = max_words_per_read

    def is_available(self) -> bool:
        """Check if PDF reading is available."""
        return PDF_AVAILABLE

    def get_document_info(self, file_path: Path) -> Dict[str, Any]:
        """Get PDF metadata without reading full content.

        Args:
            file_path: Path to the PDF file

        Returns:
            Dictionary with:
                - page_count: Total number of pages
                - file_size_bytes: File size in bytes
                - estimated_chars_per_page: Average characters per page
                - estimated_total_chars: Estimated total characters
                - title: Document title (if available)
                - author: Document author (if available)
                - creation_date: Creation date (if available)

        Raises:
            ValueError: If pdfplumber is not available
            FileNotFoundError: If file doesn't exist
        """
        if not PDF_AVAILABLE:
            raise ValueError(
                "PDF reading requires pdfplumber. Install with: pip install pdfplumber"
            )

        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        info = {
            "file_path": str(path),
            "file_name": path.name,
            "file_size_bytes": path.stat().st_size,
            "page_count": 0,
            "estimated_chars_per_page": 0,
            "estimated_total_chars": 0,
            "title": None,
            "author": None,
            "creation_date": None,
        }

        with pdfplumber.open(path) as pdf:
            info["page_count"] = len(pdf.pages)

            # Get metadata if available
            if pdf.metadata:
                info["title"] = pdf.metadata.get("Title")
                info["author"] = pdf.metadata.get("Author")
                info["creation_date"] = pdf.metadata.get("CreationDate")

            # Sample a few pages to estimate average chars per page
            sample_pages = min(5, len(pdf.pages))
            total_sample_chars = 0

            for i in range(sample_pages):
                page_text = pdf.pages[i].extract_text() or ""
                total_sample_chars += len(page_text)

            if sample_pages > 0:
                info["estimated_chars_per_page"] = total_sample_chars // sample_pages
                info["estimated_total_chars"] = (
                    info["estimated_chars_per_page"] * info["page_count"]
                )

        return info

    def read_pages(
        self,
        file_path: Path,
        page_start: Optional[int] = None,
        page_end: Optional[int] = None,
    ) -> Tuple[str, Dict[str, Any]]:
        """Read specific pages from a PDF file.

        Args:
            file_path: Path to the PDF file
            page_start: First page to read (1-indexed). Default: 1
            page_end: Last page to read (1-indexed, inclusive).
                     Default: auto-limit based on max_chars

        Returns:
            Tuple of (text_content, read_info)

            read_info contains:
                - pages_read: List of page numbers read
                - total_pages: Total pages in document
                - was_truncated: True if stopped due to size limit
                - next_page: Next page to read (if truncated)
                - chars_read: Total characters returned

        Raises:
            ValueError: If pdfplumber not available or invalid page range
            FileNotFoundError: If file doesn't exist
        """
        if not PDF_AVAILABLE:
            raise ValueError(
                "PDF reading requires pdfplumber. Install with: pip install pdfplumber"
            )

        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        # Default page_start to 1
        if page_start is None:
            page_start = 1

        # Validate page_start
        if page_start < 1:
            raise ValueError("page_start must be >= 1")

        read_info = {
            "pages_read": [],
            "total_pages": 0,
            "was_truncated": False,
            "next_page": None,
            "words_read": 0,
            "page_start": page_start,
            "page_end": page_end,
        }

        text_parts: List[str] = []
        total_words = 0

        with pdfplumber.open(path) as pdf:
            total_pages = len(pdf.pages)
            read_info["total_pages"] = total_pages

            # Validate page range
            if page_start > total_pages:
                raise ValueError(
                    f"page_start ({page_start}) exceeds total pages ({total_pages})"
                )

            # Determine effective end page
            if page_end is not None:
                if page_end < page_start:
                    raise ValueError("page_end must be >= page_start")
                effective_end = min(page_end, total_pages)
            else:
                effective_end = total_pages

            # Read pages until we hit the limit or end
            for page_num in range(page_start, effective_end + 1):
                # pdfplumber uses 0-indexed pages
                page = pdf.pages[page_num - 1]
                page_text = page.extract_text() or ""

                # Count words in this page
                page_words = len(page_text.split())

                # Check if adding this page would exceed word limit
                # (Only check when page_end is not explicitly set)
                if page_end is None and total_words + page_words > self.max_words:
                    # If we haven't read anything yet, read at least one page
                    if not text_parts:
                        text_parts.append(f"[PAGE {page_num}]\n{page_text}")
                        read_info["pages_read"].append(page_num)
                        total_words += page_words

                    read_info["was_truncated"] = True
                    read_info["next_page"] = page_num if not text_parts else page_num
                    break

                text_parts.append(f"[PAGE {page_num}]\n{page_text}")
                read_info["pages_read"].append(page_num)
                total_words += page_words

            # Set next_page if there are more pages
            if read_info["pages_read"]:
                last_read = read_info["pages_read"][-1]
                if last_read < total_pages:
                    read_info["next_page"] = last_read + 1

        read_info["words_read"] = total_words
        return "\n\n".join(text_parts), read_info


def format_document_info(info: Dict[str, Any]) -> str:
    """Format document info dictionary as human-readable string.

    Args:
        info: Dictionary from PDFReader.get_document_info()

    Returns:
        Formatted string for display
    """
    lines = [
        f"Document: {info['file_name']}",
        f"Pages: {info['page_count']}",
        f"File size: {info['file_size_bytes']:,} bytes",
    ]

    if info.get("estimated_total_chars"):
        estimated_tokens = info["estimated_total_chars"] // 4
        lines.append(
            f"Estimated content: ~{info['estimated_total_chars']:,} chars "
            f"(~{estimated_tokens:,} tokens)"
        )

    if info.get("estimated_chars_per_page"):
        tokens_per_page = info["estimated_chars_per_page"] // 4
        lines.append(
            f"Average per page: ~{info['estimated_chars_per_page']:,} chars "
            f"(~{tokens_per_page:,} tokens)"
        )

    if info.get("title"):
        lines.append(f"Title: {info['title']}")

    if info.get("author"):
        lines.append(f"Author: {info['author']}")

    return "\n".join(lines)


def format_read_info(info: Dict[str, Any], file_path: str) -> str:
    """Format read info as continuation guidance.

    Args:
        info: Dictionary from PDFReader.read_pages()
        file_path: Original file path (for continuation hint)

    Returns:
        Formatted string with guidance on continuing
    """
    if not info["was_truncated"]:
        pages = info["pages_read"]
        if len(pages) == 1:
            return f"[Page {pages[0]} of {info['total_pages']}]"
        return f"[Pages {pages[0]}-{pages[-1]} of {info['total_pages']}]"

    lines = [
        "---",
        f"Reached word limit after {len(info['pages_read'])} pages "
        f"(~{info['words_read']:,} words).",
    ]

    if info["next_page"]:
        # Suggest reading the next batch
        suggested_end = min(
            info["next_page"] + len(info["pages_read"]) - 1,
            info["total_pages"]
        )
        lines.append(
            f"To continue: read_file(\"{file_path}\", "
            f"page_start={info['next_page']}, page_end={suggested_end})"
        )

    lines.append(f"Total pages: {info['total_pages']}")

    return "\n".join(lines)
