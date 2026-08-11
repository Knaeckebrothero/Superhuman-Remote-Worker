"""Lease- and turn-fenced consumption of ``thread_interrupt_requests``.

The orchestrator admits a request only while ``run_queue`` advertises one
exact interruptible ``(lease_token, turn_id)`` pair. The serving runtime then
performs the RAM side effect, writes ``interrupt.ack`` through its own ordered
journal allocator, and terminalizes the request from that durable receipt.

There is intentionally no transient claimed state. A crash before the receipt
leaves a pending request; a crash after the receipt is recovered without
signalling RAM or allocating another journal sequence. A successor uses its
current token as writer authority while retaining the request's immutable old
accepted token, closes the abandoned journal epoch, and settles the exact
target input before selecting successor work.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID


@dataclass(frozen=True, slots=True)
class InterruptRequest:
    id: UUID
    thread_id: UUID
    client_request_id: UUID
    target_turn_id: int
    accepted_lease_token: int
    accepted_leased_by: str
    outcome: str | None = None
    result: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class InterruptReceipt:
    epoch: int
    seq: int
    kind: str
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class InterruptInputConsumption:
    """Durable settlement of one interrupted human input."""

    consumed_seq: int
    state: str
    has_pending_input: bool
    has_pending_control: bool
    advanced: bool


_NEXT_SQL = """
SELECT r.id, r.thread_id, r.client_request_id, r.target_turn_id,
       r.accepted_lease_token, r.accepted_leased_by
FROM thread_interrupt_requests r
JOIN threads t ON t.id = r.thread_id
JOIN run_queue q ON q.unit_id = r.thread_id
WHERE r.thread_id = $1::uuid
  AND r.outcome IS NULL
  AND r.accepted_lease_token = $2::bigint
  AND r.target_turn_id = $3::integer
  AND t.execution_lane = 'stateless'
  AND t.agent_id IS NULL
  AND q.unit_kind = 'session_turn'
  AND q.state = 'leased'
  AND q.lease_token = $2::bigint
ORDER BY r.requested_at, r.id
LIMIT 1
"""

_STALE_SQL = """
SELECT r.id, r.thread_id, r.client_request_id, r.target_turn_id,
       r.accepted_lease_token, r.accepted_leased_by, r.outcome, r.result
FROM thread_interrupt_requests r
JOIN threads t ON t.id = r.thread_id
JOIN run_queue q ON q.unit_id = r.thread_id
WHERE r.thread_id = $1::uuid
  AND (
      r.outcome IS NULL
      OR (
          r.outcome = 'applied'
          AND NOT (COALESCE(r.result, '{}'::jsonb) ? 'consumed_input_seq')
      )
  )
  AND r.accepted_lease_token < $2::bigint
  AND t.execution_lane = 'stateless'
  AND t.agent_id IS NULL
  AND q.unit_kind = 'session_turn'
  AND q.state = 'leased'
  AND q.lease_token = $2::bigint
ORDER BY r.accepted_lease_token, r.target_turn_id, r.requested_at, r.id
"""

_OWNER_UPDATE_THREAD_SQL = """
SELECT 1 FROM threads
WHERE id = $1::uuid
  AND execution_lane = 'stateless'
  AND agent_id IS NULL
FOR SHARE
"""

_OWNER_SHARE_QUEUE_SQL = """
SELECT 1 FROM run_queue
WHERE unit_id = $1::uuid
  AND unit_kind = 'session_turn'
  AND state = 'leased'
  AND lease_token = $2::bigint
FOR SHARE
"""

_OWNER_UPDATE_QUEUE_SQL = """
SELECT 1 FROM run_queue
WHERE unit_id = $1::uuid
  AND unit_kind = 'session_turn'
  AND state = 'leased'
  AND lease_token = $2::bigint
FOR UPDATE
"""

_RECEIPT_SQL = """
SELECT epoch, seq, kind, payload
FROM thread_events
WHERE thread_id = $1::uuid AND interrupt_request_id = $2::uuid
"""


def _uuid(value: UUID | str) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _request(row: Any) -> InterruptRequest | None:
    if row is None:
        return None
    return InterruptRequest(
        id=row["id"],
        thread_id=row["thread_id"],
        client_request_id=row["client_request_id"],
        target_turn_id=int(row["target_turn_id"]),
        accepted_lease_token=int(row["accepted_lease_token"]),
        accepted_leased_by=str(row["accepted_leased_by"]),
        outcome=(str(row["outcome"]) if row.get("outcome") is not None else None),
        result=_json_object(row.get("result")),
    )


def interrupt_receipt_result(
    *,
    request_id: UUID | str,
    client_request_id: UUID | str,
    target_turn_id: int,
    event_kind: str,
    event_payload: Any,
) -> tuple[str, str | None, str | None] | None:
    """Validate a receipt and return ``(outcome, mode, error_code)``."""

    payload = _json_object(event_payload)
    try:
        receipt_turn_id = int(payload.get("target_turn_id"))
    except (TypeError, ValueError):
        return None
    if (
        event_kind != "interrupt.ack"
        or str(payload.get("request_id") or "") != str(request_id)
        or str(payload.get("client_request_id") or "") != str(client_request_id)
        or receipt_turn_id != int(target_turn_id)
        or not isinstance(payload.get("applied"), bool)
    ):
        return None

    if payload["applied"]:
        mode = str(payload.get("mode") or "")
        if mode not in {"hard", "graceful"} or payload.get("error_code") is not None:
            return None
        return "applied", mode, None

    error_code = str(payload.get("error_code") or "")
    if not error_code or payload.get("mode") is not None:
        return None
    return "rejected", None, error_code


async def fetch_next_interrupt_request(
    conn: Any,
    *,
    thread_id: UUID | str,
    lease_token: int,
    target_turn_id: int,
) -> InterruptRequest | None:
    """Return the oldest pending request for this exact live lease and turn."""

    row = await conn.fetchrow(
        _NEXT_SQL,
        _uuid(thread_id),
        int(lease_token),
        int(target_turn_id),
    )
    return _request(row)


async def fetch_next_stale_interrupt_request(
    conn: Any,
    *,
    thread_id: UUID | str,
    current_lease_token: int,
) -> InterruptRequest | None:
    """Return the oldest recovery candidate from a superseded lease.

    The current live queue lease is part of the read fence. The caller also
    holds :func:`owner_fence_current` in its transaction so the reaper and a
    claim winner serialize on the queue row before either locks the request.
    """

    requests = await fetch_stale_interrupt_requests(
        conn,
        thread_id=thread_id,
        current_lease_token=current_lease_token,
    )
    return requests[0] if requests else None


async def fetch_stale_interrupt_requests(
    conn: Any,
    *,
    thread_id: UUID | str,
    current_lease_token: int,
) -> list[InterruptRequest]:
    """Return recovery work from every superseded lease.

    Pending requests need a receipt/finalizer repair. An already-applied row
    without ``result.consumed_input_seq`` represents the narrower
    receipt/finalizer crash window: it is retained so the successor can stamp
    the same exactly-once input marker before it fetches pending input.
    """

    rows = await conn.fetch(
        _STALE_SQL,
        _uuid(thread_id),
        int(current_lease_token),
    )
    return [request for row in rows if (request := _request(row)) is not None]


async def owner_fence_current(
    conn: Any, *, thread_id: UUID | str, lease_token: int
) -> bool:
    """Lock and validate the exact stateless lease owner."""

    tid = _uuid(thread_id)
    thread = await conn.fetchrow(_OWNER_UPDATE_THREAD_SQL, tid)
    if thread is None:
        return False
    queue = await conn.fetchrow(
        _OWNER_SHARE_QUEUE_SQL,
        tid,
        int(lease_token),
    )
    return queue is not None


async def owner_fence_current_for_update(
    conn: Any, *, thread_id: UUID | str, lease_token: int
) -> bool:
    """Lock the current queue owner before any interrupt request row.

    Applied finalization advances the input watermark. Taking the queue lock
    up front preserves the global threads -> queue -> request lock order and
    prevents two same-turn requests from deadlocking while upgrading a shared
    queue lock after separately locking their request rows.
    """

    tid = _uuid(thread_id)
    thread = await conn.fetchrow(_OWNER_UPDATE_THREAD_SQL, tid)
    if thread is None:
        return False
    queue = await conn.fetchrow(
        _OWNER_UPDATE_QUEUE_SQL,
        tid,
        int(lease_token),
    )
    return queue is not None


async def fetch_interrupt_receipt(
    conn: Any, *, thread_id: UUID | str, request_id: UUID | str
) -> InterruptReceipt | None:
    """Read the unique durable journal receipt for one request."""

    row = await conn.fetchrow(
        _RECEIPT_SQL,
        _uuid(thread_id),
        _uuid(request_id),
    )
    if row is None:
        return None
    return InterruptReceipt(
        epoch=int(row["epoch"]),
        seq=int(row["seq"]),
        kind=str(row["kind"]),
        payload=_json_object(row["payload"]),
    )


async def _consume_applied_interrupt_input(
    conn: Any,
    *,
    thread_id: UUID | str,
    current_lease_token: int,
    accepted_lease_token: int,
    target_turn_id: int,
    request_id: UUID | str,
    idle: bool,
    terminal: bool = False,
) -> InterruptInputConsumption | None:
    """Consume one input for an applied request group under the queue lock.

    The caller must be inside an explicit transaction. All requests admitted
    for one old ``(lease, turn)`` are one logical interrupt: two browser tabs
    may create two rows, but they stopped the same turn and therefore consume
    one human row. ``result.consumed_input_seq`` is the durable exactly-once
    marker. The first applied sibling advances the watermark and stamps every
    applied sibling; later finalizers copy/reuse that marker without touching
    a newer human input.
    """

    tid = _uuid(thread_id)
    rid = _uuid(request_id)
    current_token = int(current_lease_token)
    accepted_token = int(accepted_lease_token)
    turn_id = int(target_turn_id)
    if current_token <= 0 or accepted_token <= 0 or turn_id <= 0:
        raise ValueError("interrupt input settlement identifiers must be positive")
    if accepted_token > current_token:
        raise ValueError("accepted lease cannot be newer than its current owner")

    thread = await conn.fetchrow(
        "SELECT 1 FROM threads WHERE id = $1::uuid "
        "AND execution_lane = 'stateless' AND agent_id IS NULL FOR SHARE",
        tid,
    )
    if thread is None:
        return None
    if idle:
        idle_states = (
            "('queued', 'parked', 'done')" if terminal else "('queued', 'parked')"
        )
        queue = await conn.fetchrow(
            "SELECT input_seq, consumed_seq, control_input_seq, "
            "control_consumed_seq, state FROM run_queue "
            "WHERE unit_id = $1::uuid AND unit_kind = 'session_turn' "
            "AND lease_token = $2::bigint "
            f"AND state IN {idle_states} "
            "AND leased_by IS NULL AND leased_until IS NULL FOR UPDATE",
            tid,
            current_token,
        )
    else:
        queue = await conn.fetchrow(
            "SELECT input_seq, consumed_seq, control_input_seq, "
            "control_consumed_seq, state FROM run_queue "
            "WHERE unit_id = $1::uuid AND unit_kind = 'session_turn' "
            "AND lease_token = $2::bigint AND state = 'leased' FOR UPDATE",
            tid,
            current_token,
        )
    if queue is None:
        return None

    applied = await conn.fetch(
        "SELECT id, result FROM thread_interrupt_requests "
        "WHERE thread_id = $1::uuid "
        "AND accepted_lease_token = $2::bigint "
        "AND target_turn_id = $3::integer AND outcome = 'applied' "
        "ORDER BY requested_at, id FOR UPDATE",
        tid,
        accepted_token,
        turn_id,
    )
    if not applied:
        return None
    if all(row["id"] != rid for row in applied):
        return None

    marker_values: set[int] = set()
    for row in applied:
        result = _json_object(row["result"])
        marker = result.get("consumed_input_seq")
        if marker is None:
            continue
        try:
            marker_values.add(int(marker))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("interrupt input marker is not an integer") from exc
    if len(marker_values) > 1:
        raise RuntimeError("interrupt sibling rows disagree on consumed input")

    advanced = False
    if marker_values:
        consumed_seq = marker_values.pop()
        queue_consumed = queue["consumed_seq"]
        if queue_consumed is None or int(queue_consumed) < consumed_seq:
            raise RuntimeError("interrupt input marker is ahead of queue watermark")
    else:
        humans = await conn.fetch(
            "SELECT seq FROM thread_messages "
            "WHERE thread_id = $1::uuid AND role = 'human' "
            "AND rewound_at IS NULL AND turn_number = $2::integer "
            "ORDER BY seq FOR SHARE",
            tid,
            turn_id,
        )
        if len(humans) != 1:
            raise RuntimeError(
                "applied interrupt target must map to exactly one live human input"
            )
        consumed_seq = int(humans[0]["seq"])
        queue_consumed = queue["consumed_seq"]
        # A terminal applied row can predate the marker while normal turn
        # completion already advanced the queue. Exact turn identity makes
        # that case unambiguous: stamp the target seq without moving the
        # watermark again. Only marker-ahead is inconsistent (guarded above).
        advanced = queue_consumed is None or consumed_seq > int(queue_consumed)

    marker = json.dumps(
        {
            "consumed_input_seq": consumed_seq,
            "input_settled_by_lease_token": current_token,
            "input_settlement": (
                "lease_recovery"
                if idle or accepted_token < current_token
                else "lease_owner"
            ),
        }
    )
    await conn.execute(
        "UPDATE thread_interrupt_requests "
        "SET result = COALESCE(result, '{}'::jsonb) || $4::jsonb "
        "WHERE thread_id = $1::uuid "
        "AND accepted_lease_token = $2::bigint "
        "AND target_turn_id = $3::integer AND outcome = 'applied'",
        tid,
        accepted_token,
        turn_id,
        marker,
    )

    if idle:
        if terminal:
            updated = await conn.fetchrow(
                "UPDATE run_queue SET consumed_seq = GREATEST(consumed_seq, $3::bigint), "
                "attempts_since_completion = 0, queued_at = now(), run_after = now(), "
                "state = 'queued', leased_by = NULL, leased_until = NULL, "
                "interrupt_admission_lease_token = NULL, "
                "interrupt_admission_turn_id = NULL "
                "WHERE unit_id = $1::uuid AND lease_token = $2::bigint "
                "AND state IN ('queued', 'parked', 'done') "
                "AND leased_by IS NULL AND leased_until IS NULL "
                "RETURNING consumed_seq, state, input_seq, control_input_seq, "
                "control_consumed_seq",
                tid,
                current_token,
                consumed_seq,
            )
        else:
            updated = await conn.fetchrow(
                "UPDATE run_queue SET consumed_seq = GREATEST(consumed_seq, $3::bigint), "
                "attempts_since_completion = 0, queued_at = now(), run_after = now(), "
                "state = CASE WHEN (input_seq IS NOT NULL "
                "AND input_seq > GREATEST(COALESCE(consumed_seq, -1), $3::bigint)) "
                "OR control_input_seq > control_consumed_seq THEN 'queued' ELSE 'done' END, "
                "leased_by = NULL, leased_until = NULL, "
                "interrupt_admission_lease_token = NULL, "
                "interrupt_admission_turn_id = NULL "
                "WHERE unit_id = $1::uuid AND lease_token = $2::bigint "
                "AND state IN ('queued', 'parked') AND leased_by IS NULL "
                "RETURNING consumed_seq, state, input_seq, control_input_seq, "
                "control_consumed_seq",
                tid,
                current_token,
                consumed_seq,
            )
    else:
        updated = await conn.fetchrow(
            "UPDATE run_queue SET consumed_seq = GREATEST(consumed_seq, $3::bigint), "
            "attempts_since_completion = 0 "
            "WHERE unit_id = $1::uuid AND lease_token = $2::bigint "
            "AND state = 'leased' "
            "RETURNING consumed_seq, state, input_seq, control_input_seq, "
            "control_consumed_seq",
            tid,
            current_token,
            consumed_seq,
        )
    if updated is None:
        return None
    final_consumed = int(updated["consumed_seq"])
    input_seq = updated["input_seq"]
    return InterruptInputConsumption(
        consumed_seq=final_consumed,
        state=str(updated["state"]),
        has_pending_input=input_seq is not None and int(input_seq) > final_consumed,
        has_pending_control=int(updated["control_input_seq"] or 0)
        > int(updated["control_consumed_seq"] or 0),
        advanced=advanced,
    )


async def consume_applied_interrupt_input_live(
    conn: Any,
    *,
    thread_id: UUID | str,
    current_lease_token: int,
    accepted_lease_token: int,
    target_turn_id: int,
    request_id: UUID | str,
) -> InterruptInputConsumption | None:
    """Settle one applied interrupt while preserving the current lease."""

    return await _consume_applied_interrupt_input(
        conn,
        thread_id=thread_id,
        current_lease_token=current_lease_token,
        accepted_lease_token=accepted_lease_token,
        target_turn_id=target_turn_id,
        request_id=request_id,
        idle=False,
    )


async def consume_applied_interrupt_input_idle(
    conn: Any,
    *,
    thread_id: UUID | str,
    current_lease_token: int,
    accepted_lease_token: int,
    target_turn_id: int,
    request_id: UUID | str,
    terminal: bool = False,
) -> InterruptInputConsumption | None:
    """Settle one applied interrupt when no runtime owns the queue row."""

    return await _consume_applied_interrupt_input(
        conn,
        thread_id=thread_id,
        current_lease_token=current_lease_token,
        accepted_lease_token=accepted_lease_token,
        target_turn_id=target_turn_id,
        request_id=request_id,
        idle=True,
        terminal=terminal,
    )


async def finalize_interrupt_request(
    conn: Any,
    *,
    request_id: UUID | str,
    thread_id: UUID | str,
    lease_token: int,
    target_turn_id: int,
    outcome: str,
    mode: str | None,
    error_code: str | None,
    accepted_lease_token: int | None = None,
    stale_recovery: bool = False,
) -> str:
    """Terminalize from a matching receipt under the exact live lease.

    Must run inside an explicit transaction. Returns ``applied``, ``rejected``,
    ``already_terminal``, ``missing_request``, ``missing_receipt``,
    ``invalid_receipt`` or ``lost_owner``.

    In explicit stale-recovery mode, ``lease_token`` is the current journal
    authority while ``accepted_lease_token`` identifies the older request.
    The terminal row stores the older token in ``applied_lease_token`` only to
    satisfy the frozen schema's exact-token shape; it does not attribute the
    successor-written rejection to the expired process.
    """

    if outcome not in {"applied", "rejected"}:
        raise ValueError("outcome must be applied or rejected")
    if (outcome == "applied" and (mode not in {"hard", "graceful"} or error_code)) or (
        outcome == "rejected" and (mode is not None or not error_code)
    ):
        raise ValueError("interrupt terminal fields do not match outcome")

    current_token = int(lease_token)
    request_token = (
        current_token if accepted_lease_token is None else int(accepted_lease_token)
    )
    if stale_recovery:
        if request_token >= current_token:
            raise ValueError(
                "stale recovery requires an accepted token older than the owner"
            )
    elif request_token != current_token:
        raise ValueError("live interrupt request token must match its owner")

    tid = _uuid(thread_id)
    rid = _uuid(request_id)
    if not await owner_fence_current_for_update(
        conn,
        thread_id=tid,
        lease_token=current_token,
    ):
        return "lost_owner"

    row = await conn.fetchrow(
        "SELECT * FROM thread_interrupt_requests WHERE id = $1::uuid FOR UPDATE",
        rid,
    )
    if row is None:
        return "missing_request"
    if (
        row["thread_id"] != tid
        or int(row["accepted_lease_token"]) != request_token
        or int(row["target_turn_id"]) != int(target_turn_id)
    ):
        return "lost_owner"
    if row["outcome"] is not None:
        return "already_terminal"

    receipt = await fetch_interrupt_receipt(
        conn,
        thread_id=tid,
        request_id=rid,
    )
    if receipt is None:
        return "missing_receipt"
    receipt_result = interrupt_receipt_result(
        request_id=rid,
        client_request_id=row["client_request_id"],
        target_turn_id=int(row["target_turn_id"]),
        event_kind=receipt.kind,
        event_payload=receipt.payload,
    )
    if receipt_result is None:
        return "invalid_receipt"
    receipt_outcome, receipt_mode, receipt_error = receipt_result
    if (receipt_outcome, receipt_mode, receipt_error) != (outcome, mode, error_code):
        return "invalid_receipt"

    result = {
        "request_id": str(rid),
        "client_request_id": str(row["client_request_id"]),
        "target_turn_id": int(row["target_turn_id"]),
        "applied": outcome == "applied",
    }
    if mode is not None:
        result["mode"] = mode
    if error_code is not None:
        result["error_code"] = error_code
    updated = await conn.fetchrow(
        "UPDATE thread_interrupt_requests "
        "SET outcome = $2, result = $3::jsonb, applied_mode = $4, "
        "applied_at = now(), applied_lease_token = $5, "
        "journal_epoch = $6, journal_seq = $7, acknowledged_at = now(), "
        "error_code = $8 "
        "WHERE id = $1::uuid AND outcome IS NULL RETURNING id",
        rid,
        outcome,
        json.dumps(result),
        mode,
        request_token,
        receipt.epoch,
        receipt.seq,
        error_code,
    )
    if updated is None:
        return "already_terminal"
    if outcome == "applied":
        consumed = await consume_applied_interrupt_input_live(
            conn,
            thread_id=tid,
            current_lease_token=current_token,
            accepted_lease_token=request_token,
            target_turn_id=int(target_turn_id),
            request_id=rid,
        )
        if consumed is None:
            # The surrounding transaction must roll back the terminal row:
            # an applied acknowledgement without its exactly-once input mark
            # would let a successor re-run the interrupted human message.
            raise RuntimeError("applied interrupt input settlement lost owner")
    return outcome
