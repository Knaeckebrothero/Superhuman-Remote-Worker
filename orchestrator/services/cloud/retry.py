"""Retry and rate-limit primitives shared across main-cloud backends.

Two things live here:

* ``retryable_policy()`` — a ``tenacity.AsyncRetrying`` factory for the
  transient-failure retry policy described in §4.9 of the design doc.
  Honors ``CloudBackendError.retryable`` and retries on the common
  ``httpx`` transport errors.
* ``LeakyBucket`` — a minimal token-bucket rate limiter used by the
  Nextcloud adapter for the NC 30.0.10+ 20-shares-per-10-min server
  limit. Client-side throttling keeps the adapter under the server
  ceiling instead of taking 429s and backing off.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Deque

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
    wait_random,
)

from .errors import CloudBackendError

RETRYABLE_HTTPX: tuple[type[Exception], ...] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.RemoteProtocolError,
)


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, CloudBackendError):
        return exc.retryable
    return isinstance(exc, RETRYABLE_HTTPX)


def retryable_policy(*, max_attempts: int = 4) -> AsyncRetrying:
    """Shared retry policy for cloud-backend HTTP calls.

    Exponential backoff with jitter. Retries transport-level httpx failures
    and ``CloudBackendError`` instances whose ``.retryable`` flag is True
    (which the adapter sets for 429 / 5xx / timeouts).
    """
    return AsyncRetrying(
        retry=retry_if_exception(_is_retryable),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=8) + wait_random(0, 0.5),
        stop=stop_after_attempt(max_attempts),
        reraise=True,
    )


class LeakyBucket:
    """Async-safe fixed-window leaky bucket rate limiter.

    Used by NextcloudBackend.share_session_folder to stay under the
    server's 20-shares-per-10-min ceiling (NC 30.0.10+). Not a general
    rate limiter — the window semantics are simple "sliding count of
    calls in the last N seconds". Callers ``await`` ``acquire()`` before
    issuing a rate-limited operation; the call blocks until the bucket
    has capacity.

    The bucket is conservative by design: set ``max_events`` below the
    server's actual limit so that even under clock-skew the client stays
    below the ceiling.
    """

    def __init__(self, *, max_events: int, window_seconds: float) -> None:
        self._max_events = max_events
        self._window_seconds = window_seconds
        self._events: Deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Block until the bucket has capacity for one more event."""
        while True:
            async with self._lock:
                now = time.monotonic()
                cutoff = now - self._window_seconds
                while self._events and self._events[0] < cutoff:
                    self._events.popleft()
                if len(self._events) < self._max_events:
                    self._events.append(now)
                    return
                oldest = self._events[0]
                wait_for = max(0.0, (oldest + self._window_seconds) - now)
            await asyncio.sleep(wait_for)
