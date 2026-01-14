"""API layer for Universal Agent.

FastAPI application and Pydantic models for HTTP interface.
"""

from .app import create_app, set_config_path
from .models import (
    JobStatus,
    HealthStatus,
    JobSubmitRequest,
    JobSubmitResponse,
    JobStatusResponse,
    HealthResponse,
    ErrorResponse,
)

__all__ = [
    "create_app",
    "set_config_path",
    "JobStatus",
    "HealthStatus",
    "JobSubmitRequest",
    "JobSubmitResponse",
    "JobStatusResponse",
    "HealthResponse",
    "ErrorResponse",
]
