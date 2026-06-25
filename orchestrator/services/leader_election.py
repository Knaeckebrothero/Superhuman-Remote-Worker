"""Single-leader election via a session-scoped Postgres advisory lock (M1).

One ``run_as_leader`` task per orchestrator replica tries to hold ``LEADER_ID``
on a dedicated pooled connection. While held, the module-level ``is_leader``
event is set and the singleton background loops do their work; otherwise they
idle. On the leader's death the Postgres session ends and the lock
auto-releases, so a follower takes over within ~one poll interval. (Hard-kill
failover speed also depends on Postgres TCP keepalives — see the M1 chart
change; the default OS keepalive is ~2h.)

Why session-scoped (``pg_advisory_lock``), not transaction-scoped: leadership
must outlive any single transaction. This is the only session-scoped advisory
lock in the codebase; all others are ``pg_advisory_xact_lock``.

Pattern mirrors ``services/cloud/reload.py:run_listen_loop`` (dedicated
out-of-pool connection + reconnect-with-backoff + cooperative shutdown).

IMPORTANT: requires a direct/session-pooled Postgres connection. A
transaction-mode pooler (PgBouncer/pgcat/RDS-Proxy txn mode) silently breaks
session advisory locks. SRW connects direct asyncpg; external deployments must
not interpose a transaction-mode pooler (documented in helm/values.yaml).
"""
import asyncio
import logging
import random
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

_POLL_INTERVAL_SECONDS = 10.0
_RECONNECT_MIN_SECONDS = 1.0
_RECONNECT_MAX_SECONDS = 30.0
_STARTUP_JITTER_SECONDS = 3.0

# Set iff THIS replica currently holds leadership. Singleton background loops
# import this and gate their per-tick work on it. Module-level = per-process =
# per-replica, which is exactly what we want.
is_leader = asyncio.Event()


async def _sleep_or_shutdown(seconds: float, shutdown_event: asyncio.Event) -> None:
    """Sleep up to ``seconds``, waking immediately on shutdown."""
    try:
        await asyncio.wait_for(shutdown_event.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        return


async def run_as_leader(db: Any, lock_id: int, shutdown_event: asyncio.Event) -> None:
    """Contend for leadership and hold it for this process's lifetime.

    Parameters
    ----------
    db:
        The orchestrator's ``PostgresDB`` instance (uses ``db._pool``).
    lock_id:
        The session-scoped advisory-lock key (``lock_ids.LEADER_ID``).
    shutdown_event:
        Cooperative shutdown flag; the loop exits cleanly when set.
    """
    backoff = _RECONNECT_MIN_SECONDS
    # Anti-thundering-herd: stagger co-booting replicas so they don't all
    # contend for the lock on the same tick.
    await _sleep_or_shutdown(random.uniform(0.0, _STARTUP_JITTER_SECONDS), shutdown_event)

    while not shutdown_event.is_set():
        conn = None
        try:
            if db._pool is None:
                # Orchestrator hasn't finished booting; wait and retry.
                await _sleep_or_shutdown(backoff, shutdown_event)
                backoff = min(backoff * 2, _RECONNECT_MAX_SECONDS)
                continue

            conn = await db._pool.acquire()
            got = await conn.fetchval("SELECT pg_try_advisory_lock($1)", lock_id)
            backoff = _RECONNECT_MIN_SECONDS

            if got:
                logger.info("leader_election: acquired leadership (lock %s)", lock_id)
                is_leader.set()
                try:
                    # Hold leadership until shutdown. The periodic SELECT 1
                    # keeps the connection warm (so a server-side
                    # idle_session_timeout can't reap a *live* leader) and
                    # raises promptly if the connection died — which drops us
                    # out to step down and re-contend.
                    while not shutdown_event.is_set():
                        await _sleep_or_shutdown(_POLL_INTERVAL_SECONDS, shutdown_event)
                        if shutdown_event.is_set():
                            break
                        await conn.fetchval("SELECT 1")
                finally:
                    is_leader.clear()
                    try:
                        await conn.execute("SELECT pg_advisory_unlock($1)", lock_id)
                    except Exception:
                        # Best-effort: the lock also auto-releases when the
                        # session ends (e.g. if the connection is already dead).
                        pass
                    logger.info("leader_election: released leadership")
            else:
                # Follower: ensure the flag is clear and poll for takeover.
                is_leader.clear()
                await _sleep_or_shutdown(_POLL_INTERVAL_SECONDS, shutdown_event)
        except asyncio.CancelledError:
            is_leader.clear()
            raise
        except Exception as e:
            # Connection lost / DB blip: step down and reconnect with backoff.
            is_leader.clear()
            logger.warning(
                "leader_election: connection lost (%s); stepping down, retry in %.1fs",
                e,
                backoff,
            )
        finally:
            if conn is not None:
                try:
                    await db._pool.release(conn)
                except Exception:
                    pass

        if shutdown_event.is_set():
            break
        await _sleep_or_shutdown(backoff, shutdown_event)
        backoff = min(backoff * 2, _RECONNECT_MAX_SECONDS)

    is_leader.clear()
    logger.info("leader_election: shutdown")


async def run_when_leader(
    make_coro: Callable[[asyncio.Event], Awaitable[None]],
    shutdown_event: asyncio.Event,
    poll_seconds: float = 1.0,
) -> None:
    """Run a background loop only while this replica holds leadership.

    ``make_coro(shutdown_event)`` returns the loop coroutine; it is started when
    leadership is acquired and cancelled when leadership is lost (detected
    within ``poll_seconds``) or on shutdown. Loop side effects must be
    idempotent / CAS-guarded — a loop cancelled mid-tick may be retried by the
    next leader.
    """
    task: asyncio.Task | None = None
    try:
        while not shutdown_event.is_set():
            # Wait until we're the leader (or shutting down).
            while not is_leader.is_set() and not shutdown_event.is_set():
                await _sleep_or_shutdown(poll_seconds, shutdown_event)
            if shutdown_event.is_set():
                break
            # Run the loop until we lose leadership / shut down / it exits.
            task = asyncio.create_task(make_coro(shutdown_event))
            while (
                is_leader.is_set()
                and not shutdown_event.is_set()
                and not task.done()
            ):
                await _sleep_or_shutdown(poll_seconds, shutdown_event)
            if not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            task = None
    finally:
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
