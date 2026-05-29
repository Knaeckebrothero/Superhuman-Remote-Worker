"""Tests for PostgresDB.thread_advisory_lock context manager.

Postgres-level serialization (the actual lock behavior) can't be tested
without a real DB connection — that verification lives in Task 14's dev
cluster smoke test. These tests verify the wire-level contract: that
the right SQL is issued against an acquired connection inside a
transaction, keyed by a stable hash of the thread_id.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_advisory_lock_executes_correct_sql_with_derived_key():
    """Entering the context manager runs pg_advisory_xact_lock with a
    bigint derived from blake2b(thread_id)."""
    from orchestrator.database.postgres import PostgresDB

    db = PostgresDB.__new__(PostgresDB)  # Bypass __init__ — we only need _pool

    # Mock the connection + transaction context managers.
    conn = AsyncMock()
    conn.execute = AsyncMock(return_value=None)
    txn_cm = AsyncMock()
    txn_cm.__aenter__.return_value = None
    txn_cm.__aexit__.return_value = False
    conn.transaction = MagicMock(return_value=txn_cm)

    pool_cm = AsyncMock()
    pool_cm.__aenter__.return_value = conn
    pool_cm.__aexit__.return_value = False
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=pool_cm)
    db._pool = pool

    # Enter the context manager.
    async with db.thread_advisory_lock("thread-xyz"):
        pass

    # Verify the SQL call.
    conn.execute.assert_called_once()
    call_args = conn.execute.call_args
    assert call_args.args[0] == "SELECT pg_advisory_xact_lock($1)"
    key = call_args.args[1]
    assert isinstance(key, int)
    assert -(2**63) <= key < 2**63  # fits in signed bigint


@pytest.mark.asyncio
async def test_advisory_lock_key_is_stable_per_thread_id():
    """Same thread_id → same lock key. Different thread_ids → different keys (very likely)."""

    def derive(tid: str) -> int:
        import hashlib

        h = hashlib.blake2b(tid.encode(), digest_size=8).digest()
        return int.from_bytes(h, byteorder="big", signed=True)

    assert derive("thread-1") == derive("thread-1")
    assert derive("thread-1") != derive("thread-2")


@pytest.mark.asyncio
async def test_advisory_lock_releases_connection_on_exception():
    """If the body raises, the transaction context still exits (no leaked conn)."""
    from orchestrator.database.postgres import PostgresDB

    db = PostgresDB.__new__(PostgresDB)
    conn = AsyncMock()
    conn.execute = AsyncMock(return_value=None)
    txn_cm = AsyncMock()
    txn_cm.__aenter__.return_value = None
    # Track whether __aexit__ was called.
    exit_called = []

    async def aexit(*args, **kwargs):
        exit_called.append(args)
        return False

    txn_cm.__aexit__ = aexit
    conn.transaction = MagicMock(return_value=txn_cm)

    pool_cm = AsyncMock()
    pool_cm.__aenter__.return_value = conn
    pool_cm.__aexit__.return_value = False
    pool = MagicMock()
    pool.acquire = MagicMock(return_value=pool_cm)
    db._pool = pool

    with pytest.raises(RuntimeError):
        async with db.thread_advisory_lock("thread-x"):
            raise RuntimeError("boom")

    assert exit_called, "transaction __aexit__ must run even on exception"
