"""Creator Agent package for document processing and requirement extraction.

This package implements the Creator Agent, responsible for:
- Document processing (PDF, DOCX, TXT, HTML)
- Requirement candidate extraction
- Research and context enrichment
- Citation creation
- Writing requirements to the shared PostgreSQL cache

Components:
- CreatorAgent: Main LangGraph-based agent
- CreatorDocumentProcessor: Document extraction and chunking
- CandidateExtractor: Requirement candidate identification
- Researcher: Web search and graph queries
- RequirementCacheWriter: PostgreSQL cache operations
- CreatorAgentTools: Tool definitions for the agent
"""

from src.agents.creator.creator_agent import (
    CreatorAgent,
    CreatorAgentState,
    create_creator_agent,
)
from src.agents.creator.document_processor import (
    CreatorDocumentProcessor,
    create_document_processor,
)
from src.agents.creator.candidate_extractor import (
    CandidateExtractor,
    RequirementCandidate,
    create_candidate_extractor,
)
from src.agents.creator.researcher import (
    Researcher,
    create_researcher,
)
from src.agents.creator.cache_writer import (
    RequirementCacheWriter,
    create_cache_writer,
)
from src.agents.creator.tools import CreatorAgentTools

__all__ = [
    # Main agent
    "CreatorAgent",
    "CreatorAgentState",
    "create_creator_agent",
    # Document processing
    "CreatorDocumentProcessor",
    "create_document_processor",
    # Candidate extraction
    "CandidateExtractor",
    "RequirementCandidate",
    "create_candidate_extractor",
    # Research
    "Researcher",
    "create_researcher",
    # Cache operations
    "RequirementCacheWriter",
    "create_cache_writer",
    # Tools
    "CreatorAgentTools",
]
