"""Request bodies for the fleet-admin infrastructure-metering operations.

Each one carries the evidence its operation needs to be replayable and
auditable: an ``idempotency_key`` where the write is one-way, an operator
``reason`` recorded in the security log, and — for destruction — a digest of
the out-of-band evidence rather than a free-text claim.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field


class InfrastructureCutoverPrepareRequest(BaseModel):
    idempotency_key: UUID
    reason: str = Field(min_length=1, max_length=1024)


class InfrastructureCoverageWaiverRequest(BaseModel):
    idempotency_key: UUID
    reason: str = Field(min_length=1, max_length=2048)


class InfrastructureCorrectionDeltaRequest(BaseModel):
    source: Literal["infra-allocation-v2"]
    source_id: str = Field(min_length=1, max_length=256)
    unit: str = Field(min_length=1, max_length=64)
    ts: datetime
    expected_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    quantity: Decimal
    payload_overrides: dict[str, Any] = Field(default_factory=dict)
    inherit_rate: bool = True
    canonical_rate_version_id: UUID | None = None


class InfrastructureCorrectionRequest(BaseModel):
    idempotency_key: UUID
    reason: str = Field(min_length=1, max_length=2048)
    deltas: list[InfrastructureCorrectionDeltaRequest] = Field(
        min_length=1,
        max_length=100,
    )


class InfrastructureStorageActivationRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=2048)


class InfrastructureStorageActivationScheduleRequest(
    InfrastructureStorageActivationRequest
):
    activated_at: datetime


class InfrastructureComputeActivationRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=2048)


class InfrastructureComputeActivationScheduleRequest(
    InfrastructureComputeActivationRequest
):
    idempotency_key: UUID
    activated_at: datetime


class InfrastructureComputeEpochRolloverRequest(InfrastructureComputeActivationRequest):
    idempotency_key: UUID


class InfrastructureStorageDestructionRequest(BaseModel):
    idempotency_key: UUID
    effective_at: datetime
    evidence_kind: Literal["operator-attested"]
    evidence_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason_code: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")
    reason: str = Field(min_length=1, max_length=2048)
