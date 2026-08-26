"""Real-PostgreSQL proof for exact stateless workspace process-zero receipts."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
from testcontainers.postgres import PostgresContainer

from orchestrator.database.postgres import PostgresDB


SCHEMA_FILE = (
    Path(__file__).resolve().parents[1]
    / "orchestrator"
    / "database"
    / "schema_current.sql"
)
PROCESS_ZERO_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "orchestrator"
    / "database"
    / "migrations"
    / "app"
    / "0187_managed_repository_process_zero_authority.sql"
)


@pytest.fixture(scope="module")
def pg_dsn():
    try:
        container = PostgresContainer("postgres:15")
        container.start()
    except Exception as exc:
        pytest.skip(f"local Postgres container unavailable: {exc}")
    try:
        yield container.get_connection_url().replace(
            "postgresql+psycopg2", "postgresql"
        )
    finally:
        container.stop()


@pytest_asyncio.fixture(scope="module")
async def _schema_applied(pg_dsn):
    conn = await asyncpg.connect(pg_dsn)
    try:
        await conn.execute(SCHEMA_FILE.read_text())
        if not await conn.fetchval(
            "SELECT to_regclass('public.managed_repository_process_zero_receipts') "
            "IS NOT NULL"
        ):
            await conn.execute(PROCESS_ZERO_MIGRATION.read_text())
    finally:
        await conn.close()


@pytest_asyncio.fixture
async def db(pg_dsn, _schema_applied):
    store = PostgresDB(
        connection_string=pg_dsn,
        min_connections=1,
        max_connections=4,
    )
    await store.connect()
    try:
        yield store
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_terminal_receipt_is_durable_idempotent_and_uid_fenced(db):
    thread_id = uuid4()
    first_uid = uuid4()
    second_uid = uuid4()
    metadata = {
        "workspace_container": {
            "provisioner": "k8s",
            "status": "ready",
            "_runtime_incarnation": str(first_uid),
        }
    }
    async with db.acquire() as conn:
        await conn.execute(
            "INSERT INTO threads (id, status, execution_lane, metadata) "
            "VALUES ($1, 'active', 'stateless', $2::jsonb)",
            thread_id,
            json.dumps(metadata),
        )

    assert await db.record_stateless_thread_workspace_process_zero(
        str(thread_id), runtime_incarnation=str(first_uid)
    )
    first_receipt = await db.get_stateless_thread_workspace_process_zero(
        str(thread_id), expected_runtime_incarnation=str(first_uid)
    )
    assert first_receipt == str(first_uid)

    async with db.acquire() as conn:
        observed_at = await conn.fetchval(
            "SELECT observed_at FROM managed_repository_process_zero_receipts "
            "WHERE owner_kind = 'thread' AND owner_id = $1 "
            "AND scope = 'stateless_workspace'",
            thread_id,
        )
    assert await db.record_stateless_thread_workspace_process_zero(
        str(thread_id), runtime_incarnation=str(first_uid)
    )
    async with db.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT observed_at "
                "FROM managed_repository_process_zero_receipts "
                "WHERE owner_kind = 'thread' AND owner_id = $1 "
                "AND scope = 'stateless_workspace' "
                "AND runtime_incarnation = $2",
                thread_id,
                str(first_uid),
            )
            == observed_at
        )
        await conn.execute(
            "UPDATE threads SET metadata = jsonb_set(metadata, "
            "'{workspace_container,_runtime_incarnation}', to_jsonb($2::text)) "
            "WHERE id = $1",
            thread_id,
            str(second_uid),
        )

    assert await db.get_stateless_thread_workspace_process_zero(str(thread_id)) is None
    assert not await db.record_stateless_thread_workspace_process_zero(
        str(thread_id), runtime_incarnation=str(first_uid)
    )
    assert await db.record_stateless_thread_workspace_process_zero(
        str(thread_id), runtime_incarnation=str(second_uid)
    )
    assert await db.get_stateless_thread_workspace_process_zero(
        str(thread_id), expected_runtime_incarnation=str(second_uid)
    ) == str(second_uid)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("owner_kind", "scope", "provisioner"),
    (("job", "workspace_container", "k8s"), ("thread", "vm", "vm")),
)
async def test_managed_repository_process_zero_uses_server_owned_exact_ledger(
    db,
    owner_kind,
    scope,
    provisioner,
):
    owner_id = uuid4()
    first_runtime = uuid4()
    second_runtime = uuid4()
    runtime_key = (
        "_runtime_incarnation"
        if scope == "workspace_container"
        else "provision_generation"
    )
    state = {scope: {runtime_key: str(first_runtime)}}
    if scope == "workspace_container":
        state[scope]["provisioner"] = "k8s"
    table = "jobs" if owner_kind == "job" else "threads"
    column = "context" if owner_kind == "job" else "metadata"
    async with db.acquire() as conn:
        if owner_kind == "job":
            await conn.execute(
                "INSERT INTO jobs (id, description, status, context) "
                "VALUES ($1, 'process-zero ledger', 'processing', $2::jsonb)",
                owner_id,
                json.dumps(state),
            )
        else:
            await conn.execute(
                "INSERT INTO threads (id, status, execution_lane, metadata) "
                "VALUES ($1, 'active', 'pinned', $2::jsonb)",
                owner_id,
                json.dumps(state),
            )

    assert await db.record_managed_repository_workspace_process_zero(
        str(owner_id),
        owner_kind=owner_kind,
        scope=scope,
        provisioner=provisioner,
        runtime_incarnation=str(first_runtime),
    )
    assert await db.managed_repository_workspace_process_zero_is_current(
        str(owner_id),
        owner_kind=owner_kind,
        scope=scope,
        provisioner=provisioner,
        runtime_incarnation=str(first_runtime),
    )
    async with db.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM managed_repository_process_zero_receipts "
                "WHERE owner_kind = $1 AND owner_id = $2 AND scope = $3",
                owner_kind,
                owner_id,
                scope,
            )
            == 1
        )
        await conn.execute(
            f"UPDATE {table} SET {column} = jsonb_set({column}, "
            f"'{{{scope},{runtime_key}}}', to_jsonb($2::text)) WHERE id = $1",
            owner_id,
            str(second_runtime),
        )

    assert not await db.managed_repository_workspace_process_zero_is_current(
        str(owner_id),
        owner_kind=owner_kind,
        scope=scope,
        provisioner=provisioner,
        runtime_incarnation=str(first_runtime),
    )
    assert not await db.record_managed_repository_workspace_process_zero(
        str(owner_id),
        owner_kind=owner_kind,
        scope=scope,
        provisioner=provisioner,
        runtime_incarnation=str(first_runtime),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("owner_kind", "table", "column"),
    (("job", "jobs", "context"), ("thread", "threads", "metadata")),
)
async def test_vm_retirement_claim_and_receipt_gate_terminal_transition(
    db,
    owner_kind,
    table,
    column,
):
    owner_id = uuid4()
    generation = uuid4()
    state = {
        "vm": {
            "status": "ready",
            "provision_generation": str(generation),
        }
    }
    async with db.acquire() as conn:
        if owner_kind == "job":
            await conn.execute(
                "INSERT INTO jobs (id, description, status, context) "
                "VALUES ($1, 'VM process-zero gate', 'completed', $2::jsonb)",
                owner_id,
                json.dumps(state),
            )
        else:
            await conn.execute(
                "INSERT INTO threads (id, status, execution_lane, metadata) "
                "VALUES ($1, 'ended', 'pinned', $2::jsonb)",
                owner_id,
                json.dumps(state),
            )
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                f"UPDATE {table} SET {column} = jsonb_set({column}, "
                "'{vm,status}', '\"deleted\"'::jsonb) WHERE id = $1",
                owner_id,
            )

    assert await db.claim_managed_repository_workspace_retirement(
        str(owner_id),
        owner_kind=owner_kind,
        scope="vm",
        provisioner="vm",
        runtime_incarnation=str(generation),
    )
    async with db.acquire() as conn:
        assert (
            await conn.fetchval(
                f"SELECT {column}->'vm'->>'status' FROM {table} WHERE id = $1",
                owner_id,
            )
            == "retiring_process_zero"
        )

    assert await db.record_managed_repository_workspace_process_zero(
        str(owner_id),
        owner_kind=owner_kind,
        scope="vm",
        provisioner="vm",
        runtime_incarnation=str(generation),
    )
    async with db.acquire() as conn:
        await conn.execute(
            f"UPDATE {table} SET {column} = jsonb_set({column}, "
            "'{vm,status}', '\"deleted\"'::jsonb) WHERE id = $1",
            owner_id,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("table", "column", "insert_sql"),
    (
        (
            "jobs",
            "context",
            "INSERT INTO jobs (id, description, status, context) "
            "VALUES ($1, 'old writer', 'processing', '{}'::jsonb)",
        ),
        (
            "threads",
            "metadata",
            "INSERT INTO threads (id, status, execution_lane, metadata) "
            "VALUES ($1, 'active', 'pinned', '{}'::jsonb)",
        ),
    ),
)
async def test_old_replica_cannot_forge_process_zero_in_json(
    db,
    table,
    column,
    insert_sql,
):
    owner_id = uuid4()
    async with db.acquire() as conn:
        await conn.execute(insert_sql, owner_id)
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                f"UPDATE {table} SET {column} = jsonb_set({column}, "
                "'{_managed_repository_process_zero}', "
                '\'{"workspace_container": {"provisioner": "k8s"}}\'::jsonb) '
                "WHERE id = $1",
                owner_id,
            )


@pytest.mark.asyncio
async def test_legacy_stateless_json_receipt_is_ignored_and_can_remain_unchanged(db):
    thread_id = uuid4()
    runtime_uid = uuid4()
    metadata = {
        "workspace_container": {
            "provisioner": "k8s",
            "status": "ready",
            "_runtime_incarnation": str(runtime_uid),
        },
        "_stateless_workspace_process_zero_observation": {
            "runtime_incarnation": str(runtime_uid),
            "observed_at": "2026-08-25T00:00:00+00:00",
        },
    }
    async with db.acquire() as conn:
        await conn.execute(
            "ALTER TABLE threads DISABLE TRIGGER "
            "trg_threads_reject_managed_repository_process_zero_json"
        )
        try:
            await conn.execute(
                "INSERT INTO threads (id, status, execution_lane, metadata) "
                "VALUES ($1, 'active', 'stateless', $2::jsonb)",
                thread_id,
                json.dumps(metadata),
            )
        finally:
            await conn.execute(
                "ALTER TABLE threads ENABLE TRIGGER "
                "trg_threads_reject_managed_repository_process_zero_json"
            )
        await conn.execute(
            "UPDATE threads SET metadata = jsonb_set(metadata, '{note}', '1') "
            "WHERE id = $1",
            thread_id,
        )
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "UPDATE threads SET metadata = jsonb_set(metadata, "
                "'{_stateless_workspace_process_zero_observation,observed_at}', "
                "'\"2026-08-26T00:00:00+00:00\"'::jsonb) WHERE id = $1",
                thread_id,
            )
    assert (
        await db.get_stateless_thread_workspace_process_zero(
            str(thread_id), expected_runtime_incarnation=str(runtime_uid)
        )
        is None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("owner_kind", ["job", "thread"])
@pytest.mark.parametrize("final_status", ["released", "quarantined"])
async def test_docker_process_zero_requires_exact_claim_before_reuse(
    db, owner_kind, final_status
):
    owner_id = uuid4()
    lease_id = uuid4()
    workspace = {
        "provisioner": "docker",
        "status": "ready",
        "host": f"workspace-docker-proof-{owner_kind}-{final_status}",
        "port": 30022,
        "_docker_workspace_lease_id": str(lease_id),
        "_docker_workspace_trust_mode": "attested",
        "_docker_workspace_attested": True,
    }
    async with db.acquire() as conn:
        if owner_kind == "job":
            await conn.execute(
                "INSERT INTO jobs (id, description, status, context) "
                "VALUES ($1, 'docker receipt', 'processing', $2::jsonb)",
                owner_id,
                json.dumps({"workspace_container": workspace}),
            )
        else:
            await conn.execute(
                "INSERT INTO threads (id, status, execution_lane, metadata) "
                "VALUES ($1, 'active', 'pinned', $2::jsonb)",
                owner_id,
                json.dumps({"workspace_container": workspace}),
            )
        await conn.execute(
            "INSERT INTO docker_workspace_leases ("
            "host, port, status, lease_id, owner_kind, owner_id, trust_mode, "
            "host_key_fingerprint) VALUES ($1, $2, 'ready', $3, $4, $5, "
            "'attested', 'SHA256:docker-proof')",
            workspace["host"],
            workspace["port"],
            lease_id,
            owner_kind,
            owner_id,
        )

    claimed = await db.transition_docker_workspace_lease(
        owner_kind=owner_kind,
        owner_id=str(owner_id),
        expected_lease_id=str(lease_id),
        expected_statuses={"ready"},
        updates={
            "status": "releasing",
            "quarantine_reason": "managed_repository_agent_retirement_claimed",
        },
    )
    assert claimed is not None
    assert not await db.record_docker_workspace_process_zero(
        str(uuid4()), owner_kind=owner_kind, lease_id=str(lease_id)
    )
    assert not await db.record_docker_workspace_process_zero(
        str(owner_id), owner_kind=owner_kind, lease_id=str(uuid4())
    )

    final_updates = {
        "status": final_status,
        **(
            {
                "_docker_workspace_trust_mode": "attested",
                "_docker_workspace_attested": True,
            }
            if final_status == "released"
            else {"quarantine_reason": ("container_recreation_required_process_zero")}
        ),
    }
    with pytest.raises(ValueError, match="prior exact process-zero"):
        await db.transition_docker_workspace_lease(
            owner_kind=owner_kind,
            owner_id=str(owner_id),
            expected_lease_id=str(lease_id),
            expected_statuses={"releasing"},
            updates=final_updates,
        )
    async with db.acquire() as conn:
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "UPDATE docker_workspace_leases SET status = $2, "
                "quarantine_reason = $3 "
                "WHERE lease_id = $1",
                lease_id,
                final_status,
                (
                    "container_recreation_required_process_zero"
                    if final_status == "quarantined"
                    else None
                ),
            )
        table = "jobs" if owner_kind == "job" else "threads"
        column = "context" if owner_kind == "job" else "metadata"
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                f"UPDATE {table} SET {column} = jsonb_set({column}, "
                "'{workspace_container,status}', to_jsonb($2::text)) "
                "WHERE id = $1",
                owner_id,
                "released",
            )

    assert await db.record_docker_workspace_process_zero(
        str(owner_id), owner_kind=owner_kind, lease_id=str(lease_id)
    )
    assert await db.record_docker_workspace_process_zero(
        str(owner_id), owner_kind=owner_kind, lease_id=str(lease_id)
    )
    settled = await db.transition_docker_workspace_lease(
        owner_kind=owner_kind,
        owner_id=str(owner_id),
        expected_lease_id=str(lease_id),
        expected_statuses={"releasing"},
        updates=final_updates,
    )
    assert settled is not None and settled["status"] == final_status
    assert await db.docker_workspace_process_zero_is_current(
        str(owner_id), owner_kind=owner_kind, lease_id=str(lease_id)
    )


@pytest.mark.asyncio
async def test_docker_process_zero_receipt_rejects_stale_owner_mirror(db):
    job_id = uuid4()
    lease_id = uuid4()
    workspace = {
        "provisioner": "docker",
        "status": "releasing",
        "quarantine_reason": "managed_repository_agent_retirement_claimed",
        "host": "workspace-docker-stale",
        "port": 30022,
        "_docker_workspace_lease_id": str(lease_id),
    }
    async with db.acquire() as conn:
        await conn.execute(
            "INSERT INTO jobs (id, description, status, context) "
            "VALUES ($1, 'stale docker receipt', 'processing', $2::jsonb)",
            job_id,
            json.dumps({"workspace_container": workspace}),
        )
        await conn.execute(
            "INSERT INTO docker_workspace_leases ("
            "host, port, status, lease_id, owner_kind, owner_id, trust_mode, "
            "quarantine_reason) VALUES ($1, $2, 'releasing', $3, 'job', $4, "
            "'trusted_dev', 'managed_repository_agent_retirement_claimed')",
            workspace["host"],
            workspace["port"],
            lease_id,
            job_id,
        )
        await conn.execute(
            "UPDATE jobs SET context = jsonb_set(context, "
            "'{workspace_container,host}', '\"stale-owner-host\"'::jsonb) "
            "WHERE id = $1",
            job_id,
        )

    assert not await db.record_docker_workspace_process_zero(
        str(job_id), owner_kind="job", lease_id=str(lease_id)
    )
    async with db.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM managed_repository_process_zero_receipts "
                "WHERE owner_kind = 'job' AND owner_id = $1 "
                "AND scope = 'docker_workspace'",
                job_id,
            )
            == 0
        )


@pytest.mark.asyncio
async def test_pre_0175_inherited_child_is_exempt_only_for_exact_parent_runtime(db):
    parent_id = uuid4()
    child_id = uuid4()
    forged_child_id = uuid4()
    legacy_workspace = {
        "provisioner": "k8s",
        "status": "ready",
        "host": "workspace-parent.internal",
        "port": 30022,
    }
    async with db.acquire() as conn:
        await conn.execute(
            "INSERT INTO jobs (id, description, status, context) "
            "VALUES ($1, 'legacy parent', 'processing', $2::jsonb)",
            parent_id,
            json.dumps({"workspace_container": legacy_workspace}),
        )
        await conn.execute(
            "INSERT INTO jobs (id, parent_job_id, description, status, context) "
            "VALUES ($1, $2, 'exact inherited child', 'completed', $3::jsonb)",
            child_id,
            parent_id,
            json.dumps(
                {
                    "inherits_parent_workspace": True,
                    "workspace_container": legacy_workspace,
                }
            ),
        )
        await conn.execute("DELETE FROM jobs WHERE id = $1", child_id)

        forged = {**legacy_workspace, "host": "workspace-foreign.internal"}
        await conn.execute(
            "INSERT INTO jobs (id, parent_job_id, description, status, context) "
            "VALUES ($1, $2, 'forged inherited child', 'completed', $3::jsonb)",
            forged_child_id,
            parent_id,
            json.dumps(
                {
                    "inherits_parent_workspace": True,
                    "workspace_container": forged,
                }
            ),
        )
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute("DELETE FROM jobs WHERE id = $1", forged_child_id)


@pytest.mark.asyncio
async def test_unknown_or_missing_runtime_authority_cannot_be_erased(db):
    job_id = uuid4()
    async with db.acquire() as conn:
        await conn.execute(
            "INSERT INTO jobs (id, description, status, context) "
            "VALUES ($1, 'unknown workspace authority', 'completed', $2::jsonb)",
            job_id,
            json.dumps(
                {
                    "workspace_container": {
                        "status": "deleted",
                        "host": "legacy-workspace.internal",
                    }
                }
            ),
        )
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute("DELETE FROM jobs WHERE id = $1", job_id)


@pytest.mark.asyncio
async def test_retirement_claim_and_receipt_are_absorbing_for_exact_runtime(db):
    job_id = uuid4()
    runtime_uid = uuid4()
    state = {
        "workspace_container": {
            "provisioner": "k8s",
            "status": "ready",
            "_runtime_incarnation": str(runtime_uid),
        }
    }
    async with db.acquire() as conn:
        await conn.execute(
            "INSERT INTO jobs (id, description, status, context) "
            "VALUES ($1, 'absorbing retirement', 'processing', $2::jsonb)",
            job_id,
            json.dumps(state),
        )

    assert await db.claim_managed_repository_workspace_retirement(
        str(job_id),
        owner_kind="job",
        scope="workspace_container",
        provisioner="k8s",
        runtime_incarnation=str(runtime_uid),
    )
    async with db.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT context->'workspace_container'->>'status' "
                "FROM jobs WHERE id = $1",
                job_id,
            )
            == "retiring_process_zero"
        )
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "UPDATE jobs SET context = jsonb_set(context, "
                "'{workspace_container,status}', '\"ready\"'::jsonb) "
                "WHERE id = $1",
                job_id,
            )

    assert await db.record_managed_repository_workspace_process_zero(
        str(job_id),
        owner_kind="job",
        scope="workspace_container",
        provisioner="k8s",
        runtime_incarnation=str(runtime_uid),
    )
    async with db.acquire() as conn:
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "UPDATE jobs SET context = jsonb_set(context, "
                "'{workspace_container,status}', '\"ready\"'::jsonb) "
                "WHERE id = $1",
                job_id,
            )


@pytest.mark.asyncio
async def test_local_ide_receipt_uses_exact_immutable_container_id(db):
    job_id = uuid4()
    container_id = "a" * 64
    replacement_id = "b" * 64
    async with db.acquire() as conn:
        await conn.execute(
            "INSERT INTO jobs (id, description, status, context) "
            "VALUES ($1, 'local IDE receipt', 'completed', $2::jsonb)",
            job_id,
            json.dumps(
                {
                    "ide_session": {
                        "status": "cleanup_pending",
                        "restore_type": "container",
                        "container_id": container_id,
                    }
                }
            ),
        )
    assert await db.claim_managed_repository_workspace_retirement(
        str(job_id),
        owner_kind="job",
        scope="ide_local",
        provisioner="docker",
        runtime_incarnation=container_id,
    )
    assert await db.record_managed_repository_workspace_process_zero(
        str(job_id),
        owner_kind="job",
        scope="ide_local",
        provisioner="docker",
        runtime_incarnation=container_id,
    )
    assert await db.managed_repository_workspace_process_zero_is_current(
        str(job_id),
        owner_kind="job",
        scope="ide_local",
        provisioner="docker",
        runtime_incarnation=container_id,
    )
    assert not await db.managed_repository_workspace_process_zero_is_current(
        str(job_id),
        owner_kind="job",
        scope="ide_local",
        provisioner="docker",
        runtime_incarnation=replacement_id,
    )
