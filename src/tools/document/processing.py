"""Document processing tools for the Universal Agent.

Provides document extraction and chunking using langchain document loaders
and text splitters.
"""

import logging
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List

from langchain_core.tools import tool

from ..context import ToolContext

logger = logging.getLogger(__name__)


# Tool metadata for registry
# Phase availability: domain tools are tactical-only
DOCUMENT_TOOLS_METADATA: Dict[str, Dict[str, Any]] = {
    "chunk_document": {
        "module": "document.processing",
        "function": "chunk_document",
        "description": "Split document into processable chunks with different strategies",
        "category": "document",
        "defer_to_workspace": True,
        "short_description": "Split document into chunks (legal/technical/general strategies).",
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


def _chunk_text(
    text: str, strategy: str, max_chunk_size: int, overlap: int
) -> List[Dict[str, Any]]:
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
            "\n\n",  # Paragraph breaks
            "\n",  # Line breaks
            "§",  # Section symbols
            "Art.",  # Article markers
            ". ",  # Sentences
            " ",
        ]
    elif strategy == "technical":
        separators = [
            "\n\n\n",
            "\n\n",
            "\n",
            "# ",  # Markdown headers
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


@contextmanager
def _local_copy(workspace, file_path: str):
    """Ensure a workspace file is available locally for third-party loaders.

    For local workspaces, yields the resolved local path directly.
    For remote workspaces, fetches the file into a temp file with the
    correct extension and yields that, cleaning up afterwards.

    Args:
        workspace: WorkspaceManager instance (or None for no-workspace mode).
        file_path: Workspace-relative or absolute path.

    Yields:
        Local filesystem path (str) suitable for LangChain loaders.
    """
    if workspace is None:
        yield file_path
        return

    backend = workspace.backend
    if backend.host is None:
        # Local backend — resolve and yield directly
        yield str(workspace.get_path(file_path))
        return

    # Remote backend — fetch to temp file
    suffix = Path(file_path).suffix
    content = backend.read_file(file_path, binary=True)
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(content)
        tmp_path = tmp.name
    try:
        yield tmp_path
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def create_processing_tools(context: ToolContext) -> List[Any]:
    """Create document processing tools with injected context.

    Args:
        context: ToolContext with dependencies

    Returns:
        List of LangChain tool functions
    """
    # Get workspace manager if available for path resolution
    workspace = context.workspace_manager if context.has_workspace() else None

    @tool
    def chunk_document(
        file_path: str,
        strategy: str = "legal",
        max_chunk_size: int = 1000,
        overlap: int = 200,
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
            with _local_copy(workspace, file_path) as local_path:
                doc = _load_document(local_path)
            chunks = _chunk_text(doc["text"], strategy, max_chunk_size, overlap)

            # Persist ALL chunks to workspace
            chunk_files = []
            if workspace is not None:
                for i, chunk in enumerate(chunks):
                    chunk_path = f"chunks/chunk_{i + 1:03d}.txt"
                    chunk_content = chunk.get("text", "")
                    section = chunk.get("section_hierarchy", [])
                    section_str = " > ".join(section) if section else "N/A"
                    full_chunk = (
                        f"# Chunk {i + 1}\nSection: {section_str}\n\n{chunk_content}"
                    )
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
                sizes = [len(c.get("text", "")) for c in chunks]
                result += f"  Min Size: {min(sizes)} chars\n"
                result += f"  Max Size: {max(sizes)} chars\n"
                result += f"  Avg Size: {sum(sizes) // len(sizes)} chars\n"

            if chunk_files:
                result += f"\nChunks written to: chunks/chunk_001.txt through chunks/chunk_{len(chunks):03d}.txt"
                result += '\nUse read_file("chunks/chunk_001.txt") to start processing.'
            else:
                result += "\nNote: No workspace available, chunks not persisted."

            return result

        except Exception as e:
            logger.error(f"Document chunking error: {e}")
            return f"Error chunking document: {str(e)}"

    return [
        chunk_document,
    ]
