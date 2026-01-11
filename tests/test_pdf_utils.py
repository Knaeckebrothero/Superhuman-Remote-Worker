"""Tests for PDF utilities.

Tests the PDFReader class for page-based PDF reading and document info retrieval.
"""

import pytest
import sys
import importlib.util
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))


def _import_module_directly(module_path: Path, module_name: str):
    """Import a module directly without triggering __init__.py side effects."""
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


# Import pdf_utils module
pdf_utils_path = project_root / "src" / "agents" / "tools" / "pdf_utils.py"
pdf_utils_module = _import_module_directly(pdf_utils_path, "src.agents.tools.pdf_utils")

PDFReader = pdf_utils_module.PDFReader
format_document_info = pdf_utils_module.format_document_info
format_read_info = pdf_utils_module.format_read_info
PDF_AVAILABLE = pdf_utils_module.PDF_AVAILABLE


# Sample PDF for testing (if available)
SAMPLE_PDF = project_root / "data" / "GoBD.pdf"


class TestPDFReader:
    """Tests for PDFReader class."""

    @pytest.fixture
    def reader(self):
        """Create a PDFReader instance."""
        return PDFReader(max_chars_per_read=25_000)

    @pytest.fixture
    def small_reader(self):
        """Create a PDFReader with small size limit for testing truncation."""
        return PDFReader(max_chars_per_read=5_000)

    def test_is_available(self, reader):
        """Test PDF availability check."""
        # Should return True if pdfplumber is installed
        assert reader.is_available() == PDF_AVAILABLE

    @pytest.mark.skipif(not PDF_AVAILABLE, reason="pdfplumber not installed")
    @pytest.mark.skipif(not SAMPLE_PDF.exists(), reason="Sample PDF not found")
    def test_get_document_info(self, reader):
        """Test getting document info from a PDF."""
        info = reader.get_document_info(SAMPLE_PDF)

        assert "page_count" in info
        assert info["page_count"] > 0
        assert "file_size_bytes" in info
        assert info["file_size_bytes"] > 0
        assert "estimated_chars_per_page" in info
        assert "file_name" in info
        assert info["file_name"] == "GoBD.pdf"

    @pytest.mark.skipif(not PDF_AVAILABLE, reason="pdfplumber not installed")
    @pytest.mark.skipif(not SAMPLE_PDF.exists(), reason="Sample PDF not found")
    def test_read_single_page(self, reader):
        """Test reading a single page from a PDF."""
        text, info = reader.read_pages(SAMPLE_PDF, page_start=1, page_end=1)

        assert len(text) > 0
        assert "[PAGE 1]" in text
        assert info["pages_read"] == [1]
        assert info["total_pages"] > 0
        assert not info["was_truncated"]

    @pytest.mark.skipif(not PDF_AVAILABLE, reason="pdfplumber not installed")
    @pytest.mark.skipif(not SAMPLE_PDF.exists(), reason="Sample PDF not found")
    def test_read_page_range(self, reader):
        """Test reading a range of pages from a PDF."""
        text, info = reader.read_pages(SAMPLE_PDF, page_start=1, page_end=3)

        assert "[PAGE 1]" in text
        assert "[PAGE 2]" in text
        assert "[PAGE 3]" in text
        assert info["pages_read"] == [1, 2, 3]
        assert len(info["pages_read"]) == 3

    @pytest.mark.skipif(not PDF_AVAILABLE, reason="pdfplumber not installed")
    @pytest.mark.skipif(not SAMPLE_PDF.exists(), reason="Sample PDF not found")
    def test_auto_pagination(self, small_reader):
        """Test auto-pagination when content exceeds size limit."""
        text, info = small_reader.read_pages(SAMPLE_PDF)

        # Should read some pages but not all if document is large
        assert info["pages_read"]
        assert len(text) <= small_reader.max_chars + 5000  # Some buffer for last page

        # If truncated, should have next_page set
        if info["was_truncated"]:
            assert info["next_page"] is not None
            assert info["next_page"] > info["pages_read"][-1]

    @pytest.mark.skipif(not PDF_AVAILABLE, reason="pdfplumber not installed")
    @pytest.mark.skipif(not SAMPLE_PDF.exists(), reason="Sample PDF not found")
    def test_read_without_explicit_end(self, reader):
        """Test reading without specifying page_end."""
        text, info = reader.read_pages(SAMPLE_PDF, page_start=1)

        assert info["pages_read"]
        assert info["total_pages"] > 0

    @pytest.mark.skipif(not PDF_AVAILABLE, reason="pdfplumber not installed")
    def test_invalid_page_start(self, reader):
        """Test error handling for invalid page_start."""
        if not SAMPLE_PDF.exists():
            pytest.skip("Sample PDF not found")

        with pytest.raises(ValueError, match="page_start must be >= 1"):
            reader.read_pages(SAMPLE_PDF, page_start=0)

    @pytest.mark.skipif(not PDF_AVAILABLE, reason="pdfplumber not installed")
    def test_page_start_exceeds_total(self, reader):
        """Test error handling when page_start exceeds total pages."""
        if not SAMPLE_PDF.exists():
            pytest.skip("Sample PDF not found")

        with pytest.raises(ValueError, match="exceeds total pages"):
            reader.read_pages(SAMPLE_PDF, page_start=9999)

    @pytest.mark.skipif(not PDF_AVAILABLE, reason="pdfplumber not installed")
    def test_invalid_page_range(self, reader):
        """Test error handling for invalid page range."""
        if not SAMPLE_PDF.exists():
            pytest.skip("Sample PDF not found")

        with pytest.raises(ValueError, match="page_end must be >= page_start"):
            reader.read_pages(SAMPLE_PDF, page_start=5, page_end=3)

    def test_file_not_found(self, reader):
        """Test error handling for missing file."""
        if not PDF_AVAILABLE:
            with pytest.raises(ValueError):
                reader.read_pages(Path("/nonexistent/file.pdf"))
        else:
            with pytest.raises(FileNotFoundError):
                reader.read_pages(Path("/nonexistent/file.pdf"))


class TestFormatFunctions:
    """Tests for formatting helper functions."""

    def test_format_document_info(self):
        """Test formatting document info dictionary."""
        info = {
            "file_name": "test.pdf",
            "page_count": 10,
            "file_size_bytes": 50000,
            "estimated_total_chars": 40000,
            "estimated_chars_per_page": 4000,
            "title": "Test Document",
            "author": "Test Author",
        }

        formatted = format_document_info(info)

        assert "test.pdf" in formatted
        assert "10" in formatted  # Page count
        assert "50,000" in formatted  # File size
        assert "40,000" in formatted  # Estimated chars
        assert "Test Document" in formatted
        assert "Test Author" in formatted

    def test_format_document_info_minimal(self):
        """Test formatting with minimal info."""
        info = {
            "file_name": "test.pdf",
            "page_count": 5,
            "file_size_bytes": 10000,
        }

        formatted = format_document_info(info)

        assert "test.pdf" in formatted
        assert "5" in formatted

    def test_format_read_info_not_truncated(self):
        """Test formatting read info when not truncated."""
        info = {
            "pages_read": [1, 2, 3],
            "total_pages": 10,
            "was_truncated": False,
            "next_page": 4,
            "chars_read": 12000,
        }

        formatted = format_read_info(info, "doc.pdf")

        assert "[Pages 1-3 of 10]" in formatted

    def test_format_read_info_truncated(self):
        """Test formatting read info when truncated."""
        info = {
            "pages_read": [1, 2, 3],
            "total_pages": 50,
            "was_truncated": True,
            "next_page": 4,
            "chars_read": 24000,
        }

        formatted = format_read_info(info, "doc.pdf")

        assert "size limit" in formatted.lower()
        assert "read_file" in formatted
        assert "page_start=4" in formatted

    def test_format_read_info_single_page(self):
        """Test formatting read info for single page."""
        info = {
            "pages_read": [5],
            "total_pages": 10,
            "was_truncated": False,
            "next_page": 6,
            "chars_read": 4000,
        }

        formatted = format_read_info(info, "doc.pdf")

        assert "[Page 5 of 10]" in formatted
