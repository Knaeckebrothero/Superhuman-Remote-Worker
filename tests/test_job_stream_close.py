"""Pinned app adapters keep one finalizer through repeated caller cancellation."""

import asyncio
import importlib

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("module_name", ["agent.api.app", "agent.api.dual_app"])
@pytest.mark.parametrize("close_outcome", ["success", "error", "cancelled"])
async def test_repeated_cancellation_waits_for_one_close_owner(
    module_name, close_outcome
):
    adapter = importlib.import_module(module_name)
    entered = asyncio.Event()
    release = asyncio.Event()
    settled = asyncio.Event()
    calls = 0
    close_error = RuntimeError("checkpoint flush failed")

    class Stream:
        async def aclose(self):
            nonlocal calls
            calls += 1
            entered.set()
            try:
                await release.wait()
                if close_outcome == "error":
                    raise close_error
                if close_outcome == "cancelled":
                    raise asyncio.CancelledError("stream cancelled itself")
            finally:
                settled.set()

    owner = asyncio.create_task(
        adapter._close_job_stream(Stream(), job_id="job-close-characterization")
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        for reason in ("first caller cancellation", "repeated caller cancellation"):
            owner.cancel(reason)
            await asyncio.sleep(0)
            assert not owner.done()
            assert not settled.is_set()
            assert calls == 1

        release.set()
        if close_outcome == "error":
            # A failed finalizer must remain visible even when its caller was
            # cancelled; reset/reporting must not mistake it for safe cleanup.
            with pytest.raises(RuntimeError) as caught:
                await asyncio.wait_for(owner, timeout=2)
            assert caught.value is close_error
        else:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(owner, timeout=2)
        assert settled.is_set()
        assert calls == 1
    finally:
        release.set()
        if not owner.done():
            owner.cancel()
        await asyncio.gather(owner, return_exceptions=True)
