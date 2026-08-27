"""Lease-fenced LangGraph PostgreSQL checkpointer for stateless workers.

The upstream saver uses pipeline mode for checkpoint writes.  A worker write
must instead be one ordinary transaction whose first application statement
share-locks its exact ``run_queue`` lease.  This module supplies that write
cursor, bounded idempotent retries, and one process-wide psycopg pool.

Reads are intentionally unfenced.  Every mutating saver method that upstream
routes through ``_cursor(pipeline=True)`` is fenced; the two hot paths
(``aput`` and ``aput_writes``) additionally retry narrow connection failures
and re-run the fence on every attempt.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Awaitable, Callable

from langgraph.checkpoint.postgres import _ainternal
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import InterfaceError, OperationalError
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool, PoolTimeout

from src.api.lease_context import LeaseLostError, current_lease

logger = logging.getLogger(__name__)

_FENCE_SQL = """
SELECT 1
FROM run_queue
WHERE unit_id = %s::uuid
  AND unit_kind = 'worker_batch'
  AND state = 'leased'
  AND lease_token = %s::bigint
FOR SHARE
"""

_TRANSIENT_WRITE_ERRORS = (OperationalError, InterfaceError, PoolTimeout)
_DEFAULT_RETRY_ATTEMPTS = 3
_DEFAULT_RETRY_BASE_SECONDS = 0.05

PostCommitCallback = Callable[
    [dict[str, Any], Any, dict[str, Any], dict[str, Any]],
    Awaitable[None] | None,
]

_pool: AsyncConnectionPool | None = None
_pool_url: str | None = None
_pool_lock = asyncio.Lock()
_schema_ready = False


def _strict_msgpack_enabled() -> bool:
    return os.getenv("LANGGRAPH_STRICT_MSGPACK", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _positive_int_env(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


async def _get_pool(url: str) -> AsyncConnectionPool:
    global _pool, _pool_url
    async with _pool_lock:
        if _pool is not None:
            if _pool_url != url:
                raise RuntimeError(
                    "The stateless checkpoint pool is already open for a "
                    "different database URL"
                )
            return _pool

        max_size = _positive_int_env("STATELESS_CHECKPOINT_POOL_MAX", 4)
        min_size = min(
            _positive_int_env("STATELESS_CHECKPOINT_POOL_MIN", 1),
            max_size,
        )
        pool = AsyncConnectionPool(
            conninfo=url,
            min_size=min_size,
            max_size=max_size,
            kwargs={
                "autocommit": True,
                "prepare_threshold": 0,
                "row_factory": dict_row,
            },
            open=False,
            name="stateless-worker-checkpoints",
        )
        await pool.open(wait=True)
        _pool = pool
        _pool_url = url
        logger.info(
            "Stateless checkpoint pool opened (min=%d max=%d)",
            min_size,
            max_size,
        )
        return pool


async def close_fenced_checkpointer_pool() -> None:
    """Close the process-wide saver pool (idempotent, lifespan-owned)."""

    global _pool, _pool_url, _schema_ready
    async with _pool_lock:
        pool = _pool
        _pool = None
        _pool_url = None
        _schema_ready = False
        if pool is not None:
            await pool.close()
            logger.info("Stateless checkpoint pool closed")


class FencedAsyncPostgresSaver(AsyncPostgresSaver):
    """AsyncPostgresSaver bound to one immutable worker lease generation."""

    def __init__(
        self,
        conn: AsyncConnectionPool,
        *,
        unit_id: str,
        lease_token: int,
        retry_attempts: int = _DEFAULT_RETRY_ATTEMPTS,
        retry_base_seconds: float = _DEFAULT_RETRY_BASE_SECONDS,
        post_commit: PostCommitCallback | None = None,
    ) -> None:
        super().__init__(conn)
        self.unit_id = str(unit_id)
        self.lease_token = int(lease_token)
        self.retry_attempts = max(1, int(retry_attempts))
        self.retry_base_seconds = max(0.0, float(retry_base_seconds))
        self.post_commit = post_commit

    def _bound_handle(self):
        """Return this exact live handle or fail without harming a new claim."""

        handle = current_lease.get()
        exact = bool(
            handle is not None
            and handle.unit_id is not None
            and str(handle.unit_id) == self.unit_id
            and int(handle.lease_token) == self.lease_token
        )
        if not exact:
            raise LeaseLostError(
                "checkpoint saver no longer belongs to the active worker claim "
                f"({self.unit_id}/{self.lease_token})"
            )
        if handle.lost.is_set():
            raise LeaseLostError(
                "checkpoint write attempted after worker lease loss "
                f"({self.unit_id}/{self.lease_token})"
            )
        return handle

    @asynccontextmanager
    async def _cursor(
        self,
        *,
        pipeline: bool = False,
    ) -> AsyncIterator[Any]:
        """Use ordinary transactions for writes; never enter psycopg pipeline."""

        async with self.lock, _ainternal.get_connection(self.conn) as conn:
            if not pipeline:
                async with conn.cursor(binary=True, row_factory=dict_row) as cur:
                    yield cur
                return

            handle = self._bound_handle()
            async with (
                conn.transaction(),
                conn.cursor(binary=True, row_factory=dict_row) as cur,
            ):
                # First application statement in this persist transaction.
                await cur.execute(
                    _FENCE_SQL,
                    (self.unit_id, self.lease_token),
                )
                if await cur.fetchone() is None:
                    # Only mark the shared handle we just proved still belongs
                    # to this immutable saver.  A late old saver must never mark
                    # a successor claim's handle lost.
                    handle.mark_lost()
                    logger.error(
                        "checkpoint fence rejected: unit=%s token=%d",
                        self.unit_id,
                        self.lease_token,
                    )
                    raise LeaseLostError(
                        "run_queue lease rejected checkpoint write "
                        f"({self.unit_id}/{self.lease_token})"
                    )
                yield cur

    async def _retry_write(
        self,
        operation: str,
        write: Callable[[], Awaitable[Any]],
    ) -> Any:
        for attempt in range(1, self.retry_attempts + 1):
            self._bound_handle()
            try:
                return await write()
            except LeaseLostError:
                raise
            except _TRANSIENT_WRITE_ERRORS as exc:
                if attempt >= self.retry_attempts:
                    raise
                delay = self.retry_base_seconds * attempt
                logger.warning(
                    "checkpoint %s transient failure; retrying with a fresh "
                    "fence (unit=%s token=%d attempt=%d/%d delay=%.3fs): %s",
                    operation,
                    self.unit_id,
                    self.lease_token,
                    attempt,
                    self.retry_attempts,
                    delay,
                    exc,
                )
                if delay:
                    await asyncio.sleep(delay)
        raise AssertionError("unreachable checkpoint retry loop")

    async def aput(
        self,
        config: dict[str, Any],
        checkpoint: Any,
        metadata: dict[str, Any],
        new_versions: Any,
    ) -> dict[str, Any]:
        """Persist a checkpoint, re-fencing retries and notifying post-commit."""

        next_config = await self._retry_write(
            "put",
            lambda: super(FencedAsyncPostgresSaver, self).aput(
                config,
                checkpoint,
                metadata,
                new_versions,
            ),
        )
        callback = self.post_commit
        if callback is not None:
            try:
                result = callback(config, checkpoint, metadata, next_config)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                # The checkpoint is already committed.  Steering acknowledgments
                # are retryable and must never turn that durable success into an
                # apparent saver failure/replay.
                logger.warning(
                    "checkpoint post-commit callback failed (will reconcile on "
                    "next claim): unit=%s token=%d",
                    self.unit_id,
                    self.lease_token,
                    exc_info=True,
                )
        return next_config

    async def aput_writes(
        self,
        config: dict[str, Any],
        writes: Any,
        task_id: str,
        task_path: str = "",
    ) -> None:
        """Persist intermediate writes with the same exact-lease retry fence."""

        await self._retry_write(
            "put_writes",
            lambda: super(FencedAsyncPostgresSaver, self).aput_writes(
                config,
                writes,
                task_id,
                task_path,
            ),
        )


async def make_fenced_checkpointer(
    url: str,
    *,
    unit_id: str,
    lease_token: int,
    post_commit: PostCommitCallback | None = None,
) -> FencedAsyncPostgresSaver:
    """Construct one claim-bound saver over the process-wide psycopg pool."""

    global _schema_ready
    if not _strict_msgpack_enabled():
        raise RuntimeError(
            "Stateless worker checkpoints require LANGGRAPH_STRICT_MSGPACK=true"
        )
    pool = await _get_pool(url)
    saver = FencedAsyncPostgresSaver(
        pool,
        unit_id=unit_id,
        lease_token=lease_token,
        post_commit=post_commit,
    )
    async with _pool_lock:
        if not _schema_ready:
            await saver.setup()
            _schema_ready = True
            logger.info("Postgres checkpoint schema ensured for stateless workers")
    return saver


__all__ = [
    "FencedAsyncPostgresSaver",
    "PostCommitCallback",
    "close_fenced_checkpointer_pool",
    "make_fenced_checkpointer",
]
