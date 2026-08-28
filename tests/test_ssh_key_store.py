# tests/test_ssh_key_store.py
"""Unit tests for the user_ssh_keys data access methods.

The connection is faked: these assert SQL shape and control flow, which is
where the bugs live. Real SQL execution is covered by the k3d gate in plan 3.
"""

import pytest

from database.postgres import PostgresDB, SshKeyAlreadyRegistered

KEY_ID = "00000000-0000-0000-0000-0000000000aa"
USER_ID = "00000000-0000-0000-0000-000000000001"


class FakeConn:
    def __init__(self, *, fetchrow=None, fetch=None, execute=None, raises=None):
        self._fetchrow, self._fetch, self._execute = fetchrow, fetch, execute
        self._raises = raises
        self.calls = []

    async def fetchrow(self, sql, *args):
        self.calls.append((sql, args))
        if self._raises:
            raise self._raises
        return self._fetchrow

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        return self._fetch or []

    async def execute(self, sql, *args):
        self.calls.append((sql, args))
        return self._execute


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
async def test_create_returns_the_row():
    conn = FakeConn(fetchrow={"id": "k1", "name": "laptop"})
    result = await _db(conn).create_user_ssh_key(
        user_id="00000000-0000-0000-0000-000000000001",
        name="laptop",
        key_type="ssh-ed25519",
        public_key="ssh-ed25519 AAAA... me@host",
        fingerprint_sha256="SHA256:" + "A" * 43,
    )
    assert result["name"] == "laptop"


@pytest.mark.asyncio
async def test_create_translates_unique_violation():
    """A duplicate fingerprint must surface as a domain error, not a 500."""

    import asyncpg

    conn = FakeConn(raises=asyncpg.UniqueViolationError("duplicate key"))
    with pytest.raises(SshKeyAlreadyRegistered):
        await _db(conn).create_user_ssh_key(
            user_id="00000000-0000-0000-0000-000000000001",
            name="laptop",
            key_type="ssh-ed25519",
            public_key="ssh-ed25519 AAAA...",
            fingerprint_sha256="SHA256:" + "A" * 43,
        )


@pytest.mark.asyncio
async def test_list_is_scoped_to_the_user():
    conn = FakeConn(fetch=[{"id": "k1"}])
    await _db(conn).list_user_ssh_keys("00000000-0000-0000-0000-000000000001")
    sql, args = conn.calls[0]
    assert "WHERE user_id = $1" in sql
    assert len(args) == 1


@pytest.mark.asyncio
async def test_delete_is_scoped_to_the_user():
    """Deleting by id alone would let anyone remove anyone's key."""
    conn = FakeConn(execute="DELETE 1")
    assert await _db(conn).delete_user_ssh_key(KEY_ID, USER_ID) is True
    sql, args = conn.calls[0]
    assert "user_id = $2" in sql
    assert len(args) == 2


@pytest.mark.asyncio
async def test_delete_reports_miss():
    conn = FakeConn(execute="DELETE 0")
    assert await _db(conn).delete_user_ssh_key(KEY_ID, USER_ID) is False


@pytest.mark.asyncio
async def test_resolve_ignores_disabled_keys():
    conn = FakeConn(fetchrow=None)
    assert await _db(conn).resolve_user_by_ssh_fingerprint("SHA256:" + "A" * 43) is None
    sql, _ = conn.calls[0]
    assert "disabled_at IS NULL" in sql


@pytest.mark.asyncio
async def test_resolve_bumps_last_used():
    conn = FakeConn(fetchrow={"id": "u1", "is_approved": True})
    user = await _db(conn).resolve_user_by_ssh_fingerprint("SHA256:" + "A" * 43)
    assert user["id"] == "u1"
    assert any("last_used_at" in sql for sql, _ in conn.calls)
