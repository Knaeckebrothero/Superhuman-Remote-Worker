"""Request models for owner-facing thread lifecycle controls."""

from pydantic import BaseModel


class ThreadResumeRequest(BaseModel):
    """Optional resume body carrying accepted configuration-drift ids."""

    acknowledge: list[str] | None = None


class ThreadRewindRequest(BaseModel):
    """Detached-session rewind request."""

    message_id: str
    mode: str = "conversation"


__all__ = ["ThreadResumeRequest", "ThreadRewindRequest"]
