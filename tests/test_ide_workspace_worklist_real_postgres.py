"""list_active_ide_workspaces parent-status gate against a real Postgres.

The IDE-settings sweeper's worklist used to select on JSONB workspace status
alone, so terminal jobs / ended threads whose ``workspace_container.status``
was never cleared (pod died without a teardown) re-entered the sweep — and its
serial SSH dials — every cycle, forever. These tests pin the parent-status
gate on a real server (testcontainers): stale-'ready' JSONB on a terminal
parent drops out, the same JSONB on a live parent still matches.
"""

import json
import uuid

import pytest
import pytest_asyncio

from orchestrator.database.postgres import PostgresDB

READY_CONTAINER = {"workspace_container": {"status": "ready", "pod_ip": "10.42.0.9"}}
ACTIVE_IDE = {"ide_session": {"status": "active"}}


@pytest.fixture(scope="module")
def pg_dsn():
    try:
        from testcontainers.postgres import PostgresContainer

        container = PostgresContainer("postgres:16")
        container.start()
    except Exception as e:  # noqa: BLE001 — no container runtime → skip, not error
        pytest.skip(f"postgres testcontainer unavailable: {e}")
    try:
        yield container.get_connection_url().replace(
            "postgresql+psycopg2", "postgresql"
        )
    finally:
        container.stop()


@pytest_asyncio.fixture
async def db(pg_dsn):
    d = PostgresDB(connection_string=pg_dsn)
    await d.connect()
    async with d.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id uuid PRIMARY KEY,
                status text NOT NULL,
                user_id uuid,
                context jsonb
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS threads (
                id uuid PRIMARY KEY,
                status text NOT NULL,
                user_id uuid,
                metadata jsonb
            )
            """
        )
        await conn.execute("TRUNCATE jobs, threads")
    yield d
    await d.close()


async def _insert_job(db, status, context):
    job_id = uuid.uuid4()
    async with db.acquire() as conn:
        await conn.execute(
            "INSERT INTO jobs (id, status, user_id, context) VALUES ($1, $2, $3, $4)",
            job_id,
            status,
            uuid.uuid4(),
            json.dumps(context),
        )
    return str(job_id)


async def _insert_thread(db, status, metadata):
    thread_id = uuid.uuid4()
    async with db.acquire() as conn:
        await conn.execute(
            "INSERT INTO threads (id, status, user_id, metadata) "
            "VALUES ($1, $2, $3, $4)",
            thread_id,
            status,
            uuid.uuid4(),
            json.dumps(metadata),
        )
    return str(thread_id)


async def _worklist_ids(db):
    return {(r["entity_type"], r["id"]) for r in await db.list_active_ide_workspaces()}


@pytest.mark.asyncio
async def test_terminal_jobs_with_ready_container_are_dropped(db):
    completed = await _insert_job(db, "completed", READY_CONTAINER)
    failed = await _insert_job(db, "failed", READY_CONTAINER)
    cancelled = await _insert_job(db, "cancelled", READY_CONTAINER)
    processing = await _insert_job(db, "processing", READY_CONTAINER)

    ids = await _worklist_ids(db)
    assert ("job", processing) in ids
    for terminal in (completed, failed, cancelled):
        assert ("job", terminal) not in ids


@pytest.mark.asyncio
async def test_ended_thread_with_ready_container_is_dropped(db):
    ended = await _insert_thread(db, "ended", READY_CONTAINER)
    active = await _insert_thread(db, "active", READY_CONTAINER)

    ids = await _worklist_ids(db)
    assert ("thread", active) in ids
    assert ("thread", ended) not in ids


@pytest.mark.asyncio
async def test_parent_gate_applies_to_every_workspace_kind(db):
    ended_ide = await _insert_thread(db, "ended", ACTIVE_IDE)
    completed_vm = await _insert_job(db, "completed", {"vm": {"status": "ready"}})
    active_ide = await _insert_thread(db, "active", ACTIVE_IDE)
    processing_vm = await _insert_job(db, "processing", {"vm": {"status": "ready"}})

    ids = await _worklist_ids(db)
    assert ("thread", active_ide) in ids
    assert ("job", processing_vm) in ids
    assert ("thread", ended_ide) not in ids
    assert ("job", completed_vm) not in ids
