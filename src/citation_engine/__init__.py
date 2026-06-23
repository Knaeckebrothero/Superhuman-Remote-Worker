"""
Citation & Provenance Engine
============================
A structured citation system for AI agents that forces articulation of
claim-to-source relationships and enables verification.

Native Superhuman-Remote-Worker subsystem: the engine is async and runs against
SRW's shared vector store (``srw_vector``) through the agent's asyncpg pool. It
has no standalone/SQLite mode and owns no database connection of its own — the
host's ``orchestrator/database/migrations/vector/`` owns the schema, and the
agent injects the vector ``PostgresDB`` pool at construction.

See ``docs/features/citation_engine_integration.md``.

Usage:
    from citation_engine import CitationEngine, CitationContext

    engine = CitationEngine(db=agent.vector_conn, context=ctx)
    source = await engine.add_doc_source("./document.pdf", name="My Document")
    result = await engine.cite_doc(
        claim="The regulation requires X",
        source_id=source.id,
        quote_context="Full paragraph containing the quote...",
        verbatim_quote="The exact text being cited",
        locator={"page": 24, "section": "§ 8.1"},
    )
    if result.verification_status == VerificationStatus.VERIFIED:
        print(f"Citation [{result.citation_id}] verified!")
"""

from .chunking import SemanticChunker
from .engine import CitationEngine
from .models import (
    Annotation,
    AnnotationType,
    Citation,
    CitationContext,
    CitationError,
    CitationResult,
    Confidence,
    ExtractionMethod,
    Locator,
    Metadata,
    SearchResult,
    SearchResults,
    Source,
    SourceType,
    VerificationResult,
    VerificationStatus,
)

__version__ = "0.2.0"
__all__ = [
    # Core engine
    "CitationEngine",
    # Models
    "Source",
    "Annotation",
    "Citation",
    "CitationResult",
    "VerificationResult",
    "CitationContext",
    "CitationError",
    # Enums
    "SourceType",
    "AnnotationType",
    "VerificationStatus",
    "ExtractionMethod",
    "Confidence",
    # Type aliases
    "Locator",
    "Metadata",
    # Search
    "SearchResult",
    "SearchResults",
    # Chunking
    "SemanticChunker",
]
