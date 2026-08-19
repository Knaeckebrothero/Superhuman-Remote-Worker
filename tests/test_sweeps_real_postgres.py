"""Execute every background-sweep query against a REAL Postgres server.

Regression guard for the 2026-07-11 incident
(knowledge-history/done/stale_agent_detector_sql_crash_disables_recovery_sweeps.md):
a bind-type bug (`($1 || ' minutes')::INTERVAL` typed $1 as text, caller
passed int) shipped because the unit tests mock ``conn.execute`` — asyncpg's
parameter typing is only exercised against a real server. Running each sweep
once on an EMPTY schema surfaces bind-type mismatches, SQL syntax errors, and
schema drift without any row seeding (prepare/bind happens regardless of
matching rows).

Opt-in: set ``SWEEP_TEST_PG_DSN`` to a DSN with CREATE DATABASE rights, e.g.

    SWEEP_TEST_PG_DSN=postgresql://postgres:postgres@localhost:5432/postgres

The test creates a throwaway database, applies
``orchestrator/database/schema_current.sql`` (the replayed-migrations
snapshot), runs the sweeps, and drops the database. Skipped when the env var
is unset so plain unit-test runs stay hermetic.
"""

import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

SWEEP_TEST_PG_DSN = os.getenv("SWEEP_TEST_PG_DSN", "").strip()

pytestmark = pytest.mark.skipif(
    not SWEEP_TEST_PG_DSN,
    reason="SWEEP_TEST_PG_DSN not set — real-Postgres sweep check is opt-in",
)

SCHEMA_FILE = (
    Path(__file__).resolve().parent.parent
    / "orchestrator"
    / "database"
    / "schema_current.sql"
)

# Every periodic sweep / dispatcher query whose SQL must prepare+bind cleanly.
# Add new sweeps here when they are added to a background loop.
SWEEPS = [
    (
        "mark_stale_agents_offline",
        lambda db: db.mark_stale_agents_offline(timeout_minutes=3),
    ),
    (
        "mark_stuck_working_agents_ready",
        lambda db: db.mark_stuck_working_agents_ready(),
    ),
    (
        "mark_stalled_working_agents_by_graph_progress",
        lambda db: db.mark_stalled_working_agents_by_graph_progress(stall_minutes=10),
    ),
    (
        "mark_stuck_session_agents_ready",
        lambda db: db.mark_stuck_session_agents_ready(),
    ),
    (
        "reap_orphaned_session_agents",
        lambda db: db.reap_orphaned_session_agents(grace_minutes=5),
    ),
    ("mark_orphaned_threads_ended", lambda db: db.mark_orphaned_threads_ended()),
    (
        "mark_orphaned_threads_suspended",
        lambda db: db.mark_orphaned_threads_suspended(),
    ),
    ("recover_orphaned_jobs", lambda db: db.recover_orphaned_jobs()),
    ("recover_expired_lease_jobs", lambda db: db.recover_expired_lease_jobs()),
    ("gc_offline_agents", lambda db: db.gc_offline_agents(retention_hours=24)),
    (
        "cancel_stale_verification_subjobs",
        lambda db: db.cancel_stale_verification_subjobs(stale_hours=6),
    ),
    (
        "list_stale_stateless_verification_subjobs",
        lambda db: db.list_stale_stateless_verification_subjobs(stale_hours=6),
    ),
    (
        "unstick_reviewing_parents",
        lambda db: db.unstick_reviewing_parents(grace_minutes=30),
    ),
    ("list_due_llm_outage_jobs", lambda db: db.list_due_llm_outage_jobs(limit=50)),
    ("list_active_ide_workspaces", lambda db: db.list_active_ide_workspaces()),
    (
        "get_available_agents",
        lambda db: db.get_available_agents(limit=5, cooldown_seconds=30),
    ),
    ("get_preemption_candidates", lambda db: db.get_preemption_candidates()),
    (
        "claim_job_for_agent",
        lambda db: db.claim_job_for_agent(str(uuid4()), str(uuid4())),
    ),
    (
        "claim_llm_outage_redispatch",
        lambda db: db.claim_llm_outage_redispatch(str(uuid4())),
    ),
    (
        "fail_llm_outage_job",
        lambda db: db.fail_llm_outage_job(str(uuid4()), "sweep bind test"),
    ),
]


@pytest.mark.asyncio
async def test_all_sweeps_prepare_and_bind_against_real_postgres():
    import asyncpg

    from orchestrator.database.postgres import PostgresDB

    test_db_name = f"srw_sweeps_{uuid4().hex[:12]}"

    admin = await asyncpg.connect(SWEEP_TEST_PG_DSN)
    try:
        await admin.execute(f'CREATE DATABASE "{test_db_name}"')
    finally:
        await admin.close()

    # Derive a DSN pointing at the throwaway database.
    base, _, _ = SWEEP_TEST_PG_DSN.rpartition("/")
    test_dsn = f"{base}/{test_db_name}"
    if "?" in SWEEP_TEST_PG_DSN.rsplit("/", 1)[-1]:
        query = SWEEP_TEST_PG_DSN.rsplit("?", 1)[-1]
        test_dsn = f"{test_dsn}?{query}"

    db = None
    failures: list[str] = []
    try:
        schema_conn = await asyncpg.connect(test_dsn)
        try:
            await schema_conn.execute(SCHEMA_FILE.read_text())
        finally:
            await schema_conn.close()

        db = PostgresDB(
            connection_string=test_dsn, min_connections=1, max_connections=2
        )
        await db.connect()

        for name, call in SWEEPS:
            try:
                await call(db)
            except Exception as e:  # collect all, report together
                failures.append(f"{name}: {type(e).__name__}: {e}")
    finally:
        if db is not None:
            await db.disconnect()
        admin = await asyncpg.connect(SWEEP_TEST_PG_DSN)
        try:
            await admin.execute(
                f'DROP DATABASE IF EXISTS "{test_db_name}" WITH (FORCE)'
            )
        finally:
            await admin.close()

    assert not failures, "sweep queries failed against real Postgres:\n" + "\n".join(
        failures
    )


@pytest.mark.asyncio
async def test_job_execution_lease_lifecycle_against_real_postgres():
    """Semantic check of the lease (knowledge-base/knowledge/features/job_execution_lease.md):
    claim sets a pickup lease; an expired lease recovers to paused/unassigned;
    NULL-lease (pre-deploy) rows are left to the legacy sweep."""
    import asyncpg

    from orchestrator.database.postgres import (
        LeaseRecoveryBatch,
        PostgresDB,
        RecoveredJob,
    )

    test_db_name = f"srw_lease_{uuid4().hex[:12]}"
    admin = await asyncpg.connect(SWEEP_TEST_PG_DSN)
    try:
        await admin.execute(f'CREATE DATABASE "{test_db_name}"')
    finally:
        await admin.close()

    base, _, _ = SWEEP_TEST_PG_DSN.rpartition("/")
    test_dsn = f"{base}/{test_db_name}"

    db = None
    try:
        schema_conn = await asyncpg.connect(test_dsn)
        try:
            await schema_conn.execute(SCHEMA_FILE.read_text())
        finally:
            await schema_conn.close()

        db = PostgresDB(
            connection_string=test_dsn, min_connections=1, max_connections=2
        )
        await db.connect()

        async with db.acquire() as conn:
            job_id = await conn.fetchval(
                "INSERT INTO jobs (description, status) "
                "VALUES ('lease test', 'created') RETURNING id"
            )
            agent_id = await conn.fetchval(
                "INSERT INTO agents (config_name, hostname, status) "
                "VALUES ('defaults', 'lease-test-agent', 'ready') RETURNING id"
            )
            stateless_job_id = await conn.fetchval(
                "INSERT INTO jobs (description, status, execution_lane, "
                "assigned_agent_id, lease_expires_at) "
                "VALUES ('stateless lease test', 'processing', 'stateless', $1, "
                "NOW() - interval '1 second') RETURNING id",
                agent_id,
            )

        # Direct resume/manual-assignment guards consume get_job(), so the lane
        # must survive that read model instead of silently defaulting to pinned.
        assert (await db.get_job(str(job_id)))["execution_lane"] == "pinned"
        assert (await db.get_job(str(stateless_job_id)))[
            "execution_lane"
        ] == "stateless"

        # Claim sets the pickup lease.
        assert await db.claim_job_for_agent(str(job_id), str(agent_id))
        async with db.acquire() as conn:
            lease = await conn.fetchval(
                "SELECT lease_expires_at FROM jobs WHERE id = $1", job_id
            )
        assert lease is not None

        # Fresh lease: the sweep must NOT touch it.
        assert await db.recover_expired_lease_jobs() == LeaseRecoveryBatch()

        # Expired lease: recovered to paused/unassigned.
        async with db.acquire() as conn:
            await conn.execute(
                "UPDATE jobs SET lease_expires_at = NOW() - interval '1 second' "
                "WHERE id = $1",
                job_id,
            )
        recovered = await db.recover_expired_lease_jobs()
        assert recovered == LeaseRecoveryBatch(
            recovered_jobs=(RecoveredJob(job_id=str(job_id), project_id=None),)
        )
        async with db.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT status, assigned_agent_id, lease_expires_at "
                "FROM jobs WHERE id = $1",
                job_id,
            )
        assert row["status"] == "paused"
        assert row["assigned_agent_id"] is None
        assert row["lease_expires_at"] is None

        # Gate 1: an expired jobs-row lease on the stateless lane is ignored;
        # only the run_queue reaper may recover this job.
        async with db.acquire() as conn:
            stateless_row = await conn.fetchrow(
                "SELECT status, assigned_agent_id, lease_expires_at "
                "FROM jobs WHERE id = $1",
                stateless_job_id,
            )
        assert stateless_row["status"] == "processing"
        assert stateless_row["assigned_agent_id"] == agent_id
        assert stateless_row["lease_expires_at"] is not None

        # NULL-lease processing row (pre-deploy shape): left alone.
        async with db.acquire() as conn:
            legacy_job_id = await conn.fetchval(
                "INSERT INTO jobs (description, status) "
                "VALUES ('legacy processing', 'processing') RETURNING id"
            )
            pinned_boundary_id = await conn.fetchval(
                "INSERT INTO jobs (description, status, freeze_data) "
                "VALUES ('pinned batch boundary', 'paused', $1::jsonb) "
                "RETURNING id",
                '{"freeze_type": "batch_boundary", "reason": "test"}',
            )
        assert await db.recover_expired_lease_jobs() == LeaseRecoveryBatch()

        # The belt-and-suspenders orphan sweep has four mutation arms. Seed
        # one stateless row for every arm and prove all four remain untouched.
        freeze = '{"freeze_type": "batch_boundary", "reason": "test"}'
        async with db.acquire() as conn:
            await conn.execute(
                "UPDATE agents SET status = 'offline' WHERE id = $1", agent_id
            )
            orphan_rows = await conn.fetch(
                """
                INSERT INTO jobs (
                    description, status, execution_lane, assigned_agent_id,
                    freeze_data
                ) VALUES
                    ('stateless processing orphan', 'processing', 'stateless', $1, NULL),
                    ('stateless waiting orphan', 'waiting', 'stateless', $1, NULL),
                    ('stateless paused orphan', 'paused', 'stateless', $1, NULL),
                    ('stateless frozen pause', 'paused', 'stateless', NULL, $2::jsonb)
                RETURNING id, description
                """,
                agent_id,
                freeze,
            )
        # The pinned legacy row is recovered and the pinned boundary freeze is
        # cleared through the shared SQL-array registry; none of the four
        # stateless rows may contribute to the count.
        orphan_batch = await db.recover_orphaned_jobs()
        assert orphan_batch.count == 2
        assert {job.job_id for job in orphan_batch.recovered_jobs} == {
            str(legacy_job_id),
            str(pinned_boundary_id),
        }
        async with db.acquire() as conn:
            assert (
                await conn.fetchval(
                    "SELECT status FROM jobs WHERE id = $1", legacy_job_id
                )
                == "paused"
            )
            pinned_boundary = await conn.fetchrow(
                "SELECT freeze_data, context FROM jobs WHERE id = $1",
                pinned_boundary_id,
            )
            rows = await conn.fetch(
                "SELECT description, status, assigned_agent_id, freeze_data "
                "FROM jobs WHERE id = ANY($1::uuid[]) ORDER BY description",
                [row["id"] for row in orphan_rows],
            )
        assert pinned_boundary["freeze_data"] is None
        pinned_context = pinned_boundary["context"]
        if isinstance(pinned_context, str):
            pinned_context = json.loads(pinned_context)
        assert pinned_context["last_freeze_data"]["freeze_type"] == "batch_boundary"
        by_description = {row["description"]: row for row in rows}
        assert by_description["stateless processing orphan"]["status"] == "processing"
        assert (
            by_description["stateless waiting orphan"]["assigned_agent_id"] == agent_id
        )
        assert (
            by_description["stateless paused orphan"]["assigned_agent_id"] == agent_id
        )
        assert by_description["stateless frozen pause"]["freeze_data"] is not None

        # Same-host registered-agent replacement has its own recovery UPDATE,
        # outside the two periodic sweep functions. It must pause only pinned
        # work; stateless work is unrelated to the registered-agent lifecycle.
        async with db.acquire() as conn:
            registration_rows = await conn.fetch(
                """
                INSERT INTO jobs (
                    description, status, execution_lane, assigned_agent_id
                ) VALUES
                    ('pinned registration recovery', 'processing', 'pinned', $1),
                    ('stateless registration recovery', 'processing', 'stateless', $1)
                RETURNING id, description
                """,
                agent_id,
            )
        await db.register_agent(
            config_name="defaults",
            pod_ip="10.42.0.200",
            hostname="lease-test-agent",
        )
        async with db.acquire() as conn:
            registration_after = await conn.fetch(
                "SELECT description, status, assigned_agent_id FROM jobs "
                "WHERE id = ANY($1::uuid[])",
                [row["id"] for row in registration_rows],
            )
        registration_by_name = {row["description"]: row for row in registration_after}
        assert (
            registration_by_name["pinned registration recovery"]["status"] == "paused"
        )
        assert (
            registration_by_name["stateless registration recovery"]["status"]
            == "processing"
        )
        assert (
            registration_by_name["stateless registration recovery"]["assigned_agent_id"]
            == agent_id
        )
    finally:
        if db is not None:
            await db.disconnect()
        admin = await asyncpg.connect(SWEEP_TEST_PG_DSN)
        try:
            await admin.execute(
                f'DROP DATABASE IF EXISTS "{test_db_name}" WITH (FORCE)'
            )
        finally:
            await admin.close()
