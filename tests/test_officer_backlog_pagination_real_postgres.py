"""BP-06 real-pgvector keyset and index-plan proof."""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

pytest.importorskip("testcontainers.postgres")

from orchestrator.database.migrate import run_migrations  # noqa: E402
from orchestrator.services.project_backlog import (  # noqa: E402
    BacklogCursor,
    fetch_backlog,
)

PG_IMAGE = "pgvector/pgvector:pg15"
VECTOR_MIGRATIONS = (
    Path(__file__).resolve().parents[1]
    / "src"
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
    except Exception as exc:  # pragma: no cover - environment without runtime
        pytest.skip(f"no container runtime for testcontainers: {exc}")
    try:
        yield re.sub(
            r"^postgresql\+\w+://", "postgresql://", container.get_connection_url()
        )
    finally:
        container.stop()


@pytest_asyncio.fixture
async def pool(pg_dsn):
    db = await asyncpg.create_pool(pg_dsn, min_size=1, max_size=2)
    try:
        await run_migrations(db, VECTOR_MIGRATIONS)
        yield db
    finally:
        await db.close()


def _plan_index_names(node: dict) -> set[str]:
    names = {str(node["Index Name"])} if node.get("Index Name") else set()
    for child in node.get("Plans") or []:
        names.update(_plan_index_names(child))
    return names


@pytest.mark.asyncio
async def test_keyset_pages_equal_keys_at_ten_thousand_rows_and_uses_page_index(pool):
    project_id = uuid4()
    other_project_id = uuid4()
    created_at = datetime(2026, 8, 16, tzinfo=timezone.utc)
    async with pool.acquire() as conn:
        await conn.execute("TRUNCATE knowledge_index CASCADE")
        await conn.execute(
            """
            INSERT INTO knowledge_index (
                note_id, project_id, title, note_type, status, tags, content,
                priority, ready_at, created_at
            )
            SELECT 'ticket-' || lpad(n::text, 5, '0'), $1::uuid, 'Ticket ' || n,
                   'feature', 'active', ARRAY['ready', 'category:researcher'],
                   'body', 1, $3::timestamptz, $3::timestamptz
              FROM generate_series(0, 9999) AS n
            UNION ALL
            SELECT 'noise-' || lpad(n::text, 5, '0'), $2::uuid, 'Noise ' || n,
                   'feature', 'active', ARRAY['ready', 'category:researcher'],
                   'body', 1, $3::timestamptz, $3::timestamptz
              FROM generate_series(0, 9999) AS n
            """,
            project_id,
            other_project_id,
            created_at,
        )
        await conn.execute("ANALYZE knowledge_index")
        plan_doc = await conn.fetchval(
            """
            EXPLAIN (ANALYZE, FORMAT JSON)
            SELECT note_id, note_type, title, priority, tags, ready_at, created_at
              FROM knowledge_index
             WHERE project_id = $1::uuid
               AND status = 'active'
               AND note_type IN ('feature', 'issue', 'idea')
               AND tags @> $2::text[]
             ORDER BY priority ASC, created_at ASC NULLS LAST, note_id ASC
             LIMIT 100
            """,
            project_id,
            ["ready", "category:researcher"],
        )

    started = time.perf_counter()
    first, _ = await fetch_backlog(
        pool,
        str(project_id),
        require_tags=["ready", "category:researcher"],
        limit=100,
        include_counts=False,
    )
    second, _ = await fetch_backlog(
        pool,
        str(project_id),
        require_tags=["ready", "category:researcher"],
        limit=100,
        after=BacklogCursor.from_row(first[-1]),
        include_counts=False,
    )
    all_rows = [*first, *second]
    cursor = BacklogCursor.from_row(second[-1])
    while True:
        page, _ = await fetch_backlog(
            pool,
            str(project_id),
            require_tags=["ready", "category:researcher"],
            limit=100,
            after=cursor,
            include_counts=False,
        )
        if not page:
            break
        all_rows.extend(page)
        cursor = BacklogCursor.from_row(page[-1])
    elapsed = time.perf_counter() - started

    assert [row["note_id"] for row in first] == [
        f"ticket-{index:05d}" for index in range(100)
    ]
    assert [row["note_id"] for row in second] == [
        f"ticket-{index:05d}" for index in range(100, 200)
    ]
    assert not ({row["note_id"] for row in first} & {row["note_id"] for row in second})
    assert [row["note_id"] for row in all_rows] == [
        f"ticket-{index:05d}" for index in range(10000)
    ]
    assert elapsed < 5.0

    if isinstance(plan_doc, str):
        plan_doc = json.loads(plan_doc)
    plan = plan_doc[0]["Plan"]
    assert "idx_knowledge_backlog_page" in _plan_index_names(plan)
    assert float(plan["Actual Total Time"]) < 1000.0
    print(
        "BP-06 vector 10k exhaustive latency "
        f"{elapsed * 1000:.2f}ms; first-page plan "
        f"{plan['Actual Total Time']:.2f}ms"
    )
