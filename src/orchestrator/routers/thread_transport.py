"""Thread transport routes: SSE event stream, input, interrupt.

Extracted from ``orchestrator.main`` (R1.B10). The SSE epoch/presence
tuning constants live here (their sole consumer is the stream route, and
tests monkeypatch them on this module). The database handle and the
stateless workspace-ensure / protected-cloud collaborators are resolved
per request from the owning application — this module imports no
application startup.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from orchestrator.schemas.thread_transport import (
    ThreadInputRequest,
    ThreadInterruptRequest,
)
from orchestrator.security.access import require_thread_owner
from orchestrator.security.auth import require_approved_user
from orchestrator.services.thread_interrupt_inbox import (
    InterruptAdmissionError,
    admit_thread_interrupt,
    find_existing_thread_interrupt,
)
from orchestrator.services.thread_transport import (
    _ensure_thread_turn_lock,
    _forward_to_agent,
    _load_thread_for_owner,
    _no_cursor_replay_start,
    _resolve_thread_for_forwarding,
    _revalidate_pinned_forwarding_binding,
    _schedule_turn_lock_cleanup,
    _thread_input_stateless,
    _thread_turn_inflight,
)
from shared.thread_presence import (
    DEFAULT_PRESENCE_RENEW_SECONDS,
    DEFAULT_PRESENCE_TTL_SECONDS,
    refresh_thread_presence,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@dataclass
class ThreadTransportDependencies:
    """Per-request collaborators resolved from the owning application."""

    store: Any
    schedule_stateless_workspace_ensure: Callable[[str], asyncio.Task[None]]
    protected_cloud_delivery_state: Callable[..., Any]


def get_thread_transport_dependencies(
    request: Request,
) -> ThreadTransportDependencies:
    """Resolve collaborators only from the application handling this request."""
    return request.app.state.thread_transport_dependencies_factory()


# How much *accumulated idle time* (seconds with no new rows) a live SSE stream
# tolerates before it re-reads `events_epoch` to detect a mid-stream bump. The
# epoch is bumped when an agent (re-)attaches; a generator opened before the
# bump would otherwise poll the dead old epoch forever, delivering nothing but
# keepalive pings that fool the client watchdog into thinking the stream is
# healthy (the "stale → refresh to fix" zombie). Read as a module global so
# tests can monkeypatch it to 0 to force a re-check on the first empty poll.
THREAD_EVENTS_EPOCH_RECHECK_S: float = float(
    os.environ.get("THREAD_EVENTS_EPOCH_RECHECK_S", "2.0")
)


THREAD_CLIENT_PRESENCE_RENEW_S: float = max(
    1.0,
    float(
        os.environ.get(
            "THREAD_CLIENT_PRESENCE_RENEW_S",
            str(DEFAULT_PRESENCE_RENEW_SECONDS),
        )
    ),
)


THREAD_CLIENT_PRESENCE_TTL_S: float = max(
    THREAD_CLIENT_PRESENCE_RENEW_S * 2.0,
    float(
        os.environ.get(
            "THREAD_CLIENT_PRESENCE_TTL_S",
            str(DEFAULT_PRESENCE_TTL_SECONDS),
        )
    ),
)


@router.get("/api/persistent/threads/{thread_id}/stream")
async def thread_event_stream(
    thread_id: str,
    request: Request,
    *,
    dependencies: ThreadTransportDependencies = Depends(
        get_thread_transport_dependencies
    ),
) -> StreamingResponse:
    """SSE: stream this thread's event log with replay-from-cursor.

    The client sends `Last-Event-ID: <epoch>:<seq>` to resume from a known
    point. If the cursor's epoch doesn't match the server, or its seq is
    older than retention, the server emits a single `gone_beyond_horizon`
    event and closes — the client must drop its cursor and re-sync.

    Otherwise: replay everything since the cursor, then switch to live
    mode (200ms poll, adaptive backoff to 1s after 5 empty polls).
    """
    user, thread = await require_thread_owner(request, dependencies.store, thread_id)

    # The existing owner-gated SSE connection is the lane-agnostic client
    # attachment signal. No lane field crosses the wire. Pinned streams keep
    # their exact behavior; a stateless stream must establish its durable TTL
    # before the browser can believe it is attached.
    track_presence = thread.get("execution_lane") == "stateless"
    if track_presence:
        try:
            presence = await refresh_thread_presence(
                dependencies.store,
                thread_id=thread_id,
                ttl_seconds=THREAD_CLIENT_PRESENCE_TTL_S,
                establish=True,
            )
        except Exception as exc:
            logger.warning(
                "thread_event_stream presence establish failed (thread=%s): %s",
                thread_id,
                exc,
            )
            raise HTTPException(
                status_code=503,
                detail="Session presence is temporarily unavailable",
            ) from exc
        if not presence.served:
            # The row changed lane or disappeared after the owner lookup. A
            # reconnect re-runs authorization and resolves the current lane.
            raise HTTPException(status_code=409, detail="Session lane changed")

    server_epoch = int(thread.get("events_epoch") or 0)

    # Parse Last-Event-ID. Format: "<epoch>:<seq>". Missing/malformed → no
    # cursor, so the replay floor is computed by _no_cursor_replay_start below
    # (anchored past the last completed turn, not seq 0).
    #
    # EventSource doesn't let the browser set custom request headers, so the
    # cockpit hands us the cached cursor via `?last_event_id=` for the
    # initial connection. On automatic reconnect, the browser appends the
    # `Last-Event-ID` header from the latest `id:` line we yielded — that
    # path is fully native and doesn't need the query param.
    last_event_id = (
        request.headers.get("Last-Event-ID")
        or request.headers.get("last-event-id")
        or request.query_params.get("last_event_id")
    )
    cursor_epoch: Optional[int] = None
    cursor_seq: Optional[int] = None
    if last_event_id:
        try:
            e_str, s_str = last_event_id.split(":", 1)
            cursor_epoch = int(e_str)
            cursor_seq = int(s_str)
        except (ValueError, AttributeError):
            cursor_epoch = None
            cursor_seq = None

    async def event_stream():
        # Kickstart: flush a comment immediately so the browser EventSource
        # fires `onopen` at once and buffering intermediaries (Cloudflare
        # Tunnel, Traefik) don't hold the response headers / idle-timeout the
        # connection waiting for the first body byte. Without this, a connect
        # whose cursor is already at the tail sends nothing until the ~20s
        # keepalive ping below — stalling the SSE receive path ~20s. Comments
        # (lines starting with `:`) are ignored by EventSource, so this is
        # side-effect-free on the client.
        yield ": open\n\n"

        next_presence_renew = time.monotonic() + THREAD_CLIENT_PRESENCE_RENEW_S

        # Mismatched epoch → force re-sync.
        if cursor_epoch is not None and cursor_epoch != server_epoch:
            async with dependencies.store.acquire() as conn:
                tail = await conn.fetchval(
                    "SELECT COALESCE(MAX(seq), 0) FROM thread_events "
                    "WHERE thread_id = $1 AND epoch = $2",
                    thread_id,
                    server_epoch,
                )
            payload = json.dumps(
                {
                    "method": "gone_beyond_horizon",
                    "params": {
                        "epoch": server_epoch,
                        "server_seq": int(tail or 0),
                        "reason": "epoch_mismatch",
                    },
                }
            )
            yield f"id: {server_epoch}:0\nevent: gone_beyond_horizon\ndata: {payload}\n\n"
            return

        # Retention floor for the current epoch.
        async with dependencies.store.acquire() as conn:
            min_seq = await conn.fetchval(
                "SELECT MIN(seq) FROM thread_events "
                "WHERE thread_id = $1 AND epoch = $2",
                thread_id,
                server_epoch,
            )
        min_seq = int(min_seq) if min_seq is not None else 0

        # Cursor older than retention → also force re-sync.
        if cursor_seq is not None and min_seq > 0 and cursor_seq < min_seq - 1:
            async with dependencies.store.acquire() as conn:
                tail = await conn.fetchval(
                    "SELECT COALESCE(MAX(seq), 0) FROM thread_events "
                    "WHERE thread_id = $1 AND epoch = $2",
                    thread_id,
                    server_epoch,
                )
            payload = json.dumps(
                {
                    "method": "gone_beyond_horizon",
                    "params": {
                        "epoch": server_epoch,
                        "server_seq": int(tail or 0),
                        "retention_min_seq": min_seq,
                        "reason": "cursor_older_than_retention",
                    },
                }
            )
            yield f"id: {server_epoch}:0\nevent: gone_beyond_horizon\ndata: {payload}\n\n"
            return

        # Replay floor. With a cursor, resume right after it. Without one, a
        # fresh attach has already loaded completed turns from REST history, so
        # anchor past the last completed turn instead of replaying the whole
        # epoch from 0 (which doubles the last assistant turn + shows a spurious
        # "SESSION RESUMED" divider — see _no_cursor_replay_start).
        if cursor_seq is not None:
            last_sent_seq = cursor_seq
        else:
            async with dependencies.store.acquire() as conn:
                last_sent_seq = await _no_cursor_replay_start(
                    conn, thread_id, server_epoch
                )
        empty_polls = 0
        idle_keepalive_at = 0.0
        epoch_idle = 0.0
        cancelled = False
        try:
            while not cancelled:
                if await request.is_disconnected():
                    break
                if track_presence and time.monotonic() >= next_presence_renew:
                    # A long-lived stream does not retain authorization from
                    # its opening handshake forever. Re-run the same BFF-cookie
                    # owner gate before every attested renewal; expiry or an
                    # ownership change closes the stream and writes no TTL.
                    _renew_user, renew_thread = await require_thread_owner(
                        request, dependencies.store, thread_id
                    )
                    if renew_thread.get("execution_lane") != "stateless":
                        return
                    presence = await refresh_thread_presence(
                        dependencies.store,
                        thread_id=thread_id,
                        ttl_seconds=THREAD_CLIENT_PRESENCE_TTL_S,
                        establish=False,
                    )
                    if not presence.served:
                        # Lane change/deletion: close. EventSource reconnects
                        # through require_thread_owner and current DB truth.
                        return
                    next_presence_renew = (
                        time.monotonic() + THREAD_CLIENT_PRESENCE_RENEW_S
                    )
                async with dependencies.store.acquire() as conn:
                    rows = await conn.fetch(
                        "SELECT seq, kind, payload "
                        "FROM thread_events "
                        "WHERE thread_id = $1 AND epoch = $2 AND seq > $3 "
                        "ORDER BY seq ASC "
                        "LIMIT 500",
                        thread_id,
                        server_epoch,
                        last_sent_seq,
                    )
                    # Zombie-epoch guard: after enough accumulated idle time
                    # with no new rows, re-read events_epoch on the SAME
                    # connection (no extra acquire). If an agent re-attached and
                    # bumped the epoch, this generator has been polling a dead
                    # epoch — terminate deterministically so the client
                    # re-anchors, instead of feeding it pings forever.
                    if not rows and epoch_idle >= THREAD_EVENTS_EPOCH_RECHECK_S:
                        epoch_idle = 0.0
                        current_epoch = await conn.fetchval(
                            "SELECT events_epoch FROM threads WHERE id = $1",
                            thread_id,
                        )
                        if current_epoch is None:
                            # Thread deleted mid-stream — terminate silently;
                            # the client's reconnect hits require_thread_owner
                            # → 404 and it drops the thread.
                            return
                        if int(current_epoch) != server_epoch:
                            new_epoch = int(current_epoch)
                            # Anchor past the last completed turn of the NEW
                            # epoch, not its tail: the bump lands mid-turn and
                            # the client's history reload only carries completed
                            # turns, so a tail anchor would drop the in-flight
                            # turn's already-journaled frames.
                            anchor = await _no_cursor_replay_start(
                                conn, thread_id, new_epoch
                            )
                            logger.info(
                                "thread_event_stream epoch bump %d→%d "
                                "(thread=%s), re-anchoring client to seq %d",
                                server_epoch,
                                new_epoch,
                                thread_id,
                                anchor,
                            )
                            payload = json.dumps(
                                {
                                    "method": "gone_beyond_horizon",
                                    "params": {
                                        "epoch": new_epoch,
                                        "server_seq": anchor,
                                        "reason": "epoch_bumped_mid_stream",
                                    },
                                }
                            )
                            # The `id:` line carries the new epoch's floor so a
                            # browser-native reconnect (bypassing the app
                            # handler) converges to the same replay start
                            # instead of replaying the new epoch from :0.
                            yield (
                                f"id: {new_epoch}:{anchor}\n"
                                f"event: gone_beyond_horizon\n"
                                f"data: {payload}\n\n"
                            )
                            return
                if rows:
                    empty_polls = 0
                    epoch_idle = 0.0
                    for row in rows:
                        seq = int(row["seq"])
                        # row["payload"] is a JSONB column — asyncpg may
                        # return it as str or already-parsed dict depending
                        # on codec registration.
                        raw_payload = row["payload"]
                        if isinstance(raw_payload, str):
                            payload_obj = json.loads(raw_payload)
                        else:
                            payload_obj = raw_payload
                        frame = {
                            "method": row["kind"],
                            "params": payload_obj,
                        }
                        body = json.dumps(frame)
                        yield f"id: {server_epoch}:{seq}\ndata: {body}\n\n"
                        last_sent_seq = seq
                    idle_keepalive_at = 0.0
                else:
                    # Adaptive backoff: 200ms × 5 empty polls, then 1s.
                    empty_polls += 1
                    wait = 1.0 if empty_polls >= 5 else 0.2
                    epoch_idle += wait
                    # Typed `ping` event every ~20s of idle. A bare `:`
                    # comment would keep the socket warm but never fire
                    # `onmessage` in the browser, leaving silent network
                    # drops undetectable client-side. A typed event with no
                    # `id:` line lets the cockpit watchdog observe liveness
                    # without advancing the replay cursor.
                    idle_keepalive_at += wait
                    if idle_keepalive_at >= 20.0:
                        yield "event: ping\ndata: {}\n\n"
                        idle_keepalive_at = 0.0
                    try:
                        await asyncio.sleep(wait)
                    except asyncio.CancelledError:
                        cancelled = True
                        break
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.warning("thread_event_stream error (thread=%s): %s", thread_id, e)
            return

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.post("/api/persistent/threads/{thread_id}/input")
async def thread_input(
    thread_id: str,
    body: ThreadInputRequest,
    request: Request,
    *,
    dependencies: ThreadTransportDependencies = Depends(
        get_thread_transport_dependencies
    ),
) -> dict[str, Any]:
    """Submit user input to a thread. Per-turn lock returns 409 on dupes."""
    from shared.run_queue import LANE_STATELESS

    user = await require_approved_user(request, dependencies.store)

    # Stateless-lane admission (stateless_agents.md §5.3.1) resolves BEFORE
    # agent forwarding — queue-lane threads have no bound agent, so
    # _resolve_thread_for_forwarding would 503 on them. Owner gate identical
    # to the resolver's; the pinned path below is untouched (its resolver
    # re-loads the thread and re-applies the same checks).
    lane_thread = await _load_thread_for_owner(thread_id, user, db=dependencies.store)
    if lane_thread.get("execution_lane") == LANE_STATELESS:
        if not body.content or not isinstance(body.content, str):
            raise HTTPException(
                status_code=400, detail="content must be a non-empty string"
            )
        # The per-turn in-process lock below is deliberately SKIPPED on this
        # lane: the run_queue itself serializes turns (input during a leased
        # turn only advances the watermark; one row per unit dedups the
        # queue), and the lock dict is per-process state — replica-unsafe
        # under the 2-replica topology anyway. body.turn_id is ignored: the
        # queue lane derives the turn number from DB truth (total_turns + 1).
        return await _thread_input_stateless(
            lane_thread,
            body.content,
            db=dependencies.store,
            schedule_stateless_workspace_ensure=(
                dependencies.schedule_stateless_workspace_ensure
            ),
        )

    thread, binding = await _resolve_thread_for_forwarding(
        thread_id,
        user,
        db=dependencies.store,
        protected_cloud_delivery_state=(dependencies.protected_cloud_delivery_state),
    )

    if not body.content or not isinstance(body.content, str):
        raise HTTPException(
            status_code=400, detail="content must be a non-empty string"
        )

    # Turn id defaults to the thread's current total_turns + 1. Reject
    # arbitrarily-large values to bound the lock dict.
    total_turns = int(thread.get("total_turns") or 0)
    if body.turn_id is None:
        turn_id = total_turns + 1
    else:
        turn_id = body.turn_id
        if turn_id < 0 or turn_id > total_turns + 5:
            raise HTTPException(
                status_code=400,
                detail=f"turn_id out of range "
                f"(thread at turn {total_turns}, max accepted "
                f"{total_turns + 5})",
            )

    lock = _ensure_thread_turn_lock(thread_id, turn_id)
    if lock.locked():
        in_flight = _thread_turn_inflight.get(thread_id, turn_id)
        return JSONResponse(
            status_code=409,
            content={
                "error": "turn_in_flight",
                "turn_id": in_flight,
                "thread_id": thread_id,
            },
        )
    async with lock:
        _thread_turn_inflight[thread_id] = turn_id
        try:
            # Waiting for another tab's turn lock is an authority boundary.
            # Refuse a same-G Pod/attach/endpoint rotation before constructing
            # the HTTP client; _forward_to_agent performs the final reread
            # after client entry as well.
            await _revalidate_pinned_forwarding_binding(binding, db=dependencies.store)
            result = await _forward_to_agent(
                binding,
                "/api/input",
                {"content": body.content, "turn_id": turn_id},
                db=dependencies.store,
            )
        finally:
            _schedule_turn_lock_cleanup(thread_id, turn_id)
    return {
        "accepted": True,
        "turn_id": turn_id,
        "agent": result,
    }


@router.post(
    "/api/persistent/threads/{thread_id}/interrupt",
    responses={202: {"description": "Stateless interrupt admitted"}},
)
async def thread_interrupt(
    thread_id: str,
    request: Request,
    body: ThreadInterruptRequest | None = None,
    *,
    dependencies: ThreadTransportDependencies = Depends(
        get_thread_transport_dependencies
    ),
) -> Any:
    """Interrupt one exact in-flight turn without exposing its execution lane.

    Pinned sessions retain their direct agent forward. Every forwarded body is
    bound to the exact runtime fingerprint; an otherwise-empty legacy command
    still targets the active turn observed by that runtime. A correlated
    client is forwarded intact so the agent can reject a retry aimed at an
    older turn. Stateless sessions commit an exact-lease request for the
    serving executor and return admission only — that owner applies the verb
    and journals the authoritative ack.
    """
    from shared.run_queue import LANE_STATELESS

    user, lane_thread = await require_thread_owner(
        request, dependencies.store, thread_id
    )
    correlated = body is not None and body.client_request_id is not None
    if correlated and body is not None and body.target_turn_id is not None:
        try:
            existing = await find_existing_thread_interrupt(
                dependencies.store,
                thread_id=thread_id,
                owner_user_id=lane_thread.get("user_id"),
                client_request_id=body.client_request_id,
                target_turn_id=body.target_turn_id,
            )
        except InterruptAdmissionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if existing is not None:
            return JSONResponse(
                status_code=202,
                content={
                    "accepted": True,
                    "request_id": str(existing.id),
                    "client_request_id": str(existing.client_request_id),
                    "target_turn_id": existing.target_turn_id,
                    "state": existing.state,
                    "duplicate": True,
                },
            )
    if lane_thread.get("execution_lane") == LANE_STATELESS:
        if not correlated or body is None or body.target_turn_id is None:
            # Stateless interrupt did not exist for legacy clients. Refuse an
            # uncorrelated command rather than letting it strike whichever
            # lease/turn happens to be current.
            raise HTTPException(
                status_code=422,
                detail=(
                    "client_request_id and target_turn_id are required for "
                    "stateless interrupt"
                ),
            )
        try:
            admitted = await admit_thread_interrupt(
                dependencies.store,
                thread_id=thread_id,
                owner_user_id=lane_thread.get("user_id"),
                client_request_id=body.client_request_id,
                target_turn_id=body.target_turn_id,
                requested_by=str(user.get("id") or user.get("sub") or "rest_client"),
            )
        except InterruptAdmissionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        logger.info(
            "session-interrupt admission: thread=%s turn=%d token=%d duplicate=%s",
            thread_id,
            admitted.target_turn_id,
            admitted.accepted_lease_token,
            admitted.duplicate,
        )
        return JSONResponse(
            status_code=202,
            content={
                "accepted": True,
                "request_id": str(admitted.id),
                "client_request_id": str(admitted.client_request_id),
                "target_turn_id": admitted.target_turn_id,
                "state": admitted.state,
                "duplicate": admitted.duplicate,
            },
        )

    _, binding = await _resolve_thread_for_forwarding(
        thread_id,
        user,
        db=dependencies.store,
        protected_cloud_delivery_state=(dependencies.protected_cloud_delivery_state),
    )
    payload: dict[str, Any] = {}
    if correlated and body is not None and body.target_turn_id is not None:
        payload = {
            "client_request_id": str(body.client_request_id),
            "target_turn_id": body.target_turn_id,
        }
    result = await _forward_to_agent(
        binding, "/api/interrupt", payload, db=dependencies.store
    )
    return {"accepted": True, "agent": result}
