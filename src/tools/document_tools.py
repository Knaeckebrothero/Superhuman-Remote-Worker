"""Document processing tools for the Universal Agent.

Provides document extraction, chunking, and basic candidate identification
using langchain document loaders and text splitters.
"""

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from langchain_core.tools import tool

from .context import ToolContext

logger = logging.getLogger(__name__)


# Tool metadata for registry
# Phase availability: domain tools are tactical-only
DOCUMENT_TOOLS_METADATA = {
    "extract_document_text": {
        "module": "document_tools",
        "function": "extract_document_text",
        "description": "Extract text content from PDF, DOCX, TXT, or HTML documents",
        "category": "domain",
        "defer_to_workspace": True,
        "short_description": "Extract text from PDF/DOCX/TXT/HTML documents.",
        "phases": ["tactical"],
    },
    "chunk_document": {
        "module": "document_tools",
        "function": "chunk_document",
        "description": "Split document into processable chunks with different strategies",
        "category": "domain",
        "defer_to_workspace": True,
        "short_description": "Split document into chunks (legal/technical/general strategies).",
        "phases": ["tactical"],
    },
    "identify_requirement_candidates": {
        "module": "document_tools",
        "function": "identify_requirement_candidates",
        "description": "Identify requirement-like statements in text",
        "category": "domain",
        "defer_to_workspace": True,
        "short_description": "Find requirement-like statements in text.",
        "phases": ["tactical"],
    },
}


def _load_document(file_path: str) -> Dict[str, Any]:
    """Load document using appropriate langchain loader.

    Args:
        file_path: Path to the document file

    Returns:
        Dict with 'text', 'page_count', 'document_type'
    """
    path = Path(file_path)
    suffix = path.suffix.lower()

    try:
        if suffix == ".pdf":
            from langchain_community.document_loaders import PyPDFLoader
            loader = PyPDFLoader(str(path))
            docs = loader.load()
            text = "\n\n".join([doc.page_content for doc in docs])
            return {
                "text": text,
                "page_count": len(docs),
                "document_type": "PDF",
                "char_count": len(text),
            }

        elif suffix == ".docx":
            from langchain_community.document_loaders import Docx2txtLoader
            loader = Docx2txtLoader(str(path))
            docs = loader.load()
            text = "\n\n".join([doc.page_content for doc in docs])
            return {
                "text": text,
                "page_count": 1,
                "document_type": "DOCX",
                "char_count": len(text),
            }

        elif suffix == ".txt" or suffix == ".md":
            from langchain_community.document_loaders import TextLoader
            loader = TextLoader(str(path))
            docs = loader.load()
            text = "\n\n".join([doc.page_content for doc in docs])
            return {
                "text": text,
                "page_count": 1,
                "document_type": "TXT",
                "char_count": len(text),
            }

        elif suffix in [".html", ".htm"]:
            from langchain_community.document_loaders import UnstructuredHTMLLoader
            loader = UnstructuredHTMLLoader(str(path))
            docs = loader.load()
            text = "\n\n".join([doc.page_content for doc in docs])
            return {
                "text": text,
                "page_count": 1,
                "document_type": "HTML",
                "char_count": len(text),
            }

        else:
            # Try as plain text
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
            return {
                "text": text,
                "page_count": 1,
                "document_type": suffix.upper(),
                "char_count": len(text),
            }

    except Exception as e:
        logger.error(f"Error loading document {file_path}: {e}")
        raise


def _chunk_text(text: str, strategy: str, max_chunk_size: int, overlap: int) -> List[Dict[str, Any]]:
    """Split text into chunks using langchain text splitters.

    Args:
        text: Text to split
        strategy: 'legal', 'technical', or 'general'
        max_chunk_size: Maximum characters per chunk
        overlap: Characters to overlap between chunks

    Returns:
        List of chunk dictionaries with 'text' and metadata
    """
    from langchain_text_splitters import RecursiveCharacterTextSplitter

    # Configure separators based on strategy
    if strategy == "legal":
        separators = [
            "\n\n\n",  # Multiple paragraph breaks
            "\n\n",    # Paragraph breaks
            "\n",      # Line breaks
            "§",       # Section symbols
            "Art.",    # Article markers
            ". ",      # Sentences
            " ",
        ]
    elif strategy == "technical":
        separators = [
            "\n\n\n",
            "\n\n",
            "\n",
            "# ",      # Markdown headers
            "## ",
            "### ",
            ". ",
            " ",
        ]
    else:  # general
        separators = ["\n\n", "\n", ". ", " "]

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=max_chunk_size,
        chunk_overlap=overlap,
        separators=separators,
    )

    chunks = splitter.split_text(text)

    return [
        {
            "text": chunk,
            "chunk_index": i,
            "section_hierarchy": [],  # Could be enhanced with heading detection
        }
        for i, chunk in enumerate(chunks)
    ]


def _identify_candidates(text: str, mode: str) -> List[Dict[str, Any]]:
    """Identify requirement-like statements using pattern matching.

    This is a simplified implementation. For production, consider
    using an LLM-based approach for better accuracy.

    Args:
        text: Text to analyze
        mode: 'strict', 'balanced', or 'permissive'

    Returns:
        List of candidate dictionaries
    """
    candidates = []

    # Modal verb patterns indicating requirements
    modal_patterns = [
        (r'\b(must|shall|will|should)\b[^.]*\.', 0.8, "functional"),
        (r'\b(is required to|has to|needs to)\b[^.]*\.', 0.7, "functional"),
        (r'\b(ensure|verify|validate|confirm)\b[^.]*\.', 0.6, "compliance"),
    ]

    # GoBD/GDPR indicator patterns
    gobd_patterns = [
        r'\b(GoBD|Aufbewahrung|Archivierung|revisionssicher)\b',
        r'\b(Buchführung|Belege?|steuerlich relevant)\b',
    ]

    gdpr_patterns = [
        r'\b(DSGVO|GDPR|personenbezogen|Datenschutz)\b',
        r'\b(Einwilligung|Löschung|Auskunft)\b',
    ]

    # Adjust confidence thresholds based on mode
    min_confidence = {
        "strict": 0.8,
        "balanced": 0.6,
        "permissive": 0.4,
    }.get(mode, 0.6)

    sentences = re.split(r'(?<=[.!?])\s+', text)

    for sentence in sentences:
        sentence = sentence.strip()
        if len(sentence) < 20:
            continue

        for pattern, base_confidence, req_type in modal_patterns:
            if re.search(pattern, sentence, re.IGNORECASE):
                # Check for GoBD/GDPR relevance
                gobd_relevant = any(
                    re.search(p, sentence, re.IGNORECASE)
                    for p in gobd_patterns
                )
                gdpr_relevant = any(
                    re.search(p, sentence, re.IGNORECASE)
                    for p in gdpr_patterns
                )

                # Boost confidence for compliance-related sentences
                confidence = base_confidence
                if gobd_relevant or gdpr_relevant:
                    confidence = min(confidence + 0.1, 1.0)

                if confidence >= min_confidence:
                    candidates.append({
                        "text": sentence,
                        "type": req_type,
                        "confidence": confidence,
                        "gobd_relevant": gobd_relevant,
                        "gdpr_relevant": gdpr_relevant,
                        "gobd_indicators": ["GoBD"] if gobd_relevant else [],
                    })
                break

    # Sort by confidence
    candidates.sort(key=lambda x: x["confidence"], reverse=True)

    return candidates


def create_document_tools(context: ToolContext) -> List:
    """Create document processing tools with injected context.

    Args:
        context: ToolContext with dependencies

    Returns:
        List of LangChain tool functions
    """
    # Get workspace manager if available for path resolution
    workspace = context.workspace_manager if context.has_workspace() else None

    def resolve_path(path: str) -> str:
        """Resolve a path relative to workspace if available."""
        if workspace is not None:
            resolved = workspace.get_path(path)
            return str(resolved)
        return path

    @tool
    def extract_document_text(file_path: str) -> str:
        """Extract text content from a document file.

        Args:
            file_path: Path to the document (PDF, DOCX, TXT, or HTML).
                       Can be relative to workspace (e.g., "documents/file.pdf")
                       or absolute.

        Returns:
            Extraction result with metadata and text preview
        """
        try:
            resolved_path = resolve_path(file_path)
            result = _load_document(resolved_path)

            # Persist full text to workspace
            full_text = result.get('text', '')
            output_filename = f"extracted/{Path(file_path).stem}_full_text.txt"
            if workspace is not None:
                workspace.write_file(output_filename, full_text)
                persist_msg = f"\nFull text written to: {output_filename}\nUse read_file(\"{output_filename}\") to access the content."
            else:
                persist_msg = "\nNote: No workspace available, full text not persisted."

            # Auto-register document as citation source
            source_msg = ""
            try:
                source_id = context.get_or_register_doc_source(
                    resolved_path,
                    name=Path(file_path).name
                )
                source_msg = f"\nCitation Source ID: {source_id}\nThis document is now registered as a citation source. Use cite_document() to create citations."
            except ImportError:
                source_msg = "\nNote: CitationEngine not available, document not registered as source."
            except Exception as e:
                logger.warning(f"Could not register document as citation source: {e}")
                source_msg = f"\nNote: Could not register as citation source: {e}"

            return f"""Document Extraction Complete

File: {file_path}
Page Count: {result.get('page_count', 'unknown')}
Document Type: {result.get('document_type', 'unknown')}
Total Characters: {result.get('char_count', 0)}
{persist_msg}
{source_msg}

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
            file_path: Path to the document (relative to workspace or absolute)
            strategy: Chunking strategy ('legal', 'technical', 'general')
            max_chunk_size: Maximum characters per chunk
            overlap: Characters to overlap between chunks

        Returns:
            Summary of chunking results
        """
        try:
            resolved_path = resolve_path(file_path)
            doc = _load_document(resolved_path)
            chunks = _chunk_text(doc["text"], strategy, max_chunk_size, overlap)

            # Persist ALL chunks to workspace
            chunk_files = []
            if workspace is not None:
                for i, chunk in enumerate(chunks):
                    chunk_path = f"chunks/chunk_{i+1:03d}.txt"
                    chunk_content = chunk.get('text', '')
                    section = chunk.get('section_hierarchy', [])
                    section_str = ' > '.join(section) if section else 'N/A'
                    full_chunk = f"# Chunk {i+1}\nSection: {section_str}\n\n{chunk_content}"
                    workspace.write_file(chunk_path, full_chunk)
                    chunk_files.append(chunk_path)

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
                result += f"  Avg Size: {sum(sizes) // len(sizes)} chars\n"

            if chunk_files:
                result += f"\nChunks written to: chunks/chunk_001.txt through chunks/chunk_{len(chunks):03d}.txt"
                result += f"\nUse read_file(\"chunks/chunk_001.txt\") to start processing."
            else:
                result += "\nNote: No workspace available, chunks not persisted."

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
            candidates = _identify_candidates(text, mode)

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

    return [
        extract_document_text,
        chunk_document,
        identify_requirement_candidates,
    ]
