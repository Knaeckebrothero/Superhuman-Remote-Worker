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
        # Count of times a connection was actually checked out, separate from
        # `calls`: proves "no connection taken", not just "no statement
        # issued" — the two are different claims. Same harness contract as
        # tests/test_ssh_attachment_audit.py.
        self.acquired = 0

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
        self.conn.acquired += 1
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
    """A miss — disabled key, unapproved account, or unknown fingerprint — is
    one statement, not two. Regression guard: the bump must not fire on a miss."""
    conn = FakeConn(fetchrow=None)
    assert await _db(conn).resolve_user_by_ssh_fingerprint("SHA256:" + "A" * 43) is None
    assert len(conn.calls) == 1
    sql, _ = conn.calls[0]
    assert "disabled_at IS NULL" in sql
    assert "is_approved" in sql


@pytest.mark.asyncio
async def test_resolve_returns_the_user_in_one_statement():
    """Was ``test_resolve_bumps_last_used``. The bump was REMOVED from this
    method on purpose (final review, Important 1) — see
    ``test_resolve_issues_no_update`` right below for why. The assertions this
    test exists for (a hit returns the user, in exactly one round trip) are
    kept verbatim; only the ``last_used_at`` expectation is inverted, into its
    own named test."""
    conn = FakeConn(fetchrow={"id": "u1", "is_approved": True})
    user = await _db(conn).resolve_user_by_ssh_fingerprint("SHA256:" + "A" * 43)
    assert user["id"] == "u1"
    assert len(conn.calls) == 1


@pytest.mark.asyncio
async def test_resolve_issues_no_update():
    """Resolving an identity must not be a write.

    Any ``X-Internal-Key`` holder — which is every agent pod — can reach this
    through ``GET /api/internal/ssh-targets/<handle>?fingerprint=...``, and
    fingerprints are derivable from public keys published at
    ``github.com/<user>.keys``, so the victim row needs no secret to choose.
    In the gateway plan it is worse: asyncssh runs ``validate_public_key``
    during the ``publickey`` query phase, before any signature exists, so the
    bump would fire for anyone who merely *offers* a key. ``last_used_at`` is
    the field a user checks to notice a stolen key — attacker-writable
    destroys the only detection signal on this surface.
    """
    conn = FakeConn(fetchrow={"id": "u1", "is_approved": True})
    await _db(conn).resolve_user_by_ssh_fingerprint("SHA256:" + "A" * 43)
    sql, _ = conn.calls[0]
    upper = sql.upper()
    assert "UPDATE" not in upper
    assert "INSERT" not in upper
    assert "DELETE" not in upper
    assert "last_used_at" not in sql
    # conn.execute is what a write would have gone through on this fake.
    assert len(conn.calls) == 1


@pytest.mark.asyncio
async def test_resolve_returns_the_key_id_for_a_later_bump():
    """The gateway needs the matched key's id to call ``mark_ssh_key_used``
    after ``key.verify``, without a second fingerprint lookup. ``id`` stays
    the USER id — ``user_can_access_ide_entity`` and ``_ssh_target_response``
    both read it as such."""
    conn = FakeConn(fetchrow={"ssh_key_id": "k1", "id": "u1"})
    row = await _db(conn).resolve_user_by_ssh_fingerprint("SHA256:" + "A" * 43)
    assert row["ssh_key_id"] == "k1"
    assert row["id"] == "u1"
    sql, _ = conn.calls[0]
    assert "k.id AS ssh_key_id" in sql


@pytest.mark.asyncio
async def test_mark_ssh_key_used_issues_the_update():
    """The bump that ``resolve_user_by_ssh_fingerprint`` no longer does. Left
    uncalled on this branch: the gateway must call it only after
    ``key.verify`` succeeds."""
    conn = FakeConn(execute="UPDATE 1")
    await _db(conn).mark_ssh_key_used(KEY_ID)
    sql, args = conn.calls[0]
    assert "UPDATE user_ssh_keys" in sql
    assert "last_used_at = now()" in sql
    from uuid import UUID

    assert args == (UUID(KEY_ID),)


@pytest.mark.asyncio
async def test_mark_ssh_key_used_rejects_a_malformed_id_before_acquiring():
    conn = FakeConn()
    with pytest.raises(ValueError):
        await _db(conn).mark_ssh_key_used("nope")
    assert conn.calls == []
    assert conn.acquired == 0


@pytest.mark.asyncio
async def test_resolve_does_not_overfetch_columns():
    """Regression guard for the old ``SELECT u.*``: JSONB columns (settings,
    cloud_identity) come back from asyncpg as unparsed strings, so this
    network-facing identity lookup must keep using an explicit column list."""
    conn = FakeConn(fetchrow={"id": "u1"})
    await _db(conn).resolve_user_by_ssh_fingerprint("SHA256:" + "A" * 43)
    sql, _ = conn.calls[0]
    assert "u.*" not in sql
    assert "settings" not in sql
    assert "cloud_identity" not in sql
