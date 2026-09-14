"""Request bodies owned by the agent registration surface.

Moved verbatim from ``orchestrator.main`` (R1.B06, lane C, census group
``S_REG``). ``AgentRegistration``, ``AgentRegistrationResponse`` and
``AgentHeartbeat`` already live in :mod:`orchestrator.schemas.agent_runtime` and
stay there; only the pod runtime-actor body was still declared inline in main.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class PodRuntimeActorRequest(BaseModel):
    """Body for POST /api/agents/{agent_id}/runtime-actor/session."""

    thread_id: str = Field(..., description="Thread this pod was just attached to")


__all__ = ["PodRuntimeActorRequest"]
