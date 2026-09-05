"""Cancellation-safe bounded ownership for asyncio subprocess effects."""

from __future__ import annotations

import asyncio
from typing import Any


class SubprocessOutputLimit(RuntimeError):
    pass


async def stop_and_reap(
    process: asyncio.subprocess.Process,
    *,
    terminate_seconds: float = 0.5,
    kill_seconds: float = 2.0,
) -> None:
    async def _stop() -> None:
        if process.returncode is not None:
            await process.wait()
            return
        try:
            process.terminate()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(process.wait(), terminate_seconds)
            return
        except (asyncio.TimeoutError, ProcessLookupError):
            pass
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(process.wait(), kill_seconds)
        except (asyncio.TimeoutError, ProcessLookupError):
            pass

    cleanup = asyncio.create_task(_stop())
    cancelled: asyncio.CancelledError | None = None
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError as exc:
            cancelled = exc
    cleanup.result()
    if cancelled is not None:
        raise cancelled


async def create_owned_subprocess_exec(
    *cmd: str, **kwargs: Any
) -> asyncio.subprocess.Process:
    spawn = asyncio.create_task(asyncio.create_subprocess_exec(*cmd, **kwargs))
    cancelled: asyncio.CancelledError | None = None
    while not spawn.done():
        try:
            await asyncio.shield(spawn)
        except asyncio.CancelledError as exc:
            cancelled = exc
    try:
        process = spawn.result()
    except BaseException:
        if cancelled is not None:
            raise cancelled
        raise
    if cancelled is not None:
        await stop_and_reap(process)
        raise cancelled
    return process


async def _read_limited(reader: asyncio.StreamReader, limit: int) -> bytes:
    result = bytearray()
    while True:
        chunk = await reader.read(min(64 * 1024, limit + 1 - len(result)))
        if not chunk:
            return bytes(result)
        result.extend(chunk)
        if len(result) > limit:
            raise SubprocessOutputLimit(f"subprocess output exceeded {limit} bytes")


async def communicate_bounded(
    process: asyncio.subprocess.Process,
    *,
    timeout: float,
    stdout_limit: int,
    stderr_limit: int,
) -> tuple[bytes, bytes]:
    stdout_task = asyncio.create_task(_read_limited(process.stdout, stdout_limit))
    stderr_task = asyncio.create_task(_read_limited(process.stderr, stderr_limit))
    try:
        async with asyncio.timeout(timeout):
            stdout, stderr, _ = await asyncio.gather(
                stdout_task, stderr_task, process.wait()
            )
            return stdout, stderr
    except BaseException:
        try:
            await stop_and_reap(process)
        finally:
            for task in (stdout_task, stderr_task):
                task.cancel()
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
        raise


async def wait_bounded(
    process: asyncio.subprocess.Process,
    *,
    timeout: float,
) -> int:
    """Wait for a quiet child and own termination/reaping on every exit path."""

    try:
        return await asyncio.wait_for(process.wait(), timeout=timeout)
    except BaseException:
        await stop_and_reap(process)
        raise


__all__ = [
    "SubprocessOutputLimit",
    "communicate_bounded",
    "create_owned_subprocess_exec",
    "stop_and_reap",
    "wait_bounded",
]
