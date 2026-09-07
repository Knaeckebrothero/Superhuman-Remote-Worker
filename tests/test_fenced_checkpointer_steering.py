"""Checkpoint/steering ordering tests for the stateless saver."""

from unittest.mock import AsyncMock, Mock

import pytest
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from agent.core.fenced_checkpointer import FencedAsyncPostgresSaver


def _saver(post_commit: AsyncMock) -> FencedAsyncPostgresSaver:
    saver = FencedAsyncPostgresSaver(
        object(),
        unit_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        lease_token=4,
        post_commit=post_commit,
    )
    saver._bound_handle = Mock()  # type: ignore[method-assign]
    return saver


@pytest.mark.asyncio
async def test_failed_checkpoint_write_never_acks(monkeypatch) -> None:
    commit = AsyncMock(side_effect=RuntimeError("transaction rolled back"))
    monkeypatch.setattr(AsyncPostgresSaver, "aput", commit)
    post_commit = AsyncMock()
    saver = _saver(post_commit)

    with pytest.raises(RuntimeError, match="rolled back"):
        await saver.aput(
            {"configurable": {}},
            {"id": "cp-1", "channel_values": {}},
            {"step": 1},
            {},
        )

    post_commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_successful_checkpoint_write_acks_after_commit(monkeypatch) -> None:
    events: list[str] = []

    async def commit(*args, **kwargs):
        del args, kwargs
        events.append("commit")
        return {"configurable": {"checkpoint_id": "cp-1"}}

    async def post_commit(*args, **kwargs):
        del args, kwargs
        events.append("ack")

    monkeypatch.setattr(AsyncPostgresSaver, "aput", commit)
    saver = _saver(AsyncMock(side_effect=post_commit))

    await saver.aput(
        {"configurable": {}},
        {"id": "cp-1", "channel_values": {}},
        {"step": 1},
        {},
    )

    assert events == ["commit", "ack"]


@pytest.mark.asyncio
async def test_intermediate_writes_never_ack(monkeypatch) -> None:
    write = AsyncMock(return_value=None)
    monkeypatch.setattr(AsyncPostgresSaver, "aput_writes", write)
    post_commit = AsyncMock()
    saver = _saver(post_commit)

    await saver.aput_writes(
        {"configurable": {}},
        [("channel", "value")],
        "task-1",
    )

    write.assert_awaited_once()
    post_commit.assert_not_awaited()
