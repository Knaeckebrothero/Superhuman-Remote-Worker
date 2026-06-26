"""Bind an agent to a thread for the create-thread fast path.

This is the K8s-mode binding path invoked by
``main.create_thread`` as a fire-and-forget asyncio task. It owns the
lifecycle SSE emissions for that path — without these the cockpit's
startup card never sees ``provisioning``/``booting``/``ready`` because
``GET /api/sessions/{tid}/connection`` returns 200 immediately for the
warm-pool case (the idle-pool attach is synchronous inside
``create_thread``) and the cockpit's 425 fallback (which drives
``/prepare``'s lifecycle SSE) never fires.

Late imports of orchestrator singletons live inside the function body —
same pattern as ``orchestrator/routers/sessions.py::_provision_agent_for_thread`` —
so this module is unit-testable without dragging in the full ``main.py``
side-effect chain (license gate, agent provisioner, etc.).
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


async def provision_or_assign(
    uid: str,
    tid: str,
    cfg: str,
    co: dict,
    pids: list,
    ds_ids: list[str] | None,
) -> None:
    """Bind an agent to a thread and emit ``session.lifecycle`` events.

    Serialises concurrent provisioning attempts for the same thread via
    Postgres advisory lock
    (docs/issues/persistent_thread_double_provisioning_race.md).
    Idle-pool fast-path first; falls back to a dedicated pod on miss.

    Locking strategy: the advisory lock is held ONLY across the
    decide-who-provisions critical section (pool check + attach OR pod
    creation kickoff). It is released before ``wait_for_binding`` /
    ``wait_for_ready`` because the fresh-pod path's agent registers via
    ``POST /api/agents/register``, which acquires the SAME advisory lock
    for its duplicate-rejection check — holding it across the wait
    deadlocks both for the asyncpg query timeout (~60s).
    """
    from main import (  # noqa: E402  (late import — see module docstring)
        _build_datasource_tool_override,
        _build_datasources_payload,
        _find_idle_persistent_agent,
        _grant_violations_detail,
        _send_session_attach,
        _session_grant_violations,
        agent_provisioner,
        postgres_db,
    )
    from services.session_lifecycle import (
        emit as lifecycle_emit,
        wait_for_binding,
        wait_for_ready,
    )
    from services.session_provisioning_state import (
        agent_pod_provisioning_in_progress,
    )

    lifecycle_emit(uid, tid, "provisioning")
    pod_ip: str | None = None
    pod_port: int = 8001
    needs_binding_wait = False  # True iff fresh-pod path took over

    try:
        async with postgres_db.thread_advisory_lock(tid):
            cur = await postgres_db.get_thread(tid)
            if cur and cur.get("agent_id"):
                logger.info(
                    "Thread %s: already bound to agent %s — "
                    "skipping duplicate provision.",
                    tid,
                    cur["agent_id"],
                )
                # Another path (e.g. concurrent /prepare) owns the
                # remaining lifecycle emissions — exit silently rather
                # than double-emitting booting/ready.
                return

            if agent_pod_provisioning_in_progress(cur):
                logger.info(
                    "Thread %s: agent pod already provisioning — waiting for binding.",
                    tid,
                )
                needs_binding_wait = True
            else:
                # Pre-flight the capability grants BEFORE pool-attach or pod
                # spawn: a never-startable config (e.g. permission_mode above the
                # user's ceiling) would otherwise boot a dedicated pod that 403s
                # at the workspace endpoint and exits, leaving the cockpit to poll
                # /connection until its ~5m40s ready timeout. Fail fast with the
                # real reason instead.
                # docs/issues/session_permission_mode_grant_denied_ready_timeout.md
                violations = await _session_grant_violations(cur)
                if violations:
                    logger.warning(
                        "Thread %s: provisioning denied by capability grants: %s",
                        tid,
                        "; ".join(violations),
                    )
                    lifecycle_emit(
                        uid,
                        tid,
                        "failed",
                        reason=_grant_violations_detail(violations),
                    )
                    return
                # Try to attach an idle dual-mode agent from the warm pool
                # first — this is instant (no image pull or pod boot needed).
                # _send_session_attach writes threads.agent_id via the
                # orchestrator's own DB connection, so the binding is visible
                # to other lock-takers immediately on release.
                idle_agent = await _find_idle_persistent_agent()
                if idle_agent:
                    resolved_ds = await postgres_db.resolve_datasources_for_thread(
                        datasource_ids=ds_ids, project_ids=pids
                    )
                    ds_payload = _build_datasources_payload(resolved_ds)
                    attach_co = co
                    if resolved_ds:
                        attach_co = _build_datasource_tool_override(resolved_ds, co)
                    ok = await _send_session_attach(
                        idle_agent,
                        tid,
                        attach_co,
                        pids,
                        datasources=ds_payload,
                        config_name=cfg,
                    )
                    if ok:
                        logger.info(
                            "Thread %s: attached to idle pool agent %s",
                            tid,
                            idle_agent["hostname"],
                        )
                        pod_ip = idle_agent["pod_ip"]
                        pod_port = int(idle_agent.get("pod_port", 8001))
                    # else fall through to fresh-pod path

                if pod_ip is None:
                    # No idle agent (or pool attach failed) — create a
                    # dedicated session pod. Only kick off the creation here;
                    # the binding wait happens AFTER the lock is released, so
                    # the new pod's /register call can grab the same lock.
                    pod_name = await agent_provisioner.provision_agent(
                        purpose="session", thread_id=tid, config_name=cfg
                    )
                    if not pod_name:
                        logger.error(
                            "Thread %s: no idle agents and pod "
                            "provisioning failed. Check image "
                            "availability, RBAC, node resources, "
                            "or increase MAX_AGENTS.",
                            tid,
                        )
                        lifecycle_emit(
                            uid,
                            tid,
                            "failed",
                            reason="no idle agents and pod provisioning failed",
                        )
                        return
                    needs_binding_wait = True

        # Lock released. The fresh-pod path now waits for the agent's
        # /register handler to write threads.agent_id (which needs the
        # advisory lock we just dropped).
        if needs_binding_wait:
            bind_timeout_s = int(os.environ.get("AGENT_BIND_TIMEOUT_S", "300"))
            if not await wait_for_binding(tid, bind_timeout_s):
                lifecycle_emit(uid, tid, "failed", reason="agent failed to register")
                return
            thread_row = await postgres_db.get_thread(tid)
            agent = await postgres_db.get_agent(str(thread_row["agent_id"]))
            if not agent or not agent.get("pod_ip"):
                lifecycle_emit(uid, tid, "failed", reason="agent has no pod_ip")
                return
            pod_ip = agent["pod_ip"]
            pod_port = int(agent.get("pod_port", 8001))

        # Block on the actual session-ready signal so the cockpit's
        # startup card flips from "booting" to "ready" exactly when the
        # agent's _attach_session finishes (the agent's /ready is gated
        # on _session_ready()'s 3-way check). Mirrors the readiness-probe
        # tail of _do_prepare.
        lifecycle_emit(uid, tid, "booting")
        ready_timeout_s = int(os.environ.get("WS_READY_TIMEOUT_S", "180"))
        if not await wait_for_ready(pod_ip, pod_port, ready_timeout_s):
            lifecycle_emit(uid, tid, "failed", reason="agent /ready timeout")
            return

        lifecycle_emit(uid, tid, "ready")
    except Exception as e:
        logger.exception("provision_or_assign failed for thread %s: %s", tid, e)
        lifecycle_emit(uid, tid, "failed", reason=str(e))
