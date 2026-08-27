"""In-flight LangGraph checkpoint retention.

The terminal prune (``PostgresDB.delete_checkpoint_thread``, fired from
``update_job_status``) only runs once a job reaches a terminal status. A job that
runs for hours therefore accumulates a full state blob per super-step for its
ENTIRE life, and concurrent long jobs can fill the checkpointer's Postgres PVC
before any of them terminates (observed 2026-07-23 and again 2026-07-27 — the
16Gi volume refilled to ~15GB of ``checkpoint_blobs`` in 4 days).

This sweeper is the *primary* defense: periodically cap every thread to the
newest ``CHECKPOINT_RETENTION_KEEP`` checkpoints via
``PostgresDB.prune_checkpoints_keep_last`` — bounding live threads *while they
run*. Leader-gated so the two HA replicas don't run the global prune at once.

The leader check is **injected** (``is_leader_fn``) rather than imported, so this
module has no dependency on ``leader_election`` and can't accidentally bind a
different ``is_leader`` Event than ``main`` under the repo's dual import paths
(``services.x`` at runtime vs ``orchestrator.services.x`` under pytest).

Config (env):
- ``CHECKPOINT_RETENTION_KEEP``       — checkpoints kept per (thread, ns). Default 3.
- ``CHECKPOINT_RETENTION_INTERVAL_S`` — sweep interval seconds. Default 600.

NOTE: a DELETE frees pages for reuse but does not shrink the PVC; a one-time
``VACUUM FULL`` is still needed to reclaim disk after the first prune of a
grossly-bloated table (see the design doc).
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Callable

logger = logging.getLogger(__name__)


def _keep_n() -> int:
    try:
        return int(os.environ.get("CHECKPOINT_RETENTION_KEEP", "3"))
    except ValueError:
        return 3


def _interval_s() -> int:
    try:
        return int(os.environ.get("CHECKPOINT_RETENTION_INTERVAL_S", "600"))
    except ValueError:
        return 600


async def retention_tick(db: Any, *, leader: bool) -> int:
    """Run one retention pass. Prunes only when ``leader`` is True (so the two HA
    replicas don't run the global prune concurrently). Returns rows deleted
    (0 when skipped or nothing to prune)."""
    if not leader:
        return 0
    return await db.prune_checkpoints_keep_last(_keep_n())


async def run_retention_sweeper(
    db: Any,
    shutdown_event: asyncio.Event,
    is_leader_fn: Callable[[], bool],
) -> None:
    """Periodic leader-gated checkpoint-retention loop.

    ``is_leader_fn`` is called each tick for live leadership state (pass
    ``main``'s ``is_leader.is_set``). Best-effort: survives transient DB errors by
    logging and continuing. Mirrors ``security_events_prune_sweeper``.
    """
    interval_s = _interval_s()
    logger.info(
        "Checkpoint retention sweeper started (interval=%ds, keep=%d)",
        interval_s,
        _keep_n(),
    )
    while not shutdown_event.is_set():
        try:
            deleted = await retention_tick(db, leader=bool(is_leader_fn()))
            if deleted:
                logger.info(
                    "checkpoint retention: pruned %d rows (keep_last=%d)",
                    deleted,
                    _keep_n(),
                )
        except Exception as e:
            logger.warning("checkpoint retention error (non-fatal): %s", e)
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=float(interval_s))
            break
        except asyncio.TimeoutError:
            pass
    logger.info("Checkpoint retention sweeper stopped")
