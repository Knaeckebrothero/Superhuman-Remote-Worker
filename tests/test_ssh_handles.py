import asyncpg
import pytest

from database.postgres import PostgresDB
from services.ssh_handles import (
    _ALPHABET,
    HANDLE_PATTERN,
    is_valid_handle,
    mint_ssh_handle,
)


def test_minted_handles_match_the_pattern():
    for _ in range(200):
        assert HANDLE_PATTERN.fullmatch(mint_ssh_handle())


def test_minted_handles_are_distinct():
    assert len({mint_ssh_handle() for _ in range(1000)}) == 1000


def test_excludes_ambiguous_characters():
    """4000 draws (32000 characters) makes the stronger claim strictly
    cheaper to assert than the weaker one: every character the minter
    actually emits is in the declared alphabet, and every character in the
    declared alphabet eventually gets drawn — not just that four specific
    excluded letters never show up. Flake odds are negligible: the odds any
    one of 32 characters is missing after 32000 draws are (31/32)**32000."""
    alphabet = set("".join(mint_ssh_handle()[2:] for _ in range(4000)))
    assert alphabet == set(_ALPHABET)


def test_handle_pattern_rejects_a_trailing_newline_under_match_and_search():
    """re's ``$`` matches immediately before a trailing newline, so a
    ``$``-anchored pattern's ``.match()`` would accept a handle with "\\n"
    appended — exactly the ~/.ssh/config injection vector the charset
    exclusions guard against. ``is_valid_handle`` was never vulnerable (it
    uses ``fullmatch``), but ``HANDLE_PATTERN`` is exported and consumed
    directly elsewhere, so the pattern itself must be safe under ``.match()``
    and ``.search()``, not only ``.fullmatch()``."""
    handle = mint_ssh_handle()
    assert HANDLE_PATTERN.match(handle + "\n") is None
    assert HANDLE_PATTERN.search(handle + "\n") is None


@pytest.mark.parametrize(
    "value",
    [
        "s-abcdefgh\nProxyCommand rm -rf /",  # newline injection into ssh_config
        "s-abcdefgh ProxyCommand x",  # space then a directive
        "s-ABCDEFGH",  # uppercase
        "s-abcdefg",  # too short
        "s-abcdefghi",  # too long
        "abcdefgh",  # missing prefix
        "s-abcdefgi",  # excluded character
        "",
    ],
)
def test_rejects_anything_that_could_reach_ssh_config(value):
    """The handle is written verbatim into a user's ~/.ssh/config. Generated SSH
    config is an injection sink, so validate on output as well as at mint."""
    assert is_valid_handle(value) is False


def test_accepts_a_minted_handle():
    assert is_valid_handle(mint_ssh_handle()) is True


# ============================================================================
# PostgresDB.ensure_thread_ssh_handle / get_thread_id_by_ssh_handle
#
# The connection is faked, mirroring tests/test_ssh_key_store.py's harness:
# these assert control flow and savepoint discipline, which is where the
# concurrency bugs live. Real SQL execution is covered by the k3d gate.
# ============================================================================

THREAD_ID = "00000000-0000-0000-0000-0000000000aa"


class _FakeTransaction:
    """Fakes conn.transaction()'s savepoint context manager. Never swallows
    an exception, matching asyncpg: on error it rolls back to the savepoint
    and re-raises."""

    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        self._conn.transaction_entries += 1
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeConn:
    """Fakes exactly the asyncpg surface these two methods use: fetchval and
    a nested transaction() savepoint. ``fetchval_results`` is consumed in
    call order, one entry per call; an exception instance in the list is
    raised from that call instead of returned."""

    def __init__(self, fetchval_results):
        self._results = list(fetchval_results)
        self.fetchval_calls = []
        self.transaction_entries = 0

    def transaction(self):
        return _FakeTransaction(self)

    async def fetchval(self, sql, *args):
        self.fetchval_calls.append((sql, args))
        result = self._results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class _FakeAcquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


def _db(conn):
    db = PostgresDB.__new__(PostgresDB)
    db.acquire = lambda: _FakeAcquire(conn)
    return db


@pytest.mark.asyncio
async def test_ensure_thread_ssh_handle_returns_an_existing_handle_untouched():
    conn = _FakeConn(["s-existinghd"])
    assert await _db(conn).ensure_thread_ssh_handle(THREAD_ID) == "s-existinghd"
    assert len(conn.fetchval_calls) == 1  # the SELECT only — no mint needed
    assert conn.transaction_entries == 0


@pytest.mark.asyncio
async def test_ensure_thread_ssh_handle_retries_after_a_unique_violation():
    """A collision on one candidate must not abort the whole attempt loop —
    only that attempt's savepoint rolls back, and the next candidate lands."""
    conn = _FakeConn(
        [
            None,  # SELECT existing: none yet
            asyncpg.UniqueViolationError("dup"),  # attempt 1 UPDATE: collides
            "s-secondtry1",  # attempt 2 UPDATE: succeeds
        ]
    )
    result = await _db(conn).ensure_thread_ssh_handle(THREAD_ID)
    assert result == "s-secondtry1"
    assert conn.transaction_entries == 2  # one savepoint per UPDATE attempt


@pytest.mark.asyncio
async def test_ensure_thread_ssh_handle_enters_a_savepoint_per_attempt():
    """Each retry's UPDATE must run inside its own conn.transaction() (asyncpg
    maps a nested transaction to a SAVEPOINT). Without one, acquire()'s
    transaction_scope() reuse means a collision would abort the caller's
    whole enclosing transaction instead of just this attempt, and the retry's
    next query would raise InFailedSQLTransactionError on a doomed
    connection."""
    conn = _FakeConn(
        [
            None,
            asyncpg.UniqueViolationError("dup"),
            asyncpg.UniqueViolationError("dup"),
            "s-thirdtryok",
        ]
    )
    result = await _db(conn).ensure_thread_ssh_handle(THREAD_ID)
    assert result == "s-thirdtryok"
    assert conn.transaction_entries == 3


@pytest.mark.asyncio
async def test_ensure_thread_ssh_handle_raises_after_five_collisions():
    """Five straight collisions (astronomically unlikely: independent draws
    from a ~1.1e12 keyspace) must not silently return None — that sentinel
    already means both "no such thread" and "not yet minted"; collapsing a
    third meaning onto it is its own trap, even at this probability."""
    conn = _FakeConn([None] + [asyncpg.UniqueViolationError("dup")] * 5)
    with pytest.raises(RuntimeError):
        await _db(conn).ensure_thread_ssh_handle(THREAD_ID)
    assert conn.transaction_entries == 5


@pytest.mark.asyncio
async def test_ensure_thread_ssh_handle_returns_the_concurrent_writers_handle():
    """If another writer sets the handle between our SELECT and our UPDATE,
    the UPDATE matches zero rows (its "WHERE ssh_handle IS NULL" no longer
    holds) without raising. That must resolve to the winner's handle, not
    None — a lost race is not the same as no handle existing."""
    conn = _FakeConn(
        [
            None,  # SELECT existing: none yet
            None,  # UPDATE: 0 rows matched, someone else won the race
            "s-rivalwins1",  # fallback SELECT: the winner's handle
        ]
    )
    result = await _db(conn).ensure_thread_ssh_handle(THREAD_ID)
    assert result == "s-rivalwins1"
    assert conn.transaction_entries == 1  # only one UPDATE attempt was made


@pytest.mark.asyncio
async def test_ensure_thread_ssh_handle_unknown_thread_returns_none():
    conn = _FakeConn(
        [
            None,  # SELECT existing: no such thread
            None,  # UPDATE: 0 rows (no such thread)
            None,  # fallback SELECT: still no such thread
        ]
    )
    assert await _db(conn).ensure_thread_ssh_handle(THREAD_ID) is None


@pytest.mark.asyncio
async def test_ensure_thread_ssh_handle_malformed_id_is_none_not_valueerror():
    """Mirrors get_thread()'s neighboring contract: a malformed id resolves
    to None instead of raising ValueError out of the UUID bind."""
    conn = _FakeConn([])  # must never be touched
    assert await _db(conn).ensure_thread_ssh_handle("not-a-uuid") is None
    assert conn.fetchval_calls == []
    assert conn.transaction_entries == 0


@pytest.mark.asyncio
async def test_get_thread_id_by_ssh_handle_resolves_a_known_handle():
    conn = _FakeConn([THREAD_ID])
    result = await _db(conn).get_thread_id_by_ssh_handle(mint_ssh_handle())
    assert result == THREAD_ID


@pytest.mark.asyncio
async def test_get_thread_id_by_ssh_handle_unknown_handle_returns_none():
    conn = _FakeConn([None])
    assert await _db(conn).get_thread_id_by_ssh_handle(mint_ssh_handle()) is None


@pytest.mark.asyncio
async def test_get_thread_id_by_ssh_handle_rejects_a_malformed_handle_without_querying():
    """The module's stated posture is 'validate on output, not only at
    mint' — this boundary must not depend on every future caller
    pre-validating, and rejecting early also short-circuits enumeration
    probes before they reach the database."""
    conn = _FakeConn([])  # must never be touched
    assert await _db(conn).get_thread_id_by_ssh_handle("not-a-handle") is None
    assert conn.fetchval_calls == []
