"""Upgrade an existing vector database as its unprivileged application owner."""

from __future__ import annotations

import hashlib
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest

from orchestrator.database.migrate import run_migrations


VECTOR_MIGRATIONS = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "orchestrator"
    / "database"
    / "migrations"
    / "vector"
)
SEARCH_MIGRATION = "0025_knowledge_multi_angle_search.sql"
ORIGINAL_CHECKSUM = "fc9395f5ca2fd60a48932d8038e92238124ae23d7174d558d22a89a83d791ab8"
PRELOAD_CHECKSUM = "915246808c5714610aeb98faac61d96b5a2a72a81cba26677ba2d9b636325424"
PRELOAD_FRAGMENT = (
    Path(__file__).parent / "fixtures/migrations/vector_0025_097_preload.sql"
)
LEDGER_QUERY = "SELECT * FROM public.schema_migrations ORDER BY filename"
SEARCH_FUNCTION_QUERY = (
    "SELECT p.oid::pg_catalog.regprocedure::text "
    "FROM pg_catalog.pg_proc p "
    "JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace "
    "WHERE n.nspname='public' "
    "AND p.proname='knowledge_chunk_multi_angle_search'"
)


@pytest.fixture(scope="module")
def upgrade_pg_dsn():
    testcontainers = pytest.importorskip("testcontainers.postgres")
    container = testcontainers.PostgresContainer("pgvector/pgvector:pg16")
    try:
        container.start()
    except Exception as exc:
        pytest.skip(f"no container runtime for vector upgrade test: {exc}")
    try:
        yield container.get_connection_url().replace(
            "postgresql+psycopg2://", "postgresql://", 1
        )
    finally:
        container.stop()


@asynccontextmanager
async def _vector_database(upgrade_pg_dsn):
    # CI's container owner is a superuser. Use a separate database owned by a
    # normal login, as on CNPG, and install only the privileged extension as admin.
    owner = f"vector_upgrade_{uuid4().hex}"
    admin = await asyncpg.connect(upgrade_pg_dsn)
    pools = []
    try:
        await admin.execute(f"CREATE ROLE {owner} LOGIN PASSWORD '{owner}'")
        await admin.execute(f"CREATE DATABASE {owner} OWNER {owner}")
        bootstrap = await asyncpg.connect(upgrade_pg_dsn, database=owner)
        try:
            await bootstrap.execute("CREATE EXTENSION vector")
        finally:
            await bootstrap.close()

        async def cold_pool():
            pool = await asyncpg.create_pool(
                upgrade_pg_dsn,
                database=owner,
                user=owner,
                password=owner,
                min_size=1,
                max_size=1,
            )
            pools.append(pool)
            return pool

        yield owner, cold_pool
    finally:
        for pool in pools:
            await pool.close()
        await admin.execute(f"DROP DATABASE IF EXISTS {owner} WITH (FORCE)")
        await admin.execute(f"DROP ROLE IF EXISTS {owner}")
        await admin.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [False, True])
async def test_cold_non_superuser_vector_upgrade(upgrade_pg_dsn, tmp_path, dry_run):
    async with _vector_database(upgrade_pg_dsn) as (_, cold_pool):
        before_upgrade = tmp_path / "before_upgrade"
        before_upgrade.mkdir()
        for path in VECTOR_MIGRATIONS.glob("*.sql"):
            if path.name < "0023_":
                shutil.copyfile(path, before_upgrade / path.name)

        pool = await cold_pool()
        await run_migrations(pool, before_upgrade)
        before = await pool.fetch(
            "SELECT * FROM public.schema_migrations ORDER BY filename"
        )
        await pool.close()

        # Reusing the bootstrap connection hides the bug: earlier index builds
        # have already loaded pgvector and registered its USERSET parameters.
        pool = await cold_pool()
        assert await pool.fetchval("SHOW is_superuser") == "off"
        assert (
            await pool.fetchval("SELECT current_setting('hnsw.iterative_scan', true)")
            is None
        )
        await run_migrations(pool, VECTOR_MIGRATIONS, dry_run=dry_run)

        if dry_run:
            assert (
                await pool.fetch(
                    "SELECT * FROM public.schema_migrations ORDER BY filename"
                )
                == before
            )
            assert not await pool.fetchval(
                "SELECT EXISTS (SELECT 1 FROM pg_proc "
                "WHERE proname = 'knowledge_chunk_multi_angle_search')"
            )
            assert not await pool.fetchval(
                "SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm')"
            )
            await pool.close()
            pool = await cold_pool()
            await run_migrations(pool, VECTOR_MIGRATIONS)

        assert (
            await pool.fetchval(
                "SELECT success FROM public.schema_migrations "
                "WHERE filename = '0025_knowledge_multi_angle_search.sql'"
            )
            is True
        )
        assert await pool.fetchval(
            "SELECT proconfig FROM pg_proc "
            "WHERE proname = 'knowledge_chunk_multi_angle_search'"
        ) == ["hnsw.iterative_scan=relaxed_order"]
        assert (
            await pool.fetchval(
                "SELECT indisvalid FROM pg_index "
                "WHERE indexrelid = 'public.idx_knowledge_content_trgm'::regclass"
            )
            is True
        )
        await pool.close()
        pool = await cold_pool()
        await run_migrations(pool, VECTOR_MIGRATIONS)


def _search_sql(variant):
    original = (VECTOR_MIGRATIONS / SEARCH_MIGRATION).read_text()
    assert hashlib.sha256(original.encode()).hexdigest() == ORIGINAL_CHECKSUM
    if variant == "original":
        return original
    assert variant == "published_preload"
    anchor = "CREATE OR REPLACE FUNCTION public.knowledge_chunk_multi_angle_search("
    assert original.count(anchor) == 1
    published = original.replace(anchor, PRELOAD_FRAGMENT.read_text() + anchor)
    assert hashlib.sha256(published.encode()).hexdigest() == PRELOAD_CHECKSUM
    return published


def _stage_vector_migrations(tmp_path, name, variant):
    staged = tmp_path / name
    shutil.copytree(VECTOR_MIGRATIONS, staged)
    (staged / SEARCH_MIGRATION).write_text(_search_sql(variant))
    return staged


def _pending_probe_name(migrations_dir):
    # Keep the synthetic pending migration after the current production head.
    prefix = max(
        int(path.name.split("_", 1)[0]) for path in migrations_dir.glob("*.sql")
    )
    return f"{prefix + 1:04d}_checksum_compatibility_probe.sql"


def test_vector_0025_is_restored_and_published_variant_is_exact():
    # Neither fixture relies on git being installed or historical objects being
    # present in a CI shallow checkout. The fragment reconstructs the exact
    # published artifact, with both complete-file digests pinned independently.
    assert _search_sql("published_preload").replace(
        PRELOAD_FRAGMENT.read_text(), "", 1
    ) == _search_sql("original")


async def _set_search_function_owner(upgrade_pg_dsn, database, owner=None):
    admin = await asyncpg.connect(upgrade_pg_dsn, database=database)
    try:
        identity = await admin.fetchval(SEARCH_FUNCTION_QUERY)
        assert identity is not None
        quoted_owner = await admin.fetchval(
            "SELECT pg_catalog.quote_ident(COALESCE($1::text, current_user))", owner
        )
        await admin.execute(f"ALTER FUNCTION {identity} OWNER TO {quoted_owner}")
    finally:
        await admin.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", ["original", "published_preload"])
@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("pending", [False, True])
async def test_successful_vector_histories_keep_exact_ledger_rows(
    upgrade_pg_dsn, tmp_path, variant, dry_run, pending
):
    history = _stage_vector_migrations(tmp_path, "history", variant)
    target = _stage_vector_migrations(tmp_path, "target", "original")
    probe = _pending_probe_name(target)
    if pending:
        (target / probe).write_text(
            "CREATE TABLE checksum_compatibility_probe(id int);\n"
        )

    async with _vector_database(upgrade_pg_dsn) as (owner, cold_pool):
        pool = await cold_pool()
        await run_migrations(pool, history)
        before = await pool.fetch(LEDGER_QUERY)
        assert next(row for row in before if row["filename"] == SEARCH_MIGRATION)[
            "checksum"
        ] == (ORIGINAL_CHECKSUM if variant == "original" else PRELOAD_CHECKSUM)

        # Make a mistaken replay fail at PostgreSQL's ownership boundary, even
        # if a future runner would otherwise hide it by preserving ledger rows.
        await _set_search_function_owner(upgrade_pg_dsn, owner)
        await pool.close()
        pool = await cold_pool()
        assert await pool.fetchval("SHOW is_superuser") == "off"
        await run_migrations(pool, target, dry_run=dry_run)

        after = await pool.fetch(LEDGER_QUERY)
        assert [row for row in after if row["filename"] != probe] == before
        expected_pending = pending and not dry_run
        assert len(after) == len(before) + int(expected_pending)
        assert (
            bool(
                await pool.fetchval(
                    "SELECT pg_catalog.to_regclass('public.checksum_compatibility_probe')"
                )
            )
            is expected_pending
        )

        # A later ordinary startup must work as well. For dry-run this is the
        # first real apply of the independent pending migration.
        await pool.close()
        pool = await cold_pool()
        await run_migrations(pool, target)
        after = await pool.fetch(LEDGER_QUERY)
        assert [row for row in after if row["filename"] != probe] == before
        assert len(after) == len(before) + int(pending)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    [
        "unknown_ledger_checksum",
        "changed_canonical_file",
        "reverse_variant",
        "other_filename",
        "unrelated_migration_file",
        "unrelated_ledger_checksum",
    ],
)
async def test_vector_checksum_compatibility_rejects_unreviewed_drift(
    upgrade_pg_dsn, tmp_path, mutation
):
    history = _stage_vector_migrations(tmp_path, "history", "published_preload")
    target = _stage_vector_migrations(tmp_path, "target", "original")
    if mutation == "unknown_ledger_checksum":
        path = history / SEARCH_MIGRATION
        path.write_text(path.read_text() + "\n-- unreviewed historical edit\n")
    elif mutation == "changed_canonical_file":
        path = target / SEARCH_MIGRATION
        path.write_text(path.read_text() + "\n-- unreviewed current edit\n")
    elif mutation == "reverse_variant":
        (history / SEARCH_MIGRATION).write_text(_search_sql("original"))
        (target / SEARCH_MIGRATION).write_text(_search_sql("published_preload"))
    elif mutation == "other_filename":
        for directory in (history, target):
            (directory / SEARCH_MIGRATION).rename(directory / "0025_other_search.sql")
    elif mutation in {"unrelated_migration_file", "unrelated_ledger_checksum"}:
        directory = target if mutation == "unrelated_migration_file" else history
        path = next(directory.glob("0023_*.sql"))
        path.write_text(path.read_text() + "\n-- unrelated unreviewed edit\n")
    else:
        raise AssertionError(mutation)
    (target / _pending_probe_name(target)).write_text(
        "CREATE TABLE checksum_compatibility_probe(id int);\n"
    )

    async with _vector_database(upgrade_pg_dsn) as (_, cold_pool):
        pool = await cold_pool()
        # Generate every ledger checksum through an actual successful runner
        # invocation, including the deliberately unreviewed historical bytes.
        await run_migrations(pool, history)
        before = await pool.fetch(LEDGER_QUERY)
        for dry_run in (False, True):
            with pytest.raises(RuntimeError, match="checksum changed:"):
                await run_migrations(pool, target, dry_run=dry_run)
            assert await pool.fetch(LEDGER_QUERY) == before
            assert (
                await pool.fetchval(
                    "SELECT pg_catalog.to_regclass('public.checksum_compatibility_probe')"
                )
                is None
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", ["original", "published_preload"])
async def test_dirty_vector_variants_still_require_operator_recovery(
    upgrade_pg_dsn, tmp_path, variant
):
    history = _stage_vector_migrations(tmp_path, "history", variant)
    before_search = _stage_vector_migrations(tmp_path, "before_search", "original")
    (before_search / SEARCH_MIGRATION).unlink()

    async with _vector_database(upgrade_pg_dsn) as (owner, cold_pool):
        pool = await cold_pool()
        await run_migrations(pool, before_search)
        admin = await asyncpg.connect(upgrade_pg_dsn, database=owner)
        try:
            # PostgreSQL refuses the application's CREATE OR REPLACE against
            # this administrator-owned function. The ordinary runner records
            # the actual failure and the exact historical artifact checksum.
            await admin.execute(_search_sql(variant))
        finally:
            await admin.close()
        with pytest.raises(asyncpg.InsufficientPrivilegeError, match="must be owner"):
            await run_migrations(pool, history)
        before = await pool.fetch(LEDGER_QUERY)
        failed = next(row for row in before if row["filename"] == SEARCH_MIGRATION)
        assert failed["success"] is False
        assert failed["checksum"] == (
            ORIGINAL_CHECKSUM if variant == "original" else PRELOAD_CHECKSUM
        )

        # Even after fixing the underlying database permission, this change
        # must not turn a failed historical row into automatic replay authority.
        await _set_search_function_owner(upgrade_pg_dsn, owner, owner=owner)
        for dry_run in (False, True):
            with pytest.raises(RuntimeError, match="dirty migration.*0025_"):
                await run_migrations(pool, VECTOR_MIGRATIONS, dry_run=dry_run)
            assert await pool.fetch(LEDGER_QUERY) == before
