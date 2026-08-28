"""Real-Postgres proofs for human-control/completion linearization (M2)."""

from __future__ import annotations

import json
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
    CompletionControlClaimConflict,
)
from orchestrator.services.job_completion_commands import (
    CompletionControlInProgress,
    accept_completion_command,
)
from src.shared.worker_queue import claim_worker_batch
from tests._previous_release_seed import seed_previous_release_row


SCHEMA_FILE = (
    Path(__file__).resolve().parents[1]
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
    pool = await asyncpg.create_pool(pg_dsn, min_size=1, max_size=8, timeout=10)
    async with pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE completion_effects, job_completion_commands, "
            "run_queue, jobs, agents CASCADE"
        )
    try:
        yield pool
    finally:
        await pool.close()


class _PoolDB:
    _queue_job_for_resume_on_conn = PostgresDB._queue_job_for_resume_on_conn
    _completion_resume_blocked_on_conn = PostgresDB._completion_resume_blocked_on_conn
    _UNSTICK_REVIEWING_SQL = PostgresDB._UNSTICK_REVIEWING_SQL
    _UNSTICK_REVIEWING_WALLCLOCK_SQL = PostgresDB._UNSTICK_REVIEWING_WALLCLOCK_SQL

    def __init__(self, pool) -> None:
        self.pool = pool

    def acquire(self):
        return self.pool.acquire()


def _payload() -> dict[str, object]:
    return {
        "status": "completed",
        "result": {"summary": "control ordering"},
        "freeze_data": {"freeze_type": "job_complete"},
    }


async def _agent(conn) -> UUID:
    return await conn.fetchval(
        "INSERT INTO agents (config_name, hostname, status) "
        "VALUES ('developer', $1, 'working') RETURNING id",
        f"control-{uuid4().hex[:10]}",
    )


async def _pinned_job(conn, *, status: str, agent_id: UUID | None) -> UUID:
    return await conn.fetchval(
        "INSERT INTO jobs "
        "(description, status, execution_lane, assigned_agent_id) "
        "VALUES ('control ordering', $1, 'pinned', $2) RETURNING id",
        status,
        agent_id,
    )


async def _stateless_job(conn, *, status: str, agent_id: UUID | None) -> UUID:
    job_id = await conn.fetchval(
        "INSERT INTO jobs "
        "(description, status, execution_lane, assigned_agent_id) "
        "VALUES ('stateless control ordering', $1, 'stateless', $2) RETURNING id",
        status,
        agent_id,
    )
    await conn.execute(
        "INSERT INTO run_queue "
        "(unit_id, unit_kind, state, lease_token, input_seq, consumed_seq) "
        "VALUES ($1, 'worker_batch', 'queued', 11, 1, 0)",
        job_id,
    )
    return job_id


async def _unfinished_command(conn, job_id: UUID) -> UUID:
    return await conn.fetchval(
        """
        INSERT INTO job_completion_commands (
            job_id, report_seq, client_report_id, payload, payload_digest,
            origin, requested_by, state, attempts, max_attempts,
            run_after, deadline_at, code_version
        ) VALUES (
            $1, 1, $2, '{}'::jsonb, 'control-command', 'operator',
            'control-real-pg', 'pending', 0, 5, now(),
            now() + interval '1 hour', 'control-test'
        )
        RETURNING id
        """,
        job_id,
        uuid4(),
    )


async def _accept_pinned(pg, job_id: UUID, agent_id: UUID, report_id: UUID):
    return await accept_completion_command(
        pg,
        job_id=str(job_id),
        payload=_payload(),
        lease_token=None,
        agent_id=str(agent_id),
        client_report_id=str(report_id),
        requested_by="control-real-pg",
    )


@pytest.mark.asyncio
async def test_claim_first_rejects_fresh_accept_before_insert_or_hwm(pg):
    async with pg.acquire() as conn:
        agent_id = await _agent(conn)
        job_id = await _pinned_job(conn, status="pending_review", agent_id=agent_id)

    control = CompletionControl(_PoolDB(pg), AsyncMock())
    claim = await control.claim_job(
        job_id,
        source="mode_a_reject",
        expected_status="pending_review",
        expected_lane="pinned",
    )

    with pytest.raises(CompletionControlInProgress):
        await _accept_pinned(pg, job_id, agent_id, uuid4())

    async with pg.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT assigned_agent_id, completion_seq_hwm, context "
            "FROM jobs WHERE id=$1",
            job_id,
        )
        assert row["assigned_agent_id"] is None
        assert int(row["completion_seq_hwm"]) == 0
        context = row["context"]
        if isinstance(context, str):
            context = json.loads(context)
        assert str(context[COMPLETION_CONTROL_CLAIM_KEY]["claim_id"]) == claim.claim_id
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM job_completion_commands WHERE job_id=$1",
                job_id,
            )
            == 0
        )


@pytest.mark.asyncio
async def test_accept_first_makes_control_exact_409_without_second_mutation(pg):
    async with pg.acquire() as conn:
        agent_id = await _agent(conn)
        job_id = await _pinned_job(conn, status="pending_review", agent_id=agent_id)

    accepted = await _accept_pinned(pg, job_id, agent_id, uuid4())
    control = CompletionControl(_PoolDB(pg), AsyncMock())
    with pytest.raises(CompletionControlClaimConflict, match="completion finalizing"):
        await control.claim_job(
            job_id,
            source="mode_a_reject",
            expected_status="pending_review",
            expected_lane="pinned",
        )

    async with pg.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT assigned_agent_id, completion_seq_hwm, context "
            "FROM jobs WHERE id=$1",
            job_id,
        )
        assert row["assigned_agent_id"] == agent_id
        assert int(row["completion_seq_hwm"]) == 1
        context = row["context"] or {}
        if isinstance(context, str):
            context = json.loads(context)
        assert COMPLETION_CONTROL_CLAIM_KEY not in context
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM job_completion_commands WHERE job_id=$1",
                job_id,
            )
            == 1
        )
        assert accepted.report_seq == 1


@pytest.mark.asyncio
async def test_active_marker_blocks_both_dispatch_scan_and_final_claim(pg):
    async with pg.acquire() as conn:
        agent_id = await _agent(conn)
        job_id = await _pinned_job(conn, status="paused", agent_id=agent_id)

    control = CompletionControl(_PoolDB(pg), AsyncMock())
    await control.claim_job(
        job_id,
        source="missing_workspace_resume",
        expected_status="paused",
        expected_lane="pinned",
    )
    db = _PoolDB(pg)

    assert (
        await PostgresDB.get_dispatchable_jobs(
            db, limit=50, completion_commands_enabled=True
        )
        == []
    )
    assert not await PostgresDB.claim_job_for_agent(
        db,
        str(job_id),
        str(agent_id),
        completion_commands_enabled=True,
    )


@pytest.mark.asyncio
async def test_unfinished_command_blocks_dispatch_scan_and_atomic_claim(pg):
    async with pg.acquire() as conn:
        agent_id = await _agent(conn)
        job_id = await _pinned_job(conn, status="paused", agent_id=None)
        await conn.execute("UPDATE jobs SET freeze_data=NULL WHERE id=$1", job_id)
        await _unfinished_command(conn, job_id)

    db = _PoolDB(pg)
    assert (
        await PostgresDB.get_dispatchable_jobs(
            db, limit=50, completion_commands_enabled=True
        )
        == []
    )
    assert not await PostgresDB.claim_job_for_agent(
        db,
        str(job_id),
        str(agent_id),
        completion_commands_enabled=True,
    )

    # The disabled path deliberately neither names nor applies Gate-3 state.
    assert await PostgresDB.claim_job_for_agent(db, str(job_id), str(agent_id))


@pytest.mark.asyncio
async def test_active_control_claim_blocks_ordinary_resume_mutation(pg):
    async with pg.acquire() as conn:
        agent_id = await _agent(conn)
        job_id = await _pinned_job(conn, status="paused", agent_id=agent_id)

    control = CompletionControl(_PoolDB(pg), AsyncMock())
    await control.claim_job(
        job_id,
        source="mode_a_accept",
        expected_status="paused",
        expected_lane="pinned",
    )

    assert not await PostgresDB.queue_job_for_resume(
        _PoolDB(pg),
        str(job_id),
        expected_status="paused",
        completion_commands_enabled=True,
    )
    async with pg.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status::text AS status, freeze_data, context FROM jobs WHERE id=$1",
            job_id,
        )
    context = row["context"]
    if isinstance(context, str):
        context = json.loads(context)
    assert row["status"] == "paused"
    assert COMPLETION_CONTROL_CLAIM_KEY in context


@pytest.mark.asyncio
@pytest.mark.parametrize("lane", ["pinned", "stateless"])
async def test_claim_first_blocks_cancel_without_mutating_status_or_queue(pg, lane):
    async with pg.acquire() as conn:
        agent_id = await _agent(conn)
        if lane == "pinned":
            job_id = await _pinned_job(conn, status="processing", agent_id=agent_id)
        else:
            job_id = await _stateless_job(conn, status="processing", agent_id=agent_id)

    db = _PoolDB(pg)
    control = CompletionControl(db, AsyncMock())
    claim = await control.claim_job(
        job_id,
        source="mode_a_accept",
        expected_status="processing",
        expected_lane=lane,
    )
    async with pg.acquire() as conn:
        queue_before = await conn.fetchrow(
            "SELECT state, lease_token FROM run_queue WHERE unit_id=$1", job_id
        )

    if lane == "pinned":
        assert not await PostgresDB.linearize_pinned_cancel(
            db,
            str(job_id),
            expected_status="processing",
            completion_commands_enabled=True,
        )
    else:
        assert await PostgresDB.cancel_stateless_job(
            db,
            str(job_id),
            completion_commands_enabled=True,
        ) == (False, False)

    async with pg.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status::text AS status, context FROM jobs WHERE id=$1", job_id
        )
        queue_after = await conn.fetchrow(
            "SELECT state, lease_token FROM run_queue WHERE unit_id=$1", job_id
        )
    context = row["context"]
    if isinstance(context, str):
        context = json.loads(context)
    assert row["status"] == "processing"
    assert context[COMPLETION_CONTROL_CLAIM_KEY]["claim_id"] == claim.claim_id
    assert queue_after == queue_before


@pytest.mark.asyncio
@pytest.mark.parametrize("lane", ["pinned", "stateless"])
async def test_cancel_first_prevents_later_control_claim(pg, lane):
    async with pg.acquire() as conn:
        agent_id = await _agent(conn)
        if lane == "pinned":
            job_id = await _pinned_job(conn, status="processing", agent_id=agent_id)
        else:
            job_id = await _stateless_job(conn, status="processing", agent_id=agent_id)

    db = _PoolDB(pg)
    if lane == "pinned":
        assert await PostgresDB.linearize_pinned_cancel(
            db,
            str(job_id),
            expected_status="processing",
            completion_commands_enabled=True,
        )
    else:
        assert await PostgresDB.cancel_stateless_job(
            db,
            str(job_id),
            completion_commands_enabled=True,
        ) == (True, True)

    control = CompletionControl(db, AsyncMock())
    with pytest.raises(
        CompletionControlClaimConflict,
        match="job changed while control was being claimed",
    ):
        await control.claim_job(
            job_id,
            source="mode_a_accept",
            expected_status="processing",
            expected_lane=lane,
        )
    async with pg.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status::text AS status, context FROM jobs WHERE id=$1", job_id
        )
    context = row["context"] or {}
    if isinstance(context, str):
        context = json.loads(context)
    assert row["status"] == "cancelled"
    assert COMPLETION_CONTROL_CLAIM_KEY not in context


@pytest.mark.asyncio
async def test_expired_marker_releases_dispatch_but_malformed_stays_closed(pg):
    async with pg.acquire() as conn:
        agent_id = await _agent(conn)
        expired_id = await _pinned_job(conn, status="paused", agent_id=None)
        malformed_id = await _pinned_job(conn, status="paused", agent_id=None)
        await conn.execute(
            "UPDATE jobs SET context=$2::jsonb WHERE id=$1",
            expired_id,
            json.dumps(
                {
                    COMPLETION_CONTROL_CLAIM_KEY: {
                        "version": 1,
                        "expires_epoch": 1,
                    }
                }
            ),
        )
        await conn.execute(
            "UPDATE jobs SET context=$2::jsonb WHERE id=$1",
            malformed_id,
            json.dumps(
                {
                    COMPLETION_CONTROL_CLAIM_KEY: {
                        "version": 1,
                        "expires_epoch": "not-a-number",
                    }
                }
            ),
        )

    db = _PoolDB(pg)
    rows = await PostgresDB.get_dispatchable_jobs(
        db, limit=50, completion_commands_enabled=True
    )
    assert [row["id"] for row in rows] == [expired_id]
    assert await PostgresDB.claim_job_for_agent(
        db,
        str(expired_id),
        str(agent_id),
        completion_commands_enabled=True,
    )
    assert not await PostgresDB.claim_job_for_agent(
        db,
        str(malformed_id),
        str(agent_id),
        completion_commands_enabled=True,
    )


@pytest.mark.asyncio
async def test_stateless_active_marker_blocks_bypassed_queue_claim(pg):
    async with pg.acquire() as conn:
        job_id = await conn.fetchval(
            "INSERT INTO jobs (description, status, execution_lane) "
            "VALUES ('stateless control ordering', 'paused', 'stateless') "
            "RETURNING id"
        )
        # A ready Kubernetes projection with no Pod UID is previous-release
        # data; 0198 fences a writer that publishes one today. The subject here
        # is control-plane claim ordering above such a row, so seed it the way
        # the migration met it.
        await seed_previous_release_row(
            conn,
            "jobs",
            "UPDATE jobs SET context = $2::jsonb WHERE id = $1",
            job_id,
            json.dumps(
                {
                    "workspace_container": {
                        "status": "ready",
                        "provisioner": "k8s",
                        "host": "workspace.srw.svc",
                    }
                }
            ),
        )
        await conn.execute(
            "INSERT INTO run_queue "
            "(unit_id, unit_kind, state, lease_token, input_seq, consumed_seq) "
            "VALUES ($1, 'worker_batch', 'queued', 7, 1, 0)",
            job_id,
        )

    control = CompletionControl(_PoolDB(pg), AsyncMock())
    await control.claim_job(
        job_id,
        source="mode_a_reject",
        expected_status="paused",
        expected_lane="stateless",
    )
    # Simulate a raw SQL/operator bypass of the control verb. The queue-side
    # claim barrier must still reject the row without consuming another token.
    async with pg.acquire() as conn:
        before = await conn.fetchrow(
            "UPDATE run_queue SET state='queued' WHERE unit_id=$1 "
            "RETURNING lease_token, state",
            job_id,
        )

    assert (
        await claim_worker_batch(
            pg,
            pod_name="control-test-worker",
            completion_commands_enabled=True,
        )
        is None
    )
    async with pg.acquire() as conn:
        after = await conn.fetchrow(
            "SELECT lease_token, state FROM run_queue WHERE unit_id=$1",
            job_id,
        )
    assert dict(after) == dict(before)


@pytest.mark.asyncio
@pytest.mark.parametrize("lane", ["pinned", "stateless"])
async def test_delayed_agent_release_cannot_pause_successor_owner(pg, lane):
    async with pg.acquire() as conn:
        successor = await _agent(conn)
        if lane == "pinned":
            job_id = await _pinned_job(conn, status="processing", agent_id=successor)
        else:
            job_id = await _stateless_job(conn, status="processing", agent_id=None)
            await conn.execute(
                "UPDATE run_queue SET state='leased', lease_token=12 WHERE unit_id=$1",
                job_id,
            )

    db = _PoolDB(pg)
    if lane == "pinned":
        assert not await PostgresDB.pause_job(
            db,
            str(job_id),
            completion_commands_enabled=True,
            expected_agent_id=str(uuid4()),
        )
    else:
        assert not await PostgresDB.pause_stateless_job(
            db,
            str(job_id),
            completion_commands_enabled=True,
            expected_lease_token=11,
        )

    async with pg.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status::text AS status, assigned_agent_id FROM jobs WHERE id=$1",
            job_id,
        )
        queue = await conn.fetchrow(
            "SELECT state, lease_token FROM run_queue WHERE unit_id=$1",
            job_id,
        )
    assert row["status"] == "processing"
    assert row["assigned_agent_id"] == (successor if lane == "pinned" else None)
    if lane == "stateless":
        assert dict(queue) == {"state": "leased", "lease_token": 12}


@pytest.mark.asyncio
async def test_blocking_message_publishes_only_for_exact_pinned_owner(pg):
    async with pg.acquire() as conn:
        owner = await _agent(conn)
        job_id = await _pinned_job(conn, status="processing", agent_id=owner)

    db = _PoolDB(pg)
    freeze = {"freeze_type": "blocking_message", "thread_id": "abc123"}
    assert not await PostgresDB.publish_blocking_message(
        db,
        str(job_id),
        freeze,
        expected_lane="pinned",
        agent_id=str(uuid4()),
    )
    assert await PostgresDB.publish_blocking_message(
        db,
        str(job_id),
        freeze,
        expected_lane="pinned",
        agent_id=str(owner),
    )

    async with pg.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status::text AS status, freeze_data FROM jobs WHERE id=$1",
            job_id,
        )
    assert row["status"] == "waiting_for_reply"
    stored = row["freeze_data"]
    if isinstance(stored, str):
        stored = json.loads(stored)
    assert stored == freeze


@pytest.mark.asyncio
@pytest.mark.parametrize("wallclock", [False, True])
async def test_active_control_marker_blocks_reviewing_watchdogs(pg, wallclock):
    async with pg.acquire() as conn:
        parent_id = await _pinned_job(conn, status="reviewing", agent_id=None)
        if wallclock:
            await conn.execute(
                "INSERT INTO jobs "
                "(description, status, execution_lane, parent_job_id, context) "
                "VALUES ('critic', 'processing', 'pinned', $1::uuid, "
                "jsonb_build_object('verification_target', $1::uuid::text))",
                parent_id,
            )

    db = _PoolDB(pg)
    await CompletionControl(db, AsyncMock()).claim_job(
        parent_id,
        source="reviewing_control",
        expected_status="reviewing",
        expected_lane="pinned",
    )
    async with pg.acquire() as conn:
        await conn.execute(
            "UPDATE jobs SET updated_at=now()-interval '2 hours' WHERE id=$1",
            parent_id,
        )

    if wallclock:
        rows = await PostgresDB.unstick_reviewing_parents_wallclock(
            db,
            60,
            completion_commands_enabled=True,
        )
    else:
        rows = await PostgresDB.unstick_reviewing_parents(
            db,
            30,
            completion_commands_enabled=True,
        )
    assert rows == []
    async with pg.acquire() as conn:
        assert (
            await conn.fetchval("SELECT status::text FROM jobs WHERE id=$1", parent_id)
            == "reviewing"
        )


@pytest.mark.asyncio
async def test_expired_exact_claim_cannot_commit_domain_mutation(pg):
    async with pg.acquire() as conn:
        owner = await _agent(conn)
        job_id = await _pinned_job(conn, status="pending_review", agent_id=owner)

    control = CompletionControl(_PoolDB(pg), AsyncMock())
    claim = await control.claim_job(
        job_id,
        source="expiry_test",
        expected_status="pending_review",
        expected_lane="pinned",
    )
    async with pg.acquire() as conn:
        await conn.execute(
            "UPDATE jobs SET context=jsonb_set(context, "
            "'{_completion_control_claim,expires_epoch}', '1'::jsonb) "
            "WHERE id=$1",
            job_id,
        )

    with pytest.raises(
        CompletionControlClaimConflict,
        match="job changed while control was being committed",
    ):
        async with control.finish_claim(claim) as (conn, _job):
            await conn.execute(
                "UPDATE jobs SET error_message='must-not-commit' WHERE id=$1",
                job_id,
            )

    async with pg.acquire() as conn:
        assert (
            await conn.fetchval("SELECT error_message FROM jobs WHERE id=$1", job_id)
            is None
        )
