"""Leader-gated pod-deletion-cost reconciler for the stateless executor pool.

Design: knowledge-base/knowledge/features/capacity_ux_and_queue_autoscaling.md
§2 / "Scale-down safety". An HPA scale-down lets the ReplicaSet pick which pod
to remove, and it prefers the lowest ``controller.kubernetes.io/pod-deletion-
cost``. This loop stamps that annotation from ``run_queue`` lease state — high
while a pod holds a live lease (``leased_by`` is the claimant's pod name), low
while it idles — so a scale-down removes idle executors first and a user's
in-flight turn is not the pod that dies.

Shape mirrors ``services/run_queue_reaper.py``: leader-gated on its own
session-scoped advisory lock (``STATELESS_DELETION_COST_ID``), one dedicated
connection whose lifetime IS the leadership tenure, per-pod error containment,
cycle errors drop leadership and re-contend, and the loop never dies before
shutdown. The patch is idempotent, so a dual-leader window is harmless — the
lock only avoids duplicate work. Orchestrator-side because it already holds
``pods: patch`` RBAC and sees every claim; no agent change, no image roll.

Kubernetes access follows ``stateless_capacity.load_core_api`` (in-cluster
only unless a developer opts into a kubeconfig): unavailable → log once at
INFO and idle until shutdown, never crash.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any, Callable

from orchestrator.database.lock_ids import STATELESS_DELETION_COST_ID
from orchestrator.services.pinned_k8s_effect import run_bounded_k8s_call
from orchestrator.services.stateless_capacity import (
    DELETION_COST_ANNOTATION,
    busy_pod_names,
    env_flag,
    list_stateless_pods,
    load_core_api,
    namespace,
)

logger = logging.getLogger(__name__)

BUSY_COST = "10000"
IDLE_COST = "0"
DEFAULT_INTERVAL_SECONDS = 10.0
K8S_PATCH_REQUEST_TIMEOUT = (5.0, 15.0)


def reconciler_enabled() -> bool:
    return env_flag("STATELESS_DELETION_COST_RECONCILER_ENABLED", True)


def interval_seconds() -> float:
    raw = os.getenv("STATELESS_DELETION_COST_INTERVAL_S")
    if raw is None or not raw.strip():
        return DEFAULT_INTERVAL_SECONDS
    try:
        return max(1.0, float(raw))
    except ValueError:
        logger.warning(
            "STATELESS_DELETION_COST_INTERVAL_S=%r is not a number; using %.0fs",
            raw,
            DEFAULT_INTERVAL_SECONDS,
        )
        return DEFAULT_INTERVAL_SECONDS


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    pods: int
    busy: int
    patched: int
    failed: int


def _pod_name(pod: Any) -> str | None:
    name = getattr(getattr(pod, "metadata", None), "name", None)
    return str(name) if name else None


def _current_cost(pod: Any) -> str | None:
    annotations = getattr(getattr(pod, "metadata", None), "annotations", None) or {}
    value = annotations.get(DELETION_COST_ANNOTATION)
    return None if value is None else str(value)


def _terminating(pod: Any) -> bool:
    return (
        getattr(getattr(pod, "metadata", None), "deletion_timestamp", None) is not None
    )


async def reconcile_once(
    conn: Any, core_api: Any, *, pod_namespace: str
) -> ReconcileResult:
    """One pass: read live leases, list pool pods, patch only where it differs."""
    busy = await busy_pod_names(conn)
    pods = await list_stateless_pods(core_api, namespace=pod_namespace)
    patched = 0
    failed = 0
    for pod in pods:
        name = _pod_name(pod)
        if name is None or _terminating(pod):
            continue
        desired = BUSY_COST if name in busy else IDLE_COST
        if _current_cost(pod) == desired:
            continue
        try:
            await run_bounded_k8s_call(
                core_api.patch_namespaced_pod,
                name,
                pod_namespace,
                {"metadata": {"annotations": {DELETION_COST_ANNOTATION: desired}}},
                request_timeout=K8S_PATCH_REQUEST_TIMEOUT,
            )
            patched += 1
            logger.info(
                "stateless pod deletion-cost: pod=%s cost=%s busy=%s",
                name,
                desired,
                name in busy,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # One pod's failure (gone between list and patch, transient API
            # error) must not stop the others; the next cycle retries.
            failed += 1
            logger.warning(
                "stateless pod deletion-cost patch failed (non-fatal): pod=%s: %s",
                name,
                exc,
            )
    return ReconcileResult(
        pods=len(pods),
        busy=len(busy & {_pod_name(p) for p in pods}),
        patched=patched,
        failed=failed,
    )


async def _sleep_or_shutdown(seconds: float, shutdown_event: asyncio.Event) -> None:
    try:
        await asyncio.wait_for(shutdown_event.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        return


async def stateless_pod_deletion_cost_loop(
    db: Any,
    shutdown_event: asyncio.Event,
    *,
    interval: float | None = None,
    core_api_factory: Callable[[], Any | None] = load_core_api,
    pod_namespace: str | None = None,
) -> None:
    """Contend for the reconciler advisory lock and reconcile while holding it.

    ``db`` is the orchestrator's ``PostgresDB`` (uses ``db._pool`` directly:
    the advisory lock is session-scoped, so lock and reconcile must share one
    dedicated connection). Followers re-contend every ``interval`` seconds.
    Wired in ``main.py``'s lifespan beside ``run_queue_reaper_loop``.
    """
    interval = float(interval if interval is not None else interval_seconds())
    pod_namespace = pod_namespace or namespace()
    logger.info(
        "stateless pod deletion-cost reconciler started (interval=%.0fs namespace=%s)",
        interval,
        pod_namespace,
    )
    core_api: Any | None = None
    unavailable_logged = False
    while not shutdown_event.is_set():
        if core_api is None:
            try:
                core_api = await asyncio.to_thread(core_api_factory)
            except Exception as exc:
                core_api = None
                if not unavailable_logged:
                    logger.info(
                        "stateless pod deletion-cost reconciler: kubernetes client "
                        "failed to initialise (%s) — idle",
                        exc,
                    )
                    unavailable_logged = True
            if core_api is None:
                if not unavailable_logged:
                    logger.info(
                        "stateless pod deletion-cost reconciler: kubernetes "
                        "unavailable — idle"
                    )
                    unavailable_logged = True
                await _sleep_or_shutdown(interval, shutdown_event)
                continue
        pool = getattr(db, "_pool", None)
        if pool is None:
            await _sleep_or_shutdown(interval, shutdown_event)
            continue
        conn = None
        try:
            conn = await pool.acquire()
            got = await conn.fetchval(
                "SELECT pg_try_advisory_lock($1)", STATELESS_DELETION_COST_ID
            )
            if not got:
                await _sleep_or_shutdown(interval, shutdown_event)
                continue
            logger.info("stateless pod deletion-cost reconciler: leadership acquired")
            try:
                while not shutdown_event.is_set():
                    await reconcile_once(conn, core_api, pod_namespace=pod_namespace)
                    await _sleep_or_shutdown(interval, shutdown_event)
            finally:
                try:
                    await conn.execute(
                        "SELECT pg_advisory_unlock($1)", STATELESS_DELETION_COST_ID
                    )
                except Exception:
                    # Best-effort: the lock auto-releases with the session.
                    pass
                logger.info(
                    "stateless pod deletion-cost reconciler: leadership released"
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "stateless pod deletion-cost reconciler error (non-fatal, retrying): %s",
                exc,
            )
            await _sleep_or_shutdown(interval, shutdown_event)
        finally:
            if conn is not None:
                try:
                    await pool.release(conn)
                except Exception:
                    pass
    logger.info("stateless pod deletion-cost reconciler stopped")


__all__ = [
    "BUSY_COST",
    "IDLE_COST",
    "ReconcileResult",
    "interval_seconds",
    "reconcile_once",
    "reconciler_enabled",
    "stateless_pod_deletion_cost_loop",
]
