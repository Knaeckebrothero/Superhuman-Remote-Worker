"""cloud_ro_mounts staging columns (Slice C migration 0057) — DB accessors.

_ro_mount_row must json.loads JSONB payloads (asyncpg returns raw JSON
strings); update_ro_mount_staging must NULL staged_at when clearing.

Mocks the asyncpg connection pool to test method logic without a live DB
(same pattern as tests/test_cloud_ro_mounts_db.py / tests/test_thread_db.py).
"""

import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.database.postgres import PostgresDB


def _make_db_with_conn(mock_conn):
    """Create a PostgresDB instance with acquire() yielding mock_conn."""
    db = PostgresDB.__new__(PostgresDB)
    db._pool = MagicMock()
    db._connection_string = "test"
    db._queries = {}

    @asynccontextmanager
    async def mock_acquire():
        yield mock_conn

    db.acquire = mock_acquire
    return db


def _mock_conn():
    return AsyncMock()


def test_ro_mount_row_parses_jsonb_strings():
    row = {
        "id": "x",
        "credentials": None,
        "etag_baseline": json.dumps({"a.txt": "et1"}),
        "staged_summary": json.dumps(
            {"counts": {"added": 1, "modified": 0, "deleted": 0}}
        ),
    }
    d = PostgresDB._ro_mount_row(row)
    assert d["etag_baseline"] == {"a.txt": "et1"}
    assert d["staged_summary"]["counts"]["added"] == 1


def test_ro_mount_row_leaves_none_jsonb_as_none():
    d = PostgresDB._ro_mount_row(
        {"id": "x", "credentials": None, "etag_baseline": None, "staged_summary": None}
    )
    assert d["etag_baseline"] is None
    assert d["staged_summary"] is None


@pytest.mark.asyncio
async def test_update_ro_mount_baseline_executes_update():
    conn = _mock_conn()
    conn.execute = AsyncMock(return_value="UPDATE 1")
    db = _make_db_with_conn(conn)

    result = await db.update_ro_mount_baseline("row-1", {"a.txt": "et1"})

    assert result is True
    args = conn.execute.call_args.args
    assert "etag_baseline" in args[0]
    assert json.dumps({"a.txt": "et1"}) in args


@pytest.mark.asyncio
async def test_update_ro_mount_staging_clears_staged_at_when_summary_none():
    conn = _mock_conn()
    conn.execute = AsyncMock(return_value="UPDATE 1")
    db = _make_db_with_conn(conn)

    result = await db.update_ro_mount_staging(
        "row-1", staged_epoch=2, staged_summary=None
    )

    assert result is True
    args = conn.execute.call_args.args
    # SQL sets staged_at via a CASE that resolves to NULL when the summary
    # param is NULL (i.e. staged_summary=None clears staged_at).
    assert "staged_at" in args[0] and "NULL" in args[0]
    assert args[-1] is None  # json.dumps(staged_summary) short-circuits to None


# --------------------------------------------------------------------------- #
# require_active — post-review hardening: apply/reject bookkeeping must
# still reach a row the idle-drain reconciler already revoked (reads are
# revoked-tolerant — Task 8 — writes were not, until this).
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_update_ro_mount_baseline_default_scopes_to_active():
    conn = _mock_conn()
    conn.execute = AsyncMock(return_value="UPDATE 1")
    db = _make_db_with_conn(conn)

    await db.update_ro_mount_baseline("row-1", {"a.txt": "et1"})

    sql = conn.execute.call_args.args[0]
    assert "status = 'active'" in sql


@pytest.mark.asyncio
async def test_update_ro_mount_baseline_require_active_false_drops_status_clause():
    conn = _mock_conn()
    conn.execute = AsyncMock(return_value="UPDATE 1")
    db = _make_db_with_conn(conn)

    result = await db.update_ro_mount_baseline(
        "row-1", {"a.txt": "et1"}, require_active=False
    )

    assert result is True
    sql = conn.execute.call_args.args[0]
    assert "status" not in sql
    # The rest of the query is unchanged.
    assert "etag_baseline" in sql
    args = conn.execute.call_args.args
    assert json.dumps({"a.txt": "et1"}) in args


@pytest.mark.asyncio
async def test_update_ro_mount_staging_default_scopes_to_active():
    conn = _mock_conn()
    conn.execute = AsyncMock(return_value="UPDATE 1")
    db = _make_db_with_conn(conn)

    await db.update_ro_mount_staging("row-1", staged_epoch=2, staged_summary=None)

    sql = conn.execute.call_args.args[0]
    assert "status = 'active'" in sql


@pytest.mark.asyncio
async def test_update_ro_mount_staging_require_active_false_drops_status_clause():
    conn = _mock_conn()
    conn.execute = AsyncMock(return_value="UPDATE 1")
    db = _make_db_with_conn(conn)

    result = await db.update_ro_mount_staging(
        "row-1", staged_epoch=2, staged_summary=None, require_active=False
    )

    assert result is True
    sql = conn.execute.call_args.args[0]
    assert "status" not in sql
    # staged_at clearing logic is unaffected by dropping the status clause.
    assert "staged_at" in sql and "NULL" in sql
