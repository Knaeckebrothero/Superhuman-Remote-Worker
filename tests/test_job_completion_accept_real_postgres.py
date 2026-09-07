"""Real-Postgres proofs for Gate-3 completion-command admission (M2).

The acceptance transaction deliberately composes two independently durable
objects -- ``job_completion_commands`` and, for stateless workers,
``run_queue``.  Mocks cannot prove its row-lock serialization, conflict race,
or transaction-wide rollback behavior, so these tests run against PostgreSQL's
real lock manager.

The generated app-schema snapshot is applied verbatim to an isolated
testcontainers database.  That keeps the harness on the exact result of every
migration shaping ``jobs``, ``run_queue`` and the completion tables instead of
maintaining a permissive test-only schema that can drift from production.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio
from testcontainers.postgres import PostgresContainer

from orchestrator.services.job_completion_commands import (
    CompletionAcceptResult,
    CompletionFenceRejected,
    CompletionInProgress,
    CompletionNonTerminalReport,
    CompletionPayloadMismatch,
    accept_completion_command,
    canonical_completion_payload,
    completion_payload_digest,
)
from shared.worker_queue import get_worker_completion_acceptance


SCHEMA_FILE = (
    Path(__file__).resolve().parents[1]
    / "src"
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
            "TRUNCATE completion_effects, job_completion_commands, "
            "run_queue, jobs, agents CASCADE"
        )
    try:
        yield pool
    finally:
        await pool.close()


def _payload(label: str = "complete") -> dict[str, object]:
    # should_stop mirrors the real JobCompleteRequest: every genuine stop
    # declares it, and the stateless accept arm REJECTS a falsy value
    # (CompletionNonTerminalReport) — a continue-shaped report would
    # B4-terminalize the queue row under a job that stays `processing`.
    return {
        "status": "completed",
        "should_stop": True,
        "result": {"summary": label, "ok": True},
        "freeze_data": {"type": "job_complete", "reason": label},
    }


async def _insert_agent(conn: asyncpg.Connection) -> UUID:
    return await conn.fetchval(
        "INSERT INTO agents (config_name, hostname, status) "
        "VALUES ('developer', $1, 'working') RETURNING id",
        f"completion-test-{uuid4().hex[:10]}",
    )


async def _insert_job(
    conn: asyncpg.Connection,
    *,
    lane: str,
    agent_id: UUID | None = None,
) -> UUID:
    return await conn.fetchval(
        "INSERT INTO jobs "
        "(description, status, execution_lane, assigned_agent_id) "
        "VALUES ('completion admission test', 'processing', $1, $2) "
        "RETURNING id",
        lane,
        agent_id,
    )


async def _insert_worker_lease(
    conn: asyncpg.Connection,
    *,
    job_id: UUID,
    lease_token: int = 7,
) -> None:
    await conn.execute(
        """
        INSERT INTO run_queue (
            unit_id, unit_kind, state, attempts_since_completion,
            lease_token, leased_by, last_leased_by, leased_until,
            input_seq, consumed_seq
        ) VALUES (
            $1, 'worker_batch', 'leased', 3,
            $2, 'completion-test-pod', 'completion-test-pod',
            now() + interval '5 minutes', 11, 10
        )
        """,
        job_id,
        lease_token,
    )


async def _accept(
    source,
    *,
    job_id: UUID,
    payload: dict[str, object] | None = None,
    lease_token: int | None = None,
    agent_id: UUID | None = None,
    client_report_id: UUID | None = None,
    status_reorder_enabled: bool = False,
) -> CompletionAcceptResult:
    return await accept_completion_command(
        source,
        job_id=str(job_id),
        payload=payload or _payload(),
        lease_token=str(lease_token) if lease_token is not None else None,
        agent_id=str(agent_id) if agent_id is not None else None,
        client_report_id=(
            str(client_report_id) if client_report_id is not None else None
        ),
        requested_by="real-postgres-test",
        status_reorder_enabled=status_reorder_enabled,
    )


@pytest.mark.asyncio
async def test_pinned_accept_requires_the_exact_assigned_agent(pg):
    async with pg.acquire() as conn:
        assigned_agent_id = await _insert_agent(conn)
        other_agent_id = await _insert_agent(conn)
        accepted_job_id = await _insert_job(
            conn, lane="pinned", agent_id=assigned_agent_id
        )
        rejected_job_id = await _insert_job(
            conn, lane="pinned", agent_id=assigned_agent_id
        )

    report_id = uuid4()
    accepted = await _accept(
        pg,
        job_id=accepted_job_id,
        agent_id=assigned_agent_id,
        client_report_id=report_id,
        status_reorder_enabled=True,
    )
    assert isinstance(accepted, CompletionAcceptResult)

    async with pg.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT report_seq, client_report_id, accepted_lease_token, "
            "accepted_agent_id, accepted_job_status, status_reorder_enabled "
            "FROM job_completion_commands WHERE job_id = $1",
            accepted_job_id,
        )
    assert dict(row) == {
        "report_seq": 1,
        "client_report_id": report_id,
        "accepted_lease_token": None,
        "accepted_agent_id": assigned_agent_id,
        "accepted_job_status": "processing",
        "status_reorder_enabled": True,
    }

    with pytest.raises(CompletionFenceRejected):
        await _accept(
            pg,
            job_id=rejected_job_id,
            agent_id=other_agent_id,
            client_report_id=uuid4(),
        )

    async with pg.acquire() as conn:
        rejected_state = await conn.fetchrow(
            "SELECT completion_seq_hwm, "
            "(SELECT count(*) FROM job_completion_commands "
            " WHERE job_id = jobs.id) AS command_count "
            "FROM jobs WHERE id = $1",
            rejected_job_id,
        )
    assert dict(rejected_state) == {"completion_seq_hwm": 0, "command_count": 0}


@pytest.mark.asyncio
async def test_stateless_accept_rejects_non_terminal_report_without_any_mutation(
    pg,
):
    """The live wedge from the step-5 hand-check (job a61d9940): a
    continue-shaped report (should_stop false) must be refused BEFORE any
    write — accepting it B4-terminalizes the queue row under a job that
    stays `processing`, with no rescuer route left that matches."""
    lease_token = 57
    async with pg.acquire() as conn:
        job_id = await _insert_job(conn, lane="stateless")
        await _insert_worker_lease(conn, job_id=job_id, lease_token=lease_token)
        before_queue = await conn.fetchrow(
            "SELECT state, lease_token FROM run_queue WHERE unit_id = $1",
            job_id,
        )

    non_terminal = _payload("continue")
    non_terminal["should_stop"] = False
    with pytest.raises(CompletionNonTerminalReport):
        await _accept(
            pg,
            job_id=job_id,
            payload=non_terminal,
            lease_token=lease_token,
            client_report_id=uuid4(),
        )

    # Absent should_stop is equally non-terminal: fail closed, not open.
    absent = _payload("absent")
    del absent["should_stop"]
    with pytest.raises(CompletionNonTerminalReport):
        await _accept(
            pg,
            job_id=job_id,
            payload=absent,
            lease_token=lease_token,
            client_report_id=uuid4(),
        )

    async with pg.acquire() as conn:
        after = await conn.fetchrow(
            "SELECT state, lease_token, "
            "(SELECT count(*) FROM job_completion_commands "
            " WHERE job_id = $1) AS command_count, "
            "(SELECT completion_seq_hwm FROM jobs WHERE id = $1) AS hwm "
            "FROM run_queue WHERE unit_id = $1",
            job_id,
        )
    assert after["state"] == before_queue["state"] == "leased", (
        "a rejected continue-report must leave the worker lease untouched"
    )
    assert after["lease_token"] == before_queue["lease_token"]
    assert after["command_count"] == 0, "no command row for a rejected report"
    assert after["hwm"] == 0, "the report_seq cursor must not advance"


@pytest.mark.asyncio
async def test_stateless_accept_fences_stale_token_without_any_mutation_then_closes_unit(
    pg,
):
    lease_token = 41
    async with pg.acquire() as conn:
        job_id = await _insert_job(conn, lane="stateless")
        await _insert_worker_lease(conn, job_id=job_id, lease_token=lease_token)
        before_queue = await conn.fetchrow(
            "SELECT state, lease_token, leased_by, leased_until, "
            "attempts_since_completion, input_seq, consumed_seq "
            "FROM run_queue WHERE unit_id = $1",
            job_id,
        )

    with pytest.raises(CompletionFenceRejected):
        await _accept(
            pg,
            job_id=job_id,
            lease_token=lease_token - 1,
            client_report_id=uuid4(),
        )

    async with pg.acquire() as conn:
        stale_job = await conn.fetchrow(
            "SELECT completion_seq_hwm, "
            "(SELECT count(*) FROM job_completion_commands "
            " WHERE job_id = jobs.id) AS command_count "
            "FROM jobs WHERE id = $1",
            job_id,
        )
        after_stale_queue = await conn.fetchrow(
            "SELECT state, lease_token, leased_by, leased_until, "
            "attempts_since_completion, input_seq, consumed_seq "
            "FROM run_queue WHERE unit_id = $1",
            job_id,
        )
    assert dict(stale_job) == {"completion_seq_hwm": 0, "command_count": 0}
    assert dict(after_stale_queue) == dict(before_queue)

    report_id = uuid4()
    accepted = await _accept(
        pg,
        job_id=job_id,
        lease_token=lease_token,
        client_report_id=report_id,
    )
    assert isinstance(accepted, CompletionAcceptResult)

    async with pg.acquire() as conn:
        command = await conn.fetchrow(
            "SELECT report_seq, client_report_id, accepted_lease_token, "
            "accepted_agent_id, accepted_job_status, status_reorder_enabled "
            "FROM job_completion_commands WHERE job_id = $1",
            job_id,
        )
        queue = await conn.fetchrow(
            "SELECT state, lease_token, leased_by, leased_until, "
            "attempts_since_completion, input_seq, consumed_seq "
            "FROM run_queue WHERE unit_id = $1",
            job_id,
        )
    assert dict(command) == {
        "report_seq": 1,
        "client_report_id": report_id,
        "accepted_lease_token": lease_token,
        "accepted_agent_id": None,
        "accepted_job_status": "processing",
        "status_reorder_enabled": False,
    }
    assert dict(queue) == {
        "state": "done",
        "lease_token": lease_token,
        "leased_by": None,
        "leased_until": None,
        "attempts_since_completion": 0,
        "input_seq": 11,
        "consumed_seq": 11,
    }


@pytest.mark.asyncio
async def test_b4_closed_worker_exposes_exact_command_outcome_and_db_clock_bounds(pg):
    lease_token = 47
    async with pg.acquire() as conn:
        job_id = await _insert_job(conn, lane="stateless")
        await _insert_worker_lease(conn, job_id=job_id, lease_token=lease_token)

    accepted = await _accept(
        pg,
        job_id=job_id,
        lease_token=lease_token,
        client_report_id=uuid4(),
    )
    command_id = UUID(accepted.command_id)
    assert accepted.queue_terminalized

    async with pg.acquire() as conn:
        stored_times = await conn.fetchrow(
            """
            UPDATE job_completion_commands
            SET state = 'finalizing', attempts = 1,
                finalizing_by = 'worker-handoff-proof',
                deadline_at = clock_timestamp() + interval '2 minutes',
                lease_expires_at = clock_timestamp() + interval '1 minute',
                run_after = clock_timestamp() + interval '30 seconds'
            WHERE id = $1
            RETURNING deadline_at, lease_expires_at, run_after
            """,
            command_id,
        )
        before = await conn.fetchval("SELECT clock_timestamp()")
        finalizing = await get_worker_completion_acceptance(
            conn,
            unit_id=job_id,
            lease_token=lease_token,
            command_id=command_id,
        )
        after = await conn.fetchval("SELECT clock_timestamp()")

        outcome = {
            "new_status": "pending_review",
            "report_seq": accepted.report_seq,
        }
        await conn.execute(
            """
            UPDATE job_completion_commands
            SET state = 'done', outcome = $2::jsonb, finalized_at = now(),
                finalizing_by = NULL, lease_expires_at = NULL
            WHERE id = $1
            """,
            command_id,
            json.dumps(outcome),
        )
        done = await get_worker_completion_acceptance(
            conn,
            unit_id=job_id,
            lease_token=lease_token,
            command_id=command_id,
        )
        wrong_command = await get_worker_completion_acceptance(
            conn,
            unit_id=job_id,
            lease_token=lease_token,
            command_id=uuid4(),
        )

    assert finalizing is not None
    assert finalizing.job_status == "processing"
    assert finalizing.queue_state == "done"
    assert finalizing.command_state == "finalizing"
    assert finalizing.command_id == command_id
    assert finalizing.command_outcome == {}
    assert finalizing.deadline_at == stored_times["deadline_at"]
    assert finalizing.lease_expires_at == stored_times["lease_expires_at"]
    assert finalizing.run_after == stored_times["run_after"]
    assert not finalizing.deadline_expired

    for remaining, target in (
        (finalizing.deadline_remaining_seconds, finalizing.deadline_at),
        (finalizing.lease_remaining_seconds, finalizing.lease_expires_at),
        (finalizing.run_after_remaining_seconds, finalizing.run_after),
    ):
        assert remaining is not None
        assert target is not None
        lower_bound = max(0.0, (target - after).total_seconds())
        upper_bound = max(0.0, (target - before).total_seconds())
        assert lower_bound - 0.05 <= remaining <= upper_bound + 0.05

    assert done is not None
    assert done.command_id == command_id
    assert done.command_state == "done"
    assert done.command_outcome == outcome
    assert done.lease_expires_at is None
    assert done.lease_remaining_seconds is None
    assert wrong_command is None


@pytest.mark.asyncio
async def test_stateless_queue_close_failure_rolls_back_command_and_hwm(pg):
    """The queue terminalization and command INSERT are one transaction."""

    lease_token = 53
    async with pg.acquire() as conn:
        job_id = await _insert_job(conn, lane="stateless")
        await _insert_worker_lease(conn, job_id=job_id, lease_token=lease_token)
        await conn.execute(
            """
            CREATE FUNCTION test_reject_completion_queue_close()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                RAISE EXCEPTION 'injected queue-close failure';
            END;
            $$;
            CREATE TRIGGER test_reject_completion_queue_close
            BEFORE UPDATE ON run_queue
            FOR EACH ROW
            WHEN (NEW.state = 'done')
            EXECUTE FUNCTION test_reject_completion_queue_close();
            """
        )

    try:
        with pytest.raises(asyncpg.PostgresError, match="injected queue-close failure"):
            await _accept(
                pg,
                job_id=job_id,
                lease_token=lease_token,
                client_report_id=uuid4(),
            )
    finally:
        async with pg.acquire() as conn:
            await conn.execute(
                "DROP TRIGGER IF EXISTS test_reject_completion_queue_close ON run_queue"
            )
            await conn.execute(
                "DROP FUNCTION IF EXISTS test_reject_completion_queue_close()"
            )

    async with pg.acquire() as conn:
        job = await conn.fetchrow(
            "SELECT completion_seq_hwm, "
            "(SELECT count(*) FROM job_completion_commands "
            " WHERE job_id = jobs.id) AS command_count "
            "FROM jobs WHERE id = $1",
            job_id,
        )
        queue = await conn.fetchrow(
            "SELECT state, lease_token, leased_by, attempts_since_completion, "
            "input_seq, consumed_seq FROM run_queue WHERE unit_id = $1",
            job_id,
        )
    assert dict(job) == {"completion_seq_hwm": 0, "command_count": 0}
    assert dict(queue) == {
        "state": "leased",
        "lease_token": lease_token,
        "leased_by": "completion-test-pod",
        "attempts_since_completion": 3,
        "input_seq": 11,
        "consumed_seq": 10,
    }


@pytest.mark.asyncio
async def test_concurrent_same_report_has_one_winner_and_one_in_progress(pg):
    async with pg.acquire() as conn:
        agent_id = await _insert_agent(conn)
        job_id = await _insert_job(conn, lane="pinned", agent_id=agent_id)

    report_id = uuid4()
    outcomes = await asyncio.gather(
        _accept(
            pg,
            job_id=job_id,
            agent_id=agent_id,
            client_report_id=report_id,
        ),
        _accept(
            pg,
            job_id=job_id,
            agent_id=agent_id,
            client_report_id=report_id,
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(value, CompletionAcceptResult) for value in outcomes) == 1
    assert sum(isinstance(value, CompletionInProgress) for value in outcomes) == 1

    async with pg.acquire() as conn:
        state = await conn.fetchrow(
            "SELECT completion_seq_hwm, "
            "(SELECT count(*) FROM job_completion_commands "
            " WHERE job_id = jobs.id) AS command_count "
            "FROM jobs WHERE id = $1",
            job_id,
        )
    assert dict(state) == {"completion_seq_hwm": 1, "command_count": 1}


@pytest.mark.asyncio
async def test_duplicate_payload_split_is_typed_409_vs_422(pg):
    async with pg.acquire() as conn:
        agent_id = await _insert_agent(conn)
        job_id = await _insert_job(conn, lane="pinned", agent_id=agent_id)

    report_id = uuid4()
    original = _payload("original")
    await _accept(
        pg,
        job_id=job_id,
        payload=original,
        agent_id=agent_id,
        client_report_id=report_id,
    )

    # Equal digest while the first command is still pending is the retryable
    # HTTP-409 class. Reusing the UUID for different content is the permanent
    # HTTP-422 class. Keeping distinct exception types prevents a route from
    # accidentally assigning both cases the same retry policy.
    with pytest.raises(CompletionInProgress):
        await _accept(
            pg,
            job_id=job_id,
            payload=original,
            agent_id=agent_id,
            client_report_id=report_id,
        )
    with pytest.raises(CompletionPayloadMismatch):
        await _accept(
            pg,
            job_id=job_id,
            payload=_payload("divergent"),
            agent_id=agent_id,
            client_report_id=report_id,
        )

    async with pg.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT payload_digest, "
            "(SELECT completion_seq_hwm FROM jobs WHERE id = $1) AS hwm, "
            "count(*) OVER () AS command_count "
            "FROM job_completion_commands WHERE job_id = $1",
            job_id,
        )
    assert row["payload_digest"] == completion_payload_digest(str(job_id), original)
    assert row["hwm"] == 1
    assert row["command_count"] == 1


def test_canonical_payload_digest_excludes_transport_and_fence_fields():
    first = {
        **_payload(),
        "lease_token": "7",
        "agent_id": str(uuid4()),
        "client_report_id": str(uuid4()),
    }
    second = {
        **_payload(),
        "lease_token": "999",
        "agent_id": str(uuid4()),
        "client_report_id": str(uuid4()),
    }
    assert canonical_completion_payload(first) == canonical_completion_payload(second)
    job_id = str(uuid4())
    assert completion_payload_digest(job_id, first) == completion_payload_digest(
        job_id, second
    )


@pytest.mark.asyncio
async def test_missing_client_id_fallback_is_deterministic_per_job_report_seq(pg):
    async with pg.acquire() as conn:
        agent_id = await _insert_agent(conn)
        job_id = await _insert_job(conn, lane="pinned", agent_id=agent_id)

        # Allocate seq=1 once and roll the whole caller-owned transaction back.
        # Reallocating that same (job_id, report_seq) pair must synthesize the
        # same UUID; this proves determinism without duplicating the production
        # UUID namespace/encoding algorithm in the assertion.
        tx = conn.transaction()
        await tx.start()
        try:
            await _accept(conn, job_id=job_id, agent_id=agent_id)
            rolled_back = await conn.fetchrow(
                "SELECT report_seq, client_report_id "
                "FROM job_completion_commands WHERE job_id = $1",
                job_id,
            )
        finally:
            await tx.rollback()

        assert (
            await conn.fetchval(
                "SELECT completion_seq_hwm FROM jobs WHERE id = $1", job_id
            )
            == 0
        )
        assert not await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM job_completion_commands WHERE job_id = $1)",
            job_id,
        )

        await _accept(conn, job_id=job_id, agent_id=agent_id)
        committed_first = await conn.fetchrow(
            "SELECT report_seq, client_report_id "
            "FROM job_completion_commands WHERE job_id = $1",
            job_id,
        )
        await _accept(conn, job_id=job_id, agent_id=agent_id)
        committed = await conn.fetch(
            "SELECT report_seq, client_report_id "
            "FROM job_completion_commands WHERE job_id = $1 ORDER BY report_seq",
            job_id,
        )
        hwm = await conn.fetchval(
            "SELECT completion_seq_hwm FROM jobs WHERE id = $1", job_id
        )

    assert dict(rolled_back) == dict(committed_first)
    assert [row["report_seq"] for row in committed] == [1, 2]
    assert committed[0]["client_report_id"] != committed[1]["client_report_id"]
    assert hwm == 2
