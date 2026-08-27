"""Real-PostgreSQL proof for the officer roster query.

The mocked endpoint suite proves the view logic; only a real server proves the
join and its subselects: a vacant post must survive the LEFT JOIN, the wake
counts must not leak across threads, and the in-flight count must follow the
project rather than whatever thread happens to hold the post today.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
from testcontainers.postgres import PostgresContainer

from orchestrator.database.postgres import PostgresDB

SCHEMA_FILE = (
    Path(__file__).resolve().parents[1]
    / "orchestrator"
    / "database"
    / "schema_current.sql"
)


@pytest.fixture(scope="module")
def pg_dsn():
    try:
        container = PostgresContainer("postgres:15")
        container.start()
    except Exception as exc:
        pytest.skip(f"local Postgres container unavailable: {exc}")
    try:
        yield container.get_connection_url().replace(
            "postgresql+psycopg2", "postgresql"
        )
    finally:
        container.stop()


@pytest_asyncio.fixture(scope="module")
async def _schema_applied(pg_dsn):
    conn = await asyncpg.connect(pg_dsn)
    try:
        await conn.execute(SCHEMA_FILE.read_text())
    finally:
        await conn.close()


@pytest_asyncio.fixture
async def db(pg_dsn, _schema_applied, monkeypatch):
    monkeypatch.setenv("EXPERTS_DB_ENABLED", "false")
    store = PostgresDB(connection_string=pg_dsn, min_connections=1, max_connections=5)
    await store.connect()
    async with store.acquire() as conn:
        await conn.execute(
            "TRUNCATE session_wake_events, thread_messages, jobs, "
            "project_officers, threads, projects CASCADE"
        )
    try:
        yield store
    finally:
        await store.close()


@pytest_asyncio.fixture
async def seeded(db):
    """One commissioned post ('Alpha') and one vacant post ('Bravo')."""
    alpha, bravo = uuid4(), uuid4()
    thread_id = uuid4()
    other_thread = uuid4()
    fire_at = datetime.now(timezone.utc) + timedelta(minutes=25)
    metadata = {
        "config_override": {
            "llm": {"model": "gpt-5.6-sol"},
            "officer": {"enabled": True, "auto_pull": False},
        }
    }
    async with db.acquire() as conn:
        await conn.execute(
            "INSERT INTO projects (id, name) VALUES ($1, 'Alpha'), ($2, 'Bravo')",
            alpha,
            bravo,
        )
        await conn.execute(
            """
            INSERT INTO threads (id, project_id, status, metadata)
            VALUES ($1, $2, 'active', $3::jsonb), ($4, $2, 'active', '{}'::jsonb)
            """,
            thread_id,
            alpha,
            json.dumps(metadata),
            other_thread,
        )
        await conn.execute(
            """
            INSERT INTO project_officers (project_id, thread_id)
            VALUES ($1, $2), ($3, NULL)
            """,
            alpha,
            thread_id,
            bravo,
        )
        await conn.execute(
            """
            INSERT INTO session_wake_events (thread_id, source, dedup_key, fire_at)
            VALUES ($1, 'timer', 'timer', $2), ($1, 'legate', 'note-1', NULL),
                   ($3, 'timer', 'timer', $2)
            """,
            thread_id,
            fire_at,
            other_thread,
        )
        await conn.execute(
            """
            INSERT INTO jobs (description, config_name, status, project_id)
            VALUES ('running one', 'worker_base', 'processing', $1),
                   ('done one', 'worker_base', 'completed', $1),
                   ('paused zombie', 'worker_base', 'paused', $1),
                   ('elsewhere', 'worker_base', 'processing', $2)
            """,
            alpha,
            bravo,
        )
        await conn.execute(
            """
            INSERT INTO thread_messages (thread_id, role, content)
            VALUES ($1, 'ai', 'filed a sleep'), ($1, 'event', '[SITREP]')
            """,
            thread_id,
        )
    return {"alpha": alpha, "bravo": bravo, "thread_id": thread_id, "fire_at": fire_at}


@pytest.mark.asyncio
async def test_the_roster_reports_both_posts_with_their_live_numbers(db, seeded):
    rows = {r["project_name"]: r for r in await db.list_project_officer_posts()}

    alpha = rows["Alpha"]
    assert alpha["thread_id"] == seeded["thread_id"]
    assert alpha["thread_status"] == "active"
    assert alpha["next_wake_at"] == seeded["fire_at"]
    # Its own timer + its own note; the sibling thread's timer stays out.
    assert alpha["pending_events"] == 2
    # Slot-occupying jobs on THIS project only — the seeded paused zombie is
    # not in flight (owner ruling 2026-08-18: paused vacates its slot).
    assert alpha["in_flight_jobs"] == 1
    assert alpha["last_agent_activity"] is not None

    bravo = rows["Bravo"]
    assert bravo["thread_id"] is None
    assert bravo["thread_status"] is None
    assert bravo["pending_events"] == 0
    assert bravo["in_flight_jobs"] == 1


@pytest.mark.asyncio
async def test_a_vacant_post_survives_the_join(db, seeded):
    names = [r["project_name"] for r in await db.list_project_officer_posts()]
    assert names == ["Alpha", "Bravo"]


@pytest.mark.asyncio
async def test_the_visible_id_filter_narrows_the_roster(db, seeded):
    rows = await db.list_project_officer_posts([str(seeded["bravo"])])
    assert [r["project_name"] for r in rows] == ["Bravo"]


@pytest.mark.asyncio
async def test_last_activity_ignores_orchestrator_written_event_rows(db, seeded):
    """Only ai/tool rows prove the officer himself was awake."""
    async with db.acquire() as conn:
        await conn.execute(
            "DELETE FROM thread_messages WHERE role = 'ai' AND thread_id = $1",
            seeded["thread_id"],
        )
    rows = {r["project_name"]: r for r in await db.list_project_officer_posts()}
    assert rows["Alpha"]["last_agent_activity"] is None
