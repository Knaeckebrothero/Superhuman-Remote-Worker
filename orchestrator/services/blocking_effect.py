"""Cancellation-safe ownership for blocking effects.

``asyncio.to_thread`` only cancels the coroutine waiting for a worker; it does
not stop the worker itself.  Callers holding a durable lease or lifecycle lock
must therefore join a started blocking effect before they release authority.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from contextlib import suppress
from typing import Any, Callable, TypeVar


_T = TypeVar("_T")


async def joined_blocking_call(
    func: Callable[..., Any], /, *args: Any, **kwargs: Any
) -> Any:
    """Run ``func`` in a thread and defer cancellation until it is terminal."""

    task = asyncio.create_task(asyncio.to_thread(func, *args, **kwargs))
    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancellation = exc
        except Exception:
            if cancellation is None:
                raise
    if cancellation is not None:
        # Retrieve a worker exception before preserving the caller's primary
        # cancellation.  At this point the effect can no longer race a retry.
        with suppress(BaseException):
            task.result()
        raise cancellation
    return task.result()


async def joined_async_call(awaitable: Awaitable[_T]) -> _T:
    """Join an async effect before propagating cancellation of its owner."""

    task = asyncio.create_task(awaitable)
    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancellation = exc
        except Exception:
            if cancellation is None:
                raise
    if cancellation is not None:
        with suppress(BaseException):
            task.result()
        raise cancellation
    return task.result()


__all__ = ["joined_async_call", "joined_blocking_call"]
