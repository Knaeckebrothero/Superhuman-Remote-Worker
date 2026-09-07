"""One bounded graph-stream finalizer, shared by pinned worker adapters."""

import asyncio
import logging
from typing import Any

from shared.subagent_lifecycle import SubagentQuiescenceError


async def close_job_stream(
    streaming_gen: Any, *, job_id: str, timeout_seconds: float, logger: logging.Logger
) -> None:
    """Bound and shield generator finalization before releasing a pinned job.

    Closing the graph stream flushes its pending asynchronous checkpoint writes
    and runs the agent's job-scoped cleanup. A caller cancellation must still
    propagate, but only after the bounded close owner has reached a terminal
    outcome; otherwise the app can report/reset while cleanup is still running.
    """

    async def _bounded_close() -> None:
        try:
            await asyncio.wait_for(streaming_gen.aclose(), timeout=timeout_seconds)
        except asyncio.TimeoutError as exc:
            logger.error(
                "Job %s stream close timed out after %.0fs",
                job_id,
                timeout_seconds,
            )
            raise SubagentQuiescenceError(
                f"job {job_id} stream cleanup timed out"
            ) from exc
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Job %s stream close failed", job_id)
            raise

    close_task = asyncio.create_task(
        _bounded_close(), name=f"job-stream-close-{job_id[:12]}"
    )
    cancellation: asyncio.CancelledError | None = None
    while not close_task.done():
        try:
            await asyncio.shield(close_task)
        except asyncio.CancelledError as exc:
            # Keep the close task as the sole finalizer and restore caller
            # cancellation once it is terminal. Repeated cancellation remains
            # harmless and cannot start a second aclose().
            cancellation = exc

    try:
        close_task.result()
    except asyncio.CancelledError:
        if cancellation is None:
            raise
    if cancellation is not None:
        raise cancellation
