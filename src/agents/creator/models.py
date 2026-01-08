"""Pydantic models for Creator Agent API.

These models define the request/response schemas for the Creator Agent's
HTTP endpoints, enabling manual job submission and status monitoring.
"""

from datetime import datetime
from typing import Optional, List, Dict, Any
from uuid import UUID

from pydantic import BaseModel, Field


class JobSubmitRequest(BaseModel):
    """Request to submit a new document processing job."""

    prompt: str = Field(
        ...,
        description="Processing prompt describing what to extract",
        min_length=10,
        max_length=5000,
    )
    document_content: Optional[str] = Field(
        None,
        description="Base64-encoded document content",
    )
    document_filename: Optional[str] = Field(
        None,
        description="Original filename for the document",
    )
    context: Optional[Dict[str, Any]] = Field(
        default_factory=dict,
        description="Additional context for processing",
    )
    max_iterations: int = Field(
        50,
        ge=1,
        le=200,
        description="Maximum processing iterations",
    )


class JobSubmitResponse(BaseModel):
    """Response after job submission."""

    job_id: str = Field(..., description="UUID of the created job")
    status: str = Field(..., description="Initial job status")
    message: str = Field(..., description="Status message")


class JobStatusResponse(BaseModel):
    """Response with current job status."""

    job_id: str = Field(..., description="UUID of the job")
    status: str = Field(
        ...,
        description="Job status: created, processing, completed, failed, cancelled",
    )
    creator_status: str = Field(
        ...,
        description="Creator agent status: pending, processing, completed, failed",
    )
    current_phase: Optional[str] = Field(
        None,
        description="Current processing phase",
    )
    iteration: int = Field(0, description="Current iteration count")
    max_iterations: int = Field(50, description="Maximum iterations configured")
    requirements_created: int = Field(
        0,
        description="Number of requirements created so far",
    )
    progress_percent: float = Field(
        0.0,
        ge=0.0,
        le=100.0,
        description="Estimated progress percentage",
    )
    started_at: Optional[datetime] = Field(None, description="When processing started")
    completed_at: Optional[datetime] = Field(
        None,
        description="When processing completed",
    )
    error: Optional[Dict[str, Any]] = Field(None, description="Error details if failed")


class RequirementSummary(BaseModel):
    """Summary of an extracted requirement."""

    id: str = Field(..., description="Requirement UUID")
    name: Optional[str] = Field(None, description="Short requirement name")
    text: str = Field(..., description="Full requirement text")
    type: Optional[str] = Field(
        None,
        description="Type: functional, compliance, constraint, non_functional",
    )
    priority: Optional[str] = Field(
        None,
        description="Priority: high, medium, low",
    )
    confidence: float = Field(
        0.0,
        ge=0.0,
        le=1.0,
        description="Extraction confidence score",
    )
    gobd_relevant: bool = Field(False, description="GoBD compliance relevance")
    gdpr_relevant: bool = Field(False, description="GDPR compliance relevance")
    mentioned_objects: List[str] = Field(
        default_factory=list,
        description="Business objects mentioned",
    )
    mentioned_messages: List[str] = Field(
        default_factory=list,
        description="Messages mentioned",
    )
    created_at: Optional[datetime] = Field(None, description="When created")


class JobRequirementsResponse(BaseModel):
    """Response with all requirements created by a job."""

    job_id: str = Field(..., description="UUID of the job")
    total: int = Field(0, description="Total number of requirements")
    requirements: List[RequirementSummary] = Field(
        default_factory=list,
        description="List of requirement summaries",
    )
