"""Test the atomic delegation-resume claim (M1 HA — delegation timeout).

When a delegation times out, the sweeper re-queues the parent job
(waiting → paused) so the dispatcher resumes it with partial results. The
delegation timeout sweeper is leader-gated, but during the transient
dual-leader window two sweepers can both detect the same timeout. The CAS
must ensure exactly one re-queues the parent, so it is never resumed twice.
Mirrors tests/test_job_claim.py.
"""
import asyncio
from uuid import UUID

import pytest
import pytest_asyncio
from testcontainers.postgres import PostgresContainer

from orchestrator.database.postgres import PostgresDB

JOB = "22222222-2222-2222-2222-222222222222"
AGENT = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


@pytest.fixture(scope="module")
def pg_dsn():
    with PostgresContainer("postgres:16") as pg:
        yield pg.get_connection_url().replace("postgresql+psycopg2", "postgresql")


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
                assigned_agent_id uuid,
                updated_at timestamptz DEFAULT now()
            )
            """
        )
        await conn.execute("TRUNCATE jobs")
        await conn.execute(
            "INSERT INTO jobs (id, status, assigned_agent_id) VALUES ($1, 'waiting', $2)",
            UUID(JOB),
            UUID(AGENT),
        )
    yield d
    await d.close()


@pytest.mark.asyncio
async def test_delegation_resume_is_atomic_exactly_one_wins(db):
    r1, r2 = await asyncio.gather(
        db.claim_delegation_resume(JOB),
        db.claim_delegation_resume(JOB),
    )
    assert sorted([r1, r2]) == [False, True], (
        "two sweepers must not both re-queue the same delegation parent"
    )
    async with db.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status, assigned_agent_id FROM jobs WHERE id = $1", UUID(JOB)
        )
    assert row["status"] == "paused"
    assert row["assigned_agent_id"] is None  # agent cleared for re-dispatch
    # Already re-queued (now 'paused') → a later sweep cannot re-queue it.
    assert await db.claim_delegation_resume(JOB) is False


@pytest.mark.asyncio
async def test_delegation_resume_rejects_non_waiting(db):
    async with db.acquire() as conn:
        await conn.execute("UPDATE jobs SET status='processing'")
    assert await db.claim_delegation_resume(JOB) is False
