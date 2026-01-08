"""Pydantic models for Validator Agent API.

These models define the request/response schemas for the Validator Agent's
HTTP endpoints, enabling manual requirement validation and status monitoring.
"""

from datetime import datetime
from typing import Optional, List, Dict, Any

from pydantic import BaseModel, Field


class ValidateRequest(BaseModel):
    """Request to validate a requirement.

    Can either reference an existing requirement by ID, or provide
    requirement data inline for ad-hoc testing.
    """

    # Option 1: Reference existing requirement
    requirement_id: Optional[str] = Field(
        None,
        description="UUID of existing requirement in cache",
    )

    # Option 2: Inline requirement data
    text: Optional[str] = Field(
        None,
        description="Requirement text for ad-hoc validation",
        max_length=10000,
    )
    name: Optional[str] = Field(
        None,
        description="Short requirement name",
        max_length=200,
    )
    type: Optional[str] = Field(
        None,
        description="Type: functional, compliance, constraint, non_functional",
    )
    priority: Optional[str] = Field(
        None,
        description="Priority: high, medium, low",
    )
    mentioned_objects: List[str] = Field(
        default_factory=list,
        description="Business objects mentioned in requirement",
    )
    mentioned_messages: List[str] = Field(
        default_factory=list,
        description="Messages mentioned in requirement",
    )
    gobd_relevant: bool = Field(False, description="GoBD compliance relevance")
    gdpr_relevant: bool = Field(False, description="GDPR compliance relevance")

    # Processing options
    max_iterations: int = Field(
        100,
        ge=1,
        le=200,
        description="Maximum validation iterations",
    )


class ValidateSubmitResponse(BaseModel):
    """Response after submitting validation request."""

    request_id: str = Field(..., description="UUID for tracking this validation request")
    requirement_id: Optional[str] = Field(
        None,
        description="UUID of requirement being validated",
    )
    status: str = Field(..., description="Initial status: submitted, processing")
    message: str = Field(..., description="Status message")


class GraphChange(BaseModel):
    """Detail of a graph mutation."""

    operation: str = Field(
        ...,
        description="Operation type: create_node, create_relationship, update_property",
    )
    node_type: Optional[str] = Field(None, description="Node label if applicable")
    node_id: Optional[str] = Field(None, description="Node element ID if applicable")
    relationship_type: Optional[str] = Field(
        None,
        description="Relationship type if applicable",
    )
    source_id: Optional[str] = Field(None, description="Source node ID for relationship")
    target_id: Optional[str] = Field(None, description="Target node ID for relationship")
    properties: Optional[Dict[str, Any]] = Field(
        None,
        description="Properties set/updated",
    )


class ValidateStatusResponse(BaseModel):
    """Response with current validation status/result."""

    request_id: str = Field(..., description="UUID of the validation request")
    requirement_id: Optional[str] = Field(
        None,
        description="UUID of requirement being validated",
    )
    status: str = Field(
        ...,
        description="Status: processing, completed, rejected, failed",
    )
    current_phase: Optional[str] = Field(
        None,
        description="Current phase: understanding, relevance, fulfillment, planning, integration, documentation",
    )
    phases_completed: List[str] = Field(
        default_factory=list,
        description="Phases successfully completed",
    )
    iteration: int = Field(0, description="Current iteration count")
    max_iterations: int = Field(100, description="Maximum iterations configured")
    progress_percent: float = Field(
        0.0,
        ge=0.0,
        le=100.0,
        description="Estimated progress percentage",
    )

    # Results (populated when complete)
    related_objects: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Related BusinessObjects found in graph",
    )
    related_messages: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Related Messages found in graph",
    )
    fulfillment_analysis: Optional[Dict[str, Any]] = Field(
        None,
        description="Analysis of requirement fulfillment",
    )
    graph_changes: List[GraphChange] = Field(
        default_factory=list,
        description="Graph mutations performed",
    )
    graph_node_id: Optional[str] = Field(
        None,
        description="Neo4j element ID of created Requirement node",
    )
    rejection_reason: Optional[str] = Field(
        None,
        description="Reason for rejection if rejected",
    )

    started_at: Optional[datetime] = Field(None, description="When validation started")
    completed_at: Optional[datetime] = Field(
        None,
        description="When validation completed",
    )
    error: Optional[Dict[str, Any]] = Field(None, description="Error details if failed")


class PendingRequirementResponse(BaseModel):
    """Summary of a pending requirement in the cache."""

    id: str = Field(..., description="Requirement UUID")
    job_id: str = Field(..., description="Parent job UUID")
    name: Optional[str] = Field(None, description="Short requirement name")
    text: str = Field(..., description="Requirement text (may be truncated)")
    type: Optional[str] = Field(None, description="Requirement type")
    priority: Optional[str] = Field(None, description="Priority level")
    confidence: float = Field(0.0, description="Extraction confidence")
    gobd_relevant: bool = Field(False, description="GoBD relevance")
    gdpr_relevant: bool = Field(False, description="GDPR relevance")
    created_at: Optional[datetime] = Field(None, description="When created")
    status: str = Field("pending", description="Current status")


class PendingRequirementsListResponse(BaseModel):
    """Response with list of pending requirements."""

    total: int = Field(0, description="Total pending count")
    requirements: List[PendingRequirementResponse] = Field(
        default_factory=list,
        description="List of pending requirements",
    )


class RequirementDetailResponse(BaseModel):
    """Full details of a requirement."""

    id: str = Field(..., description="Requirement UUID")
    job_id: str = Field(..., description="Parent job UUID")
    candidate_id: Optional[str] = Field(None, description="Original candidate ID")
    name: Optional[str] = Field(None, description="Short requirement name")
    text: str = Field(..., description="Full requirement text")
    type: Optional[str] = Field(None, description="Requirement type")
    priority: Optional[str] = Field(None, description="Priority level")
    source_document: Optional[str] = Field(None, description="Source document path")
    source_location: Optional[Dict[str, Any]] = Field(
        None,
        description="Location within source document",
    )
    gobd_relevant: bool = Field(False, description="GoBD relevance")
    gdpr_relevant: bool = Field(False, description="GDPR relevance")
    citations: List[str] = Field(default_factory=list, description="Supporting citations")
    mentioned_objects: List[str] = Field(
        default_factory=list,
        description="Business objects mentioned",
    )
    mentioned_messages: List[str] = Field(
        default_factory=list,
        description="Messages mentioned",
    )
    reasoning: Optional[str] = Field(None, description="Extraction reasoning")
    research_notes: Optional[str] = Field(None, description="Research notes")
    confidence: float = Field(0.0, description="Extraction confidence")
    status: str = Field("pending", description="Current status")
    validation_result: Optional[Dict[str, Any]] = Field(
        None,
        description="Validation result if validated",
    )
    graph_node_id: Optional[str] = Field(
        None,
        description="Neo4j node ID if integrated",
    )
    rejection_reason: Optional[str] = Field(
        None,
        description="Rejection reason if rejected",
    )
    created_at: Optional[datetime] = Field(None, description="When created")
    updated_at: Optional[datetime] = Field(None, description="When last updated")
    validated_at: Optional[datetime] = Field(None, description="When validated")
    tags: List[str] = Field(default_factory=list, description="Traceability tags")
