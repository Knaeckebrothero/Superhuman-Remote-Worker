"""Tests for the session-rewind DB primitives in src/database/postgres_db.py."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from src.api.lease_context import LeaseLostError
from src.database.postgres_db import PostgresDB


class _FakeTxn:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeConn:
    """Captures every statement; scripted fetchrow/fetchval returns."""

    def __init__(
        self,
        fetchrow_returns=None,
        fetchval_returns=None,
        fetch_returns=None,
    ):
        self.calls = []
        self._fetchrow_returns = list(fetchrow_returns or [])
        self._fetchval_returns = list(fetchval_returns or [])
        self._fetch_returns = list(fetch_returns or [])

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

    async def fetch(self, query, *args):
        self.calls.append(("fetch", query, args))
        return self._fetch_returns.pop(0) if self._fetch_returns else []


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
        fetchval_returns=[1, 7, 3],  # thread lock, swept count, surviving turn
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
        fetchval_returns=[1, 3],  # thread lock, surviving turn only
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


@pytest.mark.parametrize(
    ("existing_cursor", "surviving_turn", "expected_cursor"),
    [(10, 5, 5), (3, 5, 3), (None, 5, None)],
)
def test_apply_rewind_clamps_existing_memory_cursor_with_transcript(
    existing_cursor, surviving_turn, expected_cursor
):
    class _CursorConn(_FakeConn):
        def __init__(self):
            super().__init__(
                fetchrow_returns=[{"id": "11111111-1111-1111-1111-111111111111"}],
                fetchval_returns=[1, 2, surviving_turn],
            )
            self.memory_cursor = existing_cursor

        async def execute(self, query, *args):
            await super().execute(query, *args)
            if "UPDATE thread_session_runtime_state" in query:
                assert "LEAST(" in query
                if self.memory_cursor is not None:
                    self.memory_cursor = min(self.memory_cursor, int(args[1]))

    conn = _CursorConn()
    db = _db_with(conn)

    asyncio.run(
        db.apply_rewind(
            "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            from_seq=42,
            mode="conversation",
        )
    )

    assert conn.memory_cursor == expected_cursor
    cursor_calls = [q for _, q, _ in conn.calls if "runtime_state" in q]
    assert len(cursor_calls) == 1


def test_apply_rewind_code_mode_does_not_move_memory_cursor():
    conn = _FakeConn(
        fetchrow_returns=[{"id": "22222222-2222-2222-2222-222222222222"}],
        fetchval_returns=[1, 5],
    )
    db = _db_with(conn)

    asyncio.run(
        db.apply_rewind(
            "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            from_seq=42,
            mode="code",
        )
    )

    assert not [q for _, q, _ in conn.calls if "runtime_state" in q]


def test_apply_rewind_cursor_failure_rolls_back_transcript_sweep():
    class _RollbackConn:
        def __init__(self):
            self.messages_swept = False
            self.memory_cursor = 10
            self._snapshot = None
            self.calls = []

        class _Txn:
            def __init__(self, conn):
                self.conn = conn

            async def __aenter__(self):
                self.conn._snapshot = (
                    self.conn.messages_swept,
                    self.conn.memory_cursor,
                )
                return self

            async def __aexit__(self, exc_type, *_exc):
                if exc_type is not None:
                    (
                        self.conn.messages_swept,
                        self.conn.memory_cursor,
                    ) = self.conn._snapshot
                return False

        def transaction(self):
            return self._Txn(self)

        async def fetchval(self, query, *_args):
            self.calls.append(query)
            if "SELECT 1 FROM threads" in query:
                return 1
            if "WITH swept" in query:
                self.messages_swept = True
                return 2
            return 5

        async def fetch(self, query, *_args):
            self.calls.append(query)
            return []

        async def fetchrow(self, query, *_args):
            self.calls.append(query)
            return {"id": "11111111-1111-1111-1111-111111111111"}

        async def execute(self, query, *_args):
            self.calls.append(query)
            if "UPDATE thread_session_runtime_state" in query:
                self.memory_cursor = 5
                raise RuntimeError("cursor write failed")

    conn = _RollbackConn()
    db = _db_with(conn)

    with pytest.raises(RuntimeError, match="cursor write failed"):
        asyncio.run(
            db.apply_rewind(
                "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                from_seq=42,
                mode="conversation",
            )
        )

    assert conn.messages_swept is False
    assert conn.memory_cursor == 10


def test_apply_rewind_refuses_unfinished_memory_source_before_sweep():
    conn = _FakeConn(fetchval_returns=[1], fetch_returns=[[{"producer_id": "p1"}]])
    db = _db_with(conn)

    with pytest.raises(RuntimeError, match="final-memory extraction"):
        asyncio.run(
            db.apply_rewind(
                "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                from_seq=42,
                mode="conversation",
            )
        )

    assert not [q for _, q, _ in conn.calls if "SET rewound_at" in q]


def test_resweep_rewind_sweeps_remaining_strays():
    conn = _FakeConn(fetchval_returns=[2])
    db = _db_with(conn)
    out = asyncio.run(
        db.resweep_rewind("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", from_seq=42)
    )
    assert out == 2
    (_, query, args) = conn.calls[0]
    assert "SET rewound_at = now()" in query
    assert "seq >= $2" in query
    assert "rewound_at IS NULL" in query
    assert args == ("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", 42)
    # Narrow mop-up, not a second rewind: no ledger insert, no surviving-turn
    # readback — just the one UPDATE.
    assert len(conn.calls) == 1


def test_resweep_rewind_idempotent_when_nothing_stray():
    conn = _FakeConn(fetchval_returns=[0])
    db = _db_with(conn)
    out = asyncio.run(
        db.resweep_rewind("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", from_seq=42)
    )
    assert out == 0


def test_record_turn_commit_upserts_at_max_seq():
    conn = _FakeConn()
    db = _db_with(conn)
    asyncio.run(db.record_turn_commit("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "sha1"))
    (_, query, args) = conn.calls[0]
    assert "INSERT INTO thread_turn_commits" in query
    assert "ON CONFLICT (thread_id, seq) DO UPDATE" in query
    assert "COALESCE(MAX(seq), 0)" in query
    assert args == ("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "sha1")


def test_record_turn_commit_stateless_fence_is_first_statement():
    conn = _FakeConn()
    db = _db_with(conn)
    lease = ("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", 17)

    async def fence(fenced_conn, received_lease):
        assert fenced_conn is conn
        conn.calls.append(("fence", "run_queue", (received_lease,)))

    with (
        patch(
            "src.database.postgres_db._active_run_queue_lease",
            return_value=lease,
        ),
        patch(
            "src.database.postgres_db._require_run_queue_fence",
            side_effect=fence,
        ),
    ):
        asyncio.run(db.record_turn_commit(lease[0], "sha1"))

    assert [operation for operation, _query, _args in conn.calls] == [
        "fence",
        "execute",
    ]
    assert "INSERT INTO thread_turn_commits" in conn.calls[1][1]


def test_record_turn_commit_stale_lease_writes_nothing():
    conn = _FakeConn()
    db = _db_with(conn)
    with (
        patch(
            "src.database.postgres_db._active_run_queue_lease",
            return_value=("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", 17),
        ),
        patch(
            "src.database.postgres_db._require_run_queue_fence",
            new=AsyncMock(side_effect=LeaseLostError("stale")),
        ),
        pytest.raises(LeaseLostError, match="stale"),
    ):
        asyncio.run(
            db.record_turn_commit(
                "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                "sha1",
            )
        )

    assert conn.calls == []


def test_list_workspace_turn_commits_is_fenced_before_complete_ordered_read():
    conn = _FakeConn(
        fetch_returns=[
            [
                {"commit_sha": "sha-current"},
                {"commit_sha": "sha-previous"},
            ]
        ]
    )
    db = _db_with(conn)
    lease = ("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", 18)

    async def fence(fenced_conn, received_lease):
        assert fenced_conn is conn
        conn.calls.append(("fence", "run_queue", (received_lease,)))

    with (
        patch(
            "src.database.postgres_db._active_run_queue_lease",
            return_value=lease,
        ),
        patch(
            "src.database.postgres_db._require_run_queue_fence",
            side_effect=fence,
        ),
    ):
        result = asyncio.run(db.list_workspace_turn_commits(lease[0]))

    assert result == ["sha-current", "sha-previous"]
    assert [operation for operation, _query, _args in conn.calls] == [
        "fence",
        "fetch",
    ]
    assert "ORDER BY seq DESC" in conn.calls[1][1]
    assert "OFFSET" not in conn.calls[1][1]


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
