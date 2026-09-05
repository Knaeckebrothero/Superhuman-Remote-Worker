"""Upgrade an existing vector database as its unprivileged application owner."""

from __future__ import annotations

import shutil
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest

from orchestrator.database.migrate import run_migrations


VECTOR_MIGRATIONS = (
    Path(__file__).resolve().parents[1] / "orchestrator/database/migrations/vector"
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


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [False, True])
async def test_cold_non_superuser_vector_upgrade(upgrade_pg_dsn, tmp_path, dry_run):
    # CI's container owner is a superuser. Use a separate database owned by a
    # normal login, as on CNPG, and install only the privileged extension as admin.
    owner = f"vector_upgrade_{uuid4().hex}"
    admin = await asyncpg.connect(upgrade_pg_dsn)
    pool = None
    try:
        await admin.execute(f"CREATE ROLE {owner} LOGIN PASSWORD '{owner}'")
        await admin.execute(f"CREATE DATABASE {owner} OWNER {owner}")
        bootstrap = await asyncpg.connect(upgrade_pg_dsn, database=owner)
        try:
            await bootstrap.execute("CREATE EXTENSION vector")
        finally:
            await bootstrap.close()

        async def cold_pool():
            return await asyncpg.create_pool(
                upgrade_pg_dsn,
                database=owner,
                user=owner,
                password=owner,
                min_size=1,
                max_size=1,
            )

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
    finally:
        if pool is not None:
            await pool.close()
        await admin.execute(f"DROP DATABASE IF EXISTS {owner} WITH (FORCE)")
        await admin.execute(f"DROP ROLE IF EXISTS {owner}")
        await admin.close()
