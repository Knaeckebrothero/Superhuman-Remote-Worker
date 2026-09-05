"""Real-PostgreSQL proofs for migration 0175's dispatch fence.

The fence classifies a dispatch claim by the row shape an UPDATE lands on,
not by who wrote it: any statement that touches status/assigned_agent_id/
lease_expires_at and leaves a job on the claimed pinned or stateless shape
must carry ``context._workspace_dispatch_authority`` matching the same row.

That makes every non-claim writer of those columns a fence participant, so
the two that exist are pinned here — the in-process resume of a parked pinned
agent (which must stamp the marker) and the stateless completion-control claim
(which must stay out of the triggering column set entirely).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio
from testcontainers.postgres import PostgresContainer

from orchestrator.database.postgres import PostgresDB
from orchestrator.services.completion_control import (
    COMPLETION_CONTROL_CLAIM_KEY,
    CompletionControl,
)

SCHEMA_FILE = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "orchestrator"
    / "database"
    / "schema_current.sql"
)


@pytest.fixture(scope="module")
def pg_dsn():
    with PostgresContainer("postgres:15") as postgres:
        yield postgres.get_connection_url().replace("postgresql+psycopg2", "postgresql")


@pytest_asyncio.fixture(scope="module")
async def _schema_applied(pg_dsn):
    conn = await asyncpg.connect(pg_dsn)
    try:
        await conn.execute(SCHEMA_FILE.read_text())
    finally:
        await conn.close()


@pytest_asyncio.fixture
async def pg(pg_dsn, _schema_applied):
    pool = await asyncpg.create_pool(pg_dsn, min_size=1, max_size=4, timeout=10)
    async with pool.acquire() as conn:
        await conn.execute("TRUNCATE run_queue, jobs, agents CASCADE")
    try:
        yield pool
    finally:
        await pool.close()


class _PoolDB:
    """The pool-only surface the services under test actually use."""

    def __init__(self, pool) -> None:
        self.pool = pool

    def acquire(self):
        return self.pool.acquire()


async def _agent(conn) -> UUID:
    return await conn.fetchval(
        "INSERT INTO agents (config_name, hostname, status) "
        "VALUES ('developer', $1, 'working') RETURNING id",
        f"fence-{uuid4().hex[:10]}",
    )


async def _parked_pinned_job(conn, agent_id: UUID) -> UUID:
    """A phase-boundary freeze: paused, agent still assigned and leased."""
    return await conn.fetchval(
        "INSERT INTO jobs (description, status, execution_lane, "
        "assigned_agent_id, lease_expires_at, freeze_data) "
        "VALUES ('fence resume', 'paused', 'pinned', $1, "
        "now() + interval '1 hour', '{\"freeze_type\": \"phase_boundary\"}') "
        "RETURNING id",
        agent_id,
    )


@pytest.mark.asyncio
async def test_in_process_pinned_resume_is_admitted_by_the_fence(pg):
    """paused → processing on the SAME parked agent is a fenced boundary."""
    async with pg.acquire() as conn:
        job_id = await _parked_pinned_job(conn, await _agent(conn))

    assert await PostgresDB.resume_pinned_job_in_process(_PoolDB(pg), str(job_id))

    async with pg.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status::text AS status, freeze_data, "
            "context->'_workspace_dispatch_authority' AS marker "
            "FROM jobs WHERE id=$1",
            job_id,
        )
    assert row["status"] == "processing"
    assert row["freeze_data"] is None
    assert row["marker"] is not None


@pytest.mark.asyncio
async def test_an_unmarked_resume_of_the_same_job_is_refused(pg):
    """Negative control: the marker is what the fence admits, not the shape."""
    async with pg.acquire() as conn:
        job_id = await _parked_pinned_job(conn, await _agent(conn))

        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            await conn.execute(
                "UPDATE jobs SET status='processing', freeze_data=NULL "
                "WHERE id=$1::uuid",
                job_id,
            )


@pytest.mark.asyncio
async def test_stateless_control_claim_is_not_a_dispatch_claim(pg):
    """Control takes a job AWAY from the worker plane; it never claims it.

    The production shape is what matters here: dispatch leaves a stateless job
    with assigned_agent_id NULL and its run_queue row leased, which is exactly
    the shape the fence reads as a worker claim.
    """
    async with pg.acquire() as conn:
        job_id = await conn.fetchval(
            "INSERT INTO jobs (description, status, execution_lane, "
            "assigned_agent_id) "
            "VALUES ('fence control', 'processing', 'stateless', NULL) "
            "RETURNING id"
        )
        await conn.execute(
            "INSERT INTO run_queue (unit_id, unit_kind, state, lease_token, "
            "leased_by, leased_until, input_seq, consumed_seq) "
            "VALUES ($1, 'worker_batch', 'leased', 11, 'worker-pod-1', "
            "now() + interval '5 min', 1, 0)",
            job_id,
        )

    claim = await CompletionControl(_PoolDB(pg), AsyncMock()).claim_job(
        job_id,
        source="mode_a_accept",
        expected_status="processing",
        expected_lane="stateless",
    )

    async with pg.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status::text AS status, assigned_agent_id, context "
            "FROM jobs WHERE id=$1",
            job_id,
        )
        queue_state = await conn.fetchval(
            "SELECT state FROM run_queue WHERE unit_id=$1", job_id
        )
    context = row["context"]
    if isinstance(context, str):
        import json

        context = json.loads(context)
    assert row["status"] == "processing"
    assert row["assigned_agent_id"] is None
    assert context[COMPLETION_CONTROL_CLAIM_KEY]["claim_id"] == claim.claim_id
    # The queue lease, not the agent column, is the stateless claim's fence.
    assert queue_state == "done"
