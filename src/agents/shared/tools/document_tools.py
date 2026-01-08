"""Document processing tools for the Universal Agent.

Migrated from Creator Agent tools. Provides document extraction,
chunking, and candidate identification capabilities.
"""

import logging
from typing import Any, Dict, List, Optional

from langchain_core.tools import tool

from .context import ToolContext

logger = logging.getLogger(__name__)


# Tool metadata for registry
DOCUMENT_TOOLS_METADATA = {
    "extract_document_text": {
        "module": "document_tools",
        "function": "extract_document_text",
        "description": "Extract text content from PDF, DOCX, TXT, or HTML documents",
        "category": "domain",
    },
    "chunk_document": {
        "module": "document_tools",
        "function": "chunk_document",
        "description": "Split document into processable chunks with different strategies",
        "category": "domain",
    },
    "identify_requirement_candidates": {
        "module": "document_tools",
        "function": "identify_requirement_candidates",
        "description": "Identify requirement-like statements in text",
        "category": "domain",
    },
    "assess_gobd_relevance": {
        "module": "document_tools",
        "function": "assess_gobd_relevance",
        "description": "Assess whether text is relevant to GoBD compliance",
        "category": "domain",
    },
    "extract_entity_mentions": {
        "module": "document_tools",
        "function": "extract_entity_mentions",
        "description": "Find business object and message mentions in text",
        "category": "domain",
    },
}


def create_document_tools(context: ToolContext) -> List:
    """Create document processing tools with injected context.

    Args:
        context: ToolContext with dependencies

    Returns:
        List of LangChain tool functions
    """
    # Lazy load document processor
    _document_processor = None
    _candidate_extractor = None

    def get_document_processor():
        nonlocal _document_processor
        if _document_processor is None:
            try:
                from src.agents.creator.document_processor import CreatorDocumentProcessor
                config = context.config or {}
                _document_processor = CreatorDocumentProcessor(
                    chunking_strategy=config.get("chunk_strategy", "legal")
                )
            except ImportError:
                logger.warning("CreatorDocumentProcessor not available")
                _document_processor = None
        return _document_processor

    def get_candidate_extractor():
        nonlocal _candidate_extractor
        if _candidate_extractor is None:
            try:
                from src.agents.creator.candidate_extractor import CandidateExtractor
                config = context.config or {}
                _candidate_extractor = CandidateExtractor(
                    mode=config.get("extraction_mode", "balanced"),
                    min_confidence=config.get("min_confidence_threshold", 0.6)
                )
            except ImportError:
                logger.warning("CandidateExtractor not available")
                _candidate_extractor = None
        return _candidate_extractor

    @tool
    def extract_document_text(file_path: str) -> str:
        """Extract text content from a document file.

        Args:
            file_path: Path to the document (PDF, DOCX, TXT, or HTML)

        Returns:
            Extraction result with metadata and text preview
        """
        try:
            processor = get_document_processor()
            if processor is None:
                return "Error: Document processor not available"

            result = processor.extract(file_path)

            return f"""Document Extraction Complete

File: {file_path}
Page Count: {result.get('page_count', 'unknown')}
Language: {result.get('language', 'unknown')}
Document Type: {result.get('document_type', 'unknown')}
Total Characters: {result.get('char_count', 0)}

Preview (first 1000 chars):
{result.get('text', '')[:1000]}...

Use 'chunk_document' to split this into processable chunks."""

        except Exception as e:
            logger.error(f"Document extraction error: {e}")
            return f"Error extracting document: {str(e)}"

    @tool
    def chunk_document(
        file_path: str,
        strategy: str = "legal",
        max_chunk_size: int = 1000,
        overlap: int = 200
    ) -> str:
        """Split a document into chunks for processing.

        Args:
            file_path: Path to the document
            strategy: Chunking strategy ('legal', 'technical', 'general')
            max_chunk_size: Maximum characters per chunk
            overlap: Characters to overlap between chunks

        Returns:
            Summary of chunking results
        """
        try:
            processor = get_document_processor()
            if processor is None:
                return "Error: Document processor not available"

            chunks = processor.chunk(
                file_path,
                strategy=strategy,
                max_chunk_size=max_chunk_size,
                overlap=overlap
            )

            result = f"""Document Chunking Complete

File: {file_path}
Strategy: {strategy}
Total Chunks: {len(chunks)}
Max Chunk Size: {max_chunk_size}
Overlap: {overlap}

Chunk Statistics:
"""
            if chunks:
                sizes = [len(c.get('text', '')) for c in chunks]
                result += f"  Min Size: {min(sizes)} chars\n"
                result += f"  Max Size: {max(sizes)} chars\n"
                result += f"  Avg Size: {sum(sizes) // len(sizes)} chars\n\n"

                result += "First 3 chunks preview:\n"
                for i, chunk in enumerate(chunks[:3]):
                    text_preview = chunk.get('text', '')[:200]
                    section = chunk.get('section_hierarchy', [])
                    result += f"\n[Chunk {i+1}] Section: {' > '.join(section) or 'N/A'}\n"
                    result += f"{text_preview}...\n"

            return result

        except Exception as e:
            logger.error(f"Document chunking error: {e}")
            return f"Error chunking document: {str(e)}"

    @tool
    def identify_requirement_candidates(
        text: str,
        mode: str = "balanced"
    ) -> str:
        """Identify requirement-like statements in text.

        Args:
            text: Text to analyze for requirements
            mode: Detection mode ('strict', 'balanced', 'permissive')

        Returns:
            List of identified candidates with confidence scores
        """
        try:
            extractor = get_candidate_extractor()
            if extractor is None:
                return "Error: Candidate extractor not available"

            candidates = extractor.identify(text, mode=mode)

            result = f"Requirement Candidate Identification\n"
            result += f"Mode: {mode}\n"
            result += f"Candidates Found: {len(candidates)}\n\n"

            for i, cand in enumerate(candidates[:10], 1):
                result += f"{i}. [{cand.get('type', 'unknown').upper()}] "
                result += f"Confidence: {cand.get('confidence', 0):.2f}\n"
                result += f"   Text: {cand.get('text', '')[:150]}...\n"
                if cand.get('gobd_relevant'):
                    result += f"   GoBD Relevant: Yes ({', '.join(cand.get('gobd_indicators', []))})\n"
                result += "\n"

            if len(candidates) > 10:
                result += f"... and {len(candidates) - 10} more candidates\n"

            return result

        except Exception as e:
            logger.error(f"Candidate identification error: {e}")
            return f"Error identifying candidates: {str(e)}"

    @tool
    def assess_gobd_relevance(text: str) -> str:
        """Assess whether text is relevant to GoBD compliance.

        GoBD (Grundsaetze zur ordnungsmaessigen Fuehrung und Aufbewahrung)
        defines German requirements for electronic bookkeeping.

        Args:
            text: Text to assess

        Returns:
            GoBD relevance assessment with indicators
        """
        try:
            extractor = get_candidate_extractor()
            if extractor is None:
                return "Error: Candidate extractor not available"

            assessment = extractor.assess_gobd(text)

            result = f"""GoBD Relevance Assessment

Is Relevant: {assessment.get('is_relevant', False)}
Confidence: {assessment.get('confidence', 0):.2f}

Indicators Found:
"""
            for ind in assessment.get('indicators', []):
                result += f"  - {ind}\n"

            result += f"\nGoBD Categories: {', '.join(assessment.get('categories', []))}\n"

            return result

        except Exception as e:
            logger.error(f"GoBD assessment error: {e}")
            return f"Error assessing GoBD relevance: {str(e)}"

    @tool
    def extract_entity_mentions(text: str) -> str:
        """Find business object and message mentions in text.

        Args:
            text: Text to analyze

        Returns:
            Extracted entity mentions
        """
        try:
            extractor = get_candidate_extractor()
            if extractor is None:
                return "Error: Candidate extractor not available"

            entities = extractor.extract_entities(text)

            result = "Entity Mentions Found\n\n"

            result += f"Business Objects ({len(entities.get('objects', []))}):\n"
            for obj in entities.get('objects', []):
                result += f"  - {obj}\n"

            result += f"\nMessages/Interfaces ({len(entities.get('messages', []))}):\n"
            for msg in entities.get('messages', []):
                result += f"  - {msg}\n"

            result += f"\nRequirement References ({len(entities.get('requirements', []))}):\n"
            for req in entities.get('requirements', []):
                result += f"  - {req}\n"

            return result

        except Exception as e:
            logger.error(f"Entity extraction error: {e}")
            return f"Error extracting entities: {str(e)}"

    return [
        extract_document_text,
        chunk_document,
        identify_requirement_candidates,
        assess_gobd_relevance,
        extract_entity_mentions,
    ]
