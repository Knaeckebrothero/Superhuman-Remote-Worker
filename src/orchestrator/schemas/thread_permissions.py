"""Request schemas for thread permission decision routes (R1.B10)."""

from pydantic import BaseModel


class ThreadApproveRequest(BaseModel):
    """Body for POST /api/persistent/threads/{id}/approve/{approval_id}."""

    decision: str  # "approve" or "deny"
