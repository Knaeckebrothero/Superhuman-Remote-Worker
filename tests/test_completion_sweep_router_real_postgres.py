"""Real-Postgres ownership and dedup proofs for completion-sweep routing."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio
from testcontainers.postgres import PostgresContainer

from orchestrator.services.completion_sweep_router import (
    CompletionSweepRouter,
    _ClaimedAction,
)
from orchestrator.database.postgres import PostgresDB
from src.shared.worker_queue import claim_worker_batch


SCHEMA_FILE = (
    Path(__file__).resolve().parents[1]
    / "orchestrator"
    / "database"
    / "schema_current.sql"
)


@pytest.fixture(scope="module")
def pg_dsn():
    with PostgresContainer("postgres:16") as postgres:
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
            "TRUNCATE job_completion_sweep_actions, completion_effects, "
            "completion_finalizer_leases, job_completion_commands, "
            "run_queue, jobs, agents CASCADE"
        )
    try:
        yield pool
    finally:
        await pool.close()


async def _job(pg) -> UUID:
    async with pg.acquire() as conn:
        return await conn.fetchval(
            "INSERT INTO jobs (description, status, execution_lane) "
            "VALUES ('completion router test', 'processing', 'pinned') "
            "RETURNING id"
        )


async def _command(
    pg,
    job_id: UUID,
    *,
    state: str = "pending",
    attempts: int = 0,
    max_attempts: int = 5,
    lease_expires_at: datetime | None = None,
    deadline_at: datetime | None = None,
) -> UUID:
    error_code = "operator_required" if state == "parked" else None
    async with pg.acquire() as conn:
        return await conn.fetchval(
            """
            INSERT INTO job_completion_commands (
                job_id, report_seq, client_report_id, payload, payload_digest,
                origin, requested_by, state, attempts, max_attempts,
                run_after, lease_expires_at, deadline_at, code_version,
                error_code
            ) VALUES (
                $1, 1, $2, '{}'::jsonb, 'digest', 'operator',
                'completion-router-test', $3, $4, $5, now(), $6, $7,
                'test-version', $8
            )
            RETURNING id
            """,
            job_id,
            uuid4(),
            state,
            attempts,
            max_attempts,
            lease_expires_at,
            deadline_at or datetime.now(UTC) + timedelta(hours=1),
            error_code,
        )


def _db_from_pool(pool) -> PostgresDB:
    db = PostgresDB.__new__(PostgresDB)

    @asynccontextmanager
    async def acquire():
        async with pool.acquire() as conn:
            yield conn

    db.acquire = acquire
    return db


def _finalizer_result(command_id: str):
    return SimpleNamespace(
        command_id=command_id,
        state="done",
        disposition="done",
        outcome={"ignored": "x" * 20_000},
        error_code=None,
    )


@pytest.mark.asyncio
async def test_resume_bypass_rolls_back_and_claim_skips_blocked_fifo_head(pg):
    """The accepted-202 catastrophe dies at both mutation and claim layers."""

    blocked_job = uuid4()
    eligible_job = uuid4()
    async with pg.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO jobs (id, description, status, execution_lane)
            VALUES ($1, 'blocked', 'paused', 'stateless'),
                   ($2, 'eligible', 'paused', 'stateless')
            """,
            blocked_job,
            eligible_job,
        )
        await conn.execute(
            """
            INSERT INTO run_queue (
                unit_id, unit_kind, state, priority, queued_at, run_after
            ) VALUES
                ($1, 'worker_batch', 'done', 100, now() - interval '2 minutes', now()),
                ($2, 'worker_batch', 'queued', 0, now() - interval '1 minute', now())
            """,
            blocked_job,
            eligible_job,
        )
    await _command(pg, blocked_job, state="pending")

    db = _db_from_pool(pg)
    assert not await db.queue_stateless_job_for_resume(
        str(blocked_job),
        {"queued_feedback": "must not start round two"},
        expected_status="paused",
        completion_commands_enabled=True,
    )
    async with pg.acquire() as conn:
        blocked_after_resume = await conn.fetchrow(
            "SELECT state, lease_token FROM run_queue WHERE unit_id=$1",
            blocked_job,
        )
        blocked_status = await conn.fetchval(
            "SELECT status::text FROM jobs WHERE id=$1", blocked_job
        )
    assert blocked_after_resume["state"] == "done"
    assert blocked_after_resume["lease_token"] == 0
    assert blocked_status == "paused"

    # Bypass the public verb exactly as the catastrophe trace does.  The
    # worker-side predicate is an independent safety layer: a command-blocked
    # high-priority head must remain byte-for-byte queued while the claimant
    # skips it and leases the next eligible unit.
    async with pg.acquire() as conn:
        await conn.execute(
            "UPDATE run_queue SET state='queued' WHERE unit_id=$1",
            blocked_job,
        )
        blocked_before_claim = await conn.fetchrow(
            "SELECT state, lease_token, attempts_since_completion, leased_by, "
            "leased_until FROM run_queue WHERE unit_id=$1",
            blocked_job,
        )
    assert blocked_before_claim["state"] == "queued"

    claim = await claim_worker_batch(
        pg,
        pod_name="m2-claim-proof",
        affinity_grace_seconds=0,
        completion_commands_enabled=True,
    )
    assert claim is not None
    assert claim.unit_id == eligible_job

    async with pg.acquire() as conn:
        blocked_after_claim = await conn.fetchrow(
            "SELECT state, lease_token, attempts_since_completion, leased_by, "
            "leased_until FROM run_queue WHERE unit_id=$1",
            blocked_job,
        )
    assert dict(blocked_after_claim) == dict(blocked_before_claim)


@pytest.mark.asyncio
async def test_accept_holding_job_lock_wins_against_concurrent_pinned_resume(pg):
    """The post-lock command read observes an accept that commits while waiting."""

    job_id = await _job(pg)
    db = _db_from_pool(pg)
    accept_conn = await pg.acquire()
    transaction = accept_conn.transaction()
    await transaction.start()
    committed = False
    resume_task = None
    try:
        await accept_conn.fetchval("SELECT id FROM jobs WHERE id=$1 FOR UPDATE", job_id)
        resume_task = asyncio.create_task(
            db.queue_job_for_resume(
                str(job_id),
                {"queued_feedback": "round two"},
                expected_status="processing",
                completion_commands_enabled=True,
            )
        )
        await asyncio.sleep(0.05)
        assert not resume_task.done()
        await accept_conn.execute(
            """
            INSERT INTO job_completion_commands (
                job_id, report_seq, client_report_id, payload, payload_digest,
                origin, requested_by, state, run_after, deadline_at, code_version
            ) VALUES (
                $1, 1, $2, '{}'::jsonb, 'digest', 'operator', 'race-test',
                'pending', now(), now() + interval '1 hour', 'test-version'
            )
            """,
            job_id,
            uuid4(),
        )
        await transaction.commit()
        committed = True
        assert not await asyncio.wait_for(resume_task, timeout=2)
    finally:
        if not committed:
            await transaction.rollback()
        if resume_task is not None and not resume_task.done():
            resume_task.cancel()
            await asyncio.gather(resume_task, return_exceptions=True)
        await pg.release(accept_conn)

    async with pg.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status::text AS status, context FROM jobs WHERE id=$1", job_id
        )
    assert row["status"] == "processing"
    context = row["context"]
    if isinstance(context, str):
        context = json.loads(context)
    assert "queued_feedback" not in (context or {})


@pytest.mark.asyncio
async def test_pinned_resume_holding_job_lock_closes_late_accept_fence(pg):
    job_id = await _job(pg)
    resume_conn = await pg.acquire()
    transaction = resume_conn.transaction()
    await transaction.start()
    committed = False
    accept_task = None

    async def _late_accept() -> bool:
        async with pg.acquire() as conn:
            async with conn.transaction():
                status = await conn.fetchval(
                    "SELECT status::text FROM jobs WHERE id=$1 FOR UPDATE", job_id
                )
                if status != "processing":
                    return False
                await conn.execute(
                    """
                    INSERT INTO job_completion_commands (
                        job_id, report_seq, client_report_id, payload,
                        payload_digest, origin, requested_by, state,
                        run_after, deadline_at, code_version
                    ) VALUES (
                        $1, 1, $2, '{}'::jsonb, 'digest', 'operator',
                        'late-accept', 'pending', now(),
                        now() + interval '1 hour', 'test-version'
                    )
                    """,
                    job_id,
                    uuid4(),
                )
                return True

    try:
        await resume_conn.fetchval("SELECT id FROM jobs WHERE id=$1 FOR UPDATE", job_id)
        accept_task = asyncio.create_task(_late_accept())
        await asyncio.sleep(0.05)
        assert not accept_task.done()
        await resume_conn.execute(
            "UPDATE jobs SET status='paused' WHERE id=$1 AND status='processing'",
            job_id,
        )
        await transaction.commit()
        committed = True
        assert not await asyncio.wait_for(accept_task, timeout=2)
    finally:
        if not committed:
            await transaction.rollback()
        if accept_task is not None and not accept_task.done():
            accept_task.cancel()
            await asyncio.gather(accept_task, return_exceptions=True)
        await pg.release(resume_conn)

    async with pg.acquire() as conn:
        assert not await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM job_completion_commands WHERE job_id=$1)",
            job_id,
        )


class _BlockingFinalizer:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls: list[tuple[str, bool]] = []

    async def finalize_command(self, command_id: str, *, inline: bool):
        self.calls.append((command_id, inline))
        self.started.set()
        await self.release.wait()
        return _finalizer_result(command_id)


@pytest.mark.asyncio
async def test_missing_command_is_legacy_and_live_finalizer_stands_down(pg):
    legacy_job = await _job(pg)
    live_job = await _job(pg)
    await _command(
        pg,
        live_job,
        state="finalizing",
        attempts=1,
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    finalizer = SimpleNamespace(finalize_command=AsyncMock())
    router = CompletionSweepRouter(pg, finalizer, claimant_id="route-proof")

    legacy = await router.route_job(legacy_job, source="orphan")
    stand_down = await router.route_job(live_job, source="expired-job-lease")

    assert legacy.disposition == "legacy"
    assert legacy.legacy
    assert stand_down.disposition == "stand_down"
    assert stand_down.route == "stand_down"
    finalizer.finalize_command.assert_not_awaited()
    async with pg.acquire() as conn:
        assert (
            await conn.fetchval("SELECT count(*) FROM job_completion_sweep_actions")
            == 0
        )
        assert (
            await conn.fetchval(
                "SELECT completion_sweep_attempt_hwm FROM jobs WHERE id=$1",
                live_job,
            )
            == 0
        )


@pytest.mark.asyncio
async def test_concurrent_routes_share_one_action_and_heartbeat_keeps_term_live(pg):
    job_id = await _job(pg)
    command_id = await _command(pg, job_id)
    finalizer = _BlockingFinalizer()
    first_router = CompletionSweepRouter(
        pg,
        finalizer,
        claimant_id="router-a",
        action_lease_seconds=0.3,
    )
    second_router = CompletionSweepRouter(
        pg,
        finalizer,
        claimant_id="router-b",
        action_lease_seconds=0.3,
    )

    first_task = asyncio.create_task(
        first_router.route_job(job_id, source="stale-agent")
    )
    await asyncio.wait_for(finalizer.started.wait(), timeout=2)
    async with pg.acquire() as conn:
        first_claim = await conn.fetchrow(
            "SELECT attempt, claimed_by, claim_expires_at "
            "FROM job_completion_sweep_actions WHERE job_id=$1",
            job_id,
        )
    initial_expiry = first_claim["claim_expires_at"]
    first_owner = str(first_claim["claimed_by"])
    assert first_owner.startswith("router-a:")

    stale_action = _ClaimedAction(
        job_id=str(job_id),
        attempt=int(first_claim["attempt"]),
        command_id=str(command_id),
        command_attempt=0,
        route="resume_finalizer",
        claimed_by=f"{first_owner}-stale",
    )
    assert not await first_router._renew(stale_action)

    renewal_deadline = asyncio.get_running_loop().time() + 2
    renewed_expiry = initial_expiry
    while renewed_expiry <= initial_expiry + timedelta(milliseconds=50):
        assert asyncio.get_running_loop().time() < renewal_deadline
        await asyncio.sleep(0.03)
        async with pg.acquire() as conn:
            renewed_expiry = await conn.fetchval(
                "SELECT claim_expires_at FROM job_completion_sweep_actions "
                "WHERE job_id=$1",
                job_id,
            )

    await asyncio.sleep(
        max(0.0, (initial_expiry - datetime.now(UTC)).total_seconds()) + 0.03
    )
    competing = await second_router.route_job(job_id, source="orphan")
    assert competing.disposition == "busy"
    assert finalizer.calls == [(str(command_id), False)]

    finalizer.release.set()
    completed = await asyncio.wait_for(first_task, timeout=2)
    assert completed.disposition == "completed"
    replay = await second_router.route_job(job_id, source="orphan")
    assert replay.disposition == "already_done"

    async with pg.acquire() as conn:
        durable = await conn.fetchrow(
            "SELECT state, source, claimed_by, result "
            "FROM job_completion_sweep_actions WHERE job_id=$1",
            job_id,
        )
        action_count = await conn.fetchval(
            "SELECT count(*) FROM job_completion_sweep_actions WHERE job_id=$1",
            job_id,
        )
        hwm = await conn.fetchval(
            "SELECT completion_sweep_attempt_hwm FROM jobs WHERE id=$1", job_id
        )
    result = durable["result"]
    if isinstance(result, str):
        result = json.loads(result)
    assert durable["state"] == "done"
    assert durable["source"] == "stale-agent"
    assert durable["claimed_by"] is None
    assert result == {
        "finalizer": {
            "command_id": str(command_id),
            "disposition": "done",
            "state": "done",
        },
        "route": "resume_finalizer",
    }
    assert action_count == 1
    assert hwm == 1


@pytest.mark.asyncio
async def test_exception_claim_is_taken_over_without_allocating_another_action(pg):
    job_id = await _job(pg)
    command_id = await _command(pg, job_id)
    failing = SimpleNamespace(
        finalize_command=AsyncMock(side_effect=RuntimeError("finalizer unavailable"))
    )
    first = CompletionSweepRouter(
        pg, failing, claimant_id="failed-router", action_lease_seconds=30
    )
    with pytest.raises(RuntimeError, match="finalizer unavailable"):
        await first.route_job(job_id, source="orphan")

    async with pg.acquire() as conn:
        abandoned_owner = await conn.fetchval(
            "SELECT claimed_by FROM job_completion_sweep_actions WHERE job_id=$1",
            job_id,
        )
        await conn.execute(
            "UPDATE job_completion_sweep_actions "
            "SET claimed_at=now()-interval '2 seconds', "
            "claim_expires_at=now()-interval '1 second' WHERE job_id=$1",
            job_id,
        )

    succeeding = SimpleNamespace(
        finalize_command=AsyncMock(return_value=_finalizer_result(str(command_id)))
    )
    second = CompletionSweepRouter(pg, succeeding, claimant_id="takeover-router")
    takeover = await second.route_job(job_id, source="pause-redispatch")

    assert takeover.disposition == "completed"
    succeeding.finalize_command.assert_awaited_once_with(str(command_id), inline=False)
    async with pg.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT attempt, state, claimed_by FROM job_completion_sweep_actions "
            "WHERE job_id=$1",
            job_id,
        )
        count = await conn.fetchval(
            "SELECT count(*) FROM job_completion_sweep_actions WHERE job_id=$1",
            job_id,
        )
        hwm = await conn.fetchval(
            "SELECT completion_sweep_attempt_hwm FROM jobs WHERE id=$1", job_id
        )
    assert str(abandoned_owner).startswith("failed-router:")
    assert dict(row) == {"attempt": 1, "state": "done", "claimed_by": None}
    assert count == 1
    assert hwm == 1


@pytest.mark.asyncio
async def test_alert_only_never_finalizes_and_park_alert_finalizes_before_alert(pg):
    parked_job = await _job(pg)
    parked_command = await _command(pg, parked_job, state="parked")
    capped_job = await _job(pg)
    capped_command = await _command(
        pg,
        capped_job,
        state="finalizing",
        attempts=5,
        max_attempts=5,
        lease_expires_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    events: list[str] = []

    async def finalize(command_id: str, *, inline: bool):
        assert not inline
        events.append(f"finalize:{command_id}")
        return _finalizer_result(command_id)

    async def alert(message: str):
        events.append(f"alert:{message.rsplit('=', 1)[-1]}")

    finalizer = SimpleNamespace(finalize_command=AsyncMock(side_effect=finalize))
    router = CompletionSweepRouter(pg, finalizer, alert, claimant_id="alert-router")

    parked_result = await router.route_job(parked_job, source="stale-agent")
    assert parked_result.disposition == "completed"
    assert parked_result.route == "alert_only"
    finalizer.finalize_command.assert_not_awaited()
    assert events == ["alert:alert_only"]

    capped_result = await router.route_job(capped_job, source="job-lease")
    assert capped_result.disposition == "completed"
    assert capped_result.route == "park_alert"
    assert events == [
        "alert:alert_only",
        f"finalize:{capped_command}",
        "alert:park_alert",
    ]
    assert parked_result.command_id == str(parked_command)


@pytest.mark.asyncio
async def test_deadline_race_promotes_same_action_to_park_alert(pg):
    job_id = await _job(pg)
    command_id = await _command(pg, job_id)
    alerts: list[str] = []
    finalizer = SimpleNamespace(
        finalize_command=AsyncMock(
            return_value=SimpleNamespace(
                command_id=str(command_id),
                state="parked",
                disposition="busy",
                error_code="deadline_or_attempts_exhausted",
            )
        )
    )
    router = CompletionSweepRouter(
        pg,
        finalizer,
        alerts.append,
        claimant_id="deadline-race-router",
    )

    result = await router.route_job(job_id, source="expired-finalizer")

    assert result.disposition == "completed"
    assert result.route == "park_alert"
    assert len(alerts) == 1
    async with pg.acquire() as conn:
        action = await conn.fetchrow(
            "SELECT route, state, result FROM job_completion_sweep_actions "
            "WHERE job_id=$1",
            job_id,
        )
        hwm = await conn.fetchval(
            "SELECT completion_sweep_attempt_hwm FROM jobs WHERE id=$1", job_id
        )
    durable_result = action["result"]
    if isinstance(durable_result, str):
        durable_result = json.loads(durable_result)
    assert action["route"] == "park_alert"
    assert action["state"] == "done"
    assert durable_result["route"] == "park_alert"
    assert durable_result["alerted"] is True
    assert hwm == 1


@pytest.mark.asyncio
async def test_route_once_skips_live_and_already_completed_actions(pg):
    actionable_jobs = [await _job(pg), await _job(pg)]
    command_ids = [await _command(pg, job_id) for job_id in actionable_jobs]
    live_job = await _job(pg)
    await _command(
        pg,
        live_job,
        state="finalizing",
        attempts=1,
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    grace_job = await _job(pg)
    grace_command = await _command(pg, grace_job)
    async with pg.acquire() as conn:
        await conn.execute(
            "UPDATE job_completion_commands "
            "SET run_after=now()+interval '5 minutes' WHERE id=$1",
            grace_command,
        )
    finalizer = SimpleNamespace(
        finalize_command=AsyncMock(
            side_effect=lambda command_id, inline: _finalizer_result(command_id)
        )
    )
    router = CompletionSweepRouter(pg, finalizer, claimant_id="batch-router")

    first = await router.route_once(limit=10)
    second = await router.route_once(limit=10)

    assert first.count == 2
    assert {result.job_id for result in first.results} == {
        str(job_id) for job_id in actionable_jobs
    }
    assert all(result.disposition == "completed" for result in first.results)
    assert second.count == 0
    assert second.results == ()
    assert {call.args[0] for call in finalizer.finalize_command.await_args_list} == {
        str(command_id) for command_id in command_ids
    }
    assert str(grace_command) not in {
        call.args[0] for call in finalizer.finalize_command.await_args_list
    }
