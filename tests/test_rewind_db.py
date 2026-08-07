"""Tests for the session-rewind DB primitives in src/database/postgres_db.py."""

import asyncio
from unittest.mock import AsyncMock


from src.database.postgres_db import PostgresDB


class _FakeTxn:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeConn:
    """Captures every statement; scripted fetchrow/fetchval returns."""

    def __init__(self, fetchrow_returns=None, fetchval_returns=None):
        self.calls = []
        self._fetchrow_returns = list(fetchrow_returns or [])
        self._fetchval_returns = list(fetchval_returns or [])

    def transaction(self):
        return _FakeTxn()

    async def execute(self, query, *args):
        self.calls.append(("execute", query, args))
        return "UPDATE 0"

    async def fetchrow(self, query, *args):
        self.calls.append(("fetchrow", query, args))
        return self._fetchrow_returns.pop(0) if self._fetchrow_returns else None

    async def fetchval(self, query, *args):
        self.calls.append(("fetchval", query, args))
        return self._fetchval_returns.pop(0) if self._fetchval_returns else None


class _FakeAcquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


def _db_with(conn):
    db = PostgresDB.__new__(PostgresDB)
    db.acquire = lambda: _FakeAcquire(conn)
    return db


def test_apply_rewind_sweeps_ledgers_and_returns_stats():
    conn = _FakeConn(
        fetchrow_returns=[{"id": "11111111-1111-1111-1111-111111111111"}],
        fetchval_returns=[7, 3],  # swept count, surviving turn
    )
    db = _db_with(conn)
    out = asyncio.run(
        db.apply_rewind(
            "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            from_seq=42,
            mode="conversation",
            actor="ws_client",
        )
    )
    assert out == {
        "rewind_id": "11111111-1111-1111-1111-111111111111",
        "swept": 7,
        "surviving_turn": 3,
    }
    sql_blob = " ".join(q for _, q, _ in conn.calls)
    assert "SET rewound_at = now()" in sql_blob
    assert "seq >= $2" in sql_blob
    assert "rewound_at IS NULL" in sql_blob
    assert "INSERT INTO thread_rewinds" in sql_blob


def test_apply_rewind_code_mode_skips_sweep():
    conn = _FakeConn(
        fetchrow_returns=[{"id": "22222222-2222-2222-2222-222222222222"}],
        fetchval_returns=[3],  # surviving turn only
    )
    db = _db_with(conn)
    out = asyncio.run(
        db.apply_rewind(
            "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            from_seq=42,
            mode="code",
            restored_to_sha="abc123",
        )
    )
    assert out["swept"] == 0
    sweep_calls = [q for _, q, _ in conn.calls if "SET rewound_at" in q]
    assert sweep_calls == []


def test_record_turn_commit_upserts_at_max_seq():
    conn = _FakeConn()
    db = _db_with(conn)
    asyncio.run(db.record_turn_commit("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "sha1"))
    (_, query, args) = conn.calls[0]
    assert "INSERT INTO thread_turn_commits" in query
    assert "ON CONFLICT (thread_id, seq) DO UPDATE" in query
    assert "COALESCE(MAX(seq), 0)" in query
    assert args == ("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "sha1")


def test_resolve_restore_commit_takes_largest_seq_below_target():
    db = PostgresDB.__new__(PostgresDB)
    db.fetchval = AsyncMock(return_value="shaX")
    got = asyncio.run(
        db.resolve_restore_commit("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", 42)
    )
    assert got == "shaX"
    query = db.fetchval.await_args.args[0]
    assert "seq < $2" in query
    assert "ORDER BY seq DESC" in query
    db.fetchval.assert_awaited_once()


def test_live_readers_filter_tombstones():
    """The three agent-side conversation reads must exclude rewound rows."""
    import inspect

    from src.database import postgres_db as mod

    hist_src = inspect.getsource(mod.PostgresDB.get_thread_messages_history)
    ckpt_src = inspect.getsource(mod.PostgresDB.get_latest_compaction_checkpoint)
    seq_src = inspect.getsource(mod.PostgresDB.get_seq_for_message_id)
    assert "rewound_at IS NULL" in hist_src
    assert "rewound_at IS NULL" in ckpt_src
    assert "rewound_at IS NULL" in seq_src
