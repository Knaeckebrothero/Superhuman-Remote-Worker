import asyncio
from unittest.mock import AsyncMock

import pytest

from orchestrator.services.restore_work_lease import (
    RestoreWorkLeaseHeartbeat,
    RestoreWorkLeaseLost,
)


@pytest.mark.asyncio
async def test_heartbeat_renews_while_restore_work_runs() -> None:
    renew = AsyncMock(return_value={"restore_work_claim_token": 7})

    async with RestoreWorkLeaseHeartbeat(renew, interval_seconds=0.001):
        await asyncio.sleep(0.005)

    assert renew.await_count >= 1


@pytest.mark.asyncio
async def test_heartbeat_cancels_stale_restore_when_renewal_is_lost() -> None:
    renew = AsyncMock(return_value=None)

    with pytest.raises(RestoreWorkLeaseLost):
        async with RestoreWorkLeaseHeartbeat(renew, interval_seconds=0.001):
            await asyncio.sleep(1)


@pytest.mark.asyncio
async def test_exit_joins_heartbeat_before_repeated_cancellation_escapes() -> None:
    heartbeat = RestoreWorkLeaseHeartbeat(AsyncMock(return_value=True))
    stopping = asyncio.Event()
    release = asyncio.Event()

    async def stubborn_heartbeat() -> None:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            stopping.set()
            await release.wait()

    background = asyncio.create_task(stubborn_heartbeat())
    heartbeat._heartbeat = background
    exiting = asyncio.create_task(heartbeat.__aexit__(None, None, None))
    await stopping.wait()

    exiting.cancel()
    await asyncio.sleep(0)
    exiting.cancel()
    assert not background.done()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await exiting
    assert background.done()
