"""Shared thread-event journal contract — epoch bumps and system frames.

The (epoch, seq) allocation contract of ``thread_events`` / ``threads``
(knowledge-base/knowledge/features/stateless_agents.md §5.3.2) is maintained from more than one
application: the agent's persistent runtime owns the streaming writer, while
the orchestrator (rewind for detached threads today; the M4 reaper's
``turn.interrupted`` / ``turn.parked`` system frames next) must append frames
and bump generations without an agent in the loop. This package is the single
implementation of the two DB-side primitives both sides share, in the same
spirit as ``shared.orch_surface``: stdlib-only, framework-free, no imports
from ``agent.api`` or ``orchestrator.*``.

Both helpers take an already-acquired asyncpg-style connection (anything with
``fetchrow(sql, *args)``) — pool management, retries, and failure taxonomy
stay with the caller. JSON payloads are bound as text and cast server-side
(``$n::jsonb``) so the helpers work on any connection regardless of codec
registration (neither existing pool registers a jsonb codec).
"""

from __future__ import annotations

import json
from typing import Any

__all__ = ["append_system_frame", "bump_epoch"]


# One statement: allocate the next seq from the thread's high-water mark and
# insert the frame under the CURRENT epoch, atomically. The UPDATE both
# reserves the seq (hwm is pre-incremented, so a later attach seeding from
# GREATEST(hwm, MAX(seq)) can never collide with it) and reads the epoch in
# the same snapshot — the epoch-read-and-stamp-in-one-transaction shape the
# doc requires for the system-writer class.
_APPEND_SYSTEM_FRAME_SQL = """
    WITH allocated AS (
        UPDATE threads
        SET events_seq_hwm = events_seq_hwm + 1
        WHERE id = $1
        RETURNING events_epoch, events_seq_hwm AS seq
    )
    INSERT INTO thread_events (
        thread_id, epoch, seq, kind, payload, interrupt_request_id,
        permission_request_id
    )
    SELECT $1, allocated.events_epoch, allocated.seq, $2, $3::jsonb,
           $4::uuid, $5::uuid
    FROM allocated
    RETURNING epoch, seq
"""

_BUMP_EPOCH_SQL = """
    UPDATE threads
    SET events_epoch = events_epoch + 1,
        events_seq_hwm = 0
    WHERE id = $1
    RETURNING events_epoch
"""


async def append_system_frame(
    conn: Any,
    *,
    thread_id: str,
    kind: str,
    payload: dict[str, Any],
    interrupt_request_id: str | None = None,
    permission_request_id: str | None = None,
) -> tuple[int, int] | None:
    """Append one system-class frame under the thread's CURRENT epoch.

    One statement: a CTE pre-increments ``threads.events_seq_hwm`` (reserving
    the seq durably — the mark survives retention pruning) and reads
    ``events_epoch`` in the same snapshot, then inserts the frame at that
    (epoch, seq). ``interrupt_request_id`` optionally links a post-steal
    ``interrupt.ack`` to its durable inbox row; the database's partial unique
    index enforces one receipt per request. ``permission_request_id`` does the
    same for an exact-lease ``permission.resolved`` owner-loss receipt. Other
    system frames leave both links NULL.
    Returns the allocated ``(epoch, seq)``, or ``None`` when the thread row
    does not exist.

    WRITER-EXCLUSION CONSTRAINT: call this only from contexts where no live
    in-process journal writer holds this epoch — reaper post-steal, post-park,
    rewind, terminal teardown. A live-epoch system write would race the
    attached runtime's in-memory seq counter (which seeds once at attach and
    increments locally); the UNIQUE (thread_id, epoch, seq) index makes that
    collision loud rather than silent, and generalizing this allocator to
    live epochs (e.g. outbox ``title.updated``) is deliberately out of scope
    until S2 (knowledge-base/knowledge/features/stateless_agents.md §5.3.2, system-writer class).
    """
    row = await conn.fetchrow(
        _APPEND_SYSTEM_FRAME_SQL,
        thread_id,
        kind,
        json.dumps(payload),
        interrupt_request_id,
        permission_request_id,
    )
    if row is None:
        return None
    return int(row["epoch"]), int(row["seq"])


async def bump_epoch(conn: Any, *, thread_id: str) -> int:
    """Open a new event generation: epoch + 1, seq high-water mark reset to 0.

    The only legitimate epoch bumpers are deliberate ones — rewind and a
    reaper/steal takeover (plus the attach-time resolver when it finds the
    previous session life terminal). Clean reattaches must NOT call this;
    they reuse the epoch so cached client cursors stay valid
    (knowledge-base/knowledge/features/stateless_agents.md §5.2/§5.3.2).

    Returns the new epoch. Raises ``LookupError`` when the thread row does
    not exist — callers wrap this in their own failure taxonomy.
    """
    row = await conn.fetchrow(_BUMP_EPOCH_SQL, thread_id)
    if row is None:
        raise LookupError(f"thread {thread_id} does not exist")
    return int(row["events_epoch"])
