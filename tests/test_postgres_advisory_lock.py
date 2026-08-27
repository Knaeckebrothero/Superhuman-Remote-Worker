"""Tests for PostgresDB.thread_advisory_lock context manager.

These tests pin the dedicated-session contract: owners never consume the
application pool, waiters use try-lock polling rather than retaining blocked
PostgreSQL backends, and every exit releases the session lock.
"""

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_advisory_lock_executes_correct_sql_with_derived_key(monkeypatch):
    """The dedicated owner uses a stable bigint session-lock key."""
    from orchestrator.database import postgres as postgres_module
    from orchestrator.database.postgres import PostgresDB

    db = PostgresDB.__new__(PostgresDB)
    db._connection_string = "postgresql://scratch/test"
    db._command_timeout = 60
    conn = MagicMock()
    conn.fetchval = AsyncMock(side_effect=[True, True])
    conn.close = AsyncMock()
    conn.terminate = MagicMock()
    connect = AsyncMock(return_value=conn)
    monkeypatch.setattr(postgres_module.asyncpg, "connect", connect)

    async with db.thread_advisory_lock("thread-xyz") as acquired:
        assert acquired is True

    assert conn.fetchval.await_args_list[0].args[0] == (
        "SELECT pg_try_advisory_lock($1)"
    )
    assert conn.fetchval.await_args_list[1].args[0] == "SELECT pg_advisory_unlock($1)"
    key = conn.fetchval.await_args_list[0].args[1]
    assert isinstance(key, int)
    assert -(2**63) <= key < 2**63  # fits in signed bigint
    assert conn.fetchval.await_args_list[1].args[1] == key
    connect.assert_awaited_once()
    conn.close.assert_awaited_once_with(timeout=5)


@pytest.mark.asyncio
async def test_dedicated_lock_preserves_production_tls_dsn(monkeypatch):
    """Direct owners use the pool's exact DSN, including TLS verification."""
    from orchestrator.database import postgres as postgres_module
    from orchestrator.database.postgres import PostgresDB

    db = PostgresDB.__new__(PostgresDB)
    db._connection_string = (
        "postgresql://srw@postgres.internal/srw?"
        "sslmode=verify-full&sslrootcert=/etc/srw/postgres-ca.pem"
    )
    db._command_timeout = 47
    conn = MagicMock()
    conn.fetchval = AsyncMock(side_effect=[True, True])
    conn.close = AsyncMock()
    conn.terminate = MagicMock()
    connect = AsyncMock(return_value=conn)
    monkeypatch.setattr(postgres_module.asyncpg, "connect", connect)

    async with db.thread_advisory_lock("thread-tls") as acquired:
        assert acquired is True

    connect.assert_awaited_once_with(
        db._connection_string,
        timeout=5,
        command_timeout=47,
    )


@pytest.mark.asyncio
async def test_advisory_lock_key_is_stable_per_thread_id():
    """UUID spelling cannot split lifecycle or datasource serialization."""
    from orchestrator.database.postgres import (
        _thread_datasource_lock_key,
        _thread_lifecycle_lock_key,
    )

    lower = "3fa85f64-5717-4562-b3fc-2c963f66afa6"
    alternate = "{3FA85F64-5717-4562-B3FC-2C963F66AFA6}"
    other = "4fa85f64-5717-4562-b3fc-2c963f66afa6"
    assert _thread_lifecycle_lock_key(lower) == _thread_lifecycle_lock_key(alternate)
    assert _thread_datasource_lock_key(lower) == _thread_datasource_lock_key(alternate)
    assert _thread_lifecycle_lock_key(lower) != _thread_lifecycle_lock_key(other)
    assert _thread_datasource_lock_key(lower) != _thread_datasource_lock_key(other)


@pytest.mark.asyncio
@pytest.mark.parametrize("domain", ["lifecycle", "datasource"])
async def test_alternate_uuid_spellings_are_mutually_exclusive(monkeypatch, domain):
    from orchestrator.database import postgres as postgres_module
    from orchestrator.database.postgres import PostgresDB

    db = PostgresDB.__new__(PostgresDB)
    db._connection_string = "postgresql://scratch/test"
    db._command_timeout = 60
    db._dedicated_advisory_lock_slot_groups = {
        "lifecycle": asyncio.Semaphore(2),
        "datasource": asyncio.Semaphore(2),
        "workspace": asyncio.Semaphore(2),
    }
    held: set[int] = set()

    async def connect(*_args, **_kwargs):
        conn = MagicMock()
        conn.terminate = MagicMock()
        conn.close = AsyncMock()

        async def fetchval(sql, key):
            if "pg_try_advisory_lock" in sql:
                if key in held:
                    return False
                held.add(key)
                return True
            assert "pg_advisory_unlock" in sql
            held.remove(key)
            return True

        conn.fetchval = AsyncMock(side_effect=fetchval)
        return conn

    monkeypatch.setattr(postgres_module.asyncpg, "connect", connect)
    lower = "3fa85f64-5717-4562-b3fc-2c963f66afa6"
    alternate = "{3FA85F64-5717-4562-B3FC-2C963F66AFA6}"
    owner_entered = asyncio.Event()
    release_owner = asyncio.Event()
    waiter_entered = asyncio.Event()

    @asynccontextmanager
    async def lock(thread_id):
        if domain == "lifecycle":
            async with db.thread_advisory_lock(thread_id) as owner:
                assert owner is True
                yield
        else:
            async with db.thread_datasource_lock(thread_id):
                yield

    async def owner():
        async with lock(lower):
            owner_entered.set()
            await release_owner.wait()

    async def waiter():
        async with lock(alternate):
            waiter_entered.set()

    owner_task = asyncio.create_task(owner())
    await owner_entered.wait()
    waiter_task = asyncio.create_task(waiter())
    await asyncio.sleep(0.01)
    assert not waiter_entered.is_set()
    release_owner.set()
    await asyncio.wait_for(asyncio.gather(owner_task, waiter_task), timeout=1)
    assert waiter_entered.is_set()
    assert not held


@pytest.mark.asyncio
async def test_advisory_lock_releases_connection_on_exception(monkeypatch):
    """If the body raises, the session lock and direct connection are released."""
    from orchestrator.database import postgres as postgres_module
    from orchestrator.database.postgres import PostgresDB

    db = PostgresDB.__new__(PostgresDB)
    db._connection_string = "postgresql://scratch/test"
    db._command_timeout = 60
    conn = MagicMock()
    conn.fetchval = AsyncMock(side_effect=[True, True])
    conn.close = AsyncMock()
    conn.terminate = MagicMock()
    monkeypatch.setattr(
        postgres_module.asyncpg, "connect", AsyncMock(return_value=conn)
    )

    with pytest.raises(RuntimeError):
        async with db.thread_advisory_lock("thread-x"):
            raise RuntimeError("boom")

    assert conn.fetchval.await_args_list[-1].args[0] == "SELECT pg_advisory_unlock($1)"
    conn.close.assert_awaited_once_with(timeout=5)


@pytest.mark.asyncio
async def test_dedicated_lock_slots_keep_waiters_out_of_postgres(monkeypatch):
    """A fan-out waits in-process; only the current owner has a DB backend."""
    from orchestrator.database import postgres as postgres_module
    from orchestrator.database.postgres import PostgresDB

    db = PostgresDB.__new__(PostgresDB)
    db._connection_string = "postgresql://scratch/test"
    db._command_timeout = 60
    db._dedicated_advisory_lock_slot_groups = {"lifecycle": asyncio.Semaphore(1)}
    connections = []

    async def connect(*_args, **_kwargs):
        conn = MagicMock()
        conn.fetchval = AsyncMock(side_effect=[True, True])
        conn.close = AsyncMock()
        conn.terminate = MagicMock()
        connections.append(conn)
        return conn

    monkeypatch.setattr(postgres_module.asyncpg, "connect", connect)
    owner_entered = asyncio.Event()
    owner_release = asyncio.Event()

    async def owner():
        async with db.thread_advisory_lock("owner"):
            owner_entered.set()
            await owner_release.wait()

    async def waiter(index: int):
        async with db.thread_advisory_lock(f"waiter-{index}"):
            return index

    owner_task = asyncio.create_task(owner())
    await owner_entered.wait()
    waiters = [asyncio.create_task(waiter(index)) for index in range(8)]
    await asyncio.sleep(0)
    assert len(connections) == 1
    owner_release.set()
    await owner_task
    assert await asyncio.gather(*waiters) == list(range(8))
    assert all(conn.close.await_count == 1 for conn in connections)


@pytest.mark.asyncio
async def test_stateless_workspace_lock_uses_dedicated_session_and_unlocks(monkeypatch):
    """Slow provisioning must not reserve a slot from the ordinary DB pool."""
    from orchestrator.database import postgres as postgres_module
    from orchestrator.database.postgres import PostgresDB

    db = PostgresDB.__new__(PostgresDB)
    db._connection_string = "postgresql://scratch/test"
    db._command_timeout = 60
    conn = MagicMock()
    conn.fetchval = AsyncMock(side_effect=[True, True])
    conn.close = AsyncMock()
    conn.terminate = MagicMock()
    connect = AsyncMock(return_value=conn)
    monkeypatch.setattr(postgres_module.asyncpg, "connect", connect)

    async with db.stateless_session_workspace_ensure_lock("thread-xyz") as owner:
        assert owner is True

    sql = [" ".join(call.args[0].split()) for call in conn.fetchval.await_args_list]
    assert sql == [
        "SELECT pg_try_advisory_lock($1)",
        "SELECT pg_advisory_unlock($1)",
    ]
    assert (
        conn.fetchval.await_args_list[0].args[1]
        == conn.fetchval.await_args_list[1].args[1]
    )
    connect.assert_awaited_once_with(
        db._connection_string,
        timeout=5,
        command_timeout=60,
    )
    conn.close.assert_awaited_once_with(timeout=5)
    conn.terminate.assert_not_called()


@pytest.mark.asyncio
async def test_stateless_terminal_workspace_lock_waits_for_reconcile_owner(monkeypatch):
    from orchestrator.database import postgres as postgres_module
    from orchestrator.database.postgres import PostgresDB

    db = PostgresDB.__new__(PostgresDB)
    db._connection_string = "postgresql://scratch/test"
    db._command_timeout = 60
    conn = MagicMock()
    conn.fetchval = AsyncMock(side_effect=[True, True])
    conn.close = AsyncMock()
    conn.terminate = MagicMock()
    monkeypatch.setattr(
        postgres_module.asyncpg,
        "connect",
        AsyncMock(return_value=conn),
    )

    async with db.stateless_session_workspace_ensure_lock(
        "thread-xyz", wait=True
    ) as owner:
        assert owner is True

    sql = [" ".join(call.args[0].split()) for call in conn.fetchval.await_args_list]
    assert sql == [
        "SELECT pg_try_advisory_lock($1)",
        "SELECT pg_advisory_unlock($1)",
    ]
    assert (
        conn.fetchval.await_args_list[0].args[1]
        == conn.fetchval.await_args_list[1].args[1]
    )


@pytest.mark.asyncio
async def test_lifecycle_fanout_can_nest_datasource_owners(monkeypatch):
    """N lifecycle owners cannot consume the nested datasource budget."""
    from orchestrator.database import postgres as postgres_module
    from orchestrator.database.postgres import PostgresDB

    db = PostgresDB.__new__(PostgresDB)
    db._connection_string = "postgresql://scratch/test"
    db._command_timeout = 60
    db._dedicated_advisory_lock_slot_groups = {
        "lifecycle": asyncio.Semaphore(2),
        "datasource": asyncio.Semaphore(2),
        "workspace": asyncio.Semaphore(2),
    }
    open_connections = 0
    peak_connections = 0

    async def connect(*_args, **_kwargs):
        nonlocal open_connections, peak_connections
        open_connections += 1
        peak_connections = max(peak_connections, open_connections)
        conn = MagicMock()
        conn.fetchval = AsyncMock(side_effect=[True, True])
        conn.terminate = MagicMock()

        async def close(*, timeout):
            del timeout
            nonlocal open_connections
            open_connections -= 1

        conn.close = AsyncMock(side_effect=close)
        return conn

    monkeypatch.setattr(postgres_module.asyncpg, "connect", connect)
    outer_barrier = asyncio.Barrier(2)
    nested_barrier = asyncio.Barrier(2)

    async def nested_owner(index: int) -> int:
        async with db.thread_advisory_lock(f"thread-{index}") as owner:
            assert owner is True
            await outer_barrier.wait()
            async with db.thread_datasource_lock(f"thread-{index}"):
                await nested_barrier.wait()
                return index

    assert await asyncio.wait_for(
        asyncio.gather(*(nested_owner(index) for index in range(2))),
        timeout=1,
    ) == [0, 1]
    # Two lifecycle owners plus their two nested datasource owners. The
    # per-domain caps, rather than one non-reentrant shared cap, are decisive.
    assert peak_connections == 4
    assert open_connections == 0


def test_stateless_workspace_lock_canonicalizes_uuid_spelling():
    from orchestrator.database.postgres import (
        _stateless_session_workspace_ensure_lock_key,
    )

    lower = "3fa85f64-5717-4562-b3fc-2c963f66afa6"
    upper = "3FA85F64-5717-4562-B3FC-2C963F66AFA6"
    assert _stateless_session_workspace_ensure_lock_key(lower) == (
        _stateless_session_workspace_ensure_lock_key(upper)
    )


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
