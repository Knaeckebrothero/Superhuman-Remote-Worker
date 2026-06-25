"""Background sweeper that reaps orphaned verification (critic) subjobs.

Critic subjobs spawned by the completion fan-out can be orphaned — left
``status IN ('created','paused')``, agentless, with a terminal parent — when
their shared workspace is reaped mid-review or their parent simply completes.
They then linger in the dispatchable set at priority 10 and **parasitically
preempt** real jobs every dispatcher cycle without ever making progress (the
source of the loop "duplication" incident). This sweeper cancels them at the
source so they stop accumulating and stop preempting.

Mirrors ``project_loop_sweeper``'s structure (pure ``_sweep_tick`` + a
shutdown-aware loop). The DB predicate lives in
``PostgresDB.cancel_stale_verification_subjobs``.

See:
- docs/issues/preemption_before_first_checkpoint_replays_job_opening.md
- docs/issues/critic_failure_leaves_parent_job_stuck_reviewing.md (fix item #4)
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# Cadence is env-tunable mainly so tests / local verification can drive ticks
# faster than the default. Reaping is not urgent — the dispatcher placeability
# guard already neutralizes the parasitic preemption in real time; this just
# cleans up the rows.
TICK_SECONDS = int(os.getenv("STALE_VERIFICATION_SWEEP_SECONDS", "300"))

# A subjob whose parent is terminal is reaped immediately; this is the fallback
# horizon for an agentless subjob whose parent is merely stuck (e.g. 'reviewing')
# so a wedged review eventually stops preempting without prematurely killing a
# live one.
STALE_HOURS = int(os.getenv("STALE_VERIFICATION_HOURS", "6"))


async def stale_verification_sweeper_loop(
    db: Any, shutdown_event: asyncio.Event
) -> None:
    """Reap orphaned verification subjobs until ``shutdown_event`` is set."""
    logger.info(
        "Stale verification sweeper started (tick=%ds, stale_hours=%d)",
        TICK_SECONDS,
        STALE_HOURS,
    )
    while not shutdown_event.is_set():
        try:
            cancelled = await _sweep_tick(db, STALE_HOURS)
            if cancelled:
                logger.info(
                    "Stale verification sweeper cancelled %d orphaned subjob(s)",
                    cancelled,
                )
        except Exception:
            logger.exception(
                "Stale verification sweeper tick raised; will retry next tick"
            )

        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=TICK_SECONDS)
            break
        except asyncio.TimeoutError:
            pass

    logger.info("Stale verification sweeper stopped")


async def _sweep_tick(db: Any, stale_hours: int) -> int:
    """Cancel stale verification subjobs once. Returns the number cancelled."""
    return await db.cancel_stale_verification_subjobs(stale_hours)
