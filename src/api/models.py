"""Request and response models for Universal Agent API.

Pydantic models for the FastAPI application, providing type-safe
request validation and response serialization.
"""

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class JobStatus(str, Enum):
    """Job processing status."""

    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"


class HealthStatus(str, Enum):
    """Service health status."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


# Request Models


class JobSubmitRequest(BaseModel):
    """Request to submit a new job for processing."""

    document_path: Optional[str] = Field(
        default=None,
        description="Path to document to process (for Creator agent)",
    )
    description: Optional[str] = Field(
        default=None,
        description="Job description - what the agent should accomplish",
    )
    requirement_id: Optional[str] = Field(
        default=None,
        description="Requirement ID to validate (for Validator agent)",
    )
    metadata: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Additional job metadata",
    )
    priority: str = Field(
        default="medium",
        description="Job priority (high, medium, low)",
    )

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "document_path": "/data/documents/gobd_spec.pdf",
                "description": "Extract GoBD compliance requirements",
                "priority": "high",
            }
        }
    )


class JobCancelRequest(BaseModel):
    """Request to cancel a running job."""

    reason: Optional[str] = Field(
        default=None,
        description="Reason for cancellation",
    )
    cleanup: bool = Field(
        default=True,
        description="Clean up workspace and resources",
    )


# Response Models


class JobSubmitResponse(BaseModel):
    """Response after submitting a job."""

    job_id: str = Field(..., description="Unique job identifier")
    status: JobStatus = Field(..., description="Initial job status")
    created_at: datetime = Field(..., description="Job creation timestamp")
    message: str = Field(..., description="Status message")


class JobStatusResponse(BaseModel):
    """Response for job status query."""

    job_id: str = Field(..., description="Job identifier")
    status: JobStatus = Field(..., description="Current status")
    created_at: datetime = Field(..., description="Creation timestamp")
    updated_at: Optional[datetime] = Field(None, description="Last update timestamp")
    iteration: int = Field(default=0, description="Current iteration count")
    error: Optional[Dict[str, Any]] = Field(None, description="Error details if failed")
    result: Optional[Dict[str, Any]] = Field(None, description="Result data if complete")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "job_id": "abc123-def456",
                "status": "processing",
                "created_at": "2024-01-08T10:30:00Z",
                "iteration": 42,
            }
        }
    )


class JobListResponse(BaseModel):
    """Response for listing jobs."""

    jobs: List[JobStatusResponse] = Field(..., description="List of jobs")
    total: int = Field(..., description="Total job count")
    page: int = Field(default=1, description="Current page")
    page_size: int = Field(default=20, description="Page size")


class HealthResponse(BaseModel):
    """Health check response."""

    status: HealthStatus = Field(..., description="Overall health status")
    agent_id: str = Field(..., description="Agent identifier")
    agent_name: str = Field(..., description="Agent display name")
    uptime_seconds: float = Field(..., description="Time since start")
    checks: Dict[str, bool] = Field(..., description="Individual health checks")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "status": "healthy",
                "agent_id": "creator",
                "agent_name": "Creator Agent",
                "uptime_seconds": 3600.5,
                "checks": {
                    "database": True,
                    "llm": True,
                    "workspace": True,
                },
            }
        }
    )


class ReadyResponse(BaseModel):
    """Readiness probe response."""

    ready: bool = Field(..., description="Whether agent is ready to accept jobs")
    message: str = Field(..., description="Status message")
    connections: Dict[str, bool] = Field(
        ..., description="Connection status for dependencies"
    )


class AgentStatusResponse(BaseModel):
    """Detailed agent status response."""

    agent_id: str = Field(..., description="Agent identifier")
    display_name: str = Field(..., description="Display name")
    initialized: bool = Field(..., description="Whether agent is initialized")
    current_job: Optional[str] = Field(None, description="Currently processing job")
    jobs_processed: int = Field(..., description="Total jobs processed")
    uptime_seconds: float = Field(..., description="Uptime in seconds")
    connections: Dict[str, bool] = Field(..., description="Connection status")
    config: Dict[str, Any] = Field(..., description="Configuration summary")


class ErrorResponse(BaseModel):
    """Error response model."""

    error: str = Field(..., description="Error type/code")
    message: str = Field(..., description="Error message")
    details: Optional[Dict[str, Any]] = Field(None, description="Additional details")
    job_id: Optional[str] = Field(None, description="Related job ID if applicable")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "error": "job_not_found",
                "message": "Job with ID 'abc123' not found",
                "job_id": "abc123",
            }
        }
    )


class WorkspaceFileResponse(BaseModel):
    """Response for workspace file operations."""

    path: str = Field(..., description="File path within workspace")
    content: Optional[str] = Field(None, description="File content (if requested)")
    size: int = Field(..., description="File size in bytes")
    modified_at: datetime = Field(..., description="Last modification time")
    is_directory: bool = Field(default=False, description="Whether path is a directory")


class WorkspaceListResponse(BaseModel):
    """Response for listing workspace contents."""

    job_id: str = Field(..., description="Job identifier")
    path: str = Field(..., description="Directory path")
    files: List[WorkspaceFileResponse] = Field(..., description="Directory contents")


class TodoResponse(BaseModel):
    """Response for todo list query."""

    job_id: str = Field(..., description="Job identifier")
    todos: List[Dict[str, Any]] = Field(..., description="Todo items")
    progress: Dict[str, int] = Field(..., description="Progress statistics")


class MetricsResponse(BaseModel):
    """Response for metrics endpoint."""

    agent_id: str = Field(..., description="Agent identifier")
    timestamp: datetime = Field(..., description="Metrics timestamp")
    jobs_total: int = Field(..., description="Total jobs processed")
    jobs_success: int = Field(..., description="Successful jobs")
    jobs_failed: int = Field(..., description="Failed jobs")
    average_duration_seconds: Optional[float] = Field(
        None, description="Average job duration"
    )
    current_iterations: int = Field(default=0, description="Iterations in current job")
    uptime_seconds: float = Field(..., description="Agent uptime")


# =============================================================================
# Orchestrator Integration Models
# =============================================================================


class JobStartRequest(BaseModel):
    """Request from orchestrator to start a job."""

    job_id: str = Field(..., description="Job UUID assigned by orchestrator")
    description: str = Field(..., description="Job description - what the agent should accomplish")
    upload_id: Optional[str] = Field(
        default=None,
        description="Upload ID for document files (from /api/uploads)",
    )
    config_upload_id: Optional[str] = Field(
        default=None,
        description="Upload ID for config YAML override",
    )
    instructions_upload_id: Optional[str] = Field(
        default=None,
        description="Upload ID for instructions markdown file",
    )
    document_path: Optional[str] = Field(
        default=None,
        description="Path to document to process",
    )
    document_dir: Optional[str] = Field(
        default=None,
        description="Directory containing documents",
    )
    config_name: Optional[str] = Field(
        default=None,
        description="Agent configuration name override",
    )
    context: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Optional context dictionary",
    )
    instructions: Optional[str] = Field(
        default=None,
        description="Additional inline instructions for the agent",
    )

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "job_id": "abc123-def456-789",
                "description": "Extract requirements from document",
                "document_path": "/data/documents/spec.pdf",
            }
        }
    )


class JobStartResponse(BaseModel):
    """Response after accepting a job from orchestrator."""

    job_id: str = Field(..., description="Accepted job ID")
    status: str = Field(default="accepted", description="Acceptance status")
    message: str = Field(
        default="Job processing started",
        description="Status message",
    )


class JobCancelByOrchestratorRequest(BaseModel):
    """Request from orchestrator to cancel current job."""

    reason: Optional[str] = Field(
        default=None,
        description="Reason for cancellation",
    )


class JobResumeRequest(BaseModel):
    """Request to resume a job from checkpoint."""

    job_id: str = Field(..., description="Job ID to resume")
    feedback: Optional[str] = Field(
        default=None,
        description="Optional feedback to inject before resuming",
    )
