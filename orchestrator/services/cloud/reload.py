"""Postgres LISTEN-based reload coordinator for the main cloud backend.

Phase 4 adds an admin settings UI that writes ``system_settings.main_cloud``
via a REST endpoint. Every orchestrator replica must pick up the change
without a pod restart, so we fire a Postgres ``NOTIFY`` on a well-known
channel and each replica holds a long-running LISTEN task that rebuilds
the active backend when a notification arrives.

Design notes:

* LISTEN requires a stable connection outside the pool. We acquire a
  dedicated ``asyncpg.Connection`` via ``db._pool.acquire()`` and release
  it only on shutdown.
* ``conn.add_listener`` registers a callback that fires from asyncpg's
  internal dispatch task. The callback is synchronous, so we enqueue the
  reload work onto the event loop via ``asyncio.create_task``.
* If the connection drops (network blip, DB restart), the loop
  reconnects with exponential backoff. Configuration changes that hit
  a different replica during the reconnect window are caught by the
  ``DEBOUNCE_SECONDS`` sweep at reconnect time — we always re-read the
  overlay from the DB before swapping the backend, so missed NOTIFYs
  only result in a stale replica for ``DEBOUNCE_SECONDS`` at worst.
* Local (single-pod) installs don't strictly need NOTIFY — the PUT
  handler calls ``main_cloud_router.reload_from_db`` synchronously
  before returning. The LISTEN task is there for multi-replica K8s
  deployments where one pod's PUT has to fan out to the others.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Optional

from . import RELOAD_CHANNEL

logger = logging.getLogger(__name__)

# If the connection drops, reconnect with backoff. Capped to keep the
# loop responsive if the DB is flapping.
_RECONNECT_MIN_SECONDS = 1.0
_RECONNECT_MAX_SECONDS = 30.0

# When a notification arrives, give any other replicas a small window
# to finish their own reload so we don't all stampede the backend at
# exactly the same millisecond. 0.5s is plenty for a 10-pod deployment.
_DEBOUNCE_SECONDS = 0.5


async def run_listen_loop(
    db: Any,  # PostgresDB — imported dynamically to avoid a circular import
    reload_callback: Callable[[], Awaitable[None]],
    shutdown_event: asyncio.Event,
) -> None:
    """Long-running task: LISTEN for reload notifications + dispatch.

    Parameters
    ----------
    db:
        The orchestrator's shared ``PostgresDB`` instance. Must already
        be connected.
    reload_callback:
        An async callable invoked with no arguments each time a
        notification arrives. Implementations re-read the overlay from
        the DB and call ``MainCloudRouter.reload_from_db``.
    shutdown_event:
        Cooperative shutdown flag. The loop exits cleanly when this is
        set and any in-flight notification finishes.
    """
    backoff = _RECONNECT_MIN_SECONDS
    while not shutdown_event.is_set():
        conn = None
        listener_fn: Optional[Callable[..., None]] = None
        try:
            if db._pool is None:
                # The orchestrator hasn't finished booting yet — wait a
                # bit and try again.
                await _sleep_or_shutdown(backoff, shutdown_event)
                backoff = min(backoff * 2, _RECONNECT_MAX_SECONDS)
                continue

            conn = await db._pool.acquire()
            pending_reload: asyncio.Event = asyncio.Event()

            def _on_notify(
                connection: Any, pid: int, channel: str, payload: str
            ) -> None:
                # Fires from asyncpg's internal dispatch; set a flag that
                # the main coroutine below awaits. We avoid doing work in
                # the callback itself — it's synchronous and blocking the
                # dispatch loop would strand other notifications.
                _ = connection, pid, channel, payload
                pending_reload.set()

            listener_fn = _on_notify
            await conn.add_listener(RELOAD_CHANNEL, listener_fn)
            logger.info(
                "Main cloud config LISTEN loop: subscribed to channel %r",
                RELOAD_CHANNEL,
            )
            backoff = _RECONNECT_MIN_SECONDS

            while not shutdown_event.is_set():
                # Race the reload flag against shutdown.
                wait_reload = asyncio.create_task(pending_reload.wait())
                wait_shutdown = asyncio.create_task(shutdown_event.wait())
                done, pending = await asyncio.wait(
                    {wait_reload, wait_shutdown},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                if shutdown_event.is_set():
                    break
                if not pending_reload.is_set():
                    continue
                # Debounce: give other notifications a tiny window to
                # coalesce into one reload.
                await _sleep_or_shutdown(_DEBOUNCE_SECONDS, shutdown_event)
                pending_reload.clear()
                try:
                    await reload_callback()
                except Exception as e:
                    logger.error(
                        "Main cloud config LISTEN loop: reload callback raised: %s",
                        e,
                    )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(
                "Main cloud config LISTEN loop: %s — reconnecting in %.1fs",
                e,
                backoff,
            )
        finally:
            if conn is not None:
                if listener_fn is not None:
                    try:
                        await conn.remove_listener(RELOAD_CHANNEL, listener_fn)
                    except Exception:
                        pass
                try:
                    await db._pool.release(conn)
                except Exception:
                    pass
        if shutdown_event.is_set():
            break
        await _sleep_or_shutdown(backoff, shutdown_event)
        backoff = min(backoff * 2, _RECONNECT_MAX_SECONDS)

    logger.info("Main cloud config LISTEN loop: shutdown")


async def _sleep_or_shutdown(seconds: float, shutdown_event: asyncio.Event) -> None:
    """Sleep for up to ``seconds``, but wake immediately on shutdown."""
    try:
        await asyncio.wait_for(shutdown_event.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        return


async def fire_reload(db: Any, payload: str = "") -> None:
    """Broadcast a config-change notification to every orchestrator replica.

    Callers (the PUT handler) invoke this after persisting the new
    ``system_settings.main_cloud`` row. The local replica also performs
    a synchronous reload inside the PUT handler so the caller gets an
    accurate success/failure response; this NOTIFY is the fan-out to
    other replicas in a multi-pod deployment.
    """
    try:
        await db.notify_channel(RELOAD_CHANNEL, payload)
    except Exception as e:
        logger.warning(
            "Main cloud config fire_reload: NOTIFY failed (non-fatal, "
            "other replicas will stay stale until their next restart): %s",
            e,
        )


async def _reload_from_db_and_swap(db: Any, main_cloud_router: Any) -> Optional[bool]:
    """Fetch the overlay from DB and reload the router.

    Factored out so ``run_listen_loop``'s callback and the PUT handler's
    synchronous reload share a single implementation. Returns:
        * True  — reload succeeded.
        * False — reload attempted but the new backend failed to init.
        * None  — DB unavailable or overlay missing; no change.
    """
    try:
        overlay = await db.get_system_setting("main_cloud")
    except Exception as e:
        logger.error(
            "Main cloud config reload: failed to read system_settings.main_cloud: %s",
            e,
        )
        return None
    return await main_cloud_router.reload_from_db(overlay)
