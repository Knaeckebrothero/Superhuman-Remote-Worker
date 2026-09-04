"""Real-Postgres proof for the KB exact/grep channel's trigram index (spec WP6).

``CREATE EXTENSION`` and ``CREATE INDEX ... USING gin`` are exactly the kind of
statement a mock can't validate — only a real server confirms the extension
actually installs and the index actually exists after ``run_migrations``.

S1 (this file's origin) adds only the index-existence assertion below; S2
extends this file with the grep-channel behavior it backs.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytest.importorskip("testcontainers.postgres")

import asyncpg  # noqa: E402
import pytest_asyncio  # noqa: E402

from orchestrator.database.migrate import run_migrations  # noqa: E402

PG_IMAGE = "pgvector/pgvector:pg15"
VECTOR_MIGRATIONS = (
    Path(__file__).resolve().parents[1]
    / "orchestrator"
    / "database"
    / "migrations"
    / "vector"
)


@pytest.fixture(scope="module")
def pg_dsn():
    from testcontainers.postgres import PostgresContainer

    container = PostgresContainer(PG_IMAGE)
    try:
        container.start()
    except Exception as exc:  # pragma: no cover - environment without a runtime
        pytest.skip(f"no container runtime for testcontainers: {exc}")
    try:
        yield re.sub(
            r"^postgresql\+\w+://", "postgresql://", container.get_connection_url()
        )
    finally:
        container.stop()


@pytest_asyncio.fixture
async def vector_pool(pg_dsn):
    pool = await asyncpg.create_pool(pg_dsn, min_size=1, max_size=2)
    try:
        await run_migrations(pool, VECTOR_MIGRATIONS)
        yield pool
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_pg_trgm_extension_and_content_index_exist(vector_pool):
    async with vector_pool.acquire() as conn:
        extnames = {
            row["extname"]
            for row in await conn.fetch("SELECT extname FROM pg_extension")
        }
        assert "pg_trgm" in extnames

        indexnames = {
            row["indexname"]
            for row in await conn.fetch(
                "SELECT indexname FROM pg_indexes WHERE schemaname = 'public'"
            )
        }
        assert "idx_knowledge_content_trgm" in indexnames
