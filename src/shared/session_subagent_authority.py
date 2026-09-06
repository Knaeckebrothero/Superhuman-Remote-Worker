"""Exact parent authority for subagents owned by a persistent session.

A thread-backed parent is not a worker job.  Its current execution is proven
either by the reciprocal pinned ``threads``/``agents`` identity or by the
exact stateless ``session_turn`` queue lease.  Child transcript and lifecycle
writers use this shared value before locking the child's generation, so a
retired session process cannot keep writing through a still-valid internal
service credential.
"""

from __future__ import annotations

from typing import Any, Literal, Mapping
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class SessionParentAuthorityRefused(RuntimeError):
    """The presented session execution is malformed, stale, or unsupported."""

    code = "session_parent_authority_refused"

    def __init__(self, reason: str = code) -> None:
        self.reason = str(reason or self.code)
        super().__init__(self.reason)

    def detail(self) -> dict[str, str]:
        return {"code": self.code, "reason": self.reason}


class SessionParentAuthority(BaseModel):
    """One immutable pinned-session life or stateless session-turn lease."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = 1
    execution_lane: Literal["pinned", "stateless"]
    parent_thread_id: UUID

    agent_id: UUID | None = None
    pod_uid: str | None = Field(default=None, max_length=256)
    session_runtime_generation: UUID | None = None
    runtime_attach_token: UUID | None = None

    lease_token: int | None = Field(default=None, ge=1)
    executor_id: str | None = Field(default=None, max_length=256)
    executor_pod_uid: str | None = Field(default=None, max_length=256)

    @field_validator("version", mode="before")
    @classmethod
    def _strict_version(cls, value: Any) -> int:
        if type(value) is not int or value != 1:
            raise ValueError("session authority version must be integer 1")
        return value

    @field_validator("lease_token", mode="before")
    @classmethod
    def _strict_optional_lease_token(cls, value: Any) -> int | None:
        if value is None:
            return None
        if type(value) is not int or value <= 0:
            raise ValueError("session authority lease token must be a positive integer")
        return value

    @field_validator("pod_uid", "executor_id", "executor_pod_uid", mode="before")
    @classmethod
    def _strict_optional_text(cls, value: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not value or value != value.strip():
            raise ValueError(
                "session authority identity text must be trimmed and nonempty"
            )
        return value

    @model_validator(mode="after")
    def _exact_lane_shape(self) -> "SessionParentAuthority":
        pinned = (
            self.agent_id,
            self.pod_uid,
            self.session_runtime_generation,
            self.runtime_attach_token,
        )
        stateless = (self.lease_token, self.executor_id, self.executor_pod_uid)
        if self.execution_lane == "pinned":
            if any(value is None for value in pinned) or any(
                value is not None for value in stateless
            ):
                raise ValueError(
                    "pinned session authority requires only agent, pod, "
                    "runtime generation, and attach token"
                )
        elif any(value is None for value in stateless) or any(
            value is not None for value in pinned
        ):
            raise ValueError(
                "stateless session authority requires only lease and executor identity"
            )
        return self

    def for_thread(self, parent_thread_id: UUID | str) -> bool:
        try:
            return self.parent_thread_id == UUID(str(parent_thread_id))
        except (TypeError, ValueError, AttributeError):
            return False

    def to_wire(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def coerce_session_parent_authority(value: Any) -> SessionParentAuthority:
    """Parse one exact session authority without an untyped fallback."""

    if isinstance(value, SessionParentAuthority):
        return value
    return SessionParentAuthority.model_validate(value)


async def require_session_parent_authority(
    conn: Any,
    authority: SessionParentAuthority | Mapping[str, Any],
    *,
    parent_thread_id: UUID | str,
    run_in_background: bool = False,
    delivery_event: bool = False,
) -> Mapping[str, Any]:
    """Lock and prove the exact current parent session inside a caller's tx.

    The authority lock is always taken before the caller touches a child row.
    Pinned authority follows ``thread -> agent``.  Stateless authority follows
    the production input-delivery gate's ``thread -> run_queue`` order.  The
    latter queue lease still precedes every child-generation lock or write.
    """

    try:
        parsed = coerce_session_parent_authority(authority)
        parent_uuid = UUID(str(parent_thread_id))
    except (TypeError, ValueError, AttributeError) as exc:
        raise SessionParentAuthorityRefused("invalid") from exc
    if parsed.parent_thread_id != parent_uuid:
        raise SessionParentAuthorityRefused("parent_mismatch")
    if run_in_background and parsed.execution_lane == "stateless":
        raise SessionParentAuthorityRefused("stateless_background_unsupported")

    from shared.persistent_input_delivery import (
        InputDeliveryAuthorityLost,
        _lock_stateless_runtime_authority,
        lock_runtime_authority,
    )

    try:
        if parsed.execution_lane == "pinned":
            await lock_runtime_authority(
                conn,
                thread_id=parent_uuid,
                agent_id=parsed.agent_id,
                pod_uid=str(parsed.pod_uid),
                session_runtime_generation=parsed.session_runtime_generation,
                runtime_attach_token=parsed.runtime_attach_token,
            )
        else:
            await _lock_stateless_runtime_authority(
                conn,
                thread_id=parent_uuid,
                lease_token=int(parsed.lease_token or 0),
                executor_id=str(parsed.executor_id),
                pod_uid=str(parsed.executor_pod_uid),
                for_update=delivery_event,
            )
    except InputDeliveryAuthorityLost as exc:
        reason = (
            "pinned_parent_not_current"
            if parsed.execution_lane == "pinned"
            else "stateless_parent_not_current"
        )
        raise SessionParentAuthorityRefused(reason) from exc

    parent = await conn.fetchrow(
        "SELECT id, kind, user_id, project_id, status, execution_lane, "
        "total_turns FROM threads WHERE id=$1::uuid",
        parent_uuid,
    )
    if parent is None or str(parent["kind"] or "") != "session":
        raise SessionParentAuthorityRefused("parent_not_session")
    if str(parent["execution_lane"] or "") != parsed.execution_lane:
        raise SessionParentAuthorityRefused("parent_lane_changed")
    return parent


def session_subagent_delivery_id(
    child_thread_id: UUID | str, runtime_generation: UUID | str
) -> UUID:
    """Server-compatible stable terminal delivery identity for one child run."""

    child = str(UUID(str(child_thread_id)))
    generation = str(UUID(str(runtime_generation)))
    return uuid5(NAMESPACE_URL, f"srw:subagent-delivery:v1:{child}:{generation}")


__all__ = [
    "SessionParentAuthority",
    "SessionParentAuthorityRefused",
    "coerce_session_parent_authority",
    "require_session_parent_authority",
    "session_subagent_delivery_id",
]
