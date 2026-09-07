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
- knowledge-history/done/preemption_before_first_checkpoint_replays_job_opening.md
- knowledge-base/knowledge/issues/critic_failure_leaves_parent_job_stuck_reviewing.md (fix item #4)
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Awaitable, Callable

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

# Grace floor (minutes) a parent must have sat in 'reviewing' before the
# watchdog un-sticks it. Long enough to clear the critic-spawn window and any
# in-flight verdict; the real gate is "no non-failed/cancelled critic exists".
REVIEWING_STUCK_MINUTES = int(os.getenv("REVIEWING_STUCK_GRACE_MINUTES", "30"))

# Wall-clock ceiling (minutes) for the LIVE-critic arm: a parent still in
# 'reviewing' past this even though its critic is alive escalates to
# pending_review with a distinct "critic did not render a verdict" message
# (fix direction 2 of rejected_verdict_livelocks_critic_and_wedges_parent.md).
# Generous by design — a healthy long review must never be pre-empted; the
# verdict-rejection cap bounds the common livelock much earlier. 0 disables.
REVIEWING_WALLCLOCK_CEILING_MINUTES = int(
    os.getenv("REVIEWING_WALLCLOCK_CEILING_MINUTES", "60")
)

StatelessCancelFn = Callable[..., Awaitable[bool]]


async def stale_verification_sweeper_loop(
    db: Any,
    shutdown_event: asyncio.Event,
    *,
    stateless_cancel_fn: StatelessCancelFn | None = None,
    completion_commands_enabled: bool = False,
) -> None:
    """Reap orphaned verification subjobs until ``shutdown_event`` is set."""
    logger.info(
        "Stale verification sweeper started (tick=%ds, stale_hours=%d, "
        "reviewing_grace_min=%d, wallclock_ceiling_min=%d)",
        TICK_SECONDS,
        STALE_HOURS,
        REVIEWING_STUCK_MINUTES,
        REVIEWING_WALLCLOCK_CEILING_MINUTES,
    )
    while not shutdown_event.is_set():
        try:
            cancelled, unstuck = await _sweep_tick(
                db,
                STALE_HOURS,
                REVIEWING_STUCK_MINUTES,
                wallclock_minutes=REVIEWING_WALLCLOCK_CEILING_MINUTES,
                stateless_cancel_fn=stateless_cancel_fn,
                completion_commands_enabled=completion_commands_enabled,
            )
            if cancelled:
                logger.info(
                    "Stale verification sweeper cancelled %d orphaned subjob(s)",
                    cancelled,
                )
            if unstuck:
                logger.info(
                    "Stale verification sweeper un-stuck %d reviewing parent(s) "
                    "→ pending_review",
                    unstuck,
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


async def _sweep_tick(
    db: Any,
    stale_hours: int,
    grace_minutes: int,
    notifier: Any = None,
    wallclock_minutes: int | None = None,
    stateless_cancel_fn: StatelessCancelFn | None = None,
    completion_commands_enabled: bool = False,
) -> tuple[int, int]:
    """Run one sweep. Returns ``(cancelled_subjobs, unstuck_parents)``.

    Step 1 cancels dead/orphaned critic subjobs (also what turns a lingering
    'paused' orphan terminal at the stale horizon). Step 2 then un-sticks any
    parent whose critic pipeline is now dead and notifies its owner. Ordering
    matters: a critic cancelled in Step 1 makes its parent eligible in Step 2
    on this same tick. Step 3 (the wall-clock arm, only when
    ``wallclock_minutes`` > 0 — the loop passes the module config; direct
    callers default to off) escalates parents whose critic is ALIVE but has
    rendered no verdict for the whole ceiling; its count is folded into the
    returned ``unstuck_parents``.
    """
    cancelled = await db.cancel_stale_verification_subjobs(stale_hours)
    if stateless_cancel_fn is not None:
        stateless_ids = await db.list_stale_stateless_verification_subjobs(stale_hours)
        for job_id in stateless_ids:
            try:
                if await stateless_cancel_fn(job_id, stale_hours=stale_hours):
                    cancelled += 1
            except Exception:
                logger.exception(
                    "Failed to settle stale stateless verification subjob %s",
                    job_id,
                )

    completion_guard = (
        {"completion_commands_enabled": True} if completion_commands_enabled else {}
    )
    unstuck_rows = await db.unstick_reviewing_parents(grace_minutes, **completion_guard)

    escalated_rows: list[dict[str, Any]] = []
    if wallclock_minutes and wallclock_minutes > 0:
        escalated_rows = await db.unstick_reviewing_parents_wallclock(
            wallclock_minutes, **completion_guard
        )
        for row in escalated_rows:
            logger.warning(
                "Verification wall-clock ceiling (%d min) hit for parent %s — "
                "live critic rendered no verdict; escalated to pending_review",
                wallclock_minutes,
                row.get("id"),
            )

    all_rows = list(unstuck_rows) + escalated_rows
    if all_rows and notifier is None:
        # Lazy import keeps the sweeper's test import free of the
        # notification_service dependency (tests always inject a notifier).
        from orchestrator.services.notification_service import (
            notification_service as notifier,
        )

    for row in all_rows:
        try:
            await notifier.record_review_returned(
                user_id=str(row["user_id"]),
                job_id=str(row["id"]),
                config_name=row.get("config_name") or "",
            )
        except Exception:
            logger.exception(
                "Failed to notify owner of un-stuck reviewing parent %s (non-fatal)",
                row.get("id"),
            )

    return cancelled, len(all_rows)
