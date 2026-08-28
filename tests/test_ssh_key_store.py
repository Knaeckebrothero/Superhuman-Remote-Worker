# tests/test_ssh_key_store.py
"""Unit tests for the user_ssh_keys data access methods.

The connection is faked: these assert SQL shape and control flow, which is
where the bugs live. Real SQL execution is covered by the k3d gate in plan 3.
"""

import pytest

from database.postgres import (
    MAX_SSH_KEYS_PER_USER,
    PostgresDB,
    SshKeyAlreadyRegistered,
    SshKeyLimitReached,
)

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


class CappedConn(FakeConn):
    """Evaluates the guarded INSERT's ``count(*) < $6`` the way Postgres will.

    The cap lives inside the statement (no separate ``SELECT count(*)``, so
    there is no window for a concurrent replica to widen), which means no
    Python branch decides it and a plain FakeConn cannot exercise the
    boundary. This one reads the bound limit out of the arguments and returns
    a row only while the user is under it — the same predicate, evaluated
    here instead of in the server.
    """

    def __init__(self, existing: int):
        super().__init__()
        self.existing = existing

    async def fetchrow(self, sql, *args):
        self.calls.append((sql, args))
        assert "count(*)" in sql, "the cap must be carried by the statement"
        return {"id": "k1", "name": "laptop"} if self.existing < args[5] else None


async def _create(db):
    return await db.create_user_ssh_key(
        user_id=USER_ID,
        name="laptop",
        key_type="ssh-ed25519",
        public_key="ssh-ed25519 AAAA... me@host",
        fingerprint_sha256="SHA256:" + "A" * 43,
    )


@pytest.mark.asyncio
async def test_the_tenth_key_is_accepted():
    """Boundary, low side. Spec §4.1 caps at 10 per user, so a user holding 9
    must still be able to register."""
    conn = CappedConn(existing=MAX_SSH_KEYS_PER_USER - 1)
    assert (await _create(_db(conn)))["id"] == "k1"


@pytest.mark.asyncio
async def test_the_eleventh_key_is_refused():
    """Boundary, high side. Each key is an independent shell credential once
    the gateway ships, so an unbounded set is an unbounded credential set."""
    conn = CappedConn(existing=MAX_SSH_KEYS_PER_USER)
    with pytest.raises(SshKeyLimitReached) as excinfo:
        await _create(_db(conn))
    assert excinfo.value.limit == MAX_SSH_KEYS_PER_USER


@pytest.mark.asyncio
async def test_the_cap_constant_is_what_gets_bound():
    """A literal in the SQL would drift from the constant the API reports."""
    conn = CappedConn(existing=0)
    await _create(_db(conn))
    _, args = conn.calls[0]
    assert args[5] == MAX_SSH_KEYS_PER_USER


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
@pytest.mark.parametrize(
    "call",
    [
        pytest.param(
            lambda db: db.create_user_ssh_key(
                user_id="nope",
                name="laptop",
                key_type="ssh-ed25519",
                public_key="ssh-ed25519 AAAA",
                fingerprint_sha256="SHA256:" + "A" * 43,
            ),
            id="create",
        ),
        pytest.param(lambda db: db.list_user_ssh_keys("nope"), id="list"),
        pytest.param(lambda db: db.delete_user_ssh_key(KEY_ID, "nope"), id="delete"),
        pytest.param(lambda db: db.mark_ssh_key_used("nope"), id="mark_used"),
    ],
)
async def test_ids_are_parsed_before_a_connection_is_acquired(call):
    """One contract across every method on this table.

    ``record_ssh_attachment``/``close_ssh_attachment`` already assert in their
    docstrings that "a malformed id has no business checking one out of the
    pool" and have tests pinning it; these three parsed INSIDE ``acquire()``
    until the final review (Minor 3). Two sibling methods on one table with
    opposite contracts is a trap this codebase has already been bitten by.
    """
    conn = FakeConn()
    with pytest.raises(ValueError):
        await call(_db(conn))
    assert conn.calls == []
    assert conn.acquired == 0


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
