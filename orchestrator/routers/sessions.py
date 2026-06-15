"""``/api/sessions`` — prepare endpoint (Task 6) + connection endpoint (Task 7).

These two endpoints replace the WS handshake's pre-flight work that used to
live inline in ``orchestrator/main.py``'s ``persistent_ws_proxy``.

  - ``POST /api/sessions/{thread_id}/prepare`` — slow path. Auth, ownership,
    provisioning, readiness. Returns 202 immediately; progress goes via the
    existing SSE notification feed on event type ``session.lifecycle``.
    Idempotent: a concurrent retry blocks on a Postgres advisory lock keyed
    by thread_id and returns the in-flight call's result.

Spec: docs/features/direct_session_websockets.md §Component details.
Pattern: late imports of postgres_db (and other singletons) inside handler
bodies to avoid circular imports at module load time — same pattern as
orchestrator/routers/automations.py.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from security.auth import require_approved_user
from services.session_lifecycle import emit as lifecycle_emit
from services.session_lifecycle import probe_ready, wait_for_binding, wait_for_ready
from services.session_provisioning_state import agent_pod_provisioning_in_progress

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/sessions", tags=["Sessions"])


def _get_db() -> Any:
    """Late-resolve the postgres_db singleton from main.

    Wrapped in a function so tests can monkeypatch this single symbol
    instead of having to patch the late `from main import postgres_db`
    inside the handler body.
    """
    from main import postgres_db  # type: ignore

    return postgres_db


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #


class PrepareRequest(BaseModel):
    config_name: str | None = Field(None, max_length=120)
    config_override: dict[str, Any] | None = None


class PrepareResponse(BaseModel):
    state: str = Field(..., examples=["provisioning"])


def _schedule_prepare_task(coro: Any) -> asyncio.Task[Any]:
    return asyncio.create_task(coro)


# --------------------------------------------------------------------------- #
# POST /api/sessions/{thread_id}/prepare
# --------------------------------------------------------------------------- #


@router.post(
    "/{thread_id}/prepare",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=PrepareResponse,
)
async def prepare_session(
    request: Request,
    thread_id: str,
    body: PrepareRequest,
):
    """Kick off (or rejoin) provisioning for the given thread.

    Returns 202 immediately. The caller subscribes to the SSE notification
    feed and waits for ``session.lifecycle`` events with state=ready, then
    calls GET /api/sessions/{tid}/connection for the token.
    """
    db = _get_db()
    user = await require_approved_user(request, db)

    thread = await db.get_thread(thread_id)
    if not thread:
        raise HTTPException(status_code=404, detail="thread not found")
    if str(thread.get("user_id") or "") != str(user["id"]):
        raise HTTPException(status_code=403, detail="thread access denied")

    # Fire-and-forget the actual work in a background task. Progress reaches
    # the cockpit via SSE. Idempotency is enforced by the advisory lock
    # inside _do_prepare.
    _schedule_prepare_task(
        _do_prepare(
            thread_id=thread_id,
            user_id=str(user["id"]),
            config_name=body.config_name or thread.get("config_name"),
            config_override=body.config_override,
        )
    )

    return PrepareResponse(state="provisioning")


async def _do_prepare(
    thread_id: str,
    user_id: str,
    config_name: str | None,
    config_override: dict[str, Any] | None,
) -> None:
    """Run the actual provisioning + readiness work asynchronously.

    Serializes concurrent prepares on the same thread via advisory lock.
    Broadcasts ``session.lifecycle`` SSE events at each phase change.

    Locking strategy: the advisory lock is held ONLY across the
    decide-who-provisions critical section. ``wait_for_binding`` and
    ``wait_for_ready`` run AFTER the lock is released — the fresh-pod
    path's agent registers via ``POST /api/agents/register``, which
    acquires the SAME advisory lock for its duplicate-rejection check,
    so holding it across the wait deadlocks both for the asyncpg query
    timeout (~60s).
    """
    db = _get_db()

    def _emit(state: str, **extra: Any) -> None:
        lifecycle_emit(user_id, thread_id, state, **extra)

    # Emit "provisioning" up-front so the cockpit's progress card surfaces
    # the phase even when a sibling path (POST /resume) wins the race and
    # has the agent_id set by the time we acquire the lock. The state names
    # describe phases of the request, not of the underlying work — if the
    # agent is already bound, we still went through a "preparing" phase
    # before we got there.
    _emit("provisioning")
    needs_binding_wait = False
    try:
        async with db.thread_advisory_lock(thread_id):
            thread = await db.get_thread(thread_id)
            if not thread:
                _emit("failed", reason="thread vanished")
                return

            # Provisioning (if needed). Only kick off the bind here; the
            # wait happens after the lock is released so the new pod's
            # /register can acquire it.
            if not thread.get("agent_id"):
                # Re-provisioning the agent (cold start / reopen): also reconcile
                # the session workspace. ensure_workspace's drift probe recreates
                # a 'ready'-but-dead pod (e.g. one destroyed by a cluster
                # restart), restores a 'suspended' one, and creates a
                # missing/failed one — so the fresh agent binds to a live
                # workspace instead of SSH-looping a dead address. Fire-and-forget
                # (mirrors the resume path in main.py); the agent tolerates a
                # not-yet-ready workspace.
                from main import (  # type: ignore
                    container_provisioner,
                    ensure_session_workspace,
                    postgres_db,
                    workspace_suspension_service,
                )

                asyncio.create_task(
                    ensure_session_workspace(
                        thread_id,
                        db=postgres_db,
                        provisioner=container_provisioner,
                        suspension=workspace_suspension_service,
                    )
                )

                if agent_pod_provisioning_in_progress(thread):
                    logger.info(
                        "Thread %s: agent pod already provisioning — "
                        "waiting for binding.",
                        thread_id,
                    )
                else:
                    await _provision_agent_for_thread(
                        thread_id=thread_id,
                        config_name=config_name or "persistent_defaults",
                        config_override=config_override,
                    )
                needs_binding_wait = True

        # Lock released. For fresh-pod paths, wait for the agent's
        # /register to write threads.agent_id (which needs the lock we
        # just dropped). For idle-pool paths, _send_session_attach has
        # already set agent_id via the orchestrator's own DB connection,
        # so wait_for_binding returns immediately.
        if needs_binding_wait:
            bind_timeout_s = int(os.environ.get("AGENT_BIND_TIMEOUT_S", "300"))
            if not await wait_for_binding(thread_id, bind_timeout_s):
                _emit("failed", reason="agent failed to register")
                return

        # Readiness probe.
        _emit("booting")
        thread = await db.get_thread(thread_id)
        agent_id = thread["agent_id"]
        agent = await db.get_agent(str(agent_id))
        if not agent or not agent.get("pod_ip"):
            _emit("failed", reason="agent has no pod_ip")
            return

        ready_timeout_s = int(os.environ.get("WS_READY_TIMEOUT_S", "180"))
        if not await wait_for_ready(
            pod_ip=agent["pod_ip"],
            pod_port=int(agent.get("pod_port", 8001)),
            timeout_s=ready_timeout_s,
        ):
            _emit("failed", reason="agent /ready timeout")
            return

        # Create the route resource.
        from main import session_router  # type: ignore

        await session_router.ensure_route(
            thread_id=thread_id,
            pod_name=agent["hostname"],
            pod_uid=agent.get("pod_uid", ""),
        )

        _emit("ready")
    except Exception as e:
        logger.exception("prepare failed for thread %s: %s", thread_id, e)
        _emit("failed", reason=str(e))


async def _provision_agent_for_thread(
    thread_id: str,
    config_name: str,
    config_override: dict[str, Any] | None,
) -> None:
    """Trigger pool-first then create-pod provisioning.

    Migrated from main.py:_ws_provision (the inline helper that used to live
    inside persistent_ws_proxy at main.py:13851-13884).
    """
    from main import (
        _find_idle_persistent_agent,
        _send_session_attach,
        agent_provisioner,
    )

    idle_agent = await _find_idle_persistent_agent()
    if idle_agent:
        ok = await _send_session_attach(
            idle_agent, thread_id, config_override or {}, [], datasources=None
        )
        if ok:
            return

    await agent_provisioner.provision_agent(
        purpose="session", thread_id=thread_id, config_name=config_name
    )


# --------------------------------------------------------------------------- #
# GET /api/sessions/{thread_id}/connection
# --------------------------------------------------------------------------- #


class ConnectionResponse(BaseModel):
    state: str = Field(..., examples=["ready"])
    ws_url: str
    token: str
    expires_at: int


@router.get(
    "/{thread_id}/connection",
    response_model=ConnectionResponse,
)
async def get_connection(
    request: Request,
    thread_id: str,
):
    """Return the canonical {ws_url, token, expires_at} for a bound session.

    Same payload shape used by cold-start (after SSE "ready") and warm
    reconnect — one token-mint code path on the orchestrator, one consumer
    code path on the cockpit.
    """
    db = _get_db()
    user = await require_approved_user(request, db)

    thread = await db.get_thread(thread_id)
    if not thread:
        raise HTTPException(status_code=404, detail="thread not found")
    if str(thread.get("user_id") or "") != str(user["id"]):
        raise HTTPException(status_code=403, detail="thread access denied")

    agent_id = thread.get("agent_id")
    if not agent_id:
        # Not bound yet — caller should POST /prepare.
        raise HTTPException(status_code=425, detail="session not ready")

    agent = await db.get_agent(str(agent_id))

    # Self-heal a stale binding. A bound agent that is gone (row GC'd) or
    # 'offline' (dead pod — the heartbeat reaper flips it) is terminal: clear
    # agent_id and return 425 so the cockpit's _resolveConnection POSTs
    # /prepare and re-provisions, instead of polling a 409 forever. A 'booting'
    # agent is NOT dead — it falls through to the 409s below and the cockpit
    # keeps polling (normal cold start).
    if agent is None or agent.get("status") == "offline":
        await db.update_thread_agent(thread_id, None)
        raise HTTPException(status_code=425, detail="session not ready")

    if not agent.get("pod_ip"):
        raise HTTPException(status_code=409, detail="agent unavailable")
    if agent.get("status") not in ("ready", "working", "session"):
        raise HTTPException(status_code=409, detail="agent not ready")

    # Verify the agent is actually session-ready before minting a token.
    # ``agent.status`` is set by the heartbeat (~60s lag), and an idle
    # pool agent that just received SESSION_ATTACH still reads "ready"
    # for that window even though its ``_attach_session`` hasn't
    # finished. For fresh-pod path, Uvicorn doesn't even start serving
    # until ``_attach_session`` completes — so the WS would 503 at
    # Traefik until K8s sees the pod's /ready flip true and updates
    # endpoints. ``probe_ready`` is the truthful signal here: 425 makes
    # the cockpit's ``_pollConnectionUntilReady`` wait (180s window)
    # instead of opening WS into the void and burning through its
    # 8-attempt reconnect budget before K8s converges.
    pod_port_int = int(agent.get("pod_port", 8001))
    if not await probe_ready(str(agent["pod_ip"]), pod_port_int):
        raise HTTPException(status_code=425, detail="session not ready")

    # Make /connection self-healing: any code path that binds an agent to a
    # thread (POST /prepare, the legacy resume in main.py, orchestrator restart
    # re-binding from DB) must end up routable. ensure_route is idempotent and
    # tolerates concurrent-create races, so calling it here guarantees the
    # Service + Ingress exist by the time the cockpit opens the WS — no matter
    # which path bound the agent.
    from main import session_router, session_tokens  # type: ignore

    await session_router.ensure_route(
        thread_id=thread_id,
        pod_name=str(agent.get("hostname") or ""),
        pod_uid=str(agent.get("pod_uid") or ""),
    )

    token, expires_at = session_tokens.mint(
        user_id=str(user["id"]),
        thread_id=thread_id,
    )

    host = os.environ.get("SESSION_INGRESS_HOST", "api.example.com")
    ws_url = f"wss://{host}/p/{thread_id}/ws?t={token}"

    return ConnectionResponse(
        state="ready",
        ws_url=ws_url,
        token=token,
        expires_at=expires_at,
    )
