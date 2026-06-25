"""Unit tests for the stale verification-subjob sweeper (D2).

Covers the pure tick, the shutdown-aware loop, and the PostgresDB helper's
wire-level contract (mocked connection — the SQL's runtime behavior is verified
on the dev cluster, there is no test DB here, matching test_admin_providers_db.py
and test_postgres_advisory_lock.py). See
docs/issues/preemption_before_first_checkpoint_replays_job_opening.md.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.database.postgres import PostgresDB
from orchestrator.services.stale_verification_sweeper import (
    _sweep_tick,
    stale_verification_sweeper_loop,
)


def _make_db(mock_conn):
    """Build a PostgresDB whose acquire() yields a mocked connection."""
    db = PostgresDB.__new__(PostgresDB)
    db._pool = MagicMock()
    db._connection_string = "test"
    db._queries = {}

    @asynccontextmanager
    async def _acquire():
        yield mock_conn

    db.acquire = _acquire
    return db


class TestSweepTick:
    @pytest.mark.asyncio
    async def test_delegates_to_db_helper_and_returns_count(self):
        db = AsyncMock()
        db.cancel_stale_verification_subjobs = AsyncMock(return_value=3)

        cancelled = await _sweep_tick(db, stale_hours=6)

        assert cancelled == 3
        db.cancel_stale_verification_subjobs.assert_awaited_once_with(6)


class TestSweeperLoop:
    @pytest.mark.asyncio
    async def test_no_tick_when_shutdown_preset(self):
        db = AsyncMock()
        db.cancel_stale_verification_subjobs = AsyncMock(return_value=0)
        shutdown = asyncio.Event()
        shutdown.set()

        await asyncio.wait_for(
            stale_verification_sweeper_loop(db, shutdown), timeout=1.0
        )
        db.cancel_stale_verification_subjobs.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_runs_one_tick_then_exits_on_shutdown(self):
        db = AsyncMock()
        shutdown = asyncio.Event()

        def _count_then_stop(*_args):
            shutdown.set()  # stop the loop after this single tick
            return 2

        db.cancel_stale_verification_subjobs = AsyncMock(side_effect=_count_then_stop)

        await asyncio.wait_for(
            stale_verification_sweeper_loop(db, shutdown), timeout=1.0
        )
        db.cancel_stale_verification_subjobs.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_tick_exception_does_not_kill_loop(self):
        db = AsyncMock()
        shutdown = asyncio.Event()

        def _raise_then_stop(*_args):
            shutdown.set()  # ensure the loop exits after the failed tick
            raise RuntimeError("boom")

        db.cancel_stale_verification_subjobs = AsyncMock(side_effect=_raise_then_stop)

        # Must swallow the tick error and exit cleanly on shutdown, not raise.
        await asyncio.wait_for(
            stale_verification_sweeper_loop(db, shutdown), timeout=1.0
        )


class TestCancelStaleVerificationSubjobs:
    @pytest.mark.asyncio
    async def test_executes_update_with_stale_hours_and_parses_count(self):
        conn = AsyncMock()
        conn.execute = AsyncMock(return_value="UPDATE 3")
        db = _make_db(conn)

        count = await db.cancel_stale_verification_subjobs(stale_hours=12)

        assert count == 3
        conn.execute.assert_awaited_once()
        args = conn.execute.await_args.args
        sql = args[0]
        # The discriminator + the staleness fallback must be in the predicate,
        # and stale_hours must bind as $1.
        assert "verification_target" in sql
        assert "make_interval" in sql
        assert "parent.status IN" in sql
        assert args[1] == 12

    @pytest.mark.asyncio
    async def test_returns_zero_when_no_rows(self):
        conn = AsyncMock()
        conn.execute = AsyncMock(return_value="UPDATE 0")
        db = _make_db(conn)

        assert await db.cancel_stale_verification_subjobs() == 0
