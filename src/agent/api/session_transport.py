"""Best-effort session output; callers own admission, journal and lifecycle."""

import asyncio
import logging
from typing import Any


async def send_message(ws: Any, method: str, params: dict[str, Any]) -> None:
    """Send a direct message, dropping ordinary socket failures.

    Cancellation propagates; the caller owns its task and connection. This
    path does not publish to subscribers or enter the durable event journal.
    """
    try:
        await ws.send_json({"method": method, "params": params})
    except Exception:
        pass


def subscribe(
    subscribers: dict[str, asyncio.Queue], client_id: str, *, maxsize: int
) -> asyncio.Queue:
    """Allocate one bounded outbound queue in the caller-owned registry."""
    queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
    subscribers[client_id] = queue
    return queue


def unsubscribe(subscribers: dict[str, asyncio.Queue], client_id: str) -> None:
    """Remove a queue without touching its pump or execution owner."""
    subscribers.pop(client_id, None)


def fan_out_frame(
    subscribers: dict[str, asyncio.Queue],
    frame: dict[str, Any],
    *,
    logger: logging.Logger,
) -> None:
    """Enqueue one already-built frame for every live control subscriber."""

    method = str(frame.get("method") or "unknown")
    for client_id, queue in list(subscribers.items()):
        try:
            queue.put_nowait(frame)
        except asyncio.QueueFull:
            # Drop oldest, retry. If the retry still fails (shouldn't -- we just
            # made room), drop the new frame and move on.
            try:
                queue.get_nowait()
                queue.put_nowait(frame)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                logger.warning(
                    "Subscriber %s queue overflow -- dropping frame %s",
                    client_id,
                    method,
                )


async def run_subscriber_pump(
    ws: Any, queue: asyncio.Queue, *, ping_interval: float
) -> None:
    """Drain one queue into its socket, returning on ordinary send failure.

    Idle pings go directly to the socket and never enter the event journal.
    The caller cancels and joins this pump and owns the subscriber registry;
    neither a quiet connection nor its closure transfers execution ownership.
    """
    try:
        while True:
            try:
                frame = await asyncio.wait_for(queue.get(), timeout=ping_interval)
            except asyncio.TimeoutError:
                frame = {"method": "ws.ping", "params": {}}
            try:
                await ws.send_json(frame)
            except Exception:
                # WS is dead — let the receive loop's exception path clean up.
                return
    except asyncio.CancelledError:
        raise
