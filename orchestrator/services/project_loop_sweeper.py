"""Safety-net sweeper for project self-improvement loops.

A project loop advances via the ``_advance_project_loop`` completion hook when
its current job reaches a terminal state. If that hook is ever missed — the
agent process dies after marking the job terminal but before the completion
callback lands, or the advance itself throws inside the fan-out — the loop would
wedge: ``status='running'`` with a terminal ``current_job`` that never advances.

This sweeper is the backstop. Each tick it scans running loops and, for any
whose current job is already terminal, re-runs the advance. The advance is
atomic and idempotent (``claim_project_loop_advance`` nulls ``current_job_id``
under a conditional UPDATE), so a sweep that races a late completion hook is a
no-op for the loser — the next job is spawned exactly once.

Mirrors ``cron_dispatcher_loop``'s structure (tick + shutdown-aware wait).

Design: docs/features/project_self_improvement_loop.md (Phase 2).
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

# Cadence is env-tunable mainly so tests can drive ticks faster than 60s. The
# completion hook handles the happy path instantly; this is only a backstop, so
# a coarse tick is fine.
TICK_SECONDS = int(os.getenv("PROJECT_LOOP_SWEEP_SECONDS", "60"))

_TERMINAL = ("completed", "failed", "cancelled")

# advance_fn(job, result, actions) -> Awaitable — wired to _advance_project_loop.
AdvanceFn = Callable[[dict[str, Any], dict[str, Any], list[str]], Awaitable[None]]


async def project_loop_sweeper_loop(
    db: Any,
    shutdown_event: asyncio.Event,
    *,
    advance_fn: AdvanceFn,
) -> None:
    """Recover wedged project loops until ``shutdown_event`` is set."""
    logger.info("Project loop sweeper started (tick=%ds)", TICK_SECONDS)
    while not shutdown_event.is_set():
        try:
            recovered = await _sweep_tick(db, advance_fn)
            if recovered:
                logger.info(
                    "Project loop sweeper recovered %d wedged loop(s)", recovered
                )
        except Exception:
            logger.exception("Project loop sweeper tick raised; will retry next tick")

        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=TICK_SECONDS)
            break
        except asyncio.TimeoutError:
            pass

    logger.info("Project loop sweeper stopped")


async def _sweep_tick(db: Any, advance_fn: AdvanceFn) -> int:
    """Re-run the advance for any running loop whose current job is terminal.

    Returns the number of loops recovered this tick.
    """
    recovered = 0
    for loop in await db.list_running_project_loops():
        cur = loop.get("current_job_id")
        if not cur:
            # A running loop should always have an in-flight job (start/advance
            # set one). None means a crash between claim and spawn left it
            # wedged — log for attention rather than guessing a recovery.
            logger.warning(
                "project loop %s is running with no current_job_id — needs attention",
                str(loop.get("id"))[:8],
            )
            continue

        job = await db.get_job(str(cur))
        if not job:
            continue
        if job.get("status") not in _TERMINAL:
            continue  # in-flight; the completion hook will advance it

        logger.warning(
            "project loop %s: current job %s is terminal (%s) but loop still "
            "running — recovering via advance (missed completion hook)",
            str(loop.get("id"))[:8],
            str(cur)[:8],
            job.get("status"),
        )
        try:
            # result={} → _advance_project_loop derives failure from job.status,
            # so terminal-success and terminal-failure are both handled right.
            await advance_fn(job, {}, [])
            recovered += 1
        except Exception:
            logger.exception("project loop %s: sweeper advance failed", loop.get("id"))
    return recovered
