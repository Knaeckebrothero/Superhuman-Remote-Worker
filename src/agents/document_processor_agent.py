"""
Document Processor Agent

LangGraph-based agent for intelligent document processing.
Handles text extraction, structure detection, and chunking with
LLM-assisted analysis for complex document structures.
"""

import os
from typing import Dict, Any, List, TypedDict, Annotated, Sequence, Optional
from datetime import datetime
from langchain_core.messages import BaseMessage, HumanMessage, AIMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langchain_core.tools import tool
from langgraph.graph import StateGraph, END
from langgraph.prebuilt import ToolNode
from langgraph.graph.message import add_messages

from src.core.document_processor import (
    DocumentProcessor,
    DocumentExtractor,
    DocumentChunker,
    detect_language,
    detect_document_type,
)
from src.core.document_models import (
    DocumentChunk,
    DocumentMetadata,
    DocumentCategory,
    ProcessingStatus,
)
from src.core.config import load_config


# =============================================================================
# Agent State Definition
# =============================================================================

class DocumentProcessorState(TypedDict):
    """State for the document processing agent."""
    # Messages for LLM conversation
    messages: Annotated[Sequence[BaseMessage], add_messages]

    # Input
    document_path: str
    document_type: str  # pdf, docx, txt, html

    # Processing state
    raw_text: str
    detected_structure: Dict[str, Any]  # sections, headings hierarchy
    chunks: List[Dict[str, Any]]  # Serialized DocumentChunks

    # Metadata
    metadata: Dict[str, Any]  # Serialized DocumentMetadata

    # Control
    processing_status: str  # pending, in_progress, completed, failed
    error_message: Optional[str]
    iteration: int
    max_iterations: int


# =============================================================================
# Document Processing Tools
# =============================================================================

class DocumentProcessingTools:
    """Tools for document processing operations."""

    def __init__(self, chunking_strategy: str = "legal"):
        """
        Initialize document processing tools.

        Args:
            chunking_strategy: Chunking preset to use
        """
        self.extractor = DocumentExtractor()
        self.chunker = DocumentChunker.from_preset(chunking_strategy)
        self.processor = DocumentProcessor(chunking_strategy)

    def get_tools(self):
        """Return list of tools for the agent."""

        @tool
        def extract_document_text(file_path: str) -> str:
            """
            Extract text content from a document file.

            Args:
                file_path: Path to the document (PDF, DOCX, TXT, or HTML)

            Returns:
                Extracted text content with metadata information

            Supports:
            - PDF files (requires pdfplumber)
            - DOCX files (requires python-docx)
            - TXT files (plain text)
            - HTML files (requires beautifulsoup4)
            """
            try:
                text, info = self.extractor.extract(file_path)

                result = f"Successfully extracted text from {file_path}\n"
                result += f"Page count: {info.get('page_count', 'unknown')}\n"
                result += f"Title: {info.get('title', 'Not detected')}\n"
                result += f"Author: {info.get('author', 'Not detected')}\n"
                result += f"Text length: {len(text)} characters\n\n"
                result += f"Preview (first 500 chars):\n{text[:500]}..."

                return result

            except Exception as e:
                return f"Error extracting document: {str(e)}"

        @tool
        def detect_document_structure(text: str) -> str:
            """
            Analyze document text to detect structural elements.

            Args:
                text: Document text to analyze

            Returns:
                Description of detected structure including:
                - Section headers and hierarchy
                - Paragraph patterns
                - Lists and numbered items
                - Language detection
                - Document type classification
            """
            try:
                # Detect language
                language = detect_language(text)

                # Detect document type
                doc_type = detect_document_type(text)

                # Find structural patterns
                import re

                # Section patterns
                section_patterns = [
                    (r"^(?:Article|Section|§|Artikel|Abschnitt)\s+\d+", "Section headers"),
                    (r"^\(\d+\)", "Numbered paragraphs"),
                    (r"^[a-z]\)", "Lettered clauses"),
                    (r"^(?:\d+\.)+\s+", "Numbered sections"),
                    (r"^\[PAGE \d+\]", "Page markers"),
                ]

                detected = []
                for pattern, name in section_patterns:
                    matches = re.findall(pattern, text, re.MULTILINE)
                    if matches:
                        detected.append(f"- {name}: {len(matches)} found (e.g., '{matches[0][:50]}')")

                result = f"Document Structure Analysis:\n\n"
                result += f"Language: {language}\n"
                result += f"Document Type: {doc_type.value}\n"
                result += f"Total length: {len(text)} characters\n\n"
                result += "Detected patterns:\n"
                result += "\n".join(detected) if detected else "No specific patterns detected"

                return result

            except Exception as e:
                return f"Error analyzing structure: {str(e)}"

        @tool
        def chunk_document(
            text: str,
            strategy: str = "legal",
            max_chunk_size: int = 1000,
            overlap: int = 200
        ) -> str:
            """
            Split document text into chunks for processing.

            Args:
                text: Full document text to chunk
                strategy: Chunking strategy - "legal", "technical", or "general"
                max_chunk_size: Maximum characters per chunk
                overlap: Characters to overlap between chunks

            Returns:
                Summary of chunking results with chunk statistics

            The legal strategy:
            - Respects section boundaries (Articles, Paragraphs, etc.)
            - Preserves hierarchy information
            - Uses intelligent boundary detection

            The technical strategy:
            - Handles markdown and technical formats
            - Detects requirement IDs

            The general strategy:
            - Simple character-based chunking
            - Good for unstructured text
            """
            try:
                chunker = DocumentChunker.from_preset(strategy)
                chunker.max_chunk_size = max_chunk_size
                chunker.overlap_size = overlap

                chunks = chunker.chunk(text, document_id="doc")

                result = f"Chunking Results:\n\n"
                result += f"Strategy: {strategy}\n"
                result += f"Total chunks: {len(chunks)}\n"
                result += f"Max chunk size: {max_chunk_size}\n"
                result += f"Overlap: {overlap}\n\n"

                if chunks:
                    sizes = [len(c.text) for c in chunks]
                    result += f"Chunk size statistics:\n"
                    result += f"  - Min: {min(sizes)} chars\n"
                    result += f"  - Max: {max(sizes)} chars\n"
                    result += f"  - Avg: {sum(sizes) // len(sizes)} chars\n\n"

                    # Show first chunk preview
                    result += f"First chunk preview:\n{chunks[0].text[:300]}..."

                return result

            except Exception as e:
                return f"Error chunking document: {str(e)}"

        @tool
        def extract_document_metadata(file_path: str, text: str) -> str:
            """
            Extract metadata from document and content.

            Args:
                file_path: Path to the document file
                text: Extracted text content

            Returns:
                Comprehensive metadata including:
                - File information
                - Detected language
                - Document category
                - Jurisdiction (for legal docs)
                - Page count estimate
            """
            try:
                from pathlib import Path

                path = Path(file_path)
                language = detect_language(text)
                doc_type = detect_document_type(text)

                # Jurisdiction detection
                jurisdiction = None
                text_lower = text.lower()
                if any(ind in text_lower for ind in ["bundesrepublik", "bgb", "hgb", "gobd"]):
                    jurisdiction = "DE"
                elif any(ind in text_lower for ind in ["european union", "eu regulation", "gdpr"]):
                    jurisdiction = "EU"

                result = f"Document Metadata:\n\n"
                result += f"File: {path.name}\n"
                result += f"Format: {path.suffix.upper()}\n"
                result += f"Size: {path.stat().st_size / 1024:.1f} KB\n"
                result += f"Language: {language}\n"
                result += f"Category: {doc_type.value}\n"
                result += f"Jurisdiction: {jurisdiction or 'Not detected'}\n"
                result += f"Estimated pages: {max(1, len(text) // 3000)}\n"

                return result

            except Exception as e:
                return f"Error extracting metadata: {str(e)}"

        @tool
        def get_processing_capabilities() -> str:
            """
            Check available document processing capabilities.

            Returns:
                List of supported formats and their availability
            """
            caps = self.extractor.capabilities

            result = "Document Processing Capabilities:\n\n"
            for format_type, available in caps.items():
                status = "Available" if available else "Not installed"
                result += f"- {format_type.upper()}: {status}\n"

            result += "\nTo enable missing formats, install:\n"
            result += "- PDF: pip install pdfplumber\n"
            result += "- DOCX: pip install python-docx\n"
            result += "- HTML: pip install beautifulsoup4\n"

            return result

        return [
            extract_document_text,
            detect_document_structure,
            chunk_document,
            extract_document_metadata,
            get_processing_capabilities,
        ]


# =============================================================================
# Document Processor Agent
# =============================================================================

class DocumentProcessorAgent:
    """
    LangGraph-based agent for document processing.

    Orchestrates text extraction, structure detection, and chunking
    with optional LLM-assisted analysis for complex documents.
    """

    def __init__(
        self,
        llm_model: str = "gpt-4o",
        temperature: float = 0.0,
        chunking_strategy: str = "legal",
        reasoning_level: str = "medium",
    ):
        """
        Initialize the document processor agent.

        Args:
            llm_model: LLM model to use
            temperature: LLM temperature setting
            chunking_strategy: Chunking preset to use
            reasoning_level: Reasoning effort level
        """
        self.llm_model = llm_model
        self.temperature = temperature
        self.chunking_strategy = chunking_strategy
        self.reasoning_level = reasoning_level

        # Initialize LLM
        llm_kwargs = {
            "model": self.llm_model,
            "temperature": self.temperature,
            "api_key": os.getenv("OPENAI_API_KEY"),
        }
        base_url = os.getenv("LLM_BASE_URL")
        if base_url:
            llm_kwargs["base_url"] = base_url

        self.llm = ChatOpenAI(**llm_kwargs)

        # Initialize tools
        self.doc_tools = DocumentProcessingTools(chunking_strategy)
        self.tools = self.doc_tools.get_tools()

        # Bind tools to LLM
        self.llm_with_tools = self.llm.bind_tools(self.tools)

        # Build the graph
        self.graph = self._build_graph()

    def _get_system_prompt(self) -> str:
        """Get the system prompt for the document processor."""
        return f"""You are a document processing specialist for a requirement traceability system.

Your task is to process documents (PDF, DOCX, TXT, HTML) and prepare them for
requirement extraction. You have access to tools for:

1. **Text Extraction**: Extract text content from various document formats
2. **Structure Detection**: Identify sections, headings, and document organization
3. **Chunking**: Split documents into manageable pieces for LLM processing
4. **Metadata Extraction**: Gather document properties and classifications

Processing Guidelines:
- Always check processing capabilities first if unsure about format support
- For legal/compliance documents, use the "legal" chunking strategy
- For technical specifications, use the "technical" strategy
- Detect the document language and type for proper handling
- Preserve document structure when chunking for context retention

Document Categories:
- LEGAL: Contracts, regulations, compliance documents (GoBD, DSGVO)
- TECHNICAL: API specs, system requirements, architecture docs
- POLICY: Guidelines, procedures, internal policies
- GENERAL: Other documents

Reasoning Level: {self.reasoning_level}

After processing, provide a summary of:
1. Document type and language detected
2. Structural elements found
3. Chunking results
4. Any issues or recommendations"""

    def _analyze_node(self, state: DocumentProcessorState) -> DocumentProcessorState:
        """Initial analysis node - plans the processing approach."""
        document_path = state["document_path"]

        system_msg = self._get_system_prompt()
        user_msg = f"""Process the following document:

File: {document_path}

Steps:
1. Check processing capabilities for this file type
2. Extract text from the document
3. Detect document structure
4. Chunk the document appropriately
5. Extract and report metadata

Begin processing and report your findings."""

        state["messages"] = [
            SystemMessage(content=system_msg),
            HumanMessage(content=user_msg),
        ]
        state["processing_status"] = ProcessingStatus.IN_PROGRESS.value
        state["iteration"] = 0

        # Get LLM response
        response = self.llm_with_tools.invoke(state["messages"])
        state["messages"].append(response)

        return state

    def _process_node(self, state: DocumentProcessorState) -> DocumentProcessorState:
        """Processing node - executes tool calls and continues processing."""
        iteration = state.get("iteration", 0)
        max_iterations = state.get("max_iterations", 5)

        # Add continuation message
        if iteration < max_iterations:
            context = f"Continue processing (iteration {iteration + 1}/{max_iterations})"
            state["messages"].append(HumanMessage(content=context))

            # Get next action from LLM
            response = self.llm_with_tools.invoke(state["messages"])
            state["messages"].append(response)

        return state

    def _should_continue(self, state: DocumentProcessorState) -> str:
        """Determine if processing should continue."""
        messages = state["messages"]
        last_message = messages[-1] if messages else None

        # Check for tool calls
        if last_message and hasattr(last_message, "tool_calls") and last_message.tool_calls:
            return "tools"

        # Check iteration limit
        if state.get("iteration", 0) >= state.get("max_iterations", 5):
            return "finalize"

        # Check if processing is complete
        if state.get("processing_status") == ProcessingStatus.COMPLETED.value:
            return "finalize"

        # Check for errors
        if state.get("error_message"):
            return "finalize"

        return "continue"

    def _increment_node(self, state: DocumentProcessorState) -> DocumentProcessorState:
        """Increment iteration counter after tool execution."""
        state["iteration"] = state.get("iteration", 0) + 1
        return state

    def _finalize_node(self, state: DocumentProcessorState) -> DocumentProcessorState:
        """Finalize processing and prepare output."""
        # If we haven't done the actual processing yet, do it now
        if not state.get("chunks"):
            try:
                document_path = state["document_path"]
                processor = DocumentProcessor(self.chunking_strategy)
                metadata, chunks = processor.process(document_path)

                state["metadata"] = metadata.to_dict()
                state["chunks"] = [c.to_dict() for c in chunks]
                state["raw_text"] = ""  # Don't store full text to save memory
                state["processing_status"] = ProcessingStatus.COMPLETED.value

            except Exception as e:
                state["error_message"] = str(e)
                state["processing_status"] = ProcessingStatus.FAILED.value

        return state

    def _build_graph(self) -> StateGraph:
        """Build the LangGraph state machine."""
        workflow = StateGraph(DocumentProcessorState)

        # Add nodes
        workflow.add_node("analyze", self._analyze_node)
        workflow.add_node("process", self._process_node)
        workflow.add_node("tools", ToolNode(self.tools))
        workflow.add_node("increment", self._increment_node)
        workflow.add_node("finalize", self._finalize_node)

        # Set entry point
        workflow.set_entry_point("analyze")

        # Add edges
        workflow.add_conditional_edges(
            "analyze",
            self._should_continue,
            {
                "tools": "tools",
                "continue": "process",
                "finalize": "finalize",
            },
        )
        workflow.add_edge("tools", "increment")
        workflow.add_edge("increment", "process")
        workflow.add_conditional_edges(
            "process",
            self._should_continue,
            {
                "tools": "tools",
                "continue": "process",
                "finalize": "finalize",
            },
        )
        workflow.add_edge("finalize", END)

        return workflow.compile()

    def process_document(
        self, document_path: str, max_iterations: int = 5
    ) -> Dict[str, Any]:
        """
        Process a document and return results.

        Args:
            document_path: Path to the document file
            max_iterations: Maximum processing iterations

        Returns:
            Dictionary with processing results
        """
        from pathlib import Path

        path = Path(document_path)
        doc_type = path.suffix.lower().lstrip(".")

        # Initialize state
        initial_state = DocumentProcessorState(
            messages=[],
            document_path=document_path,
            document_type=doc_type,
            raw_text="",
            detected_structure={},
            chunks=[],
            metadata={},
            processing_status=ProcessingStatus.PENDING.value,
            error_message=None,
            iteration=0,
            max_iterations=max_iterations,
        )

        # Run the graph
        final_state = self.graph.invoke(
            initial_state, config={"recursion_limit": 50}
        )

        return {
            "document_path": document_path,
            "document_type": doc_type,
            "metadata": final_state.get("metadata", {}),
            "chunks": final_state.get("chunks", []),
            "processing_status": final_state.get("processing_status", "unknown"),
            "error_message": final_state.get("error_message"),
        }

    def process_document_stream(self, document_path: str, max_iterations: int = 5):
        """
        Process a document and yield state updates for streaming.

        Args:
            document_path: Path to the document file
            max_iterations: Maximum processing iterations

        Yields:
            Dictionary containing current state of the workflow
        """
        from pathlib import Path

        path = Path(document_path)
        doc_type = path.suffix.lower().lstrip(".")

        initial_state = DocumentProcessorState(
            messages=[],
            document_path=document_path,
            document_type=doc_type,
            raw_text="",
            detected_structure={},
            chunks=[],
            metadata={},
            processing_status=ProcessingStatus.PENDING.value,
            error_message=None,
            iteration=0,
            max_iterations=max_iterations,
        )

        return self.graph.stream(
            initial_state, config={"recursion_limit": 50}
        )


# =============================================================================
# Factory Function
# =============================================================================

def create_document_processor_agent(
    chunking_strategy: str = "legal",
) -> DocumentProcessorAgent:
    """
    Create a document processor agent with configuration from config file.

    Args:
        chunking_strategy: Chunking preset to use

    Returns:
        Configured DocumentProcessorAgent instance
    """
    config = load_config("llm_config.json")

    # Use document_ingestion config if available, otherwise fall back to agent config
    doc_config = config.get("document_ingestion", config.get("agent", {}))

    return DocumentProcessorAgent(
        llm_model=doc_config.get("model", "gpt-4o"),
        temperature=doc_config.get("temperature", 0.0),
        chunking_strategy=doc_config.get("chunking_strategy", chunking_strategy),
        reasoning_level=doc_config.get("reasoning_level", "medium"),
    )
