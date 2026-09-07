"""Exact execution authority for a worker job that owns subagent children.

The internal key authenticates an agent *process class*; it does not prove that
the calling process still owns one particular job.  Subagent rows outlive a
process and carry their own runtime generation, so every child mutation needs
both halves of the fence:

* the immutable parent execution authority captured when the runtime starts;
* the child's current ``runtime_generation`` locked in the same transaction.

Stateless jobs are fenced by the monotonically increasing ``run_queue`` lease
token.  Pinned jobs are fenced by the reciprocal job/agent binding plus the
registration-minted process generation and observed Pod UID.  This module sits
below both database facades so the orchestrator and agent-side direct writers
cannot drift onto subtly different predicates or lock orders.
"""

from __future__ import annotations

from typing import Any, Literal, Mapping
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ParentExecutionAuthorityRefused(RuntimeError):
    """The presented parent execution is absent, stale, or no longer live."""

    code = "parent_execution_authority_refused"

    def __init__(self, reason: str = code) -> None:
        super().__init__(reason)
        self.reason = reason

    def detail(self) -> dict[str, str]:
        return {"code": self.code, "reason": self.reason}


class ParentExecutionAuthority(BaseModel):
    """One immutable worker-job execution credential.

    ``parent_job_id`` deliberately rides in the signed-shaped value even when
    the HTTP path also names the job.  Matching both closes accidental
    cross-parent reuse before any SQL is issued.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = 1
    execution_lane: Literal["pinned", "stateless"]
    parent_job_id: UUID
    worker_lease_token: int | None = Field(default=None, ge=1)
    agent_id: UUID | None = None
    pod_uid: str | None = Field(default=None, max_length=256)
    dispatch_process_generation: str | None = Field(default=None, max_length=256)

    @field_validator("pod_uid", "dispatch_process_generation", mode="before")
    @classmethod
    def _normalize_optional_text(cls, value: Any) -> str | None:
        text = str(value or "").strip()
        return text or None

    @model_validator(mode="after")
    def _exactly_one_lane_shape(self) -> "ParentExecutionAuthority":
        if self.execution_lane == "stateless":
            if self.worker_lease_token is None:
                raise ValueError("stateless authority requires worker_lease_token")
            if any(
                value is not None
                for value in (
                    self.agent_id,
                    self.pod_uid,
                    self.dispatch_process_generation,
                )
            ):
                raise ValueError("stateless authority must not carry pinned identity")
        else:
            if self.worker_lease_token is not None:
                raise ValueError("pinned authority must not carry a worker lease")
            if (
                self.agent_id is None
                or self.pod_uid is None
                or self.dispatch_process_generation is None
            ):
                raise ValueError(
                    "pinned authority requires agent_id, pod_uid, and process generation"
                )
        return self

    def for_job(self, parent_job_id: UUID | str) -> bool:
        try:
            return self.parent_job_id == UUID(str(parent_job_id))
        except (TypeError, ValueError, AttributeError):
            return False

    def to_wire(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def coerce_parent_execution_authority(value: Any) -> ParentExecutionAuthority:
    """Parse one authority without accepting an untyped/missing fallback."""

    if isinstance(value, ParentExecutionAuthority):
        return value
    return ParentExecutionAuthority.model_validate(value)


async def require_parent_execution_authority(
    conn: Any,
    authority: ParentExecutionAuthority | Mapping[str, Any],
    *,
    parent_job_id: UUID | str,
    mutation: bool,
) -> Mapping[str, Any]:
    """Lock and prove one current parent execution inside the caller's tx.

    Lock order is part of the API:

    * stateless: ``run_queue FOR SHARE`` -> ``jobs`` -> child;
    * pinned: ``jobs`` -> ``agents FOR SHARE`` -> child.

    The caller owns the final child lock/write and MUST call this before touching
    the child.  ``mutation=False`` uses a parent share lock for an exact read;
    mutations serialize on ``jobs FOR UPDATE``.  The stateless queue fence is
    always the first SQL statement, preserving the run-queue fence contract.
    """

    return await _require_parent_execution_authority(
        conn,
        authority,
        parent_job_id=parent_job_id,
        mutation=mutation,
        settlement=False,
    )


async def require_parent_execution_settlement_authority(
    conn: Any,
    authority: ParentExecutionAuthority | Mapping[str, Any],
    *,
    parent_job_id: UUID | str,
    mutation: bool,
) -> Mapping[str, Any]:
    """Prove an exact worker may settle children after parent preemption.

    Cancellation, pause, and failure publish the parent status before asking
    its process to stop.  That closes provider/tool effects immediately, but
    the exact lease/process must retain one narrower authority: ending child
    generations it admitted while it was current.  Stateless callers still
    need the live queue lease; pinned callers still need the reciprocal agent
    process identity.  A replacement process can therefore never inherit this
    settlement window.
    """

    return await _require_parent_execution_authority(
        conn,
        authority,
        parent_job_id=parent_job_id,
        mutation=mutation,
        settlement=True,
    )


async def _require_parent_execution_authority(
    conn: Any,
    authority: ParentExecutionAuthority | Mapping[str, Any],
    *,
    parent_job_id: UUID | str,
    mutation: bool,
    settlement: bool,
) -> Mapping[str, Any]:
    try:
        parsed = coerce_parent_execution_authority(authority)
        job_uuid = UUID(str(parent_job_id))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ParentExecutionAuthorityRefused("invalid") from exc
    if parsed.parent_job_id != job_uuid:
        raise ParentExecutionAuthorityRefused("parent_mismatch")

    job_lock = "UPDATE" if mutation else "SHARE"
    if parsed.execution_lane == "stateless":
        # Import lazily to keep this shared model usable in lightweight API
        # processes without eagerly loading the run-queue package.
        from shared.run_queue import fence_lease

        held = await fence_lease(
            conn,
            unit_id=job_uuid,
            lease_token=int(parsed.worker_lease_token or 0),
        )
        if not held:
            raise ParentExecutionAuthorityRefused("stale_worker_lease")
        parent = await conn.fetchrow(
            f"""
            SELECT id, status::text AS status, execution_lane, assigned_agent_id
              FROM jobs
             WHERE id = $1::uuid
             FOR {job_lock}
            """,
            job_uuid,
        )
        parent_status = str(parent["status"]) if parent is not None else ""
        admitted_status = parent_status == "processing" or (
            settlement and parent_status in {"paused", "failed", "cancelled"}
        )
        if (
            parent is None
            or not admitted_status
            or str(parent["execution_lane"]) != "stateless"
            or parent["assigned_agent_id"] is not None
        ):
            raise ParentExecutionAuthorityRefused("stateless_parent_not_current")
        return parent

    parent = await conn.fetchrow(
        f"""
        SELECT id, status::text AS status, execution_lane, assigned_agent_id
          FROM jobs
         WHERE id = $1::uuid
         FOR {job_lock}
        """,
        job_uuid,
    )
    parent_status = str(parent["status"]) if parent is not None else ""
    assigned_agent = str(parent["assigned_agent_id"] or "") if parent else ""
    live_binding = parent_status == "processing" and assigned_agent == str(
        parsed.agent_id
    )
    settling_binding = (
        settlement
        and parent_status in {"paused", "failed", "cancelled"}
        and not assigned_agent
    )
    if (
        parent is None
        or str(parent["execution_lane"]) != "pinned"
        or not (live_binding or settling_binding)
    ):
        raise ParentExecutionAuthorityRefused("pinned_parent_not_current")

    reciprocal = await conn.fetchrow(
        """
        SELECT id
          FROM agents
         WHERE id = $1::uuid
           AND status = 'working'
           AND current_job_id = $2::uuid
           AND NULLIF(btrim(pod_uid), '')
               IS NOT DISTINCT FROM $3::text
           AND NULLIF(btrim(metadata->>'dispatch_process_generation'), '')
               = $4::text
         FOR SHARE
        """,
        parsed.agent_id,
        job_uuid,
        parsed.pod_uid,
        parsed.dispatch_process_generation,
    )
    if reciprocal is None:
        raise ParentExecutionAuthorityRefused("pinned_process_not_current")
    return parent


__all__ = [
    "ParentExecutionAuthority",
    "ParentExecutionAuthorityRefused",
    "coerce_parent_execution_authority",
    "require_parent_execution_authority",
    "require_parent_execution_settlement_authority",
]
