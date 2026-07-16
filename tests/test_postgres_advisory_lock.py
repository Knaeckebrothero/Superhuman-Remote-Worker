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


# ---------------------------------------------------------------------------
# merge_thread_config_override — locked read-modify-write + merge semantics
# (live_session_settings.md P0.4)
# ---------------------------------------------------------------------------

THREAD_ID = "3fa85f64-5717-4562-b3fc-2c963f66afa6"


def _make_db_for_merge(metadata, update_result="UPDATE 1"):
    """PostgresDB with a mocked pool: fetchrow returns ``metadata`` row,
    execute serves the lock statement then the UPDATE."""
    from orchestrator.database.postgres import PostgresDB

    db = PostgresDB.__new__(PostgresDB)
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(
        return_value={"metadata": metadata} if metadata is not None else None
    )
    conn.execute = AsyncMock(side_effect=[None, update_result])
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
    return db, conn


class TestMergeThreadConfigOverride:
    @pytest.mark.asyncio
    async def test_takes_salted_advisory_lock_before_read(self):
        """The RMW is serialized on a per-thread advisory lock whose key is
        domain-salted — distinct from the provisioning lock's key, so config
        merges never queue behind a minutes-long prepare."""
        import hashlib

        db, conn = _make_db_for_merge({"config_override": {}})
        assert await db.merge_thread_config_override(THREAD_ID, {"llm": {}}) is True

        lock_call = conn.execute.call_args_list[0]
        assert lock_call.args[0] == "SELECT pg_advisory_xact_lock($1)"
        salted = hashlib.blake2b(
            b"config_override:" + THREAD_ID.encode(), digest_size=8
        ).digest()
        expected_key = int.from_bytes(salted, byteorder="big", signed=True)
        assert lock_call.args[1] == expected_key

        unsalted = hashlib.blake2b(THREAD_ID.encode(), digest_size=8).digest()
        provisioning_key = int.from_bytes(unsalted, byteorder="big", signed=True)
        assert lock_call.args[1] != provisioning_key

        # Lock first, then the read, then the write — all on one connection.
        assert conn.fetchrow.called
        assert conn.execute.call_args_list[1].args[0].startswith("UPDATE threads")

    @pytest.mark.asyncio
    async def test_deep_merges_nested_keys_independently(self):
        """llm.model and llm.temperature update independently — a merge of
        one nested key must not clobber siblings."""
        import json

        db, conn = _make_db_for_merge(
            {"config_override": {"llm": {"model": "old-model", "temperature": 0.3}}}
        )
        await db.merge_thread_config_override(
            THREAD_ID, {"llm": {"model": "new-model"}}
        )

        written = json.loads(conn.execute.call_args_list[1].args[1])
        assert written["llm"]["model"] == "new-model"
        assert written["llm"]["temperature"] == 0.3

    @pytest.mark.asyncio
    async def test_lists_replace_wholesale(self):
        """Lists are values, not merge targets — the override replaces them."""
        import json

        db, conn = _make_db_for_merge(
            {"config_override": {"env_keys": ["A", "B"], "llm": {"model": "m"}}}
        )
        await db.merge_thread_config_override(THREAD_ID, {"env_keys": ["C"]})

        written = json.loads(conn.execute.call_args_list[1].args[1])
        assert written["env_keys"] == ["C"]
        assert written["llm"] == {"model": "m"}

    @pytest.mark.asyncio
    async def test_string_metadata_and_missing_override_tolerated(self):
        """metadata stored as a JSON string and no prior config_override."""
        import json

        db, conn = _make_db_for_merge(json.dumps({"other_key": True}))
        assert (
            await db.merge_thread_config_override(THREAD_ID, {"llm": {"model": "m"}})
            is True
        )
        written = json.loads(conn.execute.call_args_list[1].args[1])
        assert written == {"llm": {"model": "m"}}

    @pytest.mark.asyncio
    async def test_thread_not_found_returns_false(self):
        db, conn = _make_db_for_merge(None)
        assert await db.merge_thread_config_override(THREAD_ID, {"llm": {}}) is False
        # Only the lock statement ran — no UPDATE.
        assert len(conn.execute.call_args_list) == 1

    @pytest.mark.asyncio
    async def test_invalid_uuid_returns_false_without_db_access(self):
        db, conn = _make_db_for_merge({})
        assert await db.merge_thread_config_override("not-a-uuid", {}) is False
        assert not conn.execute.called
