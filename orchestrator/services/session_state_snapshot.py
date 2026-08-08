"""Durable, transport-independent persistent-session state snapshots.

The legacy ``session.state`` welcome frame is assembled from agent-process
memory and sent directly over the per-session WebSocket.  A stateless thread
has no stable pod to query, so the orchestrator's REST contract is deliberately
DB-authoritative instead: one repeatable-read snapshot of the current journal
epoch, transcript, and permission rows.

Journal persistence is asynchronous and bounded with respect to the agent's
in-memory callbacks.  Consequently, ``turn_in_flight`` and ``running_tool``
mean "durably observed as of ``event_cursor``", not "agent RAM right now".  The
Cockpit applies this snapshot before opening SSE, then replays from the returned
``replay_cursor`` just before the latest surviving turn boundary.  That rebuilds
the turn consistently even when REST history raced its completion, while a
frame still waiting in the ordered writer advances the snapshot in wire order.
A completed stateless queue row also clears a stranded journal-open edge if a
terminal frame was dropped.  This is the freshness contract that makes the read
lane-agnostic without inventing pod routing from ``run_queue.leased_by`` (a
diagnostic field, not a reachable address or ownership credential).  During
coexistence, pinned sessions still receive the later exact in-memory welcome
frame over their WebSocket.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any, Awaitable, Callable


_TURN_LIFECYCLE_KINDS = (
    "turn.started",
    "turn.completed",
    "turn.error",
    "turn.interrupted",
    "turn.parked",
    "ready",
)

_TURN_TERMINAL_KINDS = (
    "turn.completed",
    "turn.error",
    "turn.interrupted",
    "turn.parked",
    "session.ended",
    "ready",
)

_TOOL_CLEAR_KINDS = (*_TURN_TERMINAL_KINDS, "turn.started")


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


def _temperature(value: Any) -> float | int | None:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (float, int)) and not isinstance(value, bool):
        return value
    return None


def _integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


async def build_session_state_snapshot(
    db: Any,
    thread_id: str,
    *,
    resolved_config: dict[str, Any] | None = None,
    config_resolver: Callable[
        [dict[str, Any], dict[str, Any]], Awaitable[dict[str, Any] | None]
    ]
    | None = None,
) -> dict[str, Any] | None:
    """Return the durable ``session.state`` params shape for ``thread_id``.

    ``None`` means the thread disappeared after the caller's ownership check.
    ``resolved_config`` is a test/convenience seam for safe display fields that
    are not first-class columns yet. Production passes ``config_resolver``:
    it runs after the repeatable-read transaction against the exact thread row
    captured with ``event_cursor``. A later config write is therefore replayed
    above that cursor instead of being hidden beneath scalars from a different
    revision. Secrets and the resolved blob itself never enter the result.
    """

    if resolved_config is not None and config_resolver is not None:
        raise ValueError("pass resolved_config or config_resolver, not both")

    async with db.acquire() as conn:
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            thread = await conn.fetchrow(
                """
                SELECT thread.*, queue.state AS queue_state
                FROM threads AS thread
                LEFT JOIN run_queue AS queue
                  ON queue.unit_id = thread.id
                 AND queue.unit_kind = 'session_turn'
                WHERE thread.id = $1
                """,
                thread_id,
            )
            if thread is None:
                return None
            thread_source = dict(thread)

            epoch = int(thread["events_epoch"] or 0)
            message_count = int(
                await conn.fetchval(
                    "SELECT COUNT(*) FROM thread_messages "
                    "WHERE thread_id = $1 AND rewound_at IS NULL",
                    thread_id,
                )
                or 0
            )
            # Input rows are durable before execution. Multiple queued humans
            # can therefore carry numbers ahead of the turn the loop is
            # currently serving. Only execution-produced rows are a safe
            # fallback when this epoch has no turn lifecycle event.
            persisted_turn_count = int(
                await conn.fetchval(
                    "SELECT COALESCE(MAX(turn_number), 0) "
                    "FROM thread_messages "
                    "WHERE thread_id = $1 AND rewound_at IS NULL "
                    "AND role NOT IN ('human', 'event')",
                    thread_id,
                )
                or 0
            )
            lifecycle = await conn.fetchrow(
                """
                SELECT
                    (
                        SELECT event.kind
                        FROM thread_events AS event
                        WHERE event.thread_id = $1
                          AND event.epoch = $2
                          AND event.kind = ANY($3::text[])
                        ORDER BY event.seq DESC
                        LIMIT 1
                    ) AS latest_kind,
                    (
                        SELECT event.payload->>'turn_id'
                        FROM thread_events AS event
                        WHERE event.thread_id = $1
                          AND event.epoch = $2
                          AND event.kind IN (
                              'turn.started', 'turn.completed', 'turn.error',
                              'turn.interrupted', 'turn.parked'
                          )
                          AND event.payload ? 'turn_id'
                        ORDER BY event.seq DESC
                        LIMIT 1
                    ) AS latest_turn_id,
                    (
                        SELECT event.seq
                        FROM thread_events AS event
                        WHERE event.thread_id = $1
                          AND event.epoch = $2
                          AND event.kind = 'turn.started'
                        ORDER BY event.seq DESC
                        LIMIT 1
                    ) AS latest_turn_start_seq
                """,
                thread_id,
                epoch,
                list(_TURN_LIFECYCLE_KINDS),
            )
            running_row = await conn.fetchrow(
                """
                SELECT started.payload
                FROM thread_events AS started
                WHERE started.thread_id = $1
                  AND started.epoch = $2
                  AND started.kind = 'tool.started'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM thread_events AS later
                      WHERE later.thread_id = started.thread_id
                        AND later.epoch = started.epoch
                        AND later.seq > started.seq
                        AND (
                            later.kind = ANY($3::text[])
                            OR (
                                later.kind = 'tool.completed'
                                AND later.payload->>'id' = started.payload->>'id'
                            )
                        )
                  )
                ORDER BY started.seq DESC
                LIMIT 1
                """,
                thread_id,
                epoch,
                list(_TOOL_CLEAR_KINDS),
            )
            permission_rows = await conn.fetch(
                """
                SELECT id, tool_call_id, tool_name, tool_args
                FROM thread_permission_requests
                WHERE thread_id = $1 AND status = 'pending'
                ORDER BY requested_at ASC, id ASC
                """,
                thread_id,
            )

    metadata = _json_object(thread_source.get("metadata"))
    if config_resolver is not None:
        resolved_config = await config_resolver(thread_source, metadata)

    stored_override = _json_object(metadata.get("config_override"))
    stored_interactive = _json_object(stored_override.get("interactive"))
    stored_llm = _json_object(stored_override.get("llm"))
    resolved = _json_object(resolved_config)
    # ``serialize_resolved_config`` wraps the actual AgentConfig beneath
    # ``agent``; reading top-level ``llm``/``interactive`` would silently turn
    # a resolved model into the config filename (usually ``session_base``).
    resolved_agent = _json_object(resolved.get("agent"))
    resolved_interactive = _json_object(resolved_agent.get("interactive"))
    resolved_llm = _json_object(resolved_agent.get("llm"))

    queue_state = thread_source.get("queue_state")
    # run_queue rows live as long as their unit and can outlive an operator's
    # detached lane transition. Only the stateless lane may use its queue row
    # to correct a dropped terminal journal edge; pinned/unknown lanes trust
    # the journal (and pinned gets the exact WS state immediately afterward).
    queue_proves_idle = thread_source.get(
        "execution_lane"
    ) == "stateless" and queue_state in {
        "queued",
        "done",
        "parked",
    }
    # Whitelist states in which the runtime is allowed to exist. Legacy
    # ``idle`` is terminal-equivalent, and an unknown future/corrupt state must
    # not resurrect a stale journal-open edge.
    status_proves_idle = thread_source.get("status") not in {
        "created",
        "active",
        "awaiting_user",
    }
    runtime_proves_idle = queue_proves_idle or status_proves_idle

    running_tool: dict[str, Any] | None = None
    if running_row is not None and not runtime_proves_idle:
        payload = _json_object(running_row["payload"])
        if payload.get("tool"):
            running_tool = {
                "id": str(payload.get("id") or ""),
                "tool": str(payload["tool"]),
                "args": _json_object(payload.get("args")),
            }

    pending_permissions: list[dict[str, Any]] = []
    for row in permission_rows:
        pending_permissions.append(
            {
                "id": str(row["tool_call_id"] or ""),
                "approval_id": str(row["id"]),
                "tool": str(row["tool_name"] or ""),
                "args": _json_object(row["tool_args"]),
            }
        )

    permission_mode = (
        thread_source.get("permission_mode")
        or resolved_interactive.get("permission_mode")
        or stored_interactive.get("permission_mode")
        or "supervised"
    )
    narration_mode = (
        resolved_interactive.get("narration_mode")
        or stored_interactive.get("narration_mode")
        or "auto"
    )
    model = resolved_llm.get("model") or stored_llm.get("model")
    temperature = _temperature(
        resolved_llm.get("temperature", stored_llm.get("temperature"))
    )
    latest_kind = lifecycle["latest_kind"] if lifecycle is not None else None
    latest_turn_id = (
        _integer(lifecycle["latest_turn_id"]) if lifecycle is not None else None
    )
    turn_count = latest_turn_id if latest_turn_id is not None else persisted_turn_count
    latest_turn_start_seq = (
        _integer(lifecycle["latest_turn_start_seq"]) if lifecycle is not None else None
    )
    hwm = int(thread_source.get("events_seq_hwm") or 0)
    # Exclusive floor for reconstructing the latest turn. It closes both a
    # cached-cursor-in-the-middle-of-a-token-stream gap and the race where a
    # turn finishes after REST history but before SSE chooses its floor.
    replay_seq = (
        hwm
        if latest_kind == "turn.started" and runtime_proves_idle
        else (max(0, latest_turn_start_seq - 1) if latest_turn_start_seq else hwm)
    )

    return {
        "thread_id": str(thread_source["id"]),
        "permission_mode": str(permission_mode),
        "narration_mode": str(narration_mode),
        "turn_count": turn_count,
        "turn_in_flight": bool(
            latest_kind == "turn.started" and not runtime_proves_idle
        ),
        "message_count": message_count,
        "model": str(model) if model is not None else None,
        "temperature": temperature,
        "running_tool": running_tool,
        "pending_permissions": pending_permissions,
        "event_cursor": {
            "epoch": epoch,
            "seq": hwm,
        },
        "replay_cursor": {
            "epoch": epoch,
            "seq": replay_seq,
        },
        "snapshot_source": "durable_journal",
    }


__all__ = ["build_session_state_snapshot"]
