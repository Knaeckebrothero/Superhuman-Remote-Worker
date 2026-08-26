"""Real-PostgreSQL lineage gates for the shipped reconciliation CLI."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
from testcontainers.postgres import PostgresContainer

from orchestrator.database.postgres import PostgresDB
from orchestrator.operator_cli.managed_repository_reconciliation import (
    _safe_inventory_counts,
)
from orchestrator.security import crypto
from orchestrator.services.managed_repository_authority import _deploy_keypair

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
async def db(pg_dsn, _schema_applied, monkeypatch):
    monkeypatch.setenv("APP_ENCRYPTION_KEY", "L" * 32)
    crypto.reset_cipher_cache()
    store = PostgresDB(
        connection_string=pg_dsn,
        min_connections=1,
        max_connections=6,
    )
    await store.connect()
    async with store.acquire() as conn:
        await conn.execute(
            "TRUNCATE managed_repository_legacy_reconciliation_rearms, "
            "managed_repository_legacy_reconciliations, "
            "managed_repository_authorities, managed_repository_creation_intents, "
            "run_queue, project_repositories, jobs, threads, agents, "
            "project_members, projects, users RESTART IDENTITY CASCADE"
        )
    try:
        yield store
    finally:
        await store.close()
        crypto.reset_cipher_cache()


async def _activate_root_authority(
    db: PostgresDB,
    *,
    root_id,
    project_id,
    repo_name: str,
):
    private_key, public_key, fingerprint = _deploy_keypair()
    reserved = await db.reserve_managed_repository_authority(
        repository_owner="srw",
        repo_name=repo_name,
        authority_kind="job",
        authority_id=str(root_id),
        project_id=str(project_id),
        access_mode="write",
        creation_intent_id=None,
        clean_repo_url=f"http://gitea:3000/srw/{repo_name}.git",
        public_key=public_key,
        public_key_fingerprint=fingerprint,
        private_key=private_key,
    )
    activated = await db.activate_managed_repository_authority(
        str(reserved["id"]), forge_key_id=91, access_mode="write"
    )
    assert activated is not None
    return activated


@pytest.mark.asyncio
async def test_completed_root_key_remains_expected_until_child_absorbs(db):
    project_id = uuid4()
    root_id = uuid4()
    child_id = uuid4()
    repo_name = f"job-{str(root_id)[:8]}"
    async with db.acquire() as conn:
        await conn.execute(
            "INSERT INTO projects (id, name) VALUES ($1, 'lineage inventory')",
            project_id,
        )
        await conn.execute(
            "INSERT INTO jobs (id, description, project_id, status, "
            "execution_lane, repo_name) VALUES "
            "($1, 'completed root', $2, 'completed', 'pinned', $3), "
            "($4, 'resumable child', $2, 'failed', 'pinned', $3)",
            root_id,
            project_id,
            repo_name,
            child_id,
        )
        await conn.execute(
            "UPDATE jobs SET parent_job_id=$1 WHERE id=$2", root_id, child_id
        )
    authority = await _activate_root_authority(
        db,
        root_id=root_id,
        project_id=project_id,
        repo_name=repo_name,
    )

    resumable = await _safe_inventory_counts(db)

    assert resumable["managed_scopes_without_active_authority"] == 0
    assert resumable["job_lineage_anomalies"] == 0
    assert resumable["unexpected_active_authorities"] == 0

    async with db.acquire() as conn:
        await conn.execute("UPDATE jobs SET status='completed' WHERE id=$1", child_id)
    absorbed = await _safe_inventory_counts(db)

    assert absorbed["managed_scopes_without_active_authority"] == 0
    assert absorbed["job_lineage_anomalies"] == 0
    assert absorbed["unexpected_active_authorities"] == 1
    assert absorbed["unexpected_active_authority_ids"] == [str(authority["id"])]


@pytest.mark.asyncio
async def test_cross_project_child_is_anomaly_not_key_cleanup_authority(db):
    root_project = uuid4()
    child_project = uuid4()
    root_id = uuid4()
    child_id = uuid4()
    repo_name = f"job-{str(root_id)[:8]}"
    async with db.acquire() as conn:
        await conn.executemany(
            "INSERT INTO projects (id, name) VALUES ($1, $2)",
            [
                (root_project, "lineage root project"),
                (child_project, "lineage child project"),
            ],
        )
        await conn.execute(
            "INSERT INTO jobs (id, description, project_id, status, "
            "execution_lane, repo_name) VALUES "
            "($1, 'root', $2, 'created', 'pinned', $3), "
            "($4, 'cross-project child', $5, 'failed', 'pinned', $3)",
            root_id,
            root_project,
            repo_name,
            child_id,
            child_project,
        )
        await conn.execute(
            "UPDATE jobs SET parent_job_id=$1 WHERE id=$2", root_id, child_id
        )
    authority = await _activate_root_authority(
        db,
        root_id=root_id,
        project_id=root_project,
        repo_name=repo_name,
    )

    report = await _safe_inventory_counts(db)

    assert report["job_lineage_anomalies"] == 2
    assert set(report["job_lineage_anomaly_ids"]) == {
        str(root_id),
        str(child_id),
    }
    assert report["unexpected_active_authorities"] == 0
    assert str(authority["id"]) not in report["unexpected_active_authority_ids"]
