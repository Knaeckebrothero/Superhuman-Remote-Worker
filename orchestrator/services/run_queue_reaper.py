"""Leader-gated run_queue lease reaper (stateless-agents S1, doc §5.2).

Steals expired leases via the shared per-row CAS in ``src/shared/run_queue``
and, for session units, writes the post-steal journal signal so the user never
gets silent dead air: one transaction per stolen unit bumps
``threads.events_epoch`` (fencing any zombie journal writer out — the client
re-anchors via ``gone_beyond_horizon``) and appends a ``turn.interrupted`` /
``turn.parked`` system frame. Post-steal is a sanctioned writer-exclusion
context for ``append_system_frame``: the steal's ``lease_token`` bump already
fenced the previous holder's persists, and the epoch bump retires its
in-memory seq counter (docs/features/stateless_agents.md §5.3.2, system-writer
class).

Leadership is a session-scoped advisory lock (``RUN_QUEUE_REAPER_ID``,
``orchestrator/database/lock_ids.py``) held on the same pooled connection the
sweep runs on, mirroring ``services/leader_election.py``'s shape: lock dies
with the session, a follower takes over within ~one interval. The lock only
avoids duplicate sweep WORK — correctness under the documented dual-leader
window comes from ``reap_expired``'s per-row CAS (each steal is returned to
exactly one caller, and the journal write is keyed off that returned record).

Error containment mirrors ``thread_events_prune_sweeper``: per-row failures
are logged and skipped, per-cycle failures drop leadership and re-contend,
and the loop itself never dies before shutdown.

Non-session unit kinds (``worker_batch``, ``bg_task``) are reaped only — no
journal writes; workers have no ``thread_events`` journal until S3 (doc §5.5).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from database.lock_ids import RUN_QUEUE_REAPER_ID
from src.shared.event_journal import append_system_frame, bump_epoch
from src.shared.run_queue import (
    REAPER_GRACE_SECONDS,
    REAPER_INTERVAL_SECONDS,
    STATE_QUEUED,
    UNIT_KIND_SESSION_TURN,
    StolenUnit,
    reap_expired,
)

logger = logging.getLogger(__name__)


async def _sleep_or_shutdown(seconds: float, shutdown_event: asyncio.Event) -> None:
    """Sleep up to ``seconds``, waking immediately on shutdown."""
    try:
        await asyncio.wait_for(shutdown_event.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        return


async def _journal_steal(conn: Any, unit: StolenUnit) -> None:
    """Post-steal journal write for ONE session unit — one transaction.

    ``bump_epoch`` first (fences the zombie's journal writer and resets the
    seq high-water mark), then the system frame lands at (new_epoch, 1). Both
    in the same transaction so a crash between them cannot leave a bumped
    epoch with no explanatory frame. ``unit.state`` is the post-steal state:
    ``'queued'`` → the turn will be retried (``turn.interrupted``);
    ``'parked'`` → attempts exhausted, operator unpark required
    (``turn.parked``).
    """
    thread_id = str(unit.unit_id)
    kind = "turn.interrupted" if unit.state == STATE_QUEUED else "turn.parked"
    async with conn.transaction():
        await bump_epoch(conn, thread_id=thread_id)
        await append_system_frame(
            conn,
            thread_id=thread_id,
            kind=kind,
            payload={
                "reason": "lease_expired",
                "attempts": unit.attempts_since_completion,
                "stolen_from": unit.leased_by,
            },
        )


async def reap_cycle(
    conn: Any,
    *,
    grace_seconds: float = REAPER_GRACE_SECONDS,
) -> int:
    """One sweep: steal expired leases, journal the session ones. Returns the
    steal count.

    Runs OUTSIDE any enclosing transaction (``reap_expired``'s contract: each
    per-row CAS steal commits independently); only the per-unit journal write
    opens its own transaction. Per-row errors are contained so one bad unit
    never blocks the rest of the pass.
    """
    stolen = await reap_expired(conn, grace_seconds=grace_seconds)
    for unit in stolen:
        # Greppable ops line — one per steal (M6 fault-injection anchors on it).
        logger.info(
            "run_queue steal: unit=%s kind=%s pod=%s attempts=%d new_state=%s",
            unit.unit_id,
            unit.unit_kind,
            unit.leased_by,
            unit.attempts_since_completion,
            unit.state,
        )
        if unit.unit_kind != UNIT_KIND_SESSION_TURN:
            # Reap only — no journal for non-session kinds (S3).
            continue
        try:
            await _journal_steal(conn, unit)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Contained: the steal itself already committed (lease fenced);
            # only the user-visible journal signal was lost for this unit.
            logger.exception(
                "run_queue reaper: journal write failed for unit %s (contained)",
                unit.unit_id,
            )
    return len(stolen)


async def run_queue_reaper_loop(
    db: Any,
    shutdown_event: asyncio.Event,
    *,
    interval_seconds: float = float(REAPER_INTERVAL_SECONDS),
    grace_seconds: float = float(REAPER_GRACE_SECONDS),
) -> None:
    """Contend for the reaper advisory lock and sweep while holding it.

    ``db`` is the orchestrator's ``PostgresDB`` (uses ``db._pool`` directly:
    the advisory lock is session-scoped, so lock and sweep must share one
    dedicated connection whose lifetime IS the leadership tenure). Followers
    re-contend every ``interval_seconds``. Wired in ``main.py``'s lifespan
    beside ``thread_events_prune_sweeper`` and stopped via the same
    ``shutdown_event``.
    """
    logger.info(
        "run_queue reaper started (interval=%.0fs grace=%.0fs)",
        interval_seconds,
        grace_seconds,
    )
    while not shutdown_event.is_set():
        pool = getattr(db, "_pool", None)
        if pool is None:
            # Orchestrator still booting; retry next interval.
            await _sleep_or_shutdown(interval_seconds, shutdown_event)
            continue
        conn = None
        try:
            conn = await pool.acquire()
            got = await conn.fetchval(
                "SELECT pg_try_advisory_lock($1)", RUN_QUEUE_REAPER_ID
            )
            if not got:
                await _sleep_or_shutdown(interval_seconds, shutdown_event)
                continue
            logger.info("run_queue reaper: leadership acquired")
            try:
                while not shutdown_event.is_set():
                    await reap_cycle(conn, grace_seconds=grace_seconds)
                    await _sleep_or_shutdown(interval_seconds, shutdown_event)
            finally:
                try:
                    await conn.execute(
                        "SELECT pg_advisory_unlock($1)", RUN_QUEUE_REAPER_ID
                    )
                except Exception:
                    # Best-effort: the lock auto-releases with the session.
                    pass
                logger.info("run_queue reaper: leadership released")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # Connection lost / DB blip / cycle-level failure: drop leadership
            # (lock releases with the session) and re-contend after a beat.
            logger.warning("run_queue reaper error (non-fatal, retrying): %s", e)
            await _sleep_or_shutdown(interval_seconds, shutdown_event)
        finally:
            if conn is not None:
                try:
                    await pool.release(conn)
                except Exception:
                    pass
    logger.info("run_queue reaper stopped")
