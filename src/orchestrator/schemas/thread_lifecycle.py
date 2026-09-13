"""Request models for owner-facing thread lifecycle controls."""

from pydantic import BaseModel


class ThreadResumeRequest(BaseModel):
    """Optional body for POST /resume. ``acknowledge`` carries the drift item
    ids the user accepted losing."""

    acknowledge: list[str] | None = None


class ThreadRewindRequest(BaseModel):
    """Body for POST /api/agents/threads/{id}/rewind (detached sessions)."""

    message_id: str
    mode: str = "conversation"


__all__ = ["ThreadResumeRequest", "ThreadRewindRequest"]
