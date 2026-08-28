# tests/test_ssh_attachment_audit.py
import pathlib

import pytest

from database.postgres import PostgresDB

MIGRATIONS = (
    pathlib.Path(__file__).resolve().parents[1]
    / "orchestrator"
    / "database"
    / "migrations"
    / "app"
)
SQL = MIGRATIONS / "0204_ssh_attachments.sql"


def test_migration_exists():
    assert SQL.exists()


def test_records_the_fields_the_existing_trail_cannot():
    """agent_audit has no user_id, client_ip or thread_id, which is exactly why
    this table exists."""
    body = SQL.read_text()
    for column in (
        "thread_id",
        "user_id",
        "ssh_key_id",
        "client_ip",
        "attached_at",
        "detached_at",
        "channels",
    ):
        assert column in body, f"missing column {column}"


def test_survives_key_deletion():
    """Revoking a key must not erase the record of what it was used for."""
    assert "ssh_key_id" in SQL.read_text()
    assert "ON DELETE SET NULL" in SQL.read_text()


class FakeConn:
    def __init__(self, fetchval=None):
        self._fetchval = fetchval
        self.calls = []

    async def fetchval(self, sql, *args):
        self.calls.append((sql, args))
        return self._fetchval

    async def execute(self, sql, *args):
        self.calls.append((sql, args))
        return "UPDATE 1"


class FakeAcquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


def _db(conn):
    db = PostgresDB.__new__(PostgresDB)
    db.acquire = lambda: FakeAcquire(conn)
    return db


@pytest.mark.asyncio
async def test_record_returns_an_id():
    conn = FakeConn(fetchval="a1")
    got = await _db(conn).record_ssh_attachment(
        thread_id="00000000-0000-0000-0000-000000000002",
        user_id="00000000-0000-0000-0000-000000000001",
        ssh_key_id="00000000-0000-0000-0000-000000000003",
        client_ip="203.0.113.7",
        handle="s-7f3a91c2",
    )
    assert got == "a1"


@pytest.mark.asyncio
async def test_close_sets_detached_at_and_channels():
    conn = FakeConn()
    await _db(conn).close_ssh_attachment("a1", ["session", "sftp"])
    sql, args = conn.calls[0]
    assert "detached_at" in sql
    assert "session" in args[1]
