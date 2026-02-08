"""
Citation & Provenance Engine
============================
A structured citation system for AI agents that forces articulation of
claim-to-source relationships and enables verification.

Usage:
    from citation_engine import CitationEngine

    # Basic mode (SQLite)
    engine = CitationEngine(mode="basic", db_path="./citations.db")

    # Multi-agent mode (PostgreSQL)
    engine = CitationEngine(mode="multi-agent")  # Uses CITATION_DB_URL env var

    with engine:
        # Register sources
        pdf_source = engine.add_doc_source("./document.pdf", name="My Document")

        # Create citations
        result = engine.cite_doc(
            claim="The regulation requires X",
            source_id=pdf_source.id,
            quote_context="Full paragraph containing the quote...",
            verbatim_quote="The exact text being cited",
            locator={"page": 24, "section": "§ 8.1"}
        )

        if result.verification_status == "verified":
            print(f"Citation [{result.citation_id}] verified!")

Environment Variables:
    CITATION_DB_URL: PostgreSQL connection string (multi-agent mode)
    CITATION_LLM_URL: Custom LLM endpoint (e.g., llama.cpp server)
    CITATION_REASONING_REQUIRED: none | low | medium | high (default: low)

Author: Claude Code Assistant
Version: 0.1.0
"""

from .engine import CitationEngine
from .models import (
    Citation,
    CitationContext,
    CitationError,
    CitationResult,
    Confidence,
    ExtractionMethod,
    Locator,
    Metadata,
    Source,
    SourceType,
    VerificationResult,
    VerificationStatus,
)
from .schema import (
    SCHEMA_VERSION,
    get_current_schema_version,
    get_schema,
)

__version__ = "0.1.0"
__all__ = [
    # Core engine
    "CitationEngine",
    # Models
    "Source",
    "Citation",
    "CitationResult",
    "VerificationResult",
    "CitationContext",
    "CitationError",
    # Enums
    "SourceType",
    "VerificationStatus",
    "ExtractionMethod",
    "Confidence",
    # Type aliases
    "Locator",
    "Metadata",
    # Schema utilities
    "get_schema",
    "get_current_schema_version",
    "SCHEMA_VERSION",
]

# Optional imports - only available if langchain/pydantic are installed
try:
    from .tool import CitationTool, create_citation_tools  # noqa: F401

    __all__.extend(["create_citation_tools", "CitationTool"])
except ImportError:
    # Tools not available without pydantic/langchain
    pass
