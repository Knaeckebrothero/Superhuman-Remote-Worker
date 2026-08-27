"""Tests for orchestrator/database/postgres.py cloud_ro_mounts CRUD.

Mocks the asyncpg connection pool to test method logic without a live DB
(same pattern as tests/test_thread_db.py).
"""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.database.postgres import (
    PostgresDB,
    _encrypt_optional,
)


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


@pytest.mark.asyncio
async def test_create_ro_mount_encrypts_credentials():
    conn = _mock_conn()
    conn.fetchval = AsyncMock(return_value="row-uuid")
    db = _make_db_with_conn(conn)
    row_id = await db.create_ro_mount(
        thread_id="11111111-1111-1111-1111-111111111111",
        user_id="22222222-2222-2222-2222-222222222222",
        backend="nextcloud",
        reader_id="srw-reader-abc",
        grant_handle='{"group_id":"g1","folder_id":"7"}',
        credentials="s3cr3t-app-pw",
        webdav_url="https://nc/remote.php/dav/files/srw-reader-abc/Proj/",
        auth_kind="basic",
        expected_runtime_generation="33333333-3333-4333-8333-333333333333",
        engage_attempt="44444444-4444-4444-8444-444444444444",
    )
    assert row_id == "row-uuid"
    # The credential passed to SQL must be ciphertext, not the plaintext.
    args = conn.fetchval.call_args.args
    assert "s3cr3t-app-pw" not in args, "plaintext credential reached SQL"


@pytest.mark.asyncio
async def test_get_ro_mount_decrypts_credentials():
    conn = _mock_conn()
    conn.fetchrow = AsyncMock(
        return_value={
            "id": "row-uuid",
            "thread_id": "t",
            "user_id": "u",
            "backend": "nextcloud",
            "reader_id": "srw-reader-abc",
            "grant_handle": "{}",
            "credentials": _encrypt_optional("s3cr3t-app-pw"),
            "webdav_url": "https://nc/...",
            "auth_kind": "basic",
            "status": "active",
            "created_at": None,
            "revoked_at": None,
        }
    )
    db = _make_db_with_conn(conn)
    row = await db.get_ro_mount_by_thread("t")
    assert row["credentials"] == "s3cr3t-app-pw"


@pytest.mark.asyncio
async def test_get_ro_mount_missing_returns_none():
    conn = _mock_conn()
    conn.fetchrow = AsyncMock(return_value=None)
    db = _make_db_with_conn(conn)
    assert await db.get_ro_mount_by_thread("nope") is None


@pytest.mark.asyncio
async def test_list_active_ro_mounts_decrypts_each():
    conn = _mock_conn()
    conn.fetch = AsyncMock(
        return_value=[
            {
                "id": "r1",
                "thread_id": "t1",
                "user_id": "u1",
                "backend": "nextcloud",
                "reader_id": "srw-reader-a",
                "grant_handle": "{}",
                "credentials": _encrypt_optional("pw-a"),
                "webdav_url": "x",
                "auth_kind": "basic",
                "status": "active",
                "created_at": None,
                "revoked_at": None,
            },
            {
                "id": "r2",
                "thread_id": "t2",
                "user_id": "u2",
                "backend": "opencloud",
                "reader_id": "srw-reader-b",
                "grant_handle": "{}",
                "credentials": None,  # OC bearer — no stored credential
                "webdav_url": "y",
                "auth_kind": "keycloak_user_impersonation",
                "status": "active",
                "created_at": None,
                "revoked_at": None,
            },
        ]
    )
    db = _make_db_with_conn(conn)
    rows = await db.list_active_ro_mounts()
    assert [r["credentials"] for r in rows] == ["pw-a", None]


@pytest.mark.asyncio
async def test_mark_ro_mount_revoked_reports_success():
    conn = _mock_conn()
    conn.execute = AsyncMock(return_value="UPDATE 1")
    db = _make_db_with_conn(conn)
    assert await db.mark_ro_mount_revoked("r2") is True


@pytest.mark.asyncio
async def test_mark_ro_mount_revoked_reports_noop():
    conn = _mock_conn()
    conn.execute = AsyncMock(return_value="UPDATE 0")
    db = _make_db_with_conn(conn)
    assert await db.mark_ro_mount_revoked("already-revoked") is False
