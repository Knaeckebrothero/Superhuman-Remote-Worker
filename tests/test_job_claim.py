"""Test the atomic CAS job claim (M1 HA dispatch).

The claim must be safe when two dispatchers (e.g. a transient dual-leader
window) race for the same job: exactly one wins, the job is never handed to
two agents. Uses a real Postgres (testcontainers) with a minimal ``jobs``
table carrying the columns the CAS touches (uuid id/agent, status, updated_at).
"""
import asyncio
from uuid import UUID

import pytest
import pytest_asyncio
from testcontainers.postgres import PostgresContainer

from orchestrator.database.postgres import PostgresDB

JOB = "11111111-1111-1111-1111-111111111111"
AGENT_1 = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
AGENT_2 = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
AGENT_3 = "cccccccc-cccc-cccc-cccc-cccccccccccc"


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
            "INSERT INTO jobs (id, status, assigned_agent_id) VALUES ($1, 'created', NULL)",
            UUID(JOB),
        )
    yield d
    await d.close()


@pytest.mark.asyncio
async def test_claim_is_atomic_exactly_one_wins(db):
    r1, r2 = await asyncio.gather(
        db.claim_job_for_agent(JOB, AGENT_1),
        db.claim_job_for_agent(JOB, AGENT_2),
    )
    assert sorted([r1, r2]) == [False, True], "two leaders must not both claim a job"
    async with db.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status, assigned_agent_id FROM jobs WHERE id = $1", UUID(JOB)
        )
    assert row["status"] == "processing"
    assert str(row["assigned_agent_id"]) in (AGENT_1, AGENT_2)
    # An already-claimed job cannot be re-claimed.
    assert await db.claim_job_for_agent(JOB, AGENT_3) is False


@pytest.mark.asyncio
async def test_claim_rejects_non_dispatchable_status(db):
    # 'processing'/terminal jobs are not claimable even if assigned_agent_id is NULL.
    async with db.acquire() as conn:
        await conn.execute("UPDATE jobs SET status='completed', assigned_agent_id=NULL")
    assert await db.claim_job_for_agent(JOB, AGENT_1) is False
