"""
Document Processing Data Models

Core data structures for the multi-agent document ingestion pipeline.
Defines types for document chunks, requirement candidates, validation results,
and pipeline state management.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional, List, Dict, Any, Tuple


# =============================================================================
# Enums
# =============================================================================

class DocumentType(Enum):
    """Supported document types for processing."""
    PDF = "pdf"
    DOCX = "docx"
    TXT = "txt"
    HTML = "html"


class DocumentCategory(Enum):
    """Categories of documents based on content type."""
    LEGAL = "legal"
    TECHNICAL = "technical"
    POLICY = "policy"
    GENERAL = "general"


class RequirementType(Enum):
    """Classification of requirement types."""
    FUNCTIONAL = "functional"
    NON_FUNCTIONAL = "non_functional"
    CONSTRAINT = "constraint"
    COMPLIANCE = "compliance"


class ValidationStatus(Enum):
    """Status of requirement validation."""
    ACCEPTED = "accepted"
    NEEDS_REVIEW = "needs_review"
    REJECTED = "rejected"


class PipelineStage(Enum):
    """Stages of the document ingestion pipeline."""
    DOCUMENT_PROCESSING = "document_processing"
    EXTRACTION = "extraction"
    VALIDATION = "validation"
    INTEGRATION = "integration"


class PipelineStatus(Enum):
    """Overall pipeline execution status."""
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"


class ProcessingStatus(Enum):
    """Status for individual processing stages."""
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


# =============================================================================
# Document Processing Models
# =============================================================================

@dataclass
class DocumentMetadata:
    """Metadata extracted from a document."""
    title: str
    source: str  # Filename or URL
    document_type: DocumentCategory
    detected_language: str
    page_count: int
    extraction_timestamp: datetime
    jurisdiction: Optional[str] = None  # For legal docs (DE, EU, etc.)
    version: Optional[str] = None
    author: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "title": self.title,
            "source": self.source,
            "document_type": self.document_type.value,
            "detected_language": self.detected_language,
            "page_count": self.page_count,
            "extraction_timestamp": self.extraction_timestamp.isoformat(),
            "jurisdiction": self.jurisdiction,
            "version": self.version,
            "author": self.author,
        }


@dataclass
class DocumentChunk:
    """A chunk of text from a processed document."""
    chunk_id: str  # Unique identifier
    text: str  # Chunk content
    start_position: int  # Character offset in original document
    end_position: int
    section_hierarchy: List[str]  # ["Chapter 3", "Section 3.2", "Paragraph a"]
    chunk_index: int  # Sequential order in document
    overlap_with_previous: int = 0  # Characters shared with previous chunk
    estimated_tokens: int = 0
    page_number: Optional[int] = None  # Source page if available

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "chunk_id": self.chunk_id,
            "text": self.text,
            "start_position": self.start_position,
            "end_position": self.end_position,
            "section_hierarchy": self.section_hierarchy,
            "chunk_index": self.chunk_index,
            "overlap_with_previous": self.overlap_with_previous,
            "estimated_tokens": self.estimated_tokens,
            "page_number": self.page_number,
        }


# =============================================================================
# Requirement Extraction Models
# =============================================================================

@dataclass
class RequirementCandidate:
    """A potential requirement extracted from document text."""
    candidate_id: str  # Generated ID
    text: str  # Extracted requirement text
    source_chunk_id: str  # Provenance tracking
    source_position: Tuple[int, int]  # Start, end in chunk

    # Classification
    requirement_type: RequirementType
    confidence_score: float  # 0.0-1.0 extraction confidence

    # GoBD-specific
    gobd_relevant: bool = False
    gobd_indicators: List[str] = field(default_factory=list)  # Keywords that triggered GoBD flag

    # Extracted entities
    mentioned_objects: List[str] = field(default_factory=list)  # Potential BusinessObject references
    mentioned_messages: List[str] = field(default_factory=list)  # Potential Message references
    mentioned_requirements: List[str] = field(default_factory=list)  # References to other requirements

    # Context
    section_context: str = ""  # Section where found
    surrounding_context: str = ""  # Sentences before/after

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "candidate_id": self.candidate_id,
            "text": self.text,
            "source_chunk_id": self.source_chunk_id,
            "source_position": list(self.source_position),
            "requirement_type": self.requirement_type.value,
            "confidence_score": self.confidence_score,
            "gobd_relevant": self.gobd_relevant,
            "gobd_indicators": self.gobd_indicators,
            "mentioned_objects": self.mentioned_objects,
            "mentioned_messages": self.mentioned_messages,
            "mentioned_requirements": self.mentioned_requirements,
            "section_context": self.section_context,
            "surrounding_context": self.surrounding_context,
        }


@dataclass
class ExtractionStats:
    """Statistics from requirement extraction phase."""
    total_chunks: int = 0
    processed_chunks: int = 0
    candidates_found: int = 0
    high_confidence: int = 0  # >= 0.8
    medium_confidence: int = 0  # 0.5-0.8
    low_confidence: int = 0  # < 0.5
    gobd_relevant: int = 0

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "total_chunks": self.total_chunks,
            "processed_chunks": self.processed_chunks,
            "candidates_found": self.candidates_found,
            "high_confidence": self.high_confidence,
            "medium_confidence": self.medium_confidence,
            "low_confidence": self.low_confidence,
            "gobd_relevant": self.gobd_relevant,
        }


# =============================================================================
# Validation Models
# =============================================================================

@dataclass
class EntityMatch:
    """A matched entity from the graph."""
    entity_id: str  # boid or mid
    name: str
    match_type: str  # exact, fuzzy, semantic
    similarity_score: float = 1.0


@dataclass
class SimilarRequirement:
    """A similar requirement found in the graph."""
    rid: str
    name: str
    text: str
    similarity_score: float


@dataclass
class ValidatedRequirement:
    """A requirement candidate after validation against the graph."""
    candidate: RequirementCandidate

    # Validation results
    validation_status: ValidationStatus
    validation_score: float  # 0.0-1.0 overall confidence

    # Duplicate detection
    similar_requirements: List[SimilarRequirement] = field(default_factory=list)
    is_duplicate: bool = False
    duplicate_of: Optional[str] = None  # RID if exact duplicate

    # Entity resolution
    resolved_objects: List[EntityMatch] = field(default_factory=list)
    resolved_messages: List[EntityMatch] = field(default_factory=list)
    unresolved_entities: List[str] = field(default_factory=list)

    # Suggested graph operations
    suggested_rid: str = ""  # Generated RID for new requirement
    suggested_relationships: List[Dict[str, Any]] = field(default_factory=list)

    # Compliance pre-check
    metamodel_valid: bool = True
    compliance_warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "candidate": self.candidate.to_dict(),
            "validation_status": self.validation_status.value,
            "validation_score": self.validation_score,
            "similar_requirements": [
                {"rid": s.rid, "name": s.name, "similarity_score": s.similarity_score}
                for s in self.similar_requirements
            ],
            "is_duplicate": self.is_duplicate,
            "duplicate_of": self.duplicate_of,
            "resolved_objects": [
                {"entity_id": e.entity_id, "name": e.name, "match_type": e.match_type}
                for e in self.resolved_objects
            ],
            "resolved_messages": [
                {"entity_id": e.entity_id, "name": e.name, "match_type": e.match_type}
                for e in self.resolved_messages
            ],
            "unresolved_entities": self.unresolved_entities,
            "suggested_rid": self.suggested_rid,
            "suggested_relationships": self.suggested_relationships,
            "metamodel_valid": self.metamodel_valid,
            "compliance_warnings": self.compliance_warnings,
        }


@dataclass
class RejectedCandidate:
    """A candidate that was rejected during validation."""
    candidate: RequirementCandidate
    rejection_reason: str  # duplicate, low_confidence, invalid_structure
    rejection_details: str

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "candidate": self.candidate.to_dict(),
            "rejection_reason": self.rejection_reason,
            "rejection_details": self.rejection_details,
        }


# =============================================================================
# Integration Models
# =============================================================================

@dataclass
class IntegrationResult:
    """Result of integrating a requirement into the graph."""
    requirement: ValidatedRequirement
    success: bool
    created_node: bool = False
    created_relationships: List[str] = field(default_factory=list)
    error_message: Optional[str] = None
    cypher_executed: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "requirement_id": self.requirement.suggested_rid,
            "success": self.success,
            "created_node": self.created_node,
            "created_relationships": self.created_relationships,
            "error_message": self.error_message,
        }


@dataclass
class HumanDecision:
    """A decision made by a human during review."""
    candidate_id: str
    decision: str  # accept, reject, modify
    modified_text: Optional[str] = None
    notes: str = ""
    decided_at: datetime = field(default_factory=datetime.utcnow)


# =============================================================================
# Pipeline Configuration
# =============================================================================

@dataclass
class ProcessingOptions:
    """User-configurable options for the pipeline."""
    extraction_mode: str = "balanced"  # strict, balanced, permissive
    min_confidence: float = 0.6
    auto_integrate: bool = False  # If False, pause for review
    include_low_confidence: bool = False  # Include needs_review in output
    parallel_extraction: bool = True  # Process chunks in parallel
    max_candidates: int = 1000  # Safety limit
    duplicate_similarity_threshold: float = 0.95
    require_entity_existence: bool = False  # Strict mode for entity resolution

    # Chunking options
    chunking_strategy: str = "legal"  # legal, technical, general
    max_chunk_size: int = 1000
    chunk_overlap: int = 200

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "extraction_mode": self.extraction_mode,
            "min_confidence": self.min_confidence,
            "auto_integrate": self.auto_integrate,
            "include_low_confidence": self.include_low_confidence,
            "parallel_extraction": self.parallel_extraction,
            "max_candidates": self.max_candidates,
            "duplicate_similarity_threshold": self.duplicate_similarity_threshold,
            "require_entity_existence": self.require_entity_existence,
            "chunking_strategy": self.chunking_strategy,
            "max_chunk_size": self.max_chunk_size,
            "chunk_overlap": self.chunk_overlap,
        }


# =============================================================================
# Pipeline Report
# =============================================================================

@dataclass
class PipelineReport:
    """Final report from the document ingestion pipeline."""
    document_path: str
    started_at: datetime
    completed_at: Optional[datetime] = None

    # Stage summaries
    total_chunks: int = 0
    total_candidates: int = 0
    accepted_count: int = 0
    rejected_count: int = 0
    needs_review_count: int = 0
    integrated_count: int = 0

    # Detailed results
    extraction_stats: Optional[ExtractionStats] = None
    stage_timings: Dict[str, float] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "document_path": self.document_path,
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "total_chunks": self.total_chunks,
            "total_candidates": self.total_candidates,
            "accepted_count": self.accepted_count,
            "rejected_count": self.rejected_count,
            "needs_review_count": self.needs_review_count,
            "integrated_count": self.integrated_count,
            "extraction_stats": self.extraction_stats.to_dict() if self.extraction_stats else None,
            "stage_timings": self.stage_timings,
            "errors": self.errors,
        }

    def format_summary(self) -> str:
        """Format a human-readable summary."""
        duration = ""
        if self.completed_at and self.started_at:
            seconds = (self.completed_at - self.started_at).total_seconds()
            duration = f" ({seconds:.1f}s)"

        lines = [
            "Document Ingestion Pipeline Report",
            "=" * 40,
            f"Document: {self.document_path}",
            f"Status: {'Completed' if self.completed_at else 'In Progress'}{duration}",
            "",
            "Processing Summary:",
            f"  Chunks processed: {self.total_chunks}",
            f"  Candidates extracted: {self.total_candidates}",
            f"  Accepted: {self.accepted_count}",
            f"  Rejected: {self.rejected_count}",
            f"  Needs Review: {self.needs_review_count}",
            f"  Integrated to Graph: {self.integrated_count}",
        ]

        if self.errors:
            lines.extend([
                "",
                "Errors:",
            ])
            for error in self.errors[:5]:
                lines.append(f"  - {error}")
            if len(self.errors) > 5:
                lines.append(f"  ... and {len(self.errors) - 5} more")

        return "\n".join(lines)
