"""
Data Models for Citation Engine
===============================
Defines the core data structures used throughout the citation system.

Based on the Citation & Provenance Engine Design Document v0.3.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any


class SourceType(str, Enum):
    """Types of sources that can be registered and cited."""
    DOCUMENT = "document"  # PDFs, markdown, txt, json, csv, images
    WEBSITE = "website"    # Web pages (archived at registration)
    DATABASE = "database"  # SQL, NoSQL, graph DB records
    CUSTOM = "custom"      # AI-generated artifacts (matrices, plots, computed results)


class VerificationStatus(str, Enum):
    """Status of citation verification."""
    PENDING = "pending"
    VERIFIED = "verified"
    FAILED = "failed"
    UNVERIFIED = "unverified"  # Skipped verification (optional)


class ExtractionMethod(str, Enum):
    """How the information was extracted from the source."""
    DIRECT_QUOTE = "direct_quote"   # Exact text quoted
    PARAPHRASE = "paraphrase"       # Rephrased in agent's words
    INFERENCE = "inference"         # Conclusion drawn from source
    AGGREGATION = "aggregation"     # Combined from multiple data points
    NEGATIVE = "negative"           # Source was checked but doesn't support claim


class Confidence(str, Enum):
    """Agent's self-assessment of citation confidence."""
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass
class Source:
    """
    Represents a canonical document, website, database, or custom artifact.

    Sources are registered when passed to the agent and stored for verification.
    The content field contains the full text extracted from the source.

    Attributes:
        id: Auto-incrementing primary key
        type: Source type (document, website, database, custom)
        identifier: Filename, URL, or database identifier
        name: Human-readable name
        version: Version identifier (e.g., "2024-01")
        content: Full text content (for verification)
        metadata: Type-specific additional data (JSON)
        content_hash: SHA-256 hash of content for integrity
        created_at: When the source was registered
    """
    id: int
    type: SourceType
    identifier: str
    name: str
    content: str
    created_at: datetime
    version: str | None = None
    metadata: dict[str, Any] | None = None
    content_hash: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert source to dictionary for JSON serialization."""
        return {
            "id": self.id,
            "type": self.type.value,
            "identifier": self.identifier,
            "name": self.name,
            "version": self.version,
            "content": self.content[:500] + "..." if len(self.content) > 500 else self.content,
            "content_hash": self.content_hash,
            "metadata": self.metadata,
            "created_at": self.created_at.isoformat(),
        }


@dataclass
class Citation:
    """
    The core record linking a claim to its supporting evidence.

    Each citation is a separate record, even if the same quote backs
    multiple claims (since claims have unique context and reasoning).

    Attributes:
        id: Auto-incrementing primary key (1, 2, 3...)
        claim: The assertion being supported
        quote_context: The paragraph/surrounding context containing the evidence
        source_id: Reference to the source table
        locator: Location data (page, section, etc.) as JSON
        created_at: When the citation was registered
        verbatim_quote: Exact quoted text (for direct citations)
        quote_language: ISO language code (e.g., "de", "en")
        relevance_reasoning: Agent's explanation of why evidence supports claim
        confidence: Agent's self-assessment (high, medium, low)
        extraction_method: How information was extracted
        verification_status: Result of verification process
        verification_notes: Explanation from verification LLM if failed
        similarity_score: How closely quote matched source (0-1)
        matched_location: Where the quote was found during verification
        created_by: Agent/session identifier for audit trails
    """
    id: int
    claim: str
    quote_context: str
    source_id: int
    locator: dict[str, Any]
    created_at: datetime
    verbatim_quote: str | None = None
    quote_language: str | None = None
    relevance_reasoning: str | None = None
    confidence: Confidence = Confidence.HIGH
    extraction_method: ExtractionMethod = ExtractionMethod.DIRECT_QUOTE
    verification_status: VerificationStatus = VerificationStatus.PENDING
    verification_notes: str | None = None
    similarity_score: float | None = None
    matched_location: dict[str, Any] | None = None
    created_by: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert citation to dictionary for JSON serialization."""
        return {
            "id": self.id,
            "claim": self.claim,
            "verbatim_quote": self.verbatim_quote,
            "quote_context": self.quote_context[:200] + "..." if len(self.quote_context) > 200 else self.quote_context,
            "quote_language": self.quote_language,
            "relevance_reasoning": self.relevance_reasoning,
            "confidence": self.confidence.value,
            "extraction_method": self.extraction_method.value,
            "source_id": self.source_id,
            "locator": self.locator,
            "verification_status": self.verification_status.value,
            "verification_notes": self.verification_notes,
            "similarity_score": self.similarity_score,
            "matched_location": self.matched_location,
            "created_at": self.created_at.isoformat(),
            "created_by": self.created_by,
        }


@dataclass
class CitationResult:
    """
    Result returned when a citation is created.

    This is what the agent receives after calling cite_*() methods.
    Contains the citation ID to embed in prose and verification results.

    Attributes:
        citation_id: The ID to embed in prose, e.g., [1], [2]
        verification_status: verified | unverified | failed | pending
        similarity_score: How closely quote matched source (0-1)
        matched_location: Where the quote was found (if verified)
        source_registered: Whether this was a new or existing source
        formatted_reference: Pre-formatted citation string (optional)
        summary_note: Short note for context window management
        verification_notes: Explanation if verification failed
    """
    citation_id: int
    verification_status: VerificationStatus
    similarity_score: float | None = None
    matched_location: dict[str, Any] | None = None
    source_registered: bool = False
    formatted_reference: str | None = None
    summary_note: str | None = None
    verification_notes: str | None = None

    def __str__(self) -> str:
        """Return citation reference for embedding in prose."""
        return f"[{self.citation_id}]"

    def to_dict(self) -> dict[str, Any]:
        """Convert result to dictionary for JSON serialization."""
        return {
            "citation_id": self.citation_id,
            "verification_status": self.verification_status.value,
            "similarity_score": self.similarity_score,
            "matched_location": self.matched_location,
            "source_registered": self.source_registered,
            "formatted_reference": self.formatted_reference,
            "summary_note": self.summary_note,
            "verification_notes": self.verification_notes,
        }


@dataclass
class VerificationResult:
    """
    Result of the verification process for a citation.

    Returned by the verification LLM when checking if a quote
    exists in the source and supports the claim.

    Attributes:
        is_verified: Whether the citation passed verification
        similarity_score: How closely the quote matched (0-1)
        matched_text: The actual text found in the source
        matched_location: Where in the source the text was found
        reasoning: Explanation of verification decision
        error: Error message if verification failed
    """
    is_verified: bool
    similarity_score: float = 0.0
    matched_text: str | None = None
    matched_location: dict[str, Any] | None = None
    reasoning: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert result to dictionary for JSON serialization."""
        return {
            "is_verified": self.is_verified,
            "similarity_score": self.similarity_score,
            "matched_text": self.matched_text,
            "matched_location": self.matched_location,
            "reasoning": self.reasoning,
            "error": self.error,
        }


@dataclass
class CitationContext:
    """
    Context information for citation auditing and tracking.

    Every citation is associated with context for audit purposes.

    Attributes:
        session_id: Unique conversation/task ID
        agent_id: Which agent created the citation
        user_id: On whose behalf the citation was made
        project_id: Which project (Fessi, GoBD, SLR, etc.)
    """
    session_id: str
    agent_id: str | None = None
    user_id: str | None = None
    project_id: str | None = None


@dataclass
class CitationError:
    """
    Error information when citation creation fails.

    Provides structured error information to help the agent
    understand what went wrong and how to fix it.

    Attributes:
        error_type: Category of error (SourceNotFound, VerificationFailed, etc.)
        message: Human-readable error description
        suggestion: How to fix the error
        partial_result: Any data captured before failure
    """
    error_type: str
    message: str
    suggestion: str | None = None
    partial_result: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert error to dictionary for JSON serialization."""
        return {
            "error_type": self.error_type,
            "message": self.message,
            "suggestion": self.suggestion,
            "partial_result": self.partial_result,
        }


# Type aliases for convenience
Locator = dict[str, Any]
Metadata = dict[str, Any]
