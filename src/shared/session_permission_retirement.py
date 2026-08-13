"""Exact-lease retirement for stateless session permission prompts.

This is deliberately not an ``expires_at`` sweeper. A healthy lease owner may
keep a question open while durable client presence remains live. Retirement is
reachable only after the caller has locked ``threads -> run_queue`` and
committed (in the same transaction) the consecutive token bump which proves
that the accepted owner can no longer answer or run the tool.

The row CAS and its linked ``permission.resolved`` event are one transaction.
An approval racing the owner-loss transaction either changes ``status`` first
and is left alone, or waits and loses the ``status = 'pending'`` CAS after the
expiry receipt commits. A generic sweep never guesses the authority of a
legacy NULL token. This helper may retire one only because its callers have
already established a writer-exclusive stateless owner-loss/terminal boundary;
that narrow exception closes rolling old-agent compatibility safely.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.shared.event_journal import append_system_frame, bump_epoch

_RETIRE_REASONS = {"lease_expired", "force_end"}

_LOCK_STALE_PENDING_SQL = """
SELECT id, tool_call_id, accepted_lease_token
FROM thread_permission_requests
WHERE thread_id = $1::uuid
  AND (accepted_lease_token IS NULL
       OR (accepted_lease_token > 0
           AND accepted_lease_token < $2::bigint))
  AND status = 'pending'
ORDER BY accepted_lease_token NULLS FIRST, requested_at, id
FOR UPDATE
"""

_EXPIRE_EXACT_PENDING_SQL = """
UPDATE thread_permission_requests
SET status = 'expired',
    decided_at = clock_timestamp(),
    decided_by = $4::text
WHERE id = $1::uuid
  AND thread_id = $2::uuid
  AND accepted_lease_token IS NOT DISTINCT FROM $3::bigint
  AND status = 'pending'
RETURNING id, tool_call_id, accepted_lease_token
"""


@dataclass(frozen=True, slots=True)
class RetiredPermission:
    """One atomically expired row and its linked journal cursor."""

    request_id: str
    tool_call_id: str
    epoch: int
    seq: int


@dataclass(frozen=True, slots=True)
class PermissionRetirementResult:
    """Rows retired at one proven consecutive lease boundary."""

    receipts: tuple[RetiredPermission, ...]
    epoch_bumped: bool = False

    @property
    def count(self) -> int:
        return len(self.receipts)


async def retire_stale_stateless_permissions(
    conn: Any,
    *,
    thread_id: str,
    retired_lease_token: int,
    successor_lease_token: int,
    reason: str,
    epoch_already_bumped: bool,
) -> PermissionRetirementResult:
    """Expire prompts admitted by exactly one proven-lost stateless lease.

    The caller owns an explicit transaction and the locked thread/queue rows.
    ``successor_lease_token`` must be exactly one greater than the boundary's
    retired token; it may be a reaper generation or public Force-End's terminal
    generation. The function locks every still-pending row carrying a positive
    immutable token older than that successor, plus legacy NULL rows created by
    an older agent on this now-proven writer-free stateless thread. This closes
    rolling-upgrade windows where either side predated the lease-binding
    contract. It never reads ``expires_at`` and never adopts a row into a later
    claimant.

    When another owner-loss mechanism has already opened a new writer-free
    journal epoch in this transaction, pass ``epoch_already_bumped=True``.
    Otherwise this helper bumps once, but only after it has locked at least one
    row that it can retire. Each status CAS and receipt append then commits or
    rolls back as one unit with the caller's token bump.
    """

    old_token = int(retired_lease_token)
    next_token = int(successor_lease_token)
    if old_token < 0 or next_token != old_token + 1:
        raise ValueError("permission retirement requires one exact token bump")
    if reason not in _RETIRE_REASONS:
        raise ValueError("permission retirement reason is invalid")

    pending = await conn.fetch(_LOCK_STALE_PENDING_SQL, thread_id, next_token)
    if not pending:
        return PermissionRetirementResult(())

    bumped = False
    if not epoch_already_bumped:
        await bump_epoch(conn, thread_id=thread_id)
        bumped = True

    decided_by = f"system/{reason}"
    receipts: list[RetiredPermission] = []
    for candidate in pending:
        request_id = str(candidate["id"])
        tool_call_id = str(candidate["tool_call_id"])
        raw_accepted_token = candidate["accepted_lease_token"]
        accepted_token = (
            int(raw_accepted_token) if raw_accepted_token is not None else None
        )
        if accepted_token is not None and (
            accepted_token <= 0 or accepted_token >= next_token
        ):
            raise RuntimeError(
                f"permission retirement selected invalid token for {request_id}"
            )
        updated = await conn.fetchrow(
            _EXPIRE_EXACT_PENDING_SQL,
            request_id,
            thread_id,
            accepted_token,
            decided_by,
        )
        if updated is None:
            # The row is locked and was selected with the same predicates. A
            # miss is corruption or an unexpected trigger, not a benign race.
            raise RuntimeError(
                f"permission retirement lost exact pending row {request_id}"
            )
        if str(updated["tool_call_id"]) != tool_call_id:
            raise RuntimeError(
                f"permission retirement identity changed for {request_id}"
            )

        cursor = await append_system_frame(
            conn,
            thread_id=thread_id,
            kind="permission.resolved",
            payload={
                "id": tool_call_id,
                "approval_id": request_id,
                "decision": "expired",
                "reason": reason,
                "accepted_lease_token": accepted_token,
                "legacy_unbound": accepted_token is None,
            },
            permission_request_id=request_id,
        )
        if cursor is None:
            raise RuntimeError(
                f"thread disappeared while retiring permission {request_id}"
            )
        receipts.append(
            RetiredPermission(
                request_id=request_id,
                tool_call_id=tool_call_id,
                epoch=int(cursor[0]),
                seq=int(cursor[1]),
            )
        )

    return PermissionRetirementResult(tuple(receipts), epoch_bumped=bumped)


__all__ = [
    "PermissionRetirementResult",
    "RetiredPermission",
    "retire_stale_stateless_permissions",
]
