"""Test the atomic delegation-resume claim (M1 HA — delegation timeout).

When a delegation times out, the sweeper re-queues the parent job
(waiting → paused) so the dispatcher resumes it with partial results. The
delegation timeout sweeper is leader-gated, but during the transient
dual-leader window two sweepers can both detect the same timeout. The CAS
must ensure exactly one re-queues the parent, so it is never resumed twice.
Mirrors tests/test_job_claim.py.

Also locks the dispatcher-visibility contract: the claim must clear the
delegation ``freeze_data`` blob, because ``get_dispatchable_jobs`` requires
``freeze_data IS NULL`` — a kept freeze hides the re-queued parent forever
(knowledge-base/knowledge/issues/delegation_freeze_lifecycle_gaps.md, Gap 1).
"""

import asyncio
import json
from uuid import UUID

import pytest
import pytest_asyncio
from testcontainers.postgres import PostgresContainer

from orchestrator.database.postgres import PostgresDB

JOB = "22222222-2222-2222-2222-222222222222"
AGENT = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"

DELEGATION_FREEZE = {
    "freeze_type": "delegation",
    "child_job_ids": ["33333333-3333-3333-3333-333333333333"],
    "child_count": 1,
    "timeout": 7200,
    "timestamp": "2026-07-16T00:00:00+00:00",
}


@pytest.fixture(scope="module")
def pg_dsn():
    with PostgresContainer("postgres:16") as pg:
        yield pg.get_connection_url().replace("postgresql+psycopg2", "postgresql")


@pytest_asyncio.fixture
async def db(pg_dsn):
    d = PostgresDB(connection_string=pg_dsn)
    await d.connect()
    async with d.acquire() as conn:
        # Minimal jobs table, but with every column get_dispatchable_jobs
        # touches so the dispatcher-visibility contract can be asserted here.
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id uuid PRIMARY KEY,
                description text,
                status text NOT NULL,
                config_name text,
                config_override jsonb,
                assigned_agent_id uuid,
                user_id uuid,
                project_id uuid,
                parent_job_id uuid,
                priority int DEFAULT 5,
                runner_kind text,
                execution_lane text NOT NULL DEFAULT 'pinned',
                branch_name text,
                repo_name text,
                context jsonb,
                freeze_data jsonb,
                created_at timestamptz DEFAULT now(),
                updated_at timestamptz DEFAULT now()
            )
            """
        )
        await conn.execute("TRUNCATE jobs")
        await conn.execute(
            """
            INSERT INTO jobs (id, status, assigned_agent_id, freeze_data)
            VALUES ($1, 'waiting', $2, $3::jsonb)
            """,
            UUID(JOB),
            UUID(AGENT),
            json.dumps(DELEGATION_FREEZE),
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


@pytest.mark.asyncio
async def test_delegation_resume_clears_freeze_so_dispatcher_sees_parent(db):
    """The claim must clear freeze_data, or the re-queued parent is invisible.

    get_dispatchable_jobs requires ``freeze_data IS NULL`` (partial-index
    contract, 0046); every other frozen→paused transition in the codebase
    clears the freeze in the same statement. Regression for
    knowledge-base/knowledge/issues/delegation_freeze_lifecycle_gaps.md (Gap 1).
    """
    assert await db.claim_delegation_resume(JOB) is True
    async with db.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status, freeze_data FROM jobs WHERE id = $1", UUID(JOB)
        )
    assert row["status"] == "paused"
    assert row["freeze_data"] is None

    dispatchable = await db.get_dispatchable_jobs()
    assert [str(j["id"]) for j in dispatchable] == [JOB], (
        "re-queued delegation parent must satisfy the dispatcher's "
        "freeze_data IS NULL contract"
    )
