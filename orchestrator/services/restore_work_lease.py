"""Heartbeat one durable post-creation restore-work lease.

Workspace restore tails can stream snapshots or synchronize Git repositories for
longer than a single database lease. This helper renews the exact-B token while
those effects run and cancels the local coroutine if durable authority is lost.
It deliberately stops before the caller's atomic completion transaction so a
completed row is never mistaken for a failed renewal.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Awaitable, Callable

from services.blocking_effect import joined_async_call


class RestoreWorkLeaseLost(RuntimeError):
    """The exact-B restore-work token was lost while effects were running."""


class RestoreWorkLeaseHeartbeat:
    """Renew a durable lease and interrupt stale local work on token loss."""

    def __init__(
        self,
        renew: Callable[[], Awaitable[object]],
        *,
        interval_seconds: float = 60.0,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("restore-work renewal interval must be positive")
        self._renew = renew
        self._interval_seconds = interval_seconds
        self._owner: asyncio.Task[object] | None = None
        self._heartbeat: asyncio.Task[None] | None = None
        self._lost = False

    async def __aenter__(self) -> "RestoreWorkLeaseHeartbeat":
        owner = asyncio.current_task()
        if owner is None:
            raise RuntimeError("restore-work heartbeat requires an asyncio task")
        self._owner = owner
        # Validate before the first byte is written. Waiting one interval grants
        # an already-reclaimed worker a stale-write window.
        try:
            renewed = await self._renew()
        except Exception:
            renewed = None
        if not renewed:
            self._lost = True
            raise RestoreWorkLeaseLost(
                "durable restore-work authority changed before external effects"
            )
        self._heartbeat = asyncio.create_task(self._run())
        return self

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._interval_seconds)
            try:
                renewed = await self._renew()
            except Exception:
                renewed = None
            if renewed:
                continue
            self._lost = True
            if self._owner is not None:
                self._owner.cancel()
            return

    async def __aexit__(self, exc_type, exc, traceback) -> bool:
        heartbeat = self._heartbeat
        cancellation: asyncio.CancelledError | None = None
        if heartbeat is not None:

            async def _stop() -> None:
                heartbeat.cancel()
                with suppress(asyncio.CancelledError):
                    await heartbeat

            try:
                await joined_async_call(_stop())
            except asyncio.CancelledError as exc:
                # joined_async_call has already proved the heartbeat terminal;
                # preserve cancellation only after the lease task is gone.
                cancellation = exc
        if self._lost and (
            exc_type is asyncio.CancelledError or cancellation is not None
        ):
            raise RestoreWorkLeaseLost(
                "durable restore-work authority changed during external effects"
            ) from None
        if cancellation is not None:
            raise cancellation
        return False
