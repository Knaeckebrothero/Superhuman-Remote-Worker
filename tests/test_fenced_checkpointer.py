"""Fenced Postgres saver invariants independent of a live database."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from psycopg import OperationalError

import agent.core.fenced_checkpointer as fenced
from agent.api.lease_context import LeaseHandle, LeaseLostError, current_lease
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver


class _Context:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *_exc):
        return False


class _Cursor:
    def __init__(self, row=(1,)):
        self.row = row
        self.execute = AsyncMock()

    async def fetchone(self):
        return self.row


class _Connection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.pipeline = MagicMock(side_effect=AssertionError("pipeline is forbidden"))

    def transaction(self):
        return _Context(None)

    def cursor(self, **_kwargs):
        return _Context(self._cursor)


@asynccontextmanager
async def _lease(unit_id, token):
    handle = LeaseHandle()
    handle.update(unit_id, token)
    reset = current_lease.set(handle)
    try:
        yield handle
    finally:
        current_lease.reset(reset)


@pytest.mark.asyncio
async def test_write_cursor_fences_first_and_never_pipelines(monkeypatch):
    unit_id = str(uuid4())
    cursor = _Cursor()
    conn = _Connection(cursor)

    @asynccontextmanager
    async def get_connection(_source):
        yield conn

    monkeypatch.setattr(fenced._ainternal, "get_connection", get_connection)
    async with _lease(unit_id, 9):
        saver = fenced.FencedAsyncPostgresSaver(
            MagicMock(), unit_id=unit_id, lease_token=9
        )
        async with saver._cursor(pipeline=True) as yielded:
            assert yielded is cursor

    cursor.execute.assert_awaited_once_with(fenced._FENCE_SQL, (unit_id, 9))
    conn.pipeline.assert_not_called()


@pytest.mark.asyncio
async def test_rejected_fence_marks_exact_handle_lost(monkeypatch):
    unit_id = str(uuid4())
    cursor = _Cursor(row=None)
    conn = _Connection(cursor)

    @asynccontextmanager
    async def get_connection(_source):
        yield conn

    monkeypatch.setattr(fenced._ainternal, "get_connection", get_connection)
    async with _lease(unit_id, 11) as handle:
        saver = fenced.FencedAsyncPostgresSaver(
            MagicMock(), unit_id=unit_id, lease_token=11
        )
        with pytest.raises(LeaseLostError):
            async with saver._cursor(pipeline=True):
                pass
        assert handle.lost.is_set()


@pytest.mark.asyncio
async def test_transient_retry_refences_each_attempt():
    unit_id = str(uuid4())
    write = AsyncMock(side_effect=[OperationalError("drop"), "committed"])
    async with _lease(unit_id, 13):
        saver = fenced.FencedAsyncPostgresSaver(
            MagicMock(),
            unit_id=unit_id,
            lease_token=13,
            retry_attempts=2,
            retry_base_seconds=0,
        )
        saver._bound_handle = MagicMock(wraps=saver._bound_handle)
        assert await saver._retry_write("put", write) == "committed"

    assert write.await_count == 2
    assert saver._bound_handle.call_count == 2


@pytest.mark.asyncio
async def test_post_commit_runs_only_after_aput_not_aput_writes():
    unit_id = str(uuid4())
    callback = AsyncMock()
    config = {
        "configurable": {
            "thread_id": unit_id,
            "checkpoint_ns": "",
            "checkpoint_id": "old",
        }
    }
    checkpoint = {"id": "new", "channel_values": {}}
    metadata = {"step": 4}
    next_config = {"configurable": {"checkpoint_id": "new"}}

    async with _lease(unit_id, 17):
        saver = fenced.FencedAsyncPostgresSaver(
            MagicMock(), unit_id=unit_id, lease_token=17, post_commit=callback
        )
        with (
            patch.object(
                AsyncPostgresSaver,
                "aput",
                AsyncMock(return_value=next_config),
            ),
            patch.object(
                AsyncPostgresSaver,
                "aput_writes",
                AsyncMock(return_value=None),
            ),
        ):
            assert (
                await saver.aput(config, checkpoint, metadata, {"messages": "1"})
                == next_config
            )
            await saver.aput_writes(config, [("messages", "value")], "task")

    callback.assert_awaited_once_with(config, checkpoint, metadata, next_config)
