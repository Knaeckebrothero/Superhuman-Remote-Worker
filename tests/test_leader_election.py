"""Tests for the session-scoped Postgres advisory-lock leader election (M1).

Uses a real Postgres (testcontainers) because the whole point is the
session-lifetime + auto-release-on-disconnect semantics of
``pg_advisory_lock`` — there is nothing meaningful to assert against a mock.
"""
import asyncio

import asyncpg
import pytest
from testcontainers.postgres import PostgresContainer

from orchestrator.database.lock_ids import LEADER_ID
from orchestrator.services import leader_election


@pytest.fixture(scope="module")
def pg_dsn():
    with PostgresContainer("postgres:16") as pg:
        # testcontainers yields a SQLAlchemy-style URL; asyncpg wants postgresql://
        yield pg.get_connection_url().replace("postgresql+psycopg2", "postgresql")


class _FakeDB:
    """Minimal stand-in exposing the ``_pool`` attribute run_as_leader uses."""

    def __init__(self, pool):
        self._pool = pool


@pytest.mark.asyncio
async def test_advisory_lock_exclusive_and_releases_on_disconnect(pg_dsn):
    """Core mechanism: only one session holds LEADER_ID; it auto-releases when
    that session ends, so a follower can take over."""
    c1 = await asyncpg.connect(pg_dsn)
    c2 = await asyncpg.connect(pg_dsn)
    try:
        assert await c1.fetchval("SELECT pg_try_advisory_lock($1)", LEADER_ID) is True
        assert await c2.fetchval("SELECT pg_try_advisory_lock($1)", LEADER_ID) is False
        await c1.close()  # leader "dies": session ends, lock auto-releases
        acquired = False
        for _ in range(50):
            if await c2.fetchval("SELECT pg_try_advisory_lock($1)", LEADER_ID):
                acquired = True
                break
            await asyncio.sleep(0.1)
        assert acquired, "follower never acquired after leader disconnected"
    finally:
        await c2.close()


@pytest.mark.asyncio
async def test_run_as_leader_sets_then_clears(pg_dsn):
    """run_as_leader acquires leadership (sets is_leader, genuinely holds the
    lock) and releases + clears on shutdown."""
    pool = await asyncpg.create_pool(pg_dsn, min_size=1, max_size=3)
    db = _FakeDB(pool)
    leader_election.is_leader.clear()
    shutdown = asyncio.Event()
    task = asyncio.create_task(leader_election.run_as_leader(db, LEADER_ID, shutdown))
    try:
        await asyncio.wait_for(leader_election.is_leader.wait(), timeout=15)
        assert leader_election.is_leader.is_set()
        # The lock is genuinely held: an independent connection cannot acquire it.
        probe = await asyncpg.connect(pg_dsn)
        try:
            held = await probe.fetchval("SELECT pg_try_advisory_lock($1)", LEADER_ID)
            assert held is False, "LEADER_ID was not actually held by run_as_leader"
        finally:
            await probe.close()
    finally:
        shutdown.set()
        await asyncio.wait_for(task, timeout=15)
    assert not leader_election.is_leader.is_set()
    await pool.close()
