"""Task-isolation tests for ``PostgresDB.transaction_scope``."""

from __future__ import annotations

import asyncio

import pytest

from orchestrator.database.postgres import PostgresDB


class _Transaction:
    def __init__(self, connection: "_Connection") -> None:
        self.connection = connection

    async def __aenter__(self):
        self.connection.events.append("begin")
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        self.connection.events.append("rollback" if exc_type else "commit")


class _Connection:
    def __init__(self, name: str) -> None:
        self.name = name
        self.events: list[str] = []

    def transaction(self) -> _Transaction:
        return _Transaction(self)


class _PoolAcquire:
    def __init__(self, pool: "_Pool", connection: _Connection) -> None:
        self.pool = pool
        self.connection = connection

    async def __aenter__(self) -> _Connection:
        self.pool.events.append(f"acquire:{self.connection.name}")
        return self.connection

    async def __aexit__(self, exc_type, exc, traceback):
        self.pool.events.append(f"release:{self.connection.name}")


class _Pool:
    def __init__(self, *connections: _Connection) -> None:
        self.connections = list(connections)
        self.events: list[str] = []

    def acquire(self) -> _PoolAcquire:
        if not self.connections:
            raise AssertionError("unexpected pool acquisition")
        return _PoolAcquire(self, self.connections.pop(0))


def _database(*connections: _Connection) -> tuple[PostgresDB, _Pool]:
    database = PostgresDB.__new__(PostgresDB)
    pool = _Pool(*connections)
    database._pool = pool
    return database, pool


@pytest.mark.asyncio
async def test_acquire_outside_scope_keeps_historical_pool_behavior():
    first = _Connection("first")
    second = _Connection("second")
    database, pool = _database(first, second)

    async with database.acquire() as observed_first:
        assert observed_first is first
    async with database.acquire() as observed_second:
        assert observed_second is second

    assert pool.events == [
        "acquire:first",
        "release:first",
        "acquire:second",
        "release:second",
    ]


@pytest.mark.asyncio
async def test_nested_acquire_reuses_scoped_connection_in_same_task():
    scoped = _Connection("scoped")
    database, pool = _database(scoped)

    async with database.transaction_scope() as transaction_connection:
        async with database.acquire() as first:
            async with database.acquire() as second:
                assert first is transaction_connection
                assert second is transaction_connection

    assert scoped.events == ["begin", "commit"]
    assert pool.events == ["acquire:scoped", "release:scoped"]


@pytest.mark.asyncio
async def test_nested_transaction_scope_uses_a_savepoint_on_same_connection():
    scoped = _Connection("scoped")
    database, pool = _database(scoped)

    async with database.transaction_scope() as outer:
        async with database.transaction_scope() as inner:
            assert inner is outer

    assert scoped.events == ["begin", "begin", "commit", "commit"]
    assert pool.events == ["acquire:scoped", "release:scoped"]


@pytest.mark.asyncio
async def test_child_task_does_not_inherit_parents_scoped_connection():
    parent_connection = _Connection("parent")
    child_connection = _Connection("child")
    database, pool = _database(parent_connection, child_connection)

    async with database.transaction_scope() as parent:

        async def child_acquire():
            async with database.acquire() as child:
                return child

        child = await asyncio.create_task(child_acquire())
        assert parent is parent_connection
        assert child is child_connection

    assert parent_connection.events == ["begin", "commit"]
    assert child_connection.events == []
    assert pool.events == [
        "acquire:parent",
        "acquire:child",
        "release:child",
        "release:parent",
    ]


@pytest.mark.asyncio
async def test_exception_rolls_back_and_clears_scope_before_next_acquire():
    scoped = _Connection("scoped")
    after = _Connection("after")
    database, pool = _database(scoped, after)

    with pytest.raises(RuntimeError, match="abort"):
        async with database.transaction_scope():
            raise RuntimeError("abort")

    async with database.acquire() as observed:
        assert observed is after

    assert scoped.events == ["begin", "rollback"]
    assert pool.events == [
        "acquire:scoped",
        "release:scoped",
        "acquire:after",
        "release:after",
    ]


@pytest.mark.asyncio
async def test_cancellation_rolls_back_and_clears_scope_in_cancelled_task():
    scoped = _Connection("scoped")
    after = _Connection("after")
    database, pool = _database(scoped, after)
    entered = asyncio.Event()
    release = asyncio.Event()
    observed_after_cancel: list[_Connection] = []

    async def transaction_task():
        try:
            async with database.transaction_scope():
                entered.set()
                await release.wait()
        except asyncio.CancelledError:
            async with database.acquire() as observed:
                observed_after_cancel.append(observed)
            raise

    task = asyncio.create_task(transaction_task())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert observed_after_cancel == [after]
    assert scoped.events == ["begin", "rollback"]
    assert pool.events == [
        "acquire:scoped",
        "release:scoped",
        "acquire:after",
        "release:after",
    ]
