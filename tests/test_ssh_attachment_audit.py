# tests/test_ssh_attachment_audit.py
import pathlib
import re
from uuid import UUID

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
    """Revoking a key must not erase the record of what it was used for.

    Pinned as one clause, not two independent substring checks: user_id also
    carries ON DELETE SET NULL, so checking "ssh_key_id" and "ON DELETE SET
    NULL" separately would still pass if ssh_key_id alone were switched to
    CASCADE.
    """
    assert re.search(
        r"ssh_key_id\s+uuid\s+REFERENCES public\.user_ssh_keys\(id\)\s+"
        r"ON DELETE SET NULL",
        SQL.read_text(),
    )


class FakeConn:
    def __init__(self, fetchval=None):
        self._fetchval = fetchval
        self.calls = []
        # Count of times a connection was actually checked out, separate from
        # `calls`: proves "no connection taken", not just "no statement
        # issued" — the two are different claims (see
        # test_close_rejects_a_malformed_attachment_id).
        self.acquired = 0

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
        self.conn.acquired += 1
        return self.conn

    async def __aexit__(self, *exc):
        return False


def _db(conn):
    db = PostgresDB.__new__(PostgresDB)
    db.acquire = lambda: FakeAcquire(conn)
    return db


@pytest.mark.asyncio
async def test_record_returns_an_id():
    thread_id = "00000000-0000-0000-0000-000000000002"
    conn = FakeConn(fetchval=UUID("00000000-0000-0000-0000-0000000000a1"))
    got = await _db(conn).record_ssh_attachment(
        thread_id=thread_id,
        user_id="00000000-0000-0000-0000-000000000001",
        ssh_key_id="00000000-0000-0000-0000-000000000003",
        client_ip="203.0.113.7",
        handle="s-7f3a91c2",
    )
    assert got == "00000000-0000-0000-0000-0000000000a1"
    # Bound as a UUID, not the raw string — same contract as
    # close_ssh_attachment. Pinned here too so deleting the UUID() wrap on
    # either method fails a test, not just one of them.
    assert conn.calls[0][1][0] == UUID(thread_id)


@pytest.mark.asyncio
async def test_close_sets_detached_at_and_channels():
    conn = FakeConn()
    attachment_id = "00000000-0000-0000-0000-0000000000a1"
    await _db(conn).close_ssh_attachment(attachment_id, ["session", "sftp"])
    sql, args = conn.calls[0]
    assert "detached_at" in sql
    assert "session" in args[1]
    # Bound as a UUID, not the raw string, so a malformed id fails here rather
    # than reaching the driver — same contract as record_ssh_attachment.
    assert args[0] == UUID(attachment_id)


@pytest.mark.asyncio
async def test_close_rejects_a_malformed_attachment_id():
    conn = FakeConn()
    with pytest.raises(ValueError):
        await _db(conn).close_ssh_attachment("a1", ["session"])
    assert conn.calls == []
    # conn.calls == [] only proves no statement was issued. The id is parsed
    # before self.acquire() runs, so a malformed id never checks a connection
    # out of the pool either -- assert that claim directly, not just its
    # weaker cousin.
    assert conn.acquired == 0


@pytest.mark.asyncio
async def test_record_rejects_a_malformed_thread_id_before_acquiring():
    conn = FakeConn()
    with pytest.raises(ValueError):
        await _db(conn).record_ssh_attachment(
            thread_id="not-a-uuid",
            user_id="00000000-0000-0000-0000-000000000001",
            ssh_key_id=None,
            client_ip="203.0.113.7",
            handle="s-7f3a91c2",
        )
    assert conn.calls == []
    assert conn.acquired == 0


@pytest.mark.asyncio
async def test_record_nulls_a_socket_path_client_ip():
    """A unix-socket peer has no parseable address. The audit row must still
    be written -- a row missing client_ip beats no row at all."""
    conn = FakeConn(fetchval=UUID("00000000-0000-0000-0000-0000000000a1"))
    await _db(conn).record_ssh_attachment(
        thread_id="00000000-0000-0000-0000-000000000002",
        user_id="00000000-0000-0000-0000-000000000001",
        ssh_key_id=None,
        client_ip="/run/ssh.sock",
        handle="s-7f3a91c2",
    )
    assert conn.calls[0][1][4] is None


@pytest.mark.asyncio
async def test_record_nulls_an_empty_client_ip():
    conn = FakeConn(fetchval=UUID("00000000-0000-0000-0000-0000000000a1"))
    await _db(conn).record_ssh_attachment(
        thread_id="00000000-0000-0000-0000-000000000002",
        user_id="00000000-0000-0000-0000-000000000001",
        ssh_key_id=None,
        client_ip="",
        handle="s-7f3a91c2",
    )
    assert conn.calls[0][1][4] is None


@pytest.mark.asyncio
async def test_record_keeps_a_valid_client_ip():
    conn = FakeConn(fetchval=UUID("00000000-0000-0000-0000-0000000000a1"))
    await _db(conn).record_ssh_attachment(
        thread_id="00000000-0000-0000-0000-000000000002",
        user_id="00000000-0000-0000-0000-000000000001",
        ssh_key_id=None,
        client_ip="203.0.113.7",
        handle="s-7f3a91c2",
    )
    assert conn.calls[0][1][4] == "203.0.113.7"
