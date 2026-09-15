"""Thread transport: turn locks, pinned forwarding, stateless input admission.

Extracted from ``orchestrator.main`` (R1.B10). Owns the per-turn lock and
in-flight registries (shared process lifetime), the exact pinned-binding
revalidation and agent-forwarding path, the stateless-lane input admission
transaction, and the owner-scoped thread loader. The database handle and the
protected-cloud delivery collaborator are injected by application composition
— this module imports no application startup.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from uuid import uuid4
from fastapi import HTTPException
from orchestrator.services.session_class_policy import (
    require_stateless_workspace as _require_stateless_workspace,
)
from orchestrator.services.session_runtime_admission import (
    ThreadRuntimeAuthority,
    pinned_binding_invalid_detail,
    protected_cloud_marker_state,
    thread_runtime_authority,
    thread_runtime_refusal_detail,
)
from orchestrator.services.session_runtime_identity import (
    thread_accepts_runtime as _thread_accepts_runtime,
)
from orchestrator.services.stateless_workspace_gate import thread_metadata_object
from orchestrator.services.workspace_suspension import workspace_suspension_service
from shared.pinned_session_identity import PinnedSessionBinding

logger = logging.getLogger(__name__)


# Headless persistent sessions — Phase 2 SSE + REST transport
# =============================================================================
#
# SSE replaces the WebSocket as the primary server→client path; the existing
# /ws/persistent/{thread_id} stays as a fallback. Per
# knowledge-base/knowledge/features/headless_persistent_sessions.md.
#
# The per-turn input lock guards against duplicate POSTs from concurrent
# cockpit tabs racing on the same turn. Single-instance orchestrator, so a
# module-level dict is enough; entries auto-clean 5 min after release.

_thread_turn_locks: dict[tuple[str, int], asyncio.Lock] = {}


_thread_turn_inflight: dict[str, int] = {}


def _ensure_thread_turn_lock(thread_id: str, turn_id: int) -> asyncio.Lock:
    """Get or create the lock for (thread_id, turn_id). Concurrent callers
    landing on the same tuple share the same Lock object."""
    key = (thread_id, turn_id)
    lock = _thread_turn_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _thread_turn_locks[key] = lock
    return lock


def _schedule_turn_lock_cleanup(thread_id: str, turn_id: int) -> None:
    """Remove the lock entry 5 minutes after release. Memory-leak guard
    for long-lived sessions accumulating per-turn locks."""

    async def _later() -> None:
        await asyncio.sleep(300)
        _thread_turn_locks.pop((thread_id, turn_id), None)
        if _thread_turn_inflight.get(thread_id) == turn_id:
            _thread_turn_inflight.pop(thread_id, None)

    asyncio.create_task(_later(), name=f"turn-lock-cleanup-{thread_id[:8]}")


async def _resolve_thread_for_forwarding(
    thread_id: str,
    user: dict,
    *,
    db: Any,
    protected_cloud_delivery_state: Callable[..., Awaitable[Any]],
) -> tuple[dict, PinnedSessionBinding]:
    """Resolve one owner-visible thread and its exact pinned runtime binding.

    Stateless callers branch before this helper.  All agent/endpoint fields in
    the result come from one reciprocal DB snapshot rather than independent
    thread and agent reads.  A suspended pinned workspace is restored before
    that final snapshot.
    """
    thread = await db.get_thread(thread_id)
    if not thread:
        raise HTTPException(status_code=404, detail="Thread not found")
    # Fail-closed for orphans (user_id IS NULL); admins bypass.
    if not user.get("is_admin") and str(thread.get("user_id") or "") != str(user["id"]):
        raise HTTPException(status_code=403, detail="Not your thread")
    if not _thread_accepts_runtime(thread):
        raise HTTPException(
            status_code=409, detail=thread_runtime_refusal_detail(thread)
        )
    if thread.get("execution_lane") != "pinned":
        raise HTTPException(
            status_code=409,
            detail="Thread execution lane does not support direct forwarding",
        )

    async def _refresh_runtime_authority() -> dict[str, Any]:
        current = await db.get_thread(thread_id)
        if not _thread_accepts_runtime(current):
            raise HTTPException(
                status_code=409, detail=thread_runtime_refusal_detail(current)
            )
        if not user.get("is_admin") and str(current.get("user_id") or "") != str(
            user["id"]
        ):
            raise HTTPException(status_code=403, detail="Not your thread")
        if current.get("execution_lane") != "pinned":
            raise HTTPException(
                status_code=409,
                detail="Thread execution lane does not support direct forwarding",
            )
        marker = protected_cloud_marker_state(thread_metadata_object(current))
        if marker == "malformed":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "protected_cloud_malformed",
                    "message": "Protected cloud session state is invalid.",
                },
            )
        if marker == "on":
            state, code = await protected_cloud_delivery_state(
                current, thread_metadata_object(current)
            )
            if state != "ready":
                raise HTTPException(
                    status_code=425,
                    detail={
                        "code": "protected_cloud_not_ready",
                        "state": state,
                        "reason": code,
                    },
                )
        return current

    thread = await _refresh_runtime_authority()

    # Restore suspended workspace before forwarding (mirrors persistent_ws_proxy)
    metadata = thread.get("metadata") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (json.JSONDecodeError, TypeError):
            metadata = {}
    ws_ctx = metadata.get("workspace_container") or {}
    if ws_ctx.get("status") == "suspended" and workspace_suspension_service.is_enabled:
        logger.info("Restoring suspended workspace for thread %s", thread_id)
        ok = await workspace_suspension_service.restore_thread_workspace(thread_id)
        if not ok:
            raise HTTPException(
                status_code=503,
                detail="Failed to restore suspended workspace",
            )
        thread = await _refresh_runtime_authority()

    runtime_authority = thread_runtime_authority(thread)
    if runtime_authority is None:  # _refresh_runtime_authority proves this
        raise HTTPException(
            status_code=409, detail=thread_runtime_refusal_detail(thread)
        )
    binding = await db.get_pinned_session_binding(
        thread_id,
        expected_runtime_generation=runtime_authority.generation,
    )
    if binding is None:
        raise HTTPException(
            status_code=409,
            detail=pinned_binding_invalid_detail(runtime_authority),
        )
    _require_forwardable_pinned_binding(binding)
    return thread, binding


def _require_forwardable_pinned_binding(binding: PinnedSessionBinding) -> None:
    """Require a currently live agent status without freezing status equality."""

    if binding.agent_status not in {"ready", "working", "session"}:
        raise HTTPException(status_code=425, detail="session not ready")


def _binding_runtime_authority(
    binding: PinnedSessionBinding,
) -> ThreadRuntimeAuthority:
    return ThreadRuntimeAuthority(
        thread_id=binding.thread_id,
        generation=binding.runtime_generation,
    )


async def _revalidate_pinned_forwarding_binding(
    binding: PinnedSessionBinding,
    *,
    db: Any,
) -> PinnedSessionBinding:
    """Re-read and compare every immutable DB/routing coordinate."""

    current = await db.get_pinned_session_binding(
        binding.thread_id,
        expected_runtime_generation=binding.runtime_generation,
    )
    if current is None or current.target_key != binding.target_key:
        raise HTTPException(
            status_code=409,
            detail=pinned_binding_invalid_detail(_binding_runtime_authority(binding)),
        )
    _require_forwardable_pinned_binding(current)
    return current


async def _forward_to_agent(
    binding: PinnedSessionBinding,
    path: str,
    payload: dict,
    timeout: float = 30.0,
    *,
    db: Any,
) -> dict[str, Any]:
    """POST to one exact pinned Pod after a client-boundary DB reread."""

    identity_fingerprint = binding.session_identity_fingerprint
    forwarded_payload = dict(payload)
    supplied_fingerprint = forwarded_payload.get("session_identity_fingerprint")
    if supplied_fingerprint not in (None, identity_fingerprint):
        raise ValueError("forwarded session identity does not match its binding")
    forwarded_payload["session_identity_fingerprint"] = identity_fingerprint
    agent_url = f"http://{binding.pod_ip}:{binding.pod_port}{path}"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            # Client/pool entry may await.  Re-read after it so no stale target
            # receives an effect merely because it was authoritative before
            # transport setup.  The endpoint validates the fingerprint again
            # across the final network race.
            await _revalidate_pinned_forwarding_binding(binding, db=db)
            response = await client.post(agent_url, json=forwarded_payload)
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(
            "Agent forward failed: %s %s -> %s",
            path,
            binding.agent_id,
            e,
        )
        raise HTTPException(status_code=503, detail=f"Agent unreachable: {e}") from e
    try:
        response_body = response.json()
    except Exception:
        response_body = None
    if (
        response.status_code == 409
        and isinstance(response_body, dict)
        and response_body.get("error") == "session_identity_mismatch"
    ):
        raise HTTPException(
            status_code=409,
            detail=pinned_binding_invalid_detail(_binding_runtime_authority(binding)),
        )
    if response.status_code == 503:
        if (
            isinstance(response_body, dict)
            and response_body.get("error") == "runtime_terminating"
        ):
            raise HTTPException(
                status_code=503,
                detail={
                    "error": "runtime_terminating",
                    "retryable": True,
                    "message": "The runtime is terminating; retry on its replacement.",
                },
                headers={"Retry-After": response.headers.get("Retry-After", "5")},
            )
    if response.status_code >= 500:
        raise HTTPException(
            status_code=502,
            detail=f"Agent error: {response.status_code} {response.text[:200]}",
        )
    if response.status_code >= 400:
        raise HTTPException(
            status_code=response.status_code,
            detail=response.text[:200],
        )
    return (
        response_body
        if isinstance(response_body, dict)
        else {"raw": response.text[:500]}
    )


async def _no_cursor_replay_start(conn, thread_id: str, epoch: int) -> int:
    """Replay floor (exclusive) for an SSE attach that carries no cursor.

    A fresh client — opening the session on a second device, or any client
    with no cached cursor for this thread — has already painted the thread's
    completed turns from REST history. Replaying the whole epoch from seq 0
    would re-deliver each completed turn as a *live* copy the cockpit reducer
    can't reconcile (history turns are keyed by message id, replayed turns by
    turn_id), so the last assistant turn renders twice, split by a spurious
    "SESSION RESUMED" divider — the cold-attach twin of the gone_beyond_horizon
    duplicate render.

    Anchor instead just past the last turn-terminal event (``turn.completed`` /
    ``turn.error``, both of which persist their turn to ``thread_messages``), so
    the replay carries only the in-flight, not-yet-persisted turn. Returns 0
    when no turn has finished yet (first turn still streaming) so that turn —
    absent from REST history — still replays from the start.
    """
    anchor = await conn.fetchval(
        "SELECT COALESCE(MAX(seq), 0) FROM thread_events "
        "WHERE thread_id = $1 AND epoch = $2 "
        "AND kind IN ('turn.completed', 'turn.error')",
        thread_id,
        epoch,
    )
    return int(anchor or 0)


async def _load_thread_for_owner(thread_id: str, user: dict, *, db: Any) -> dict:
    """Load a thread under the same owner gate ``_resolve_thread_for_forwarding``
    applies (404 unknown; fail-closed 403 for orphans and non-owners; admin
    bypass) — WITHOUT its agent-resolution / workspace-restore side effects.

    Used by the stateless-lane branches: queue-lane threads have no bound
    agent, so the forwarding resolver's 503 would mask the lane entirely.
    """
    thread = await db.get_thread(thread_id)
    if not thread:
        raise HTTPException(status_code=404, detail="Thread not found")
    if not user.get("is_admin") and str(thread.get("user_id") or "") != str(user["id"]):
        raise HTTPException(status_code=403, detail="Not your thread")
    return thread


async def _thread_input_stateless(
    thread: dict,
    content: str,
    *,
    db: Any,
    schedule_stateless_workspace_ensure: Callable[[str], asyncio.Task[None]],
) -> dict[str, Any]:
    """Admit one user turn for a stateless-lane thread (stateless_agents.md
    §5.3.1): persist the message, advance the input watermark, and queue the
    unit — all in ONE transaction, so "message durable ⟺ watermark advanced"
    can never tear and a signal can never be lost.

    The message row is indistinguishable from the agent's accept-time persist
    of a plain-text human message (``src/api/persistent_app._accept_user_input``
    → ``src/database/db.save_thread_message``): same ``msg_`` id mint
    with the agent's own uuid5 row-id coercion, ``role='human'``,
    ``turn_number = total_turns + 1``, all other columns at their NULL
    defaults, and the same ``threads`` last_activity/total_turns bump.

    Admission is ``record_input_seq`` — the input-during-anything path: it
    creates a fresh ``'queued'`` row, revives ``'done'``, merges the watermark
    into ``'queued'``, bumps ONLY the watermark on ``'leased'`` (the running
    turn's completion re-queues via ``input_seq > consumed_seq``), and records
    input on ``'parked'`` without reviving it (explicit unpark only). No
    separate ``enqueue_unit`` call is needed: every branch leaves the unit
    queued, leased-with-watermark, or deliberately parked.
    """
    from shared.row_identity import _coerce_row_id
    from orchestrator.services.stateless_queue_state import queue_block
    from shared.run_queue import (
        LANE_STATELESS,
        UNIT_KIND_SESSION_TURN,
        queue_depth_for,
        queue_state_for,
        record_input_seq,
    )

    # The unlocked preflight provides a fast refusal. The locked copy below is
    # authoritative against lane/tier/lifecycle changes before message commit.
    _require_stateless_workspace(thread)

    thread_id = str(thread["id"])
    # Mirror the agent's accept-time mint exactly; the row id is the same
    # deterministic uuid5 the agent-side coercion would derive from this raw
    # id, so a later executor re-persist upserts onto this row (ON CONFLICT
    # (id)) instead of duplicating the user bubble.
    raw_msg_id = f"msg_{uuid4().hex[:24]}"
    row_id = _coerce_row_id(raw_msg_id)

    async with db.acquire() as conn:
        async with conn.transaction():
            locked_thread = await conn.fetchrow(
                "SELECT id, user_id, execution_lane, agent_id, status, "
                "       total_turns, metadata "
                "FROM threads WHERE id = $1 FOR UPDATE",
                thread_id,
            )
            if (
                locked_thread is None
                or str(locked_thread["execution_lane"] or "") != LANE_STATELESS
                or locked_thread["agent_id"] is not None
            ):
                raise HTTPException(
                    status_code=409,
                    detail="Thread is no longer eligible for stateless admission",
                )
            locked_thread_dict = dict(locked_thread)
            locked_backend = _require_stateless_workspace(locked_thread_dict)
            locked_status = str(locked_thread["status"] or "")
            if locked_status not in {
                "created",
                "active",
                "awaiting_user",
                "suspended",
            }:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Thread is not currently accepting stateless input "
                        f"(status={locked_status or 'unknown'})"
                    ),
                )
            needs_workspace_ensure = locked_backend == "sandbox"
            if locked_status == "suspended":
                # Wake and enqueue are one lifecycle transaction. Workspace
                # restore remains a post-commit side effect, but no claimant
                # can observe a runnable queue paired with a still-suspended
                # thread (the claim/credential boundary correctly refuses
                # suspended rows).
                woke = await conn.fetchval(
                    "UPDATE threads SET status = 'created', "
                    "agent_id = NULL, control_admission_agent_id = NULL, "
                    "awaiting_user_since = NULL, extend_count = 0 "
                    "WHERE id = $1::uuid AND execution_lane = 'stateless' "
                    "AND status = 'suspended' RETURNING id",
                    thread_id,
                )
                if woke is None:
                    raise RuntimeError(
                        "stateless suspended-input wake lost thread authority"
                    )
            turn_number = int(locked_thread["total_turns"] or 0) + 1
            fair_key = (
                str(locked_thread["user_id"])
                if locked_thread["user_id"] is not None
                else None
            )
            seq = await conn.fetchval(
                """
                INSERT INTO thread_messages (id, thread_id, role, content, turn_number)
                VALUES ($1, $2, 'human', $3, $4)
                RETURNING seq
                """,
                row_id,
                thread_id,
                content,
                turn_number,
            )
            # Same activity bump the agent's save_thread_message performs.
            await conn.execute(
                """
                UPDATE threads
                SET last_activity = CURRENT_TIMESTAMP,
                    total_turns   = GREATEST(total_turns, COALESCE($2, 0))
                WHERE id = $1
                """,
                thread_id,
                turn_number,
            )
            state = await record_input_seq(
                conn,
                unit_id=thread_id,
                unit_kind=UNIT_KIND_SESSION_TURN,
                input_seq=int(seq),
                fair_key=fair_key,
            )

        if needs_workspace_ensure:
            # Queue admission commits before this side effect. The claimant may
            # arrive first, but its internal workspace poll independently
            # suppresses cached Ready credentials until the exact Pod UID is
            # live. Always schedule sandbox reconciliation: a DB-Ready row can
            # be stale even though its lifecycle string looks terminally good.
            schedule_stateless_workspace_ensure(thread_id)
        # Post-commit watermark read (same conn): §5.3.1 response parity —
        # queue_depth comes from unconsumed watermarks, not a process queue.
        wm = await queue_depth_for(conn, unit_id=thread_id)
        queue_state = await queue_state_for(conn, unit_id=thread_id)

    queue_depth = 1 if (wm is not None and wm.has_pending_input) else 0
    # Lifecycle block (stateless_turn_resilience.md step 2): the SAME shape
    # /connection and GET …/queue return, so a parked unit is never mistaken
    # for a busy pool by the client.
    lifecycle = queue_block(queue_state, thread.get("metadata"))
    logger.info(
        "run_queue enqueue: thread=%s turn=%d input_seq=%d state=%s",
        thread_id,
        turn_number,
        int(seq),
        state,
    )
    return {
        "accepted": True,
        "turn_id": turn_number,
        "queue": {
            "state": state,
            "queue_depth": queue_depth,
            "message_id": raw_msg_id,
            "input_seq": int(seq),
            "park_reason": lifecycle["park_reason"],
            "parked_at": lifecycle["parked_at"],
            "retryable": lifecycle["retryable"],
            "attempts": lifecycle["attempts"],
            "pending_input": lifecycle["pending_input"],
        },
    }
