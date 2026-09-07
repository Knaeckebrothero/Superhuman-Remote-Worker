"""BP-12 real-PostgreSQL proof for batched Officer ready depth."""

from __future__ import annotations

import asyncio
import json
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

pytest.importorskip("testcontainers.postgres")

from orchestrator.database.migrate import run_migrations  # noqa: E402
from orchestrator.database.postgres import PostgresDB  # noqa: E402
from orchestrator.services.officer_backlog import ready_depth_by_pool  # noqa: E402

PG_IMAGE = "pgvector/pgvector:pg15"
ROOT = Path(__file__).resolve().parents[1]
APP_SCHEMA = ROOT / "src" / "orchestrator" / "database" / "schema_current.sql"
VECTOR_MIGRATIONS = ROOT / "src" / "orchestrator" / "database" / "migrations" / "vector"


def _database_dsn(dsn: str, database: str) -> str:
    parts = urlsplit(dsn)
    return urlunsplit(parts._replace(path=f"/{database}"))


class _CountingConnection:
    def __init__(self, connection, owner):
        self._connection = connection
        self._owner = owner

    async def fetch(self, *args, **kwargs):
        self._owner.query_count += 1
        return await self._connection.fetch(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._connection, name)


class _CountingVectorPool:
    def __init__(self, pool):
        self._pool = pool
        self.query_count = 0

    @asynccontextmanager
    async def acquire(self):
        async with self._pool.acquire() as connection:
            yield _CountingConnection(connection, self)


class _CountingPostgresDB(PostgresDB):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.query_count = 0

    @asynccontextmanager
    async def acquire(self):
        async with super().acquire() as connection:
            yield _CountingConnection(connection, self)


def _plan_index_names(node: dict) -> set[str]:
    names = {str(node["Index Name"])} if node.get("Index Name") else set()
    for child in node.get("Plans") or []:
        names.update(_plan_index_names(child))
    return names


@pytest.fixture(scope="module")
def pg_dsn():
    from testcontainers.postgres import PostgresContainer

    container = PostgresContainer(PG_IMAGE)
    try:
        container.start()
    except Exception as exc:  # pragma: no cover - environment without runtime
        pytest.skip(f"no container runtime for testcontainers: {exc}")
    try:
        yield re.sub(
            r"^postgresql\+\w+://", "postgresql://", container.get_connection_url()
        )
    finally:
        container.stop()


@pytest_asyncio.fixture
async def stores(pg_dsn):
    app_database = f"ready_depth_{uuid4().hex[:12]}"
    admin = await asyncpg.connect(pg_dsn)
    try:
        await admin.execute(f'CREATE DATABASE "{app_database}"')
    finally:
        await admin.close()

    app_dsn = _database_dsn(pg_dsn, app_database)
    app_schema_connection = await asyncpg.connect(app_dsn)
    try:
        await app_schema_connection.execute(APP_SCHEMA.read_text())
    finally:
        await app_schema_connection.close()

    vector_pool = await asyncpg.create_pool(pg_dsn, min_size=1, max_size=4)
    await run_migrations(vector_pool, VECTOR_MIGRATIONS)
    app = _CountingPostgresDB(
        connection_string=app_dsn,
        min_connections=1,
        max_connections=4,
    )
    await app.connect()
    try:
        yield app, _CountingVectorPool(vector_pool), vector_pool
    finally:
        await app.close()
        await vector_pool.close()
        admin = await asyncpg.connect(pg_dsn)
        try:
            await admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = $1 AND pid <> pg_backend_pid()",
                app_database,
            )
            await admin.execute(f'DROP DATABASE IF EXISTS "{app_database}"')
        finally:
            await admin.close()


@pytest.mark.asyncio
async def test_max_roster_and_twelve_viewers_use_three_queries_at_ten_thousand_rows(
    stores,
):
    app, vector, raw_vector_pool = stores
    project_id = uuid4()
    other_project_id = uuid4()
    async with raw_vector_pool.acquire() as connection:
        await connection.execute("TRUNCATE knowledge_index CASCADE")
        await connection.execute(
            """
            INSERT INTO knowledge_index (
                note_id, project_id, title, note_type, status, tags, content,
                priority, ready_at, created_at
            )
            SELECT 'ticket-' || lpad(n::text, 5, '0'), $1::uuid,
                   'Ticket ' || n, 'feature', 'active',
                   CASE
                     WHEN n = 2 THEN ARRAY[
                         'ready', 'category:researcher', 'category:tester'
                     ]
                     WHEN n < 4000 THEN ARRAY['ready', 'category:researcher']
                     WHEN n < 7000 THEN ARRAY['ready', 'category:tester']
                     ELSE ARRAY['ready', 'category:executor']
                   END,
                   'body', 1,
                   CASE WHEN n = 3 THEN NULL ELSE $3::timestamptz END,
                   $3::timestamptz
              FROM generate_series(0, 9999) AS n
            UNION ALL
            SELECT 'noise-' || lpad(n::text, 5, '0'), $2::uuid,
                   'Noise ' || n, 'feature', 'active',
                   ARRAY['ready', 'category:researcher'], 'body', 1,
                   $3::timestamptz, $3::timestamptz
              FROM generate_series(0, 9999) AS n
            """,
            project_id,
            other_project_id,
            datetime(2026, 8, 26, 9, 0, tzinfo=timezone.utc),
        )
        await connection.execute("ANALYZE knowledge_index")
        plan_doc = await connection.fetchval(
            """
            EXPLAIN (ANALYZE, FORMAT JSON)
            SELECT note_id, note_type, title, priority, tags, ready_at, created_at
              FROM knowledge_index
             WHERE project_id = $1::uuid
               AND status = 'active'
               AND note_type IN ('feature', 'issue', 'idea')
               AND tags @> $2::text[]
               AND tags && $3::text[]
             ORDER BY priority ASC, created_at ASC NULLS LAST, note_id ASC
             LIMIT 50001
            """,
            project_id,
            ["ready"],
            [
                "category:researcher",
                "category:tester",
                "category:executor",
            ],
        )

    categories = ("researcher", "tester", "executor")
    pools = {
        f"pool-{index:02d}": {
            "count": 20,
            "category": categories[index % len(categories)],
        }
        for index in range(8)
    }
    expected_by_category = {
        "researcher": 3998,
        "tester": 3000,
        "executor": 3000,
    }
    expected = {
        pool: expected_by_category[str(spec["category"])]
        for pool, spec in pools.items()
    }

    app.query_count = 0
    vector.query_count = 0
    started = time.perf_counter()
    results = await asyncio.gather(
        *(
            ready_depth_by_pool(
                app,
                vector,
                str(project_id),
                pools,
                caller="officer_summary",
            )
            for _ in range(12)
        )
    )
    elapsed = time.perf_counter() - started

    assert results == [expected] * 12
    assert vector.query_count == 1
    assert app.query_count == 2
    assert elapsed < 5.0

    if isinstance(plan_doc, str):
        plan_doc = json.loads(plan_doc)
    plan = plan_doc[0]["Plan"]
    # An exhaustive bounded batch may prefer the older three-column backlog
    # index plus a small final tie sort over the page index.  Either keeps the
    # project/status/note-type scan off a whole-table sequential path.
    assert _plan_index_names(plan) & {
        "idx_knowledge_backlog",
        "idx_knowledge_backlog_page",
        "idx_knowledge_project",
    }
    assert float(plan["Actual Total Time"]) < 1000.0
    print(
        "BP-12 8-pool/160-agent/12-viewer/10k batch: "
        f"3 queries, {elapsed * 1000:.2f}ms; vector plan "
        f"{plan['Actual Total Time']:.2f}ms"
    )
