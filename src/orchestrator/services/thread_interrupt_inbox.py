"""Transactional admission for exact-turn stateless interrupts.

The public browser supplies a stable UUID and the numeric turn it is looking
at. Admission is intentionally narrower than the scalar control inbox: an
interrupt may target only the exact stateless lease whose executor has opened
its interrupt gate for that turn. It never wakes or requeues a unit. A
successor never signals its own RAM from the old request or retargets it, but
may durably settle that exact admitted stop intent after owner loss so the
interrupted input is not replayed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from orchestrator.services.stateless_workspace_gate import (
    stateless_session_workspace_check,
)
from shared.session_retirement import stateless_stop_markers


@dataclass(frozen=True, slots=True)
class AdmittedInterrupt:
    id: UUID
    client_request_id: UUID
    target_turn_id: int
    accepted_lease_token: int
    accepted_leased_by: str
    state: str
    duplicate: bool


class InterruptAdmissionError(RuntimeError):
    """Safe, public-facing interrupt admission refusal."""


def _admitted(row: Any, *, duplicate: bool) -> AdmittedInterrupt:
    return AdmittedInterrupt(
        id=row["id"],
        client_request_id=row["client_request_id"],
        target_turn_id=int(row["target_turn_id"]),
        accepted_lease_token=int(row["accepted_lease_token"]),
        accepted_leased_by=str(row["accepted_leased_by"]),
        state=str(row["outcome"] or "pending"),
        duplicate=duplicate,
    )


async def find_existing_thread_interrupt(
    db: Any,
    *,
    thread_id: UUID | str,
    owner_user_id: UUID | str | None,
    client_request_id: UUID,
    target_turn_id: int,
) -> AdmittedInterrupt | None:
    """Find an exact committed request without consulting mutable lane state.

    This preflight lets a masked stateless admission remain observable even if
    the thread changes lane before the browser retries. Ownership is joined in
    the read; a UUID reused for another target fails without disclosing it.
    ``admit_thread_interrupt`` still repeats the check under the thread lock.
    """

    tid = UUID(str(thread_id))
    uid = UUID(str(owner_user_id)) if owner_user_id is not None else None
    async with db.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT request.id, request.client_request_id, "
            "       request.target_turn_id, request.accepted_lease_token, "
            "       request.accepted_leased_by, request.outcome "
            "FROM thread_interrupt_requests request "
            "JOIN threads thread ON thread.id = request.thread_id "
            "WHERE request.thread_id = $1 "
            "AND request.client_request_id = $2 "
            "AND thread.user_id IS NOT DISTINCT FROM $3::uuid",
            tid,
            client_request_id,
            uid,
        )
    if row is None:
        return None
    if int(row["target_turn_id"]) != int(target_turn_id):
        raise InterruptAdmissionError(
            "client_request_id was already used for a different turn"
        )
    return _admitted(row, duplicate=True)


async def admit_thread_interrupt(
    db: Any,
    *,
    thread_id: UUID | str,
    owner_user_id: UUID | str | None,
    client_request_id: UUID,
    target_turn_id: int,
    requested_by: str,
) -> AdmittedInterrupt:
    """Commit one interrupt iff its exact stateless turn gate is open.

    The thread lock serializes ownership/lane changes with admission; the queue
    lock then snapshots the immutable target lease credentials. An exact UUID
    retry is returned before mutable lifecycle/gate checks, so a masked commit
    remains observable after the turn has already ended. Reusing the UUID for
    another target fails loudly.
    """

    tid = UUID(str(thread_id))
    uid = UUID(str(owner_user_id)) if owner_user_id is not None else None

    async with db.acquire() as conn:
        async with conn.transaction():
            thread = await conn.fetchrow(
                "SELECT id, user_id, execution_lane, agent_id, status, metadata "
                "FROM threads WHERE id = $1 FOR UPDATE",
                tid,
            )
            if thread is None or thread["user_id"] != uid:
                raise InterruptAdmissionError("Thread is unavailable")

            existing = await conn.fetchrow(
                "SELECT id, client_request_id, target_turn_id, "
                "       accepted_lease_token, accepted_leased_by, outcome "
                "FROM thread_interrupt_requests "
                "WHERE thread_id = $1 AND client_request_id = $2",
                tid,
                client_request_id,
            )
            if existing is not None:
                if int(existing["target_turn_id"]) != int(target_turn_id):
                    raise InterruptAdmissionError(
                        "client_request_id was already used for a different turn"
                    )
                return _admitted(existing, duplicate=True)

            if (
                str(thread["execution_lane"] or "") != "stateless"
                or thread["agent_id"] is not None
            ):
                raise InterruptAdmissionError(
                    "Session execution lane is unavailable for queued interrupt"
                )

            if str(thread["status"] or "") not in {
                "created",
                "active",
                "awaiting_user",
            }:
                raise InterruptAdmissionError(
                    "Session is not currently able to accept interrupts"
                )

            _backend, workspace_refusal = stateless_session_workspace_check(
                dict(thread)
            )
            if workspace_refusal is not None:
                raise InterruptAdmissionError(
                    "Stateless execution does not support this session's "
                    f"workspace binding ({workspace_refusal})"
                )

            try:
                stop_markers = stateless_stop_markers(thread["metadata"])
            except RuntimeError as exc:
                raise InterruptAdmissionError(
                    "Session lifecycle metadata is malformed"
                ) from exc
            if stop_markers:
                raise InterruptAdmissionError(
                    "Session is not currently able to accept interrupts"
                )

            queue = await conn.fetchrow(
                "SELECT unit_kind, state, lease_token, leased_by, "
                "       interrupt_admission_lease_token, "
                "       interrupt_admission_turn_id "
                "FROM run_queue WHERE unit_id = $1 FOR UPDATE",
                tid,
            )
            if (
                queue is None
                or str(queue["unit_kind"] or "") != "session_turn"
                or str(queue["state"] or "") != "leased"
                or not str(queue["leased_by"] or "").strip()
                or queue["interrupt_admission_lease_token"] is None
                or queue["interrupt_admission_turn_id"] is None
                or int(queue["interrupt_admission_lease_token"])
                != int(queue["lease_token"])
                or int(queue["interrupt_admission_turn_id"]) != int(target_turn_id)
            ):
                raise InterruptAdmissionError(
                    "The target turn is no longer accepting interrupts"
                )

            row = await conn.fetchrow(
                "INSERT INTO thread_interrupt_requests ("
                "thread_id, client_request_id, target_turn_id, "
                "accepted_lease_token, accepted_leased_by, requested_by"
                ") VALUES ($1, $2, $3, $4, $5, $6) "
                "RETURNING id, client_request_id, target_turn_id, "
                "          accepted_lease_token, accepted_leased_by, outcome",
                tid,
                client_request_id,
                int(target_turn_id),
                int(queue["lease_token"]),
                str(queue["leased_by"]).strip(),
                requested_by,
            )
            return _admitted(row, duplicate=False)
