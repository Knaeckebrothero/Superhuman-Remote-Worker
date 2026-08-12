"""Real-Postgres contract tests for Gate-3 completion command schema (M1)."""

import json
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
from asyncpg import CheckViolationError, UniqueViolationError
from testcontainers.postgres import PostgresContainer


MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "orchestrator"
    / "database"
    / "migrations"
    / "app"
    / "0140_job_completion_commands.sql"
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
    await connection.execute("DROP TABLE IF EXISTS completion_effects CASCADE")
    await connection.execute("DROP TABLE IF EXISTS completion_finalizer_leases CASCADE")
    await connection.execute("DROP TABLE IF EXISTS job_completion_commands CASCADE")
    await connection.execute("DROP TABLE IF EXISTS jobs CASCADE")
    await connection.execute("CREATE TABLE jobs (id UUID PRIMARY KEY)")
    await connection.execute(MIGRATION.read_text())
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
            state, lease_expires_at, deadline_at, code_version, outcome,
            finalized_at, error_code
        ) VALUES (
            $1, $2, $3, $4::jsonb, $5, $6, $7, $8, $9,
            $10, $11::text::timestamptz, $12::text::timestamptz, $13,
            $14::jsonb, $15::text::timestamptz, $16
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
        values["lease_expires_at"],
        values["deadline_at"],
        values["code_version"],
        json.dumps(values["outcome"], sort_keys=True)
        if values["outcome"] is not None
        else None,
        values["finalized_at"],
        values["error_code"],
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
async def test_sweep_view_excludes_only_live_or_parked_commands(conn):
    live_job = await _job(conn)
    expired_job = await _job(conn)
    parked_job = await _job(conn)
    done_job = await _job(conn)
    await _insert_command(
        conn,
        live_job,
        lease_expires_at="2100-01-01T00:00:00Z",
    )
    await _insert_command(
        conn,
        expired_job,
        lease_expires_at="2000-01-01T00:00:00Z",
    )
    await _insert_command(
        conn,
        parked_job,
        state="parked",
        error_code="operator_required",
        lease_expires_at="2000-01-01T00:00:00Z",
    )
    await _insert_command(
        conn,
        done_job,
        state="done",
        outcome={"status": "completed"},
        finalized_at="2026-08-12T00:00:00Z",
        lease_expires_at="2100-01-01T00:00:00Z",
    )
    excluded = {
        row["job_id"]
        for row in await conn.fetch(
            "SELECT job_id FROM job_completion_sweep_exclusions ORDER BY job_id"
        )
    }
    assert excluded == {live_job, parked_job}


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
