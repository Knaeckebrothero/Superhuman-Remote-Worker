"""Migration 0205 — the experts.tags role backfill (U1 B.4).

Structural assertions in the repo's migration-test idiom (read the file,
assert on the SQL shape — see test_ssh_handle_migration.py), plus one
real-Postgres proof that the UPDATE lands exactly what
``src/core/expert_resolution.py::with_role_tag`` computes, since the two are
the same rule written twice. The structural tests need no database; the
behavioural one skips without a container runtime.
"""

from __future__ import annotations

import pathlib
import re
from uuid import uuid4

import asyncpg
import pytest

from orchestrator.database.migrate import discover, run_migrations
from shared.runtime.core.expert_resolution import with_role_tag

ROOT = pathlib.Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "src" / "orchestrator" / "database" / "migrations" / "app"
NAME = "0205_experts_role_tags_backfill.sql"
SQL = MIGRATIONS / NAME
PREDECESSOR = "0204_ssh_attachments.sql"
PARENTS = ("0028_experts.sql", "0064_db_backed_default_expert_columns.sql")


def _statements() -> str:
    """The migration's SQL with comment lines stripped.

    The header explains the DDL the file does NOT contain (no ALTER, no
    index, no COALESCE), so a whole-file ``not in`` assertion would trip on
    the prose — the 0202 idiom.
    """
    return "\n".join(
        line
        for line in SQL.read_text().splitlines()
        if not line.lstrip().startswith("--")
    )


def _compact(sql: str) -> str:
    return " ".join(sql.split())


def test_migration_exists_at_its_number():
    assert SQL.exists(), f"{NAME} missing"
    assert int(SQL.name[:4]) == 205
    assert SQL.read_text().startswith(f"-- migration:     {NAME}")


def test_header_declares_both_parents():
    # Filename only — asserting on header column alignment makes the test
    # fail for cosmetic reasons when an implementer follows house format.
    text = SQL.read_text()
    for parent in PARENTS:
        assert parent in text, f"depends-on must name {parent}"
    assert "-- transactional: yes" in text


def test_is_wrapped_and_bounded():
    """The runner strips exactly a whole-file BEGIN;/COMMIT; wrapper and
    rejects any other transaction-control shape, so the wrapper has to be
    the first and the last statement."""
    sql = _statements()
    statements = [s.strip() for s in sql.split(";") if s.strip()]
    assert statements[0].upper() == "BEGIN"
    assert statements[-1].upper() == "COMMIT"
    assert "SET LOCAL lock_timeout" in sql
    assert "SET LOCAL statement_timeout" in sql
    assert "SET LOCAL idle_in_transaction_session_timeout" in sql


def test_appends_the_role_once_and_only_where_missing():
    sql = _compact(_statements())
    assert "UPDATE public.experts" in sql
    assert sql.count("UPDATE ") == 1
    assert "SET tags = array_append(tags, expert_type::text)" in sql
    assert "WHERE NOT (expert_type::text = ANY (tags))" in sql


def test_is_data_only():
    """No DDL: nothing for squawk to split, nothing for schema_current.sql to
    pick up, no ACCESS EXCLUSIVE lock on experts."""
    sql = _statements()
    assert not re.search(r"\b(ALTER|CREATE|DROP|TRUNCATE)\b", sql, re.IGNORECASE)
    assert not re.search(r"\b(INSERT|DELETE)\b", sql, re.IGNORECASE)
    # Metadata catch-up, not an edit: the cockpit's optimistic-concurrency
    # check and the managed rows' seed_version comparison must see nothing.
    assert "version" not in sql
    assert "updated_at" not in sql


def test_premise_tags_is_not_null_with_an_empty_default():
    """The guard needs no COALESCE only because the column is NOT NULL: a
    NULL tags would make ``= ANY (tags)`` NULL, ``NOT NULL`` NULL, and the
    row would be silently skipped. Assert on the replayed chain's artifact,
    not on 0028 alone, so a later relaxation would fail here."""
    schema = (ROOT / "src/orchestrator/database/schema_current.sql").read_text()
    start = schema.index("CREATE TABLE public.experts (")
    block = schema[start : schema.index(");", start)]
    assert "tags text[] DEFAULT '{}'::text[] NOT NULL" in block
    assert "expert_type character varying(10) NOT NULL" in block
    assert "COALESCE" not in _statements().upper()


def test_discover_orders_it_right_after_0204_with_a_unique_prefix():
    """Assert against the real rule, not a reimplementation: discover() is
    what boot runs, and it raises on a duplicate prefix. Deliberately not
    "is the newest migration" — head tracking belongs to
    test_infrastructure_metering_migrations.py."""
    names = [path.name for path in discover(MIGRATIONS)]
    assert names.count(NAME) == 1
    assert names.index(NAME) == names.index(PREDECESSOR) + 1


# --- real-Postgres proof ----------------------------------------------------

# (key, expert_type, tags before). The expectation is computed by
# with_role_tag itself, so the test fails if either side drifts.
FIXTURES = (
    ("empty-worker", "worker", []),
    ("empty-session", "session", []),
    ("already-tagged-worker", "worker", ["general", "worker", "safe-default"]),
    ("session-with-tags", "session", ["assistant", "chat"]),
    ("foreign-role-only", "worker", ["session"]),
    ("role-last-already", "session", ["chat", "session"]),
)


@pytest.fixture(scope="module")
def scratch_pg_dsn() -> str:
    testcontainers = pytest.importorskip("testcontainers.postgres")
    try:
        container = testcontainers.PostgresContainer("postgres:16")
        container.start()
    except Exception as exc:  # no container runtime on this box
        pytest.skip(f"no container runtime for the 0205 backfill test: {exc}")
    try:
        yield re.sub(
            r"^postgresql\+\w+://", "postgresql://", container.get_connection_url()
        )
    finally:
        container.stop()


def _swap_db(dsn: str, dbname: str) -> str:
    head, _, tail = dsn.rpartition("/")
    query = "?" + tail.split("?", 1)[1] if "?" in tail else ""
    return f"{head}/{dbname}{query}"


@pytest.mark.asyncio
async def test_backfill_matches_with_role_tag_and_is_idempotent(
    scratch_pg_dsn: str, tmp_path: pathlib.Path
) -> None:
    """Replay the chain through 0204, seed rows in every tag shape that
    matters, apply 0205 through the real runner, and compare each row with
    what with_role_tag() says. Then run the UPDATE a second time: 0 rows."""
    dbname = f"role_tags_backfill_{uuid4().hex[:12]}"
    admin = await asyncpg.connect(scratch_pg_dsn)
    try:
        await admin.execute(f'CREATE DATABASE "{dbname}"')
    finally:
        await admin.close()

    dsn = _swap_db(scratch_pg_dsn, dbname)
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    before_0205 = tmp_path / "deployed-through-0204"
    before_0205.mkdir()
    try:
        for path in discover(MIGRATIONS):
            if path.name >= NAME:
                break
            (before_0205 / path.name).write_bytes(path.read_bytes())
        await run_migrations(pool, before_0205)

        async with pool.acquire() as conn:
            for key, expert_type, tags in FIXTURES:
                # Managed rows need no users row (0064: managed_key set,
                # owner_id NULL, is_global true).
                await conn.execute(
                    "INSERT INTO public.experts "
                    "(name, display_name, expert_type, tags, managed_key, is_global) "
                    "VALUES ($1, $2, $3, $4::text[], $5, TRUE)",
                    key,
                    key,
                    expert_type,
                    tags,
                    f"test-{key}",
                )
            stamps_before = {
                row["managed_key"]: (row["version"], row["updated_at"])
                for row in await conn.fetch(
                    "SELECT managed_key, version, updated_at FROM public.experts"
                )
            }

        await run_migrations(pool, MIGRATIONS)

        async with pool.acquire() as conn:
            assert await conn.fetchval(
                "SELECT success FROM schema_migrations WHERE filename=$1", NAME
            )
            by_key = {
                row["managed_key"]: row
                for row in await conn.fetch(
                    "SELECT managed_key, tags, version, updated_at FROM public.experts"
                )
            }
            for key, expert_type, tags in FIXTURES:
                row = by_key[f"test-{key}"]
                assert list(row["tags"]) == with_role_tag(expert_type, tags), key
                assert list(row["tags"]).count(expert_type) == 1, key
                assert (row["version"], row["updated_at"]) == stamps_before[
                    f"test-{key}"
                ], key

            # Idempotent: the UPDATE, taken verbatim from the file and run
            # again, matches nothing.
            update = re.search(r"UPDATE public\.experts.*?;", _statements(), re.S)
            assert update is not None
            assert await conn.execute(update.group(0)) == "UPDATE 0"
    finally:
        await pool.close()
        admin = await asyncpg.connect(scratch_pg_dsn)
        try:
            await admin.execute(f'DROP DATABASE "{dbname}" WITH (FORCE)')
        finally:
            await admin.close()
