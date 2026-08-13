"""Real-Postgres ownership and dedup proofs for completion-sweep routing."""

from __future__ import annotations

import asyncio
import json
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


def _finalizer_result(command_id: str):
    return SimpleNamespace(
        command_id=command_id,
        state="done",
        disposition="done",
        outcome={"ignored": "x" * 20_000},
        error_code=None,
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
