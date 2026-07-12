"""Execute every background-sweep query against a REAL Postgres server.

Regression guard for the 2026-07-11 incident
(docs/issues/stale_agent_detector_sql_crash_disables_recovery_sweeps.md):
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
    ("gc_offline_agents", lambda db: db.gc_offline_agents(retention_hours=24)),
    (
        "cancel_stale_verification_subjobs",
        lambda db: db.cancel_stale_verification_subjobs(stale_hours=6),
    ),
    (
        "unstick_reviewing_parents",
        lambda db: db.unstick_reviewing_parents(grace_minutes=30),
    ),
    ("list_due_llm_outage_jobs", lambda db: db.list_due_llm_outage_jobs(limit=50)),
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
