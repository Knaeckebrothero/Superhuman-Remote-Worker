# src/services/document_renderer.py
"""
Document Renderer service for converting document pages to images.

Renders pages from PDF, PPTX, and DOCX files as PNG images for visual analysis.
Used by the read_file tool when visual content needs to be processed.

Ported from Advanced-LLM-Chat/backend/services/document_handler.py with extensions:
- Added PPTX slide rendering via LibreOffice
- Added DOCX page rendering via LibreOffice
- Returns bytes directly instead of saving to files
"""

import io
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Maximum pages to render (safety limit)
MAX_RENDER_PAGES = 20

# Default DPI for rendering
DEFAULT_DPI = 150


class DocumentRenderer:
    """
    Renders document pages as PNG images for visual analysis.

    Supports PDF, PPTX, and DOCX formats:
    - PDF: Direct rendering via pdf2image (requires poppler)
    - PPTX: Convert to PDF via LibreOffice, then render
    - DOCX: Convert to PDF via LibreOffice, then render

    Example:
        ```python
        renderer = DocumentRenderer()

        # Render a single PDF page
        png_bytes = renderer.render_pdf_page(Path("doc.pdf"), page_num=1)

        # Get page count
        count = renderer.get_page_count(Path("doc.pdf"))

        # Render any document type
        png_bytes = renderer.render_page(Path("slides.pptx"), page_num=3)
        ```
    """

    def __init__(
        self,
        dpi: int = DEFAULT_DPI,
        max_pages: int = MAX_RENDER_PAGES,
        libreoffice_path: Optional[str] = None,
    ):
        """Initialize the document renderer.

        Args:
            dpi: Resolution for rendering (default: 150)
            max_pages: Maximum pages to render (default: 20)
            libreoffice_path: Path to LibreOffice executable.
                             Auto-detected if not provided.
        """
        self.dpi = dpi
        self.max_pages = max_pages
        self.libreoffice_path = libreoffice_path or self._find_libreoffice()

        # Check dependencies
        self._check_pdf2image()

    def _find_libreoffice(self) -> Optional[str]:
        """Find LibreOffice executable on the system."""
        # Common paths for LibreOffice
        candidates = [
            "soffice",  # Usually in PATH on Linux
            "libreoffice",
            "/usr/bin/soffice",
            "/usr/bin/libreoffice",
            "/opt/libreoffice/program/soffice",
            "/Applications/LibreOffice.app/Contents/MacOS/soffice",  # macOS
        ]

        for candidate in candidates:
            if shutil.which(candidate):
                logger.debug(f"Found LibreOffice at: {candidate}")
                return candidate

        logger.warning(
            "LibreOffice not found. PPTX/DOCX rendering will not be available. "
            "Install with: sudo dnf install libreoffice (Fedora) or "
            "sudo apt install libreoffice (Ubuntu)"
        )
        return None

    def _check_pdf2image(self) -> bool:
        """Check if pdf2image and poppler are available."""
        try:
            import pdf2image  # noqa: F401
            # Quick test to see if poppler is installed
            # This will fail fast if poppler is missing
            return True
        except ImportError:
            logger.warning(
                "pdf2image not installed. PDF rendering will not be available. "
                "Install with: pip install pdf2image"
            )
            return False
        except Exception as e:
            logger.warning(f"pdf2image check failed: {e}")
            return False

    def render_pdf_page(
        self,
        file_path: Path,
        page_num: int,
        dpi: Optional[int] = None,
    ) -> bytes:
        """Render a single PDF page as PNG.

        Args:
            file_path: Path to the PDF file
            page_num: Page number to render (1-indexed)
            dpi: Resolution (default: instance dpi)

        Returns:
            PNG image as bytes

        Raises:
            ImportError: If pdf2image is not available
            ValueError: If page_num is invalid
            RuntimeError: If rendering fails
        """
        try:
            from pdf2image import convert_from_path
        except ImportError:
            raise ImportError(
                "pdf2image not installed. Install with: pip install pdf2image"
            )

        dpi = dpi or self.dpi

        if page_num < 1:
            raise ValueError("page_num must be >= 1")

        if page_num > self.max_pages:
            raise ValueError(
                f"page_num ({page_num}) exceeds max_pages limit ({self.max_pages})"
            )

        try:
            images = convert_from_path(
                file_path,
                first_page=page_num,
                last_page=page_num,
                dpi=dpi,
                fmt="png",
            )

            if not images:
                raise ValueError(f"Could not render page {page_num} from {file_path}")

            # Convert PIL image to bytes
            buffer = io.BytesIO()
            images[0].save(buffer, format="PNG")
            return buffer.getvalue()

        except Exception as e:
            logger.error(f"Error rendering PDF page {page_num} from {file_path}: {e}")
            raise RuntimeError(f"Failed to render PDF page {page_num}: {e}")

    def render_pptx_slide(
        self,
        file_path: Path,
        slide_num: int,
        dpi: Optional[int] = None,
    ) -> bytes:
        """Render a single PowerPoint slide as PNG.

        Uses LibreOffice to convert to PDF, then renders the page.

        Args:
            file_path: Path to the PPTX file
            slide_num: Slide number to render (1-indexed)
            dpi: Resolution (default: instance dpi)

        Returns:
            PNG image as bytes

        Raises:
            RuntimeError: If LibreOffice is not available or conversion fails
            ValueError: If slide_num is invalid
        """
        if not self.libreoffice_path:
            raise RuntimeError(
                "LibreOffice not available. PPTX rendering requires LibreOffice. "
                "Install with: sudo dnf install libreoffice"
            )

        if slide_num < 1:
            raise ValueError("slide_num must be >= 1")

        if slide_num > self.max_pages:
            raise ValueError(
                f"slide_num ({slide_num}) exceeds max_pages limit ({self.max_pages})"
            )

        # Convert PPTX to PDF in temp directory
        pdf_path = self._convert_to_pdf(file_path)

        try:
            # Render the specific page from the PDF
            return self.render_pdf_page(pdf_path, slide_num, dpi)
        finally:
            # Clean up temp PDF
            if pdf_path.exists():
                pdf_path.unlink()
            # Clean up temp directory if empty
            if pdf_path.parent != file_path.parent:
                try:
                    pdf_path.parent.rmdir()
                except OSError:
                    pass

    def render_docx_page(
        self,
        file_path: Path,
        page_num: int,
        dpi: Optional[int] = None,
    ) -> bytes:
        """Render a single Word document page as PNG.

        Uses LibreOffice to convert to PDF, then renders the page.

        Args:
            file_path: Path to the DOCX file
            page_num: Page number to render (1-indexed)
            dpi: Resolution (default: instance dpi)

        Returns:
            PNG image as bytes

        Raises:
            RuntimeError: If LibreOffice is not available or conversion fails
            ValueError: If page_num is invalid
        """
        if not self.libreoffice_path:
            raise RuntimeError(
                "LibreOffice not available. DOCX rendering requires LibreOffice. "
                "Install with: sudo dnf install libreoffice"
            )

        if page_num < 1:
            raise ValueError("page_num must be >= 1")

        if page_num > self.max_pages:
            raise ValueError(
                f"page_num ({page_num}) exceeds max_pages limit ({self.max_pages})"
            )

        # Convert DOCX to PDF in temp directory
        pdf_path = self._convert_to_pdf(file_path)

        try:
            # Render the specific page from the PDF
            return self.render_pdf_page(pdf_path, page_num, dpi)
        finally:
            # Clean up temp PDF
            if pdf_path.exists():
                pdf_path.unlink()
            # Clean up temp directory if empty
            if pdf_path.parent != file_path.parent:
                try:
                    pdf_path.parent.rmdir()
                except OSError:
                    pass

    def render_page(
        self,
        file_path: Path,
        page_num: int,
        dpi: Optional[int] = None,
    ) -> bytes:
        """Render a page from any supported document type.

        Automatically detects file type and uses appropriate renderer.

        Args:
            file_path: Path to the document
            page_num: Page/slide number to render (1-indexed)
            dpi: Resolution (default: instance dpi)

        Returns:
            PNG image as bytes

        Raises:
            ValueError: If file type is not supported
        """
        suffix = file_path.suffix.lower()

        if suffix == ".pdf":
            return self.render_pdf_page(file_path, page_num, dpi)
        elif suffix == ".pptx":
            return self.render_pptx_slide(file_path, page_num, dpi)
        elif suffix == ".docx":
            return self.render_docx_page(file_path, page_num, dpi)
        else:
            raise ValueError(
                f"Unsupported document type: {suffix}. "
                f"Supported: .pdf, .pptx, .docx"
            )

    def get_page_count(self, file_path: Path) -> int:
        """Get the total page/slide count for a document.

        Args:
            file_path: Path to the document

        Returns:
            Total number of pages/slides

        Raises:
            ValueError: If file type is not supported
        """
        suffix = file_path.suffix.lower()

        if suffix == ".pdf":
            return self._get_pdf_page_count(file_path)
        elif suffix == ".pptx":
            return self._get_pptx_slide_count(file_path)
        elif suffix == ".docx":
            return self._get_docx_page_count(file_path)
        else:
            raise ValueError(
                f"Unsupported document type: {suffix}. "
                f"Supported: .pdf, .pptx, .docx"
            )

    def _get_pdf_page_count(self, file_path: Path) -> int:
        """Get page count for a PDF file."""
        try:
            import pdfplumber

            with pdfplumber.open(file_path) as pdf:
                return len(pdf.pages)
        except ImportError:
            raise ImportError(
                "pdfplumber not installed. Install with: pip install pdfplumber"
            )

    def _get_pptx_slide_count(self, file_path: Path) -> int:
        """Get slide count for a PPTX file."""
        try:
            from pptx import Presentation

            prs = Presentation(file_path)
            return len(prs.slides)
        except ImportError:
            raise ImportError(
                "python-pptx not installed. Install with: pip install python-pptx"
            )

    def _get_docx_page_count(self, file_path: Path) -> int:
        """Get page count for a DOCX file.

        Note: DOCX files don't have a fixed page count - it depends on
        rendering. We convert to PDF to get an accurate count.
        """
        if not self.libreoffice_path:
            # Fallback: estimate based on content
            logger.warning(
                "LibreOffice not available - estimating DOCX page count"
            )
            return self._estimate_docx_pages(file_path)

        # Convert to PDF and count pages
        pdf_path = self._convert_to_pdf(file_path)
        try:
            return self._get_pdf_page_count(pdf_path)
        finally:
            if pdf_path.exists():
                pdf_path.unlink()
            if pdf_path.parent != file_path.parent:
                try:
                    pdf_path.parent.rmdir()
                except OSError:
                    pass

    def _estimate_docx_pages(self, file_path: Path) -> int:
        """Estimate DOCX page count without LibreOffice."""
        try:
            from docx import Document

            doc = Document(file_path)

            # Rough estimate: ~500 words per page
            word_count = sum(
                len(para.text.split())
                for para in doc.paragraphs
            )
            estimated_pages = max(1, word_count // 500)

            logger.debug(
                f"Estimated {file_path.name}: {word_count} words -> "
                f"~{estimated_pages} pages"
            )
            return estimated_pages

        except ImportError:
            raise ImportError(
                "python-docx not installed. Install with: pip install python-docx"
            )

    def _convert_to_pdf(self, file_path: Path) -> Path:
        """Convert a document to PDF using LibreOffice.

        Args:
            file_path: Path to the document (PPTX, DOCX, etc.)

        Returns:
            Path to the converted PDF (in a temp directory)

        Raises:
            RuntimeError: If conversion fails
        """
        if not self.libreoffice_path:
            raise RuntimeError("LibreOffice not available for conversion")

        # Create temp directory for output
        temp_dir = Path(tempfile.mkdtemp(prefix="docrender_"))

        try:
            # Run LibreOffice headless conversion
            result = subprocess.run(
                [
                    self.libreoffice_path,
                    "--headless",
                    "--convert-to", "pdf",
                    "--outdir", str(temp_dir),
                    str(file_path),
                ],
                capture_output=True,
                text=True,
                timeout=120,  # 2 minute timeout
            )

            if result.returncode != 0:
                logger.error(f"LibreOffice conversion failed: {result.stderr}")
                raise RuntimeError(
                    f"LibreOffice conversion failed: {result.stderr}"
                )

            # Find the output PDF
            pdf_name = file_path.stem + ".pdf"
            pdf_path = temp_dir / pdf_name

            if not pdf_path.exists():
                # LibreOffice might have created it with different name
                pdf_files = list(temp_dir.glob("*.pdf"))
                if pdf_files:
                    pdf_path = pdf_files[0]
                else:
                    raise RuntimeError(
                        f"PDF not found after conversion. "
                        f"Temp dir contents: {list(temp_dir.iterdir())}"
                    )

            logger.debug(f"Converted {file_path.name} to PDF: {pdf_path}")
            return pdf_path

        except subprocess.TimeoutExpired:
            raise RuntimeError(
                f"LibreOffice conversion timed out for {file_path}"
            )
        except Exception:
            # Clean up temp dir on error
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise


# Module-level singleton (lazy-loaded)
_document_renderer: Optional[DocumentRenderer] = None


def get_document_renderer() -> DocumentRenderer:
    """Get or create the DocumentRenderer singleton instance.

    Returns:
        Shared DocumentRenderer instance
    """
    global _document_renderer
    if _document_renderer is None:
        _document_renderer = DocumentRenderer()
    return _document_renderer
