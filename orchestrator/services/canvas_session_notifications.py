"""Replica-local cancellation accelerated by PostgreSQL Canvas notifications."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
import json
import logging
import os
from typing import Any, AsyncIterator
from uuid import UUID

from services.canvas_viewer_sessions import CanvasViewerError

logger = logging.getLogger(__name__)

CANVAS_SESSION_CHANGE_CHANNEL = "canvas_session_changes"


@dataclass(slots=True)
class CanvasConnectionLease:
    session_id: UUID
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)


class CanvasConnectionRegistry:
    """Bound active exchanges and cancel them without retaining credentials."""

    def __init__(
        self,
        *,
        max_connections: int | None = None,
        max_per_session: int | None = None,
    ) -> None:
        self._max = max_connections or max(
            1, int(os.getenv("CANVAS_VIEWER_MAX_CONNECTIONS", "256"))
        )
        self._max_per_session = max_per_session or max(
            1, int(os.getenv("CANVAS_VIEWER_MAX_HTTP_PER_SESSION", "16"))
        )
        self._leases: dict[UUID, dict[int, CanvasConnectionLease]] = {}
        self._count = 0
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def register(self, session_id: UUID) -> AsyncIterator[CanvasConnectionLease]:
        lease = CanvasConnectionLease(session_id=session_id)
        key = id(lease)
        async with self._lock:
            current = self._leases.setdefault(session_id, {})
            if self._count >= self._max:
                if not current:
                    self._leases.pop(session_id, None)
                raise CanvasViewerError(
                    503,
                    "canvas_viewer_capacity_exhausted",
                    "Canvas viewer capacity is currently exhausted",
                )
            if len(current) >= self._max_per_session:
                raise CanvasViewerError(
                    429,
                    "canvas_session_connection_limit",
                    "Canvas viewer connection limit reached",
                )
            current[key] = lease
            self._count += 1
        try:
            yield lease
        finally:
            async with self._lock:
                current = self._leases.get(session_id)
                if current is not None and current.pop(key, None) is not None:
                    self._count = max(0, self._count - 1)
                    if not current:
                        self._leases.pop(session_id, None)

    async def revoke_session(self, session_id: UUID) -> None:
        async with self._lock:
            leases = list(self._leases.get(session_id, {}).values())
        for lease in leases:
            lease.cancelled.set()

    async def close_all(self) -> None:
        async with self._lock:
            leases = [
                lease
                for session_leases in self._leases.values()
                for lease in session_leases.values()
            ]
        for lease in leases:
            lease.cancelled.set()


class CanvasSessionNotificationListener:
    """Keep one LISTEN connection; periodic DB guards cover missed messages."""

    def __init__(
        self, db: Any, registry: CanvasConnectionRegistry, *, retry_seconds: float = 1
    ) -> None:
        self._db = db
        self._registry = registry
        self._retry_seconds = max(0.1, retry_seconds)
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(
                self._run(), name="canvas-session-notifications"
            )

    async def stop(self) -> None:
        self._stop.set()
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    def _notification(
        self, connection: Any, pid: int, channel: str, payload: str
    ) -> None:
        del connection, pid
        if channel != CANVAS_SESSION_CHANGE_CHANNEL or len(payload) > 512:
            return
        try:
            message = json.loads(payload)
            if message.get("kind") != "session":
                return
            session_id = UUID(str(message.get("id")))
        except (TypeError, ValueError, json.JSONDecodeError):
            logger.warning("Ignored malformed Canvas session notification")
            return
        asyncio.create_task(self._registry.revoke_session(session_id))

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                async with self._db.acquire() as conn:
                    await conn.add_listener(
                        CANVAS_SESSION_CHANGE_CHANNEL, self._notification
                    )
                    try:
                        await self._stop.wait()
                    finally:
                        with suppress(Exception):
                            await conn.remove_listener(
                                CANVAS_SESSION_CHANGE_CHANNEL, self._notification
                            )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "Canvas session LISTEN connection failed; bounded DB "
                    "revalidation remains active",
                    exc_info=True,
                )
                try:
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=self._retry_seconds
                    )
                except TimeoutError:
                    pass


CANVAS_CONNECTION_REGISTRY = CanvasConnectionRegistry()


__all__ = [
    "CANVAS_CONNECTION_REGISTRY",
    "CANVAS_SESSION_CHANGE_CHANNEL",
    "CanvasConnectionLease",
    "CanvasConnectionRegistry",
    "CanvasSessionNotificationListener",
]
