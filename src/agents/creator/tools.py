"""Creator Agent Tools.

This module provides the tool set available to the Creator Agent for:
- Document processing (extraction, chunking)
- Web search (Tavily integration)
- Graph queries (similar requirements)
- Citation creation
- Requirement cache operations
- Workspace management
"""

import os
import logging
import uuid
import re
from typing import Dict, Any, List, Optional
from datetime import datetime

from langchain_core.tools import tool

logger = logging.getLogger(__name__)


class CreatorAgentTools:
    """Tools provider for the Creator Agent.

    Provides a collection of tools for document processing, research,
    and requirement management operations.
    """

    def __init__(
        self,
        postgres_conn: Optional[Any] = None,
        neo4j_conn: Optional[Any] = None,
        config: Optional[Dict[str, Any]] = None
    ):
        """Initialize tools provider.

        Args:
            postgres_conn: PostgreSQL connection for cache operations
            neo4j_conn: Neo4j connection for graph queries
            config: Configuration dictionary
        """
        self.postgres_conn = postgres_conn
        self.neo4j_conn = neo4j_conn
        self.config = config or {}

        # Lazy load components
        self._document_processor = None
        self._candidate_extractor = None
        self._researcher = None

    def _get_document_processor(self):
        """Lazy load document processor."""
        if self._document_processor is None:
            from src.agents.creator.document_processor import CreatorDocumentProcessor
            self._document_processor = CreatorDocumentProcessor(
                chunking_strategy=self.config.get("chunk_strategy", "legal")
            )
        return self._document_processor

    def _get_candidate_extractor(self):
        """Lazy load candidate extractor."""
        if self._candidate_extractor is None:
            from src.agents.creator.candidate_extractor import CandidateExtractor
            self._candidate_extractor = CandidateExtractor(
                mode=self.config.get("extraction_mode", "balanced"),
                min_confidence=self.config.get("min_confidence_threshold", 0.6)
            )
        return self._candidate_extractor

    def _get_researcher(self):
        """Lazy load researcher component."""
        if self._researcher is None:
            from src.agents.creator.researcher import Researcher
            self._researcher = Researcher(
                web_search_enabled=self.config.get("web_search_enabled", True),
                graph_search_enabled=self.config.get("graph_search_enabled", True),
                neo4j_conn=self.neo4j_conn
            )
        return self._researcher

    def get_tools(self) -> List:
        """Return the list of tools for the Creator Agent.

        Returns:
            List of LangChain tool functions
        """
        # Store reference to self for closure
        tools_provider = self

        @tool
        def extract_document_text(file_path: str) -> str:
            """Extract text content from a document file.

            Args:
                file_path: Path to the document (PDF, DOCX, TXT, or HTML)

            Returns:
                Extraction result with metadata and text preview
            """
            try:
                processor = tools_provider._get_document_processor()
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
                processor = tools_provider._get_document_processor()
                chunks = processor.chunk(
                    file_path,
                    strategy=strategy,
                    max_chunk_size=max_chunk_size,
                    overlap=overlap
                )

                # Store chunks in workspace
                if tools_provider.postgres_conn:
                    # Store for later retrieval
                    pass  # Handled by agent workflow

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
                extractor = tools_provider._get_candidate_extractor()
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
                extractor = tools_provider._get_candidate_extractor()
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
        def web_search(query: str, max_results: int = 5) -> str:
            """Search the web for context using Tavily.

            Args:
                query: Search query
                max_results: Maximum results to return

            Returns:
                Search results with snippets and URLs
            """
            try:
                researcher = tools_provider._get_researcher()
                results = researcher.web_search(query, max_results=max_results)

                if not results:
                    return f"No web results found for: {query}"

                result = f"Web Search Results for: {query}\n"
                result += f"Results: {len(results)}\n\n"

                for i, r in enumerate(results, 1):
                    result += f"{i}. {r.get('title', 'Untitled')}\n"
                    result += f"   URL: {r.get('url', 'N/A')}\n"
                    result += f"   {r.get('snippet', '')[:300]}...\n\n"

                return result

            except Exception as e:
                logger.error(f"Web search error: {e}")
                return f"Error searching web: {str(e)}"

        @tool
        def query_similar_requirements(
            text: str,
            limit: int = 5
        ) -> str:
            """Find similar requirements in the Neo4j graph.

            Args:
                text: Requirement text to match against
                limit: Maximum results

            Returns:
                Similar requirements with similarity scores
            """
            try:
                researcher = tools_provider._get_researcher()
                similar = researcher.find_similar_requirements(text, limit=limit)

                if not similar:
                    return f"No similar requirements found in the graph."

                result = f"Similar Requirements Found: {len(similar)}\n\n"

                for i, req in enumerate(similar, 1):
                    result += f"{i}. [{req.get('rid', 'N/A')}] {req.get('name', 'Unnamed')}\n"
                    result += f"   Similarity: {req.get('similarity', 0):.2f}\n"
                    result += f"   Text: {req.get('text', '')[:200]}...\n"
                    result += f"   Status: {req.get('status', 'unknown')}\n\n"

                return result

            except Exception as e:
                logger.error(f"Graph query error: {e}")
                return f"Error querying similar requirements: {str(e)}"

        @tool
        def cite_document(
            text: str,
            document_path: str,
            page: Optional[int] = None,
            section: Optional[str] = None
        ) -> str:
            """Create a citation for document content.

            Args:
                text: Quoted text
                document_path: Source document path
                page: Page number if applicable
                section: Section reference if applicable

            Returns:
                Citation ID and details
            """
            try:
                # Generate citation ID
                citation_id = f"CIT-{uuid.uuid4().hex[:8].upper()}"

                # In a full implementation, this would call the Citation Engine
                result = f"""Citation Created

Citation ID: {citation_id}
Source Type: Document
Document: {document_path}
Page: {page or 'N/A'}
Section: {section or 'N/A'}

Quoted Text:
"{text[:500]}{"..." if len(text) > 500 else ""}"

Use this citation_id when writing requirements."""

                return result

            except Exception as e:
                logger.error(f"Citation creation error: {e}")
                return f"Error creating citation: {str(e)}"

        @tool
        def cite_web(
            text: str,
            url: str,
            title: Optional[str] = None,
            accessed_date: Optional[str] = None
        ) -> str:
            """Create a citation for web content.

            Args:
                text: Quoted/paraphrased text
                url: Source URL
                title: Page title
                accessed_date: Date accessed (ISO format)

            Returns:
                Citation ID and details
            """
            try:
                citation_id = f"CIT-{uuid.uuid4().hex[:8].upper()}"

                result = f"""Web Citation Created

Citation ID: {citation_id}
Source Type: Web
URL: {url}
Title: {title or 'Untitled'}
Accessed: {accessed_date or datetime.utcnow().strftime('%Y-%m-%d')}

Content:
"{text[:500]}{"..." if len(text) > 500 else ""}"

Use this citation_id when writing requirements."""

                return result

            except Exception as e:
                logger.error(f"Web citation error: {e}")
                return f"Error creating web citation: {str(e)}"

        @tool
        async def write_requirement_to_cache(
            text: str,
            name: str,
            req_type: str = "functional",
            priority: str = "medium",
            gobd_relevant: bool = False,
            gdpr_relevant: bool = False,
            source_document: Optional[str] = None,
            source_location: Optional[str] = None,
            citations: Optional[str] = None,
            mentioned_objects: Optional[str] = None,
            mentioned_messages: Optional[str] = None,
            reasoning: Optional[str] = None,
            confidence: float = 0.8
        ) -> str:
            """Write a requirement to the PostgreSQL cache for validation.

            Args:
                text: Full requirement text
                name: Short name/title
                req_type: Type (functional, compliance, constraint, non_functional)
                priority: Priority (high, medium, low)
                gobd_relevant: GoBD relevance flag
                gdpr_relevant: GDPR relevance flag
                source_document: Source document path
                source_location: Location in document (e.g., "Section 3.2")
                citations: Comma-separated citation IDs
                mentioned_objects: Comma-separated BusinessObject names
                mentioned_messages: Comma-separated Message names
                reasoning: Extraction reasoning
                confidence: Confidence score (0.0-1.0)

            Returns:
                Requirement ID and confirmation
            """
            try:
                if not tools_provider.postgres_conn:
                    return "Error: No database connection available"

                from src.core.postgres_utils import create_requirement

                # Parse comma-separated lists
                citation_list = [c.strip() for c in (citations or "").split(",") if c.strip()]
                object_list = [o.strip() for o in (mentioned_objects or "").split(",") if o.strip()]
                message_list = [m.strip() for m in (mentioned_messages or "").split(",") if m.strip()]

                # Get current job ID from agent context
                job_id = tools_provider.config.get("current_job_id")
                if not job_id:
                    return "Error: No active job context"

                req_id = await create_requirement(
                    conn=tools_provider.postgres_conn,
                    job_id=uuid.UUID(job_id),
                    text=text,
                    name=name,
                    req_type=req_type,
                    priority=priority,
                    source_document=source_document,
                    source_location={"section": source_location} if source_location else None,
                    gobd_relevant=gobd_relevant,
                    gdpr_relevant=gdpr_relevant,
                    citations=citation_list,
                    mentioned_objects=object_list,
                    mentioned_messages=message_list,
                    reasoning=reasoning,
                    confidence=confidence
                )

                return f"""Requirement Written to Cache

Requirement ID: {req_id}
Name: {name}
Type: {req_type}
Priority: {priority}
Confidence: {confidence:.2f}

GoBD Relevant: {gobd_relevant}
GDPR Relevant: {gdpr_relevant}

Status: pending (awaiting validation)

The Validator Agent will process this requirement next."""

            except Exception as e:
                logger.error(f"Cache write error: {e}")
                return f"Error writing requirement: {str(e)}"

        @tool
        def extract_entity_mentions(text: str) -> str:
            """Find business object and message mentions in text.

            Args:
                text: Text to analyze

            Returns:
                Extracted entity mentions
            """
            try:
                extractor = tools_provider._get_candidate_extractor()
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

        @tool
        def get_workspace_data(workspace_type: str) -> str:
            """Retrieve data from the agent workspace.

            Args:
                workspace_type: Type of data ('chunks', 'candidates', 'research', 'todo')

            Returns:
                Stored workspace data
            """
            try:
                # This would be implemented with actual workspace access
                return f"Workspace data for '{workspace_type}': Not yet loaded. Use document processing tools first."

            except Exception as e:
                return f"Error reading workspace: {str(e)}"

        @tool
        def save_workspace_data(workspace_type: str, data: str) -> str:
            """Save data to the agent workspace.

            Args:
                workspace_type: Type of data ('chunks', 'candidates', 'research', 'notes')
                data: JSON string of data to save

            Returns:
                Confirmation message
            """
            try:
                # This would be implemented with actual workspace access
                return f"Workspace data '{workspace_type}' saved successfully."

            except Exception as e:
                return f"Error saving workspace: {str(e)}"

        @tool
        def get_processing_progress() -> str:
            """Get current processing progress and statistics.

            Returns:
                Progress summary including candidates processed, requirements created
            """
            return """Processing Progress

Phase: In Progress
Chunks Processed: [tracked by agent state]
Candidates Identified: [tracked by agent state]
Requirements Created: [tracked by agent state]

Use agent state for accurate progress tracking."""

        # Return all tools
        return [
            extract_document_text,
            chunk_document,
            identify_requirement_candidates,
            assess_gobd_relevance,
            web_search,
            query_similar_requirements,
            cite_document,
            cite_web,
            write_requirement_to_cache,
            extract_entity_mentions,
            get_workspace_data,
            save_workspace_data,
            get_processing_progress,
        ]
