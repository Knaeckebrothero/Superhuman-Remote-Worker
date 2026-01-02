"""Document Processor for Creator Agent.

Extends the core document processing capabilities with additional
features for the Creator Agent workflow, including section hierarchy
detection, workspace persistence, and enhanced metadata extraction.
"""

import logging
import re
from typing import Dict, Any, List, Optional, Tuple
from pathlib import Path
from datetime import datetime

from src.core.document_processor import (
    DocumentProcessor,
    DocumentExtractor,
    DocumentChunker,
    detect_language,
    detect_document_type,
    CHUNKING_PRESETS,
)
from src.core.document_models import (
    DocumentChunk,
    DocumentMetadata,
    DocumentCategory,
)

logger = logging.getLogger(__name__)


class CreatorDocumentProcessor:
    """Document processor tailored for Creator Agent requirements.

    Provides enhanced document processing with:
    - Legal document structure detection (articles, sections, paragraphs)
    - Hierarchical section tracking
    - Cross-reference detection
    - Workspace integration for chunk persistence
    - Metadata enrichment for traceability
    """

    def __init__(
        self,
        chunking_strategy: str = "legal",
        max_chunk_size: Optional[int] = None,
        chunk_overlap: Optional[int] = None
    ):
        """Initialize the document processor.

        Args:
            chunking_strategy: Chunking preset ('legal', 'technical', 'general')
            max_chunk_size: Override max chunk size
            chunk_overlap: Override chunk overlap
        """
        self.chunking_strategy = chunking_strategy
        self.extractor = DocumentExtractor()

        # Get preset config
        preset = CHUNKING_PRESETS.get(chunking_strategy, CHUNKING_PRESETS["legal"])

        self.chunker = DocumentChunker(
            max_chunk_size=max_chunk_size or preset.get("max_chunk_size", 1000),
            overlap_size=chunk_overlap or preset.get("overlap_size", 200),
            respect_boundaries=preset.get("respect_boundaries", True),
            boundary_patterns=preset.get("boundary_patterns", []),
            preserve_hierarchy=preset.get("preserve_hierarchy", True),
        )

        # Additional legal document patterns
        self.legal_section_patterns = [
            # German legal patterns
            (r"^§\s*(\d+(?:\.\d+)*)\s*(.*)$", "paragraph"),
            (r"^Artikel\s+(\d+)\s*(.*)$", "article"),
            (r"^Abschnitt\s+(\d+)\s*(.*)$", "section"),
            (r"^Absatz\s+(\d+)(?:\s*(?:Satz|S\.)\s*(\d+))?", "subsection"),
            (r"^\((\d+)\)\s*", "numbered_clause"),
            (r"^([a-z])\)\s*", "lettered_clause"),

            # English legal patterns
            (r"^Article\s+(\d+)\s*(.*)$", "article"),
            (r"^Section\s+(\d+(?:\.\d+)*)\s*(.*)$", "section"),
            (r"^Paragraph\s+(\d+)\s*(.*)$", "paragraph"),
            (r"^Clause\s+(\d+(?:\.\d+)*)\s*(.*)$", "clause"),

            # Numbered outline patterns
            (r"^(\d+)\.(?:\d+)*\s+(.*)$", "numbered_section"),
            (r"^(\d+)\.\s+(.*)$", "top_level_section"),
        ]

        # Cross-reference patterns
        self.xref_patterns = [
            r"(?:gemäß|pursuant to|per|see|cf\.?)\s*(?:§|Art\.?|Section)\s*\d+",
            r"(?:Abs\.|Paragraph|Abs)\s*\d+",
            r"(?:as defined in|as specified in)\s+(?:Section|Article)\s*\d+",
        ]

    def extract(self, file_path: str) -> Dict[str, Any]:
        """Extract text and metadata from a document.

        Args:
            file_path: Path to document file

        Returns:
            Dictionary with extracted text and metadata
        """
        path = Path(file_path)

        try:
            text, info = self.extractor.extract(file_path)

            # Detect language and document type
            language = detect_language(text)
            doc_type = detect_document_type(text)

            # Detect structure
            structure = self._detect_legal_structure(text)

            return {
                "text": text,
                "file_path": str(path.absolute()),
                "file_name": path.name,
                "file_size": path.stat().st_size,
                "page_count": info.get("page_count", 1),
                "title": info.get("title", path.stem),
                "author": info.get("author"),
                "language": language,
                "document_type": doc_type.value,
                "char_count": len(text),
                "structure": structure,
                "extraction_time": datetime.utcnow().isoformat(),
            }

        except Exception as e:
            logger.error(f"Extraction error for {file_path}: {e}")
            raise

    def chunk(
        self,
        file_path: str,
        strategy: Optional[str] = None,
        max_chunk_size: Optional[int] = None,
        overlap: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """Chunk a document with enhanced metadata.

        Args:
            file_path: Path to document file
            strategy: Override chunking strategy
            max_chunk_size: Override max chunk size
            overlap: Override overlap size

        Returns:
            List of chunk dictionaries with metadata
        """
        # Extract text first
        extraction = self.extract(file_path)
        text = extraction["text"]

        # Create chunker with optional overrides
        if strategy and strategy != self.chunking_strategy:
            preset = CHUNKING_PRESETS.get(strategy, CHUNKING_PRESETS["legal"])
            chunker = DocumentChunker(
                max_chunk_size=max_chunk_size or preset.get("max_chunk_size", 1000),
                overlap_size=overlap or preset.get("overlap_size", 200),
                respect_boundaries=preset.get("respect_boundaries", True),
                boundary_patterns=preset.get("boundary_patterns", []),
                preserve_hierarchy=preset.get("preserve_hierarchy", True),
            )
        else:
            chunker = self.chunker
            if max_chunk_size:
                chunker.max_chunk_size = max_chunk_size
            if overlap:
                chunker.overlap_size = overlap

        # Generate document ID
        doc_id = Path(file_path).stem.replace(" ", "_")[:20]

        # Chunk the document
        chunks = chunker.chunk(text, document_id=doc_id)

        # Enhance chunks with additional metadata
        enhanced_chunks = []
        for chunk in chunks:
            chunk_dict = chunk.to_dict() if hasattr(chunk, 'to_dict') else dict(chunk)

            # Add cross-references
            chunk_dict["cross_references"] = self._find_cross_references(chunk_dict.get("text", ""))

            # Add legal section context
            chunk_dict["legal_context"] = self._extract_legal_context(chunk_dict.get("text", ""))

            # Add document metadata
            chunk_dict["document_metadata"] = {
                "file_path": extraction["file_path"],
                "title": extraction["title"],
                "language": extraction["language"],
                "document_type": extraction["document_type"],
            }

            enhanced_chunks.append(chunk_dict)

        logger.info(f"Created {len(enhanced_chunks)} chunks from {file_path}")
        return enhanced_chunks

    def _detect_legal_structure(self, text: str) -> Dict[str, Any]:
        """Detect legal document structure.

        Args:
            text: Document text

        Returns:
            Structure information dictionary
        """
        structure = {
            "articles": [],
            "sections": [],
            "paragraphs": [],
            "has_numbered_sections": False,
            "has_lettered_clauses": False,
            "estimated_hierarchy_depth": 0,
        }

        max_depth = 0

        for line in text.split('\n'):
            line = line.strip()
            if not line:
                continue

            for pattern, section_type in self.legal_section_patterns:
                match = re.match(pattern, line, re.IGNORECASE)
                if match:
                    section_info = {
                        "type": section_type,
                        "number": match.group(1),
                        "title": match.group(2) if len(match.groups()) > 1 else "",
                        "text": line,
                    }

                    if section_type in ("article", "section"):
                        structure["articles"].append(section_info)
                    elif section_type in ("paragraph", "numbered_section"):
                        structure["sections"].append(section_info)
                        # Calculate depth
                        parts = section_info["number"].split('.')
                        max_depth = max(max_depth, len(parts))
                    elif section_type in ("numbered_clause", "lettered_clause"):
                        structure["paragraphs"].append(section_info)

                    if section_type == "numbered_section":
                        structure["has_numbered_sections"] = True
                    if section_type == "lettered_clause":
                        structure["has_lettered_clauses"] = True

                    break

        structure["estimated_hierarchy_depth"] = max_depth
        structure["total_sections"] = (
            len(structure["articles"]) +
            len(structure["sections"]) +
            len(structure["paragraphs"])
        )

        return structure

    def _find_cross_references(self, text: str) -> List[Dict[str, str]]:
        """Find cross-references in text.

        Args:
            text: Text to search

        Returns:
            List of cross-reference dictionaries
        """
        references = []

        for pattern in self.xref_patterns:
            for match in re.finditer(pattern, text, re.IGNORECASE):
                references.append({
                    "text": match.group(0),
                    "position": match.start(),
                })

        return references

    def _extract_legal_context(self, text: str) -> Dict[str, Any]:
        """Extract legal context from chunk text.

        Args:
            text: Chunk text

        Returns:
            Legal context information
        """
        context = {
            "mentions_gobd": False,
            "mentions_gdpr": False,
            "mentions_hgb": False,
            "mentions_ao": False,
            "obligation_indicators": [],
            "constraint_indicators": [],
        }

        text_lower = text.lower()

        # Check for compliance framework mentions
        context["mentions_gobd"] = "gobd" in text_lower or "ordnungsmäßig" in text_lower
        context["mentions_gdpr"] = "gdpr" in text_lower or "dsgvo" in text_lower or "datenschutz" in text_lower
        context["mentions_hgb"] = "hgb" in text_lower or "handelsgesetzbuch" in text_lower
        context["mentions_ao"] = " ao " in text_lower or "abgabenordnung" in text_lower

        # Find obligation indicators
        obligation_patterns = [
            r"\b(?:must|shall|required|verpflichtet|muss)\b",
            r"\b(?:is obligated|ist verpflichtet)\b",
        ]
        for pattern in obligation_patterns:
            for match in re.finditer(pattern, text, re.IGNORECASE):
                context["obligation_indicators"].append(match.group(0))

        # Find constraint indicators
        constraint_patterns = [
            r"\b(?:at least|at most|within|maximum|minimum)\b",
            r"\b(?:mindestens|höchstens|spätestens|maximal)\b",
            r"\b\d+\s*(?:days?|hours?|years?|Tage?|Stunden?|Jahre?)\b",
        ]
        for pattern in constraint_patterns:
            for match in re.finditer(pattern, text, re.IGNORECASE):
                context["constraint_indicators"].append(match.group(0))

        return context

    def process(self, file_path: str) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        """Full document processing pipeline.

        Args:
            file_path: Path to document

        Returns:
            Tuple of (metadata dict, list of chunk dicts)
        """
        metadata = self.extract(file_path)
        chunks = self.chunk(file_path)

        return metadata, chunks

    def get_capabilities(self) -> Dict[str, bool]:
        """Get available document processing capabilities.

        Returns:
            Dictionary of format: available
        """
        return self.extractor.capabilities


# =============================================================================
# Factory Functions
# =============================================================================

def create_document_processor(
    chunking_strategy: str = "legal",
    max_chunk_size: Optional[int] = None,
    chunk_overlap: Optional[int] = None
) -> CreatorDocumentProcessor:
    """Create a document processor instance.

    Args:
        chunking_strategy: Chunking preset
        max_chunk_size: Max characters per chunk
        chunk_overlap: Overlap between chunks

    Returns:
        Configured CreatorDocumentProcessor
    """
    return CreatorDocumentProcessor(
        chunking_strategy=chunking_strategy,
        max_chunk_size=max_chunk_size,
        chunk_overlap=chunk_overlap
    )
