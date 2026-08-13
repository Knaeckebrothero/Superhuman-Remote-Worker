"""Real-Postgres semantics for the completion-aware expired-lease rescuer."""

import json
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
from testcontainers.postgres import PostgresContainer

from orchestrator.database.postgres import PostgresDB


SCHEMA_FILE = (
    Path(__file__).resolve().parents[1]
    / "orchestrator"
    / "database"
    / "schema_current.sql"
)


@pytest.mark.asyncio
async def test_expired_command_routes_away_from_legacy_lease_recovery():
    """An expired finalizer lease must never fall through to agent recovery."""
    with PostgresContainer("postgres:15") as pg:
        dsn = pg.get_connection_url().replace("postgresql+psycopg2", "postgresql")
        conn = await asyncpg.connect(dsn)
        db = None
        try:
            await conn.execute(SCHEMA_FILE.read_text())
            # pg_dump deliberately leaves its replay session with an empty
            # search_path; runtime pool connections start on ``public``.
            await conn.execute("SET search_path TO public")
            agent_id = await conn.fetchval(
                "INSERT INTO agents (config_name, hostname, status) "
                "VALUES ('defaults', 'completion-route-test', 'working') "
                "RETURNING id"
            )
            command_job_id, legacy_job_id = await conn.fetchrow(
                """
                WITH command_job AS (
                    INSERT INTO jobs (
                        description, status, execution_lane,
                        assigned_agent_id, lease_expires_at
                    ) VALUES (
                        'expired finalizer owns recovery', 'processing', 'pinned',
                        $1, now() - interval '1 minute'
                    ) RETURNING id
                ), legacy_job AS (
                    INSERT INTO jobs (
                        description, status, execution_lane,
                        assigned_agent_id, lease_expires_at
                    ) VALUES (
                        'legacy agent recovery', 'processing', 'pinned',
                        $1, now() - interval '1 minute'
                    ) RETURNING id
                )
                SELECT command_job.id, legacy_job.id
                FROM command_job CROSS JOIN legacy_job
                """,
                agent_id,
            )
            await conn.execute(
                """
                INSERT INTO job_completion_commands (
                    job_id, report_seq, client_report_id, payload,
                    payload_digest, accepted_lease_token, origin, requested_by,
                    state, attempts, max_attempts, run_after, lease_expires_at,
                    deadline_at, code_version
                ) VALUES (
                    $1, 1, $2, $3::jsonb, 'digest', 1, 'agent', 'test-agent',
                    'finalizing', 1, 5, now() - interval '1 minute',
                    now() - interval '1 second', now() + interval '1 hour',
                    'test'
                )
                """,
                command_job_id,
                uuid4(),
                json.dumps({"result": "ok"}),
            )

            db = PostgresDB(connection_string=dsn, min_connections=1, max_connections=2)
            await db.connect()

            assert await db.recover_expired_lease_jobs(
                completion_commands_enabled=True
            ) == [str(legacy_job_id)]
            routed = await conn.fetchrow(
                "SELECT status, assigned_agent_id, lease_expires_at "
                "FROM jobs WHERE id=$1",
                command_job_id,
            )
            assert routed["status"] == "processing"
            assert routed["assigned_agent_id"] == agent_id
            assert routed["lease_expires_at"] is not None

            # Flag-off remains the exact legacy path: the same row is now
            # recovered once command ownership is deliberately ignored.
            assert await db.recover_expired_lease_jobs(
                completion_commands_enabled=False
            ) == [str(command_job_id)]
            legacy = await conn.fetchrow(
                "SELECT status, assigned_agent_id, lease_expires_at "
                "FROM jobs WHERE id=$1",
                command_job_id,
            )
            assert legacy["status"] == "paused"
            assert legacy["assigned_agent_id"] is None
            assert legacy["lease_expires_at"] is None
        finally:
            if db is not None:
                await db.disconnect()
            await conn.close()
