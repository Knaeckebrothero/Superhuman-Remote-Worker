"""Request bodies for the runtime-actor and Officer verification surfaces.

Moved verbatim from ``orchestrator.main`` (R1.B06, lane C, census group
``J_runtime_verification``). Class names and every field constraint are part of
the published request contract, so they are reproduced exactly rather than
"tidied": the ``ge``/``le`` bounds below are the only thing standing between an
admin typo and an unbounded verification window.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class RuntimeActorAuthorizationRequest(BaseModel):
    """Target of a sensitive knowledge write; identity stays in the header."""

    action: Literal["machine_tags", "charter"]
    project_id: str


class OfficerRuntimeVerificationPlanRequest(BaseModel):
    """Bounded admin-only runtime verification parameters."""

    idempotency_key: UUID
    exercise: Literal["longevity", "response_loss", "maintenance_failure"]
    expires_in_seconds: int = Field(default=900, ge=120, le=3600)
    logical_window_seconds: int | None = Field(default=None, ge=30, le=600)
    response_losses: int | None = Field(default=None, ge=1, le=2)
    response_loss_gap_seconds: int | None = Field(default=None, ge=0, le=300)


__all__ = [
    "OfficerRuntimeVerificationPlanRequest",
    "RuntimeActorAuthorizationRequest",
]
