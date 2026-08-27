"""Real-Postgres contract tests for Gate-3 completion command schema (M1)."""

import json
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
from asyncpg import CheckViolationError, UniqueViolationError
from testcontainers.postgres import PostgresContainer


MIGRATION_DIR = (
    Path(__file__).resolve().parents[1]
    / "orchestrator"
    / "database"
    / "migrations"
    / "app"
)
MIGRATIONS = (
    MIGRATION_DIR / "0140_job_completion_commands.sql",
    MIGRATION_DIR / "0141_job_completion_sweep_routing.sql",
    MIGRATION_DIR / "0142_job_completion_sweep_route_precedence.sql",
    MIGRATION_DIR / "0143_job_completion_accept_status.sql",
    MIGRATION_DIR / "0144_job_completion_status_reorder.sql",
)


@pytest.fixture(scope="module")
def pg_dsn():
    with PostgresContainer("postgres:15") as pg:
        yield pg.get_connection_url().replace("postgresql+psycopg2", "postgresql")


@pytest_asyncio.fixture
async def conn(pg_dsn):
    connection = await asyncpg.connect(pg_dsn)
    await connection.execute('CREATE EXTENSION IF NOT EXISTS "uuid-ossp"')
    await connection.execute("DROP VIEW IF EXISTS job_completion_sweep_exclusions")
    await connection.execute(
        "DROP TABLE IF EXISTS job_completion_sweep_actions CASCADE"
    )
    await connection.execute("DROP TABLE IF EXISTS completion_effects CASCADE")
    await connection.execute("DROP TABLE IF EXISTS completion_finalizer_leases CASCADE")
    await connection.execute("DROP TABLE IF EXISTS job_completion_commands CASCADE")
    await connection.execute("DROP TABLE IF EXISTS jobs CASCADE")
    await connection.execute("CREATE TABLE jobs (id UUID PRIMARY KEY)")
    for migration in MIGRATIONS:
        await connection.execute(migration.read_text())
    try:
        yield connection
    finally:
        await connection.close()


async def _job(conn):
    return await conn.fetchval(
        "INSERT INTO jobs (id) VALUES ($1) RETURNING id", uuid4()
    )


async def _insert_command(conn, job_id, **overrides):
    values = {
        "report_seq": 1,
        "client_report_id": uuid4(),
        "payload": {"result": "ok"},
        "payload_digest": "digest",
        "accepted_lease_token": 1,
        "accepted_agent_id": None,
        "origin": "agent",
        "requested_by": "test-agent",
        "state": "pending",
        "attempts": 0,
        "max_attempts": 5,
        "run_after": "2000-01-01T00:00:00Z",
        "lease_expires_at": None,
        "deadline_at": "2100-01-01T00:00:00Z",
        "code_version": "test",
        "outcome": None,
        "finalized_at": None,
        "error_code": None,
    }
    values.update(overrides)
    return await conn.fetchval(
        """
        INSERT INTO job_completion_commands (
            job_id, report_seq, client_report_id, payload, payload_digest,
            accepted_lease_token, accepted_agent_id, origin, requested_by,
            state, attempts, max_attempts, run_after, lease_expires_at,
            deadline_at, code_version, outcome, finalized_at, error_code
        ) VALUES (
            $1, $2, $3, $4::jsonb, $5, $6, $7, $8, $9,
            $10, $11, $12, $13::text::timestamptz,
            $14::text::timestamptz, $15::text::timestamptz, $16,
            $17::jsonb, $18::text::timestamptz, $19
        )
        RETURNING id
        """,
        job_id,
        values["report_seq"],
        values["client_report_id"],
        json.dumps(values["payload"], sort_keys=True),
        values["payload_digest"],
        values["accepted_lease_token"],
        values["accepted_agent_id"],
        values["origin"],
        values["requested_by"],
        values["state"],
        values["attempts"],
        values["max_attempts"],
        values["run_after"],
        values["lease_expires_at"],
        values["deadline_at"],
        values["code_version"],
        json.dumps(values["outcome"], sort_keys=True)
        if values["outcome"] is not None
        else None,
        values["finalized_at"],
        values["error_code"],
    )


async def _insert_sweep_action(conn, job_id, command_id, **overrides):
    values = {
        "attempt": 1,
        "command_attempt": 0,
        "route": "resume_finalizer",
        "source": "test-rescuer",
        "state": "pending",
        "claimed_by": None,
        "claimed_at": None,
        "claim_expires_at": None,
        "result": None,
        "error_code": None,
        "completed_at": None,
    }
    values.update(overrides)
    return await conn.fetchval(
        """
        INSERT INTO job_completion_sweep_actions (
            job_id, attempt, command_id, command_attempt, route, source,
            state, claimed_by, claimed_at, claim_expires_at, result,
            error_code, completed_at
        ) VALUES (
            $1, $2, $3, $4, $5, $6, $7, $8,
            $9::text::timestamptz, $10::text::timestamptz, $11::jsonb,
            $12, $13::text::timestamptz
        )
        RETURNING attempt
        """,
        job_id,
        values["attempt"],
        command_id,
        values["command_attempt"],
        values["route"],
        values["source"],
        values["state"],
        values["claimed_by"],
        values["claimed_at"],
        values["claim_expires_at"],
        json.dumps(values["result"], sort_keys=True)
        if values["result"] is not None
        else None,
        values["error_code"],
        values["completed_at"],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"accepted_lease_token": None},
        {"accepted_agent_id": uuid4()},
        {
            "accepted_lease_token": None,
            "accepted_agent_id": uuid4(),
            "origin": "operator",
        },
    ],
)
async def test_fence_check_rejects_every_invalid_agent_operator_shape(conn, overrides):
    job_id = await _job(conn)
    with pytest.raises(CheckViolationError, match="job_completion_fence_exactly_one"):
        await _insert_command(conn, job_id, **overrides)


@pytest.mark.asyncio
async def test_fence_check_accepts_both_agent_lanes_and_operator_origin(conn):
    job_id = await _job(conn)
    await _insert_command(conn, job_id)
    await _insert_command(
        conn,
        job_id,
        report_seq=2,
        accepted_lease_token=None,
        accepted_agent_id=uuid4(),
    )
    await _insert_command(
        conn,
        job_id,
        report_seq=3,
        accepted_lease_token=None,
        accepted_agent_id=None,
        origin="operator",
    )
    assert (
        await conn.fetchval(
            "SELECT count(*) FROM job_completion_commands WHERE job_id=$1", job_id
        )
        == 3
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"state": "pending", "outcome": {"ok": True}},
        {"state": "finalizing", "error_code": "half-written"},
        {"state": "done", "outcome": {"ok": True}},
        {"state": "done", "finalized_at": "2026-08-12T00:00:00Z"},
        {
            "state": "done",
            "outcome": {"ok": True},
            "finalized_at": "2026-08-12T00:00:00Z",
            "error_code": "impossible",
        },
        {"state": "parked"},
        {"state": "parked", "error_code": "blocked", "outcome": {"ok": False}},
        {"state": "superseded", "outcome": {"winner_report_seq": 1}},
        {"state": "force_resolved", "finalized_at": "2026-08-12T00:00:00Z"},
        {"state": "unknown"},
    ],
)
async def test_state_checks_reject_every_half_written_terminal_shape(conn, overrides):
    job_id = await _job(conn)
    with pytest.raises(CheckViolationError):
        await _insert_command(conn, job_id, **overrides)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {
            "state": "done",
            "outcome": {"status": "completed"},
            "finalized_at": "2026-08-12T00:00:00Z",
        },
        {"state": "parked", "error_code": "operator_required"},
        {
            "state": "superseded",
            "outcome": {"winner_report_seq": 1},
            "finalized_at": "2026-08-12T00:00:00Z",
            "error_code": "entry_status_superseded",
        },
        {
            "state": "force_resolved",
            "outcome": {"abandoned_effects": ["workspace_archive"]},
            "finalized_at": "2026-08-12T00:00:00Z",
            "error_code": "forced_by_operator",
        },
    ],
)
async def test_state_checks_accept_every_terminal_shape(conn, overrides):
    job_id = await _job(conn)
    assert await _insert_command(conn, job_id, **overrides) is not None


@pytest.mark.asyncio
async def test_client_report_id_deduplicates_per_job(conn):
    first_job = await _job(conn)
    second_job = await _job(conn)
    report_id = uuid4()
    await _insert_command(conn, first_job, client_report_id=report_id)
    with pytest.raises(UniqueViolationError, match="uq_job_completion_client"):
        await _insert_command(conn, first_job, report_seq=2, client_report_id=report_id)
    await _insert_command(conn, second_job, client_report_id=report_id)


@pytest.mark.asyncio
async def test_accepted_job_status_is_nullable_but_never_blank(conn):
    job_id = await _job(conn)
    command_id = await _insert_command(conn, job_id)

    assert (
        await conn.fetchval(
            "SELECT accepted_job_status FROM job_completion_commands WHERE id=$1",
            command_id,
        )
        is None
    )
    with pytest.raises(
        CheckViolationError, match="job_completion_accepted_status_nonempty"
    ):
        await conn.execute(
            "UPDATE job_completion_commands SET accepted_job_status='   ' WHERE id=$1",
            command_id,
        )
    await conn.execute(
        "UPDATE job_completion_commands SET accepted_job_status='processing' "
        "WHERE id=$1",
        command_id,
    )
    assert (
        await conn.fetchval(
            "SELECT accepted_job_status FROM job_completion_commands WHERE id=$1",
            command_id,
        )
        == "processing"
    )


@pytest.mark.asyncio
async def test_status_reorder_policy_is_non_null_and_default_off(conn):
    job_id = await _job(conn)
    command_id = await _insert_command(conn, job_id)

    assert (
        await conn.fetchval(
            "SELECT status_reorder_enabled FROM job_completion_commands WHERE id=$1",
            command_id,
        )
        is False
    )
    with pytest.raises(asyncpg.NotNullViolationError):
        await conn.execute(
            "UPDATE job_completion_commands SET status_reorder_enabled=NULL "
            "WHERE id=$1",
            command_id,
        )


@pytest.mark.asyncio
async def test_0143_backfills_only_completed_s1_proof_and_normalizes_superseded(
    conn,
):
    await conn.execute("DROP VIEW IF EXISTS job_completion_sweep_exclusions")
    await conn.execute("DROP TABLE IF EXISTS job_completion_sweep_actions CASCADE")
    await conn.execute("DROP TABLE IF EXISTS completion_effects CASCADE")
    await conn.execute("DROP TABLE IF EXISTS completion_finalizer_leases CASCADE")
    await conn.execute("DROP TABLE IF EXISTS job_completion_commands CASCADE")
    await conn.execute("DROP TABLE IF EXISTS jobs CASCADE")
    await conn.execute("CREATE TABLE jobs (id UUID PRIMARY KEY)")
    for migration in MIGRATIONS[:-2]:
        await conn.execute(migration.read_text())

    job_id = await _job(conn)
    proved = await _insert_command(conn, job_id, report_seq=1)
    pending = await _insert_command(conn, job_id, report_seq=2)
    unjournaled = await _insert_command(conn, job_id, report_seq=3)
    historical_superseded = await _insert_command(
        conn,
        job_id,
        report_seq=4,
        state="superseded",
        outcome={"status": "superseded"},
        finalized_at="2026-08-12T00:00:00Z",
        error_code=None,
    )
    await conn.executemany(
        """
        INSERT INTO completion_effects (
            producer_kind, producer_id, effect_name, effect_group, state, detail
        ) VALUES ('job_completion', $1, 'late_callback_guard', 'entry', $2, $3::jsonb)
        """,
        [
            (
                proved,
                "done",
                json.dumps({"output": {"entry_status": " processing "}}),
            ),
            (
                pending,
                "pending",
                json.dumps({"output": {"entry_status": "paused"}}),
            ),
        ],
    )

    await conn.execute(MIGRATIONS[-2].read_text())

    rows = await conn.fetch(
        "SELECT id, accepted_job_status, state, error_code, finalizing_by, "
        "lease_expires_at FROM job_completion_commands ORDER BY report_seq"
    )
    by_id = {row["id"]: row for row in rows}
    assert by_id[proved]["accepted_job_status"] == "processing"
    assert by_id[pending]["accepted_job_status"] is None
    assert by_id[unjournaled]["accepted_job_status"] is None
    normalized = by_id[historical_superseded]
    assert normalized["state"] == "superseded"
    assert normalized["error_code"] == "entry_status_superseded"
    assert normalized["finalizing_by"] is None
    assert normalized["lease_expires_at"] is None


@pytest.mark.asyncio
async def test_drain_index_exists_and_effects_has_only_its_primary_key(conn):
    drain = await conn.fetchrow(
        """
        SELECT indexdef, pg_get_expr(indexprs.indpred, indexprs.indrelid) AS predicate
        FROM pg_indexes AS indexes
        JOIN pg_class AS relations ON relations.relname = indexes.tablename
        JOIN pg_index AS indexprs
          ON indexprs.indexrelid = to_regclass(indexes.indexname)
        WHERE indexes.schemaname = 'public'
          AND indexes.indexname = 'idx_job_completion_drain'
        """
    )
    assert drain is not None
    assert "run_after" in drain["indexdef"]
    assert "pending" in drain["predicate"]
    assert "finalizing" in drain["predicate"]

    effect_indexes = await conn.fetch(
        """
        SELECT indexname, indexdef
        FROM pg_indexes
        WHERE schemaname = 'public' AND tablename = 'completion_effects'
        """
    )
    assert [row["indexname"] for row in effect_indexes] == ["completion_effects_pkey"]
    assert all(" WHERE " not in row["indexdef"] for row in effect_indexes)


@pytest.mark.asyncio
async def test_sweep_view_routes_every_unfinished_command_state(conn):
    no_command_job = await _job(conn)
    live_job = await _job(conn)
    live_capped_job = await _job(conn)
    expired_job = await _job(conn)
    pending_job = await _job(conn)
    deadline_job = await _job(conn)
    capped_job = await _job(conn)
    parked_job = await _job(conn)
    done_job = await _job(conn)
    await _insert_command(
        conn,
        live_job,
        state="finalizing",
        attempts=1,
        lease_expires_at="2100-01-01T00:00:00Z",
    )
    await _insert_command(
        conn,
        live_capped_job,
        state="finalizing",
        attempts=5,
        max_attempts=5,
        lease_expires_at="2100-01-01T00:00:00Z",
    )
    await _insert_command(
        conn,
        expired_job,
        state="finalizing",
        attempts=1,
        lease_expires_at="2000-01-01T00:00:00Z",
    )
    await _insert_command(conn, pending_job)
    await _insert_command(
        conn,
        deadline_job,
        state="finalizing",
        attempts=1,
        deadline_at="2000-01-01T00:00:00Z",
        lease_expires_at="2000-01-01T00:00:00Z",
    )
    await _insert_command(
        conn,
        capped_job,
        state="finalizing",
        attempts=5,
        max_attempts=5,
        lease_expires_at="2000-01-01T00:00:00Z",
    )
    await _insert_command(
        conn,
        parked_job,
        state="parked",
        error_code="operator_required",
        deadline_at="2000-01-01T00:00:00Z",
        lease_expires_at="2100-01-01T00:00:00Z",
    )
    await _insert_command(
        conn,
        done_job,
        state="done",
        outcome={"status": "completed"},
        finalized_at="2026-08-12T00:00:00Z",
        lease_expires_at="2100-01-01T00:00:00Z",
    )
    routes = {
        row["job_id"]: row["route"]
        for row in await conn.fetch(
            "SELECT job_id, route FROM job_completion_sweep_exclusions"
        )
    }
    assert routes == {
        live_job: "stand_down",
        live_capped_job: "stand_down",
        expired_job: "resume_finalizer",
        pending_job: "resume_finalizer",
        deadline_job: "park_alert",
        capped_job: "park_alert",
        parked_job: "alert_only",
    }
    assert no_command_job not in routes
    assert done_job not in routes


@pytest.mark.asyncio
async def test_sweep_view_chooses_oldest_unfinished_report_sequence(conn):
    ordered_job = await _job(conn)
    first_id = await _insert_command(
        conn,
        ordered_job,
        report_seq=1,
        lease_expires_at="2000-01-01T00:00:00Z",
    )
    await _insert_command(
        conn,
        ordered_job,
        report_seq=2,
        state="parked",
        error_code="later_operator_hold",
    )

    after_terminal_job = await _job(conn)
    await _insert_command(
        conn,
        after_terminal_job,
        report_seq=1,
        state="done",
        outcome={"status": "completed"},
        finalized_at="2026-08-12T00:00:00Z",
    )
    second_id = await _insert_command(
        conn,
        after_terminal_job,
        report_seq=2,
        lease_expires_at="2100-01-01T00:00:00Z",
    )

    first_route = await conn.fetchrow(
        "SELECT command_id, report_seq, route "
        "FROM job_completion_sweep_exclusions WHERE job_id=$1",
        ordered_job,
    )
    assert dict(first_route) == {
        "command_id": first_id,
        "report_seq": 1,
        "route": "resume_finalizer",
    }

    second_route = await conn.fetchrow(
        "SELECT command_id, report_seq, route "
        "FROM job_completion_sweep_exclusions WHERE job_id=$1",
        after_terminal_job,
    )
    assert dict(second_route) == {
        "command_id": second_id,
        "report_seq": 2,
        "route": "resume_finalizer",
    }


@pytest.mark.asyncio
async def test_sweep_attempt_hwm_and_all_valid_action_shapes(conn):
    job_id = await _job(conn)
    assert (
        await conn.fetchval(
            "SELECT completion_sweep_attempt_hwm FROM jobs WHERE id=$1", job_id
        )
        == 0
    )

    command_ids = [
        await _insert_command(conn, job_id, report_seq=report_seq)
        for report_seq in (1, 2, 3)
    ]
    attempts = []
    for _ in command_ids:
        attempts.append(
            await conn.fetchval(
                "UPDATE jobs SET completion_sweep_attempt_hwm = "
                "completion_sweep_attempt_hwm + 1 WHERE id=$1 "
                "RETURNING completion_sweep_attempt_hwm",
                job_id,
            )
        )
    assert attempts == [1, 2, 3]

    await _insert_sweep_action(conn, job_id, command_ids[0], attempt=1)
    await _insert_sweep_action(
        conn,
        job_id,
        command_ids[1],
        attempt=2,
        command_attempt=1,
        route="park_alert",
        state="claimed",
        claimed_by="orchestrator-a",
        claimed_at="2026-08-13T00:00:00Z",
        claim_expires_at="2026-08-13T00:02:00Z",
    )
    await _insert_sweep_action(
        conn,
        job_id,
        command_ids[2],
        attempt=3,
        command_attempt=2,
        route="alert_only",
        state="done",
        claimed_at="2026-08-13T00:00:00Z",
        completed_at="2026-08-13T00:00:01Z",
        result={"alerted": True},
    )
    assert (
        await conn.fetchval(
            "SELECT count(*) FROM job_completion_sweep_actions WHERE job_id=$1",
            job_id,
        )
        == 3
    )


@pytest.mark.asyncio
async def test_sweep_action_deduplicates_and_route_can_change_while_pending(conn):
    job_id = await _job(conn)
    first_command = await _insert_command(conn, job_id, report_seq=1)
    second_command = await _insert_command(conn, job_id, report_seq=2)
    await _insert_sweep_action(conn, job_id, first_command)

    with pytest.raises(
        UniqueViolationError, match="uq_job_completion_sweep_command_attempt"
    ):
        await _insert_sweep_action(
            conn,
            job_id,
            first_command,
            attempt=2,
            route="park_alert",
            source="competing-rescuer",
        )
    with pytest.raises(UniqueViolationError, match="job_completion_sweep_actions_pkey"):
        await _insert_sweep_action(conn, job_id, second_command, attempt=1)

    assert (
        await conn.fetchval(
            "UPDATE job_completion_sweep_actions SET route='park_alert', "
            "updated_at=now() WHERE job_id=$1 AND attempt=1 RETURNING route",
            job_id,
        )
        == "park_alert"
    )
    assert (
        await conn.fetchval(
            "SELECT count(*) FROM job_completion_sweep_actions WHERE job_id=$1",
            job_id,
        )
        == 1
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"attempt": 0},
        {"command_attempt": -1},
        {"route": "stand_down"},
        {"source": "   "},
        {"state": "unknown"},
        {"claimed_by": "owner"},
        {"state": "claimed", "claimed_by": "owner"},
        {
            "state": "claimed",
            "claimed_by": "owner",
            "claimed_at": "2026-08-13T00:02:00Z",
            "claim_expires_at": "2026-08-13T00:01:00Z",
        },
        {
            "state": "done",
            "claimed_at": "2026-08-13T00:00:00Z",
            "completed_at": "2026-08-13T00:00:01Z",
        },
        {
            "state": "done",
            "claimed_at": "2026-08-13T00:00:00Z",
            "completed_at": "2026-08-13T00:00:01Z",
            "result": [],
        },
    ],
)
async def test_sweep_action_checks_reject_invalid_shapes(conn, overrides):
    job_id = await _job(conn)
    command_id = await _insert_command(conn, job_id)
    with pytest.raises(CheckViolationError):
        await _insert_sweep_action(conn, job_id, command_id, **overrides)


@pytest.mark.asyncio
async def test_sweep_action_command_delete_cascades(conn):
    job_id = await _job(conn)
    command_id = await _insert_command(conn, job_id)
    await _insert_sweep_action(conn, job_id, command_id)
    await conn.execute("DELETE FROM job_completion_commands WHERE id=$1", command_id)
    assert (
        await conn.fetchval(
            "SELECT count(*) FROM job_completion_sweep_actions WHERE job_id=$1",
            job_id,
        )
        == 0
    )


@pytest.mark.asyncio
async def test_finalizer_lease_row_enforces_term_shape(conn):
    elected_at = await conn.fetchval("SELECT now()")
    await conn.execute(
        """
        INSERT INTO completion_finalizer_leases (
            lease_name, leader_id, elected_at, expires_at
        ) VALUES (
            'job_completion', 'pod-a', $1::timestamptz,
            $1::timestamptz + interval '30 seconds'
        )
        """,
        elected_at,
    )
    assert (
        await conn.fetchval(
            """
        UPDATE completion_finalizer_leases
        SET expires_at = expires_at + interval '30 seconds'
        WHERE lease_name = 'job_completion'
          AND leader_id = 'pod-a'
          AND elected_at = $1
        RETURNING leader_id
        """,
            elected_at,
        )
        == "pod-a"
    )
    with pytest.raises(CheckViolationError, match="lease_expiry_order"):
        await conn.execute(
            """
            INSERT INTO completion_finalizer_leases (
                lease_name, leader_id, elected_at, expires_at
            ) VALUES ('invalid', 'pod-b', $1::timestamptz, $1::timestamptz)
            """,
            elected_at,
        )
