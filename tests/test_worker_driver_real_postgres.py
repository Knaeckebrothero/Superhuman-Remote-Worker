"""Real-Postgres proofs for the stateless worker driver.

These tests exercise the worker composition and fenced LangGraph saver against
PostgreSQL's real lock manager.  Mocks cannot prove the queue/jobs atomic
claim, the saver ``FOR SHARE`` exclusion against a steal, or transaction-wide
rollback of stale checkpoint/blob/write mutations.

Gate: ``RUN_QUEUE_TEST_DSN`` must point at a disposable database whose name
contains ``test``.  The module drops and recreates its scratch tables.  It is
therefore skipped in normal unit-test runs and refuses the live application
database, following :mod:`tests.test_run_queue`'s safety contract.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.metadata
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import OperationalError
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from orchestrator.database.postgres import PostgresDB
from src.api.lease_context import LeaseHandle, LeaseLostError, current_lease
from src.core.fenced_checkpointer import FencedAsyncPostgresSaver
from src.shared.run_queue import (
    UNIT_KIND_WORKER_BATCH,
    reap_expired,
    release_unit,
    unpark_unit,
)
from src.shared.worker_queue import (
    claim_worker_batch,
    enqueue_worker_batch,
    hold_worker_batch_for_preflight,
    release_worker_batch,
    rotate_worker_batch,
)


DSN = os.environ.get("RUN_QUEUE_TEST_DSN", "")

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not DSN,
        reason="RUN_QUEUE_TEST_DSN not set (scratch Postgres required)",
    ),
]

_MIGRATIONS_DIR = (
    Path(__file__).resolve().parents[1]
    / "orchestrator"
    / "database"
    / "migrations"
    / "app"
)
_QUEUE_MIGRATIONS = (
    _MIGRATIONS_DIR / "0115a_run_queue.sql",
    _MIGRATIONS_DIR / "0117_run_queue_affinity.sql",
)
_JOB_MIGRATIONS = (
    _MIGRATIONS_DIR / "0054_jobs_execution_lease.sql",
    _MIGRATIONS_DIR / "0118_jobs_execution_lane.sql",
)

_CLAIM_RACE_ROUNDS = 32
_ROTATION_ROUNDS = 25
_EXACT_DEPENDENCIES = {
    "langgraph": "1.2.10",
    "langgraph-checkpoint": "4.1.1",
    "langgraph-checkpoint-postgres": "3.1.1",
    "psycopg-pool": "3.3.1",
}


def _assert_scratch_dsn() -> None:
    """Refuse any database that is not explicitly named as a test scratch."""

    database = urlsplit(DSN).path.rsplit("/", 1)[-1]
    if "test" not in database.lower():
        pytest.exit(
            f"RUN_QUEUE_TEST_DSN points at database {database!r}; refusing "
            "destructive setup because its name does not contain 'test'"
        )


async def _apply_schema() -> None:
    conn = await asyncpg.connect(DSN, timeout=10)
    try:
        await conn.execute("DROP TABLE IF EXISTS checkpoint_writes CASCADE")
        await conn.execute("DROP TABLE IF EXISTS checkpoint_blobs CASCADE")
        await conn.execute("DROP TABLE IF EXISTS checkpoints CASCADE")
        await conn.execute("DROP TABLE IF EXISTS checkpoint_migrations CASCADE")
        await conn.execute("DROP TABLE IF EXISTS retry_probe CASCADE")
        await conn.execute("DROP TABLE IF EXISTS run_queue CASCADE")
        await conn.execute("DROP TABLE IF EXISTS docker_workspace_leases CASCADE")
        await conn.execute("DROP TABLE IF EXISTS jobs CASCADE")
        await conn.execute("DROP TABLE IF EXISTS threads CASCADE")
        await conn.execute('CREATE EXTENSION IF NOT EXISTS "uuid-ossp"')
        await conn.execute("CREATE TABLE threads (id UUID PRIMARY KEY)")
        await conn.execute(
            """
            CREATE TABLE jobs (
                id UUID PRIMARY KEY,
                parent_job_id UUID,
                status TEXT NOT NULL DEFAULT 'created',
                priority INTEGER NOT NULL DEFAULT 0,
                user_id UUID,
                assigned_agent_id UUID,
                error_message TEXT,
                error_details JSONB,
                freeze_data JSONB,
                context JSONB NOT NULL DEFAULT '{}'::jsonb,
                config_override JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE docker_workspace_leases (
                owner_kind TEXT,
                owner_id UUID,
                status TEXT,
                quarantine_reason TEXT,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        for migration in (*_QUEUE_MIGRATIONS, *_JOB_MIGRATIONS):
            await conn.execute(migration.read_text())

        # These columns are added by the complete 0119/0127 migrations.  The
        # worker tests need their production queue shape, but not those
        # migrations' session-only inbox tables and foreign-key prerequisites.
        await conn.execute(
            """
            ALTER TABLE run_queue
                ADD COLUMN control_input_seq BIGINT NOT NULL DEFAULT 0,
                ADD COLUMN control_consumed_seq BIGINT NOT NULL DEFAULT 0,
                ADD COLUMN interrupt_admission_lease_token BIGINT,
                ADD COLUMN interrupt_admission_turn_id INTEGER
            """
        )
        await conn.execute(
            """
            CREATE TABLE retry_probe (
                unit_id UUID NOT NULL,
                attempt INTEGER NOT NULL,
                PRIMARY KEY (unit_id, attempt)
            )
            """
        )
    finally:
        await conn.close()

    # Apply the exact upstream checkpointer schema once.  Its setup path is
    # intentionally unfenced; only mutation hot paths require a worker lease.
    async with AsyncPostgresSaver.from_conn_string(DSN) as saver:
        await saver.setup()


@pytest.fixture(scope="session")
def worker_driver_pg_schema():
    _assert_scratch_dsn()
    actual = {
        package: importlib.metadata.version(package) for package in _EXACT_DEPENDENCIES
    }
    assert actual == _EXACT_DEPENDENCIES
    asyncio.run(_apply_schema())
    yield


@pytest_asyncio.fixture
async def pg(worker_driver_pg_schema):
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=8, timeout=10)
    async with pool.acquire() as conn:
        await conn.execute(
            """
            TRUNCATE checkpoint_writes, checkpoint_blobs, checkpoints,
                     retry_probe, run_queue, docker_workspace_leases, jobs
            RESTART IDENTITY CASCADE
            """
        )
    try:
        yield pool
    finally:
        await pool.close()


@pytest_asyncio.fixture
async def saver_pool(worker_driver_pg_schema):
    pool = AsyncConnectionPool(
        conninfo=DSN,
        min_size=1,
        max_size=4,
        kwargs={
            "autocommit": True,
            "prepare_threshold": 0,
            "row_factory": dict_row,
        },
        open=False,
        name="worker-driver-real-postgres-test",
    )
    await pool.open(wait=True)
    try:
        yield pool
    finally:
        await pool.close()


@asynccontextmanager
async def _lease(unit_id: UUID, lease_token: int):
    handle = LeaseHandle()
    handle.update(unit_id, lease_token)
    reset = current_lease.set(handle)
    try:
        yield handle
    finally:
        current_lease.reset(reset)


async def _insert_stateless_job(conn: asyncpg.Connection, job_id: UUID) -> None:
    await conn.execute(
        """
        INSERT INTO jobs (id, status, execution_lane)
        VALUES ($1, 'created', 'stateless')
        """,
        job_id,
    )
    await enqueue_worker_batch(conn, job_id=job_id, fair_key="test-tenant")


def _postgres_db_from_pool(pool) -> PostgresDB:
    db = PostgresDB.__new__(PostgresDB)
    db._connection_string = DSN
    db._command_timeout = 10

    @asynccontextmanager
    async def acquire():
        async with pool.acquire() as conn:
            yield conn

    db.acquire = acquire
    return db


async def _checkpoint_snapshot(conn: asyncpg.Connection) -> tuple[tuple, ...]:
    """Stable byte-for-byte snapshot of all three saver mutation tables."""

    rows: list[tuple] = []
    for table, query in (
        (
            "checkpoints",
            """
            SELECT thread_id, checkpoint_ns, checkpoint_id,
                   parent_checkpoint_id, type, checkpoint::text, metadata::text
            FROM checkpoints
            ORDER BY thread_id, checkpoint_ns, checkpoint_id
            """,
        ),
        (
            "checkpoint_blobs",
            """
            SELECT thread_id, checkpoint_ns, channel, version, type, blob
            FROM checkpoint_blobs
            ORDER BY thread_id, checkpoint_ns, channel, version
            """,
        ),
        (
            "checkpoint_writes",
            """
            SELECT thread_id, checkpoint_ns, checkpoint_id, task_id, idx,
                   channel, type, blob, task_path
            FROM checkpoint_writes
            ORDER BY thread_id, checkpoint_ns, checkpoint_id, task_id, idx
            """,
        ),
    ):
        for row in await conn.fetch(query):
            rows.append((table, *tuple(row)))
    return tuple(rows)


def _checkpoint(*, blob_value: str, version: int):
    checkpoint = empty_checkpoint()
    channel_version = f"{version:032}.0000000000000000"
    checkpoint["channel_values"] = {
        "blob": {"value": blob_value},
        "scalar": version,
    }
    checkpoint["channel_versions"] = {
        "blob": channel_version,
        "scalar": channel_version,
    }
    checkpoint["updated_channels"] = ["blob", "scalar"]
    return checkpoint, {
        "blob": channel_version,
        "scalar": channel_version,
    }


async def test_two_claimers_have_one_atomic_jobs_queue_winner(pg):
    """Thirty-two same-unit races produce one winner and one miss each."""

    winners = []
    for round_number in range(_CLAIM_RACE_ROUNDS):
        job_id = uuid4()
        async with pg.acquire() as conn:
            await _insert_stateless_job(conn, job_id)

        left, right = await asyncio.gather(
            claim_worker_batch(
                pg,
                pod_name=f"race-{round_number}-a",
                affinity_grace_seconds=0,
            ),
            claim_worker_batch(
                pg,
                pod_name=f"race-{round_number}-b",
                affinity_grace_seconds=0,
            ),
        )
        round_winners = [claim for claim in (left, right) if claim is not None]
        assert len(round_winners) == 1
        assert round_winners[0].unit_id == job_id
        assert round_winners[0].lease_token == 1
        winners.extend(round_winners)

    async with pg.acquire() as conn:
        queue_rows = await conn.fetch(
            """
            SELECT queue.unit_id, queue.state, queue.lease_token,
                   queue.attempts_since_completion, job.status,
                   job.execution_lane, job.assigned_agent_id
            FROM run_queue AS queue
            JOIN jobs AS job ON job.id = queue.unit_id
            ORDER BY queue.unit_id
            """
        )

    assert len(winners) == _CLAIM_RACE_ROUNDS
    assert len(queue_rows) == _CLAIM_RACE_ROUNDS
    assert all(row["state"] == "leased" for row in queue_rows)
    assert all(row["lease_token"] == 1 for row in queue_rows)
    assert all(row["attempts_since_completion"] == 1 for row in queue_rows)
    assert all(row["status"] == "processing" for row in queue_rows)
    assert all(row["execution_lane"] == "stateless" for row in queue_rows)
    assert all(row["assigned_agent_id"] is None for row in queue_rows)


async def test_twenty_five_rotations_stay_queued_and_reset_attempts(pg):
    job_id = uuid4()
    async with pg.acquire() as conn:
        await _insert_stateless_job(conn, job_id)

    input_seq = None
    for expected_token in range(1, _ROTATION_ROUNDS + 1):
        claim = await claim_worker_batch(
            pg,
            pod_name="rotation-pod",
            affinity_grace_seconds=0,
        )
        assert claim is not None
        assert claim.unit_id == job_id
        assert claim.lease_token == expected_token
        assert claim.unit.input_seq == input_seq

        rotation = await rotate_worker_batch(
            pg,
            unit_id=job_id,
            lease_token=claim.lease_token,
            input_seq=claim.unit.input_seq,
            fair_key="test-tenant",
        )
        assert rotation is not None
        assert rotation.state == "queued"
        assert rotation.enqueue.state == "leased"
        assert rotation.next_input_seq == expected_token
        input_seq = rotation.next_input_seq

        async with pg.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT state, lease_token, leased_by, leased_until, input_seq,
                       consumed_seq, attempts_since_completion
                FROM run_queue
                WHERE unit_id = $1
                """,
                job_id,
            )
        assert row["state"] == "queued"
        assert row["lease_token"] == expected_token
        assert row["leased_by"] is None
        assert row["leased_until"] is None
        assert row["input_seq"] == expected_token
        assert row["consumed_seq"] == expected_token - 1 or (
            expected_token == 1 and row["consumed_seq"] is None
        )
        assert row["attempts_since_completion"] == 0


async def test_stale_rotation_cannot_advance_successor_input(pg):
    job_id = uuid4()
    async with pg.acquire() as conn:
        await _insert_stateless_job(conn, job_id)
    first = await claim_worker_batch(
        pg,
        pod_name="rotation-pod-a",
        affinity_grace_seconds=0,
    )
    assert first is not None
    async with pg.acquire() as conn:
        assert (
            await release_unit(
                conn,
                unit_id=job_id,
                lease_token=first.lease_token,
            )
            == "queued"
        )
    successor = await claim_worker_batch(
        pg,
        pod_name="rotation-pod-b",
        affinity_grace_seconds=0,
    )
    assert successor is not None
    assert successor.lease_token == first.lease_token + 1

    async with pg.acquire() as conn:
        before = dict(
            await conn.fetchrow(
                "SELECT state, lease_token, leased_by, input_seq, consumed_seq "
                "FROM run_queue WHERE unit_id = $1",
                job_id,
            )
        )
    assert (
        await rotate_worker_batch(
            pg,
            unit_id=job_id,
            lease_token=first.lease_token,
            input_seq=first.unit.input_seq,
            fair_key="test-tenant",
        )
        is None
    )
    async with pg.acquire() as conn:
        after = dict(
            await conn.fetchrow(
                "SELECT state, lease_token, leased_by, input_seq, consumed_seq "
                "FROM run_queue WHERE unit_id = $1",
                job_id,
            )
        )
    assert after == before


async def test_workspace_preflight_revokes_live_holder_until_readmission(pg):
    job_id = uuid4()
    async with pg.acquire() as conn:
        await _insert_stateless_job(conn, job_id)
    holder = await claim_worker_batch(
        pg,
        pod_name="preflight-old-holder",
        affinity_grace_seconds=0,
    )
    assert holder is not None

    async with pg.acquire() as conn:
        await conn.execute(
            "UPDATE run_queue SET attempts_since_completion = max_attempts "
            "WHERE unit_id = $1",
            job_id,
        )
        async with conn.transaction():
            await hold_worker_batch_for_preflight(conn, job_id=job_id)
        closed = await conn.fetchrow(
            "SELECT state, lease_token, leased_by, leased_until, "
            "attempts_since_completion FROM run_queue WHERE unit_id = $1",
            job_id,
        )

    assert closed["state"] == "done"
    assert closed["lease_token"] == holder.lease_token + 1
    assert closed["leased_by"] is None
    assert closed["leased_until"] is None
    assert closed["attempts_since_completion"] == 0
    assert (
        await claim_worker_batch(
            pg,
            pod_name="preflight-too-early",
            affinity_grace_seconds=0,
        )
        is None
    )

    async with pg.acquire() as conn:
        await enqueue_worker_batch(conn, job_id=job_id, fair_key="test-tenant")
    successor = await claim_worker_batch(
        pg,
        pod_name="preflight-after-attestation",
        affinity_grace_seconds=0,
    )
    assert successor is not None
    assert successor.lease_token == holder.lease_token + 2
    assert successor.unit.attempts_since_completion == 1


async def test_recoverable_release_parks_but_terminal_report_retry_never_does(pg):
    job_id = uuid4()
    async with pg.acquire() as conn:
        await _insert_stateless_job(conn, job_id)

    for attempt in range(1, 6):
        claim = await claim_worker_batch(
            pg,
            pod_name="recoverable-pod",
            affinity_grace_seconds=0,
        )
        assert claim is not None
        assert claim.unit.attempts_since_completion == attempt
        async with pg.acquire() as conn:
            state = await release_worker_batch(
                conn,
                unit_id=job_id,
                lease_token=claim.lease_token,
                park_on_exhaustion=True,
                backoff_base_seconds=0,
            )
        assert state == ("parked" if attempt == 5 else "queued")

    async with pg.acquire() as conn:
        assert await unpark_unit(conn, unit_id=job_id)

    for attempt in range(1, 8):
        claim = await claim_worker_batch(
            pg,
            pod_name="report-retry-pod",
            affinity_grace_seconds=0,
        )
        assert claim is not None
        assert claim.unit.attempts_since_completion == attempt
        async with pg.acquire() as conn:
            state = await release_worker_batch(
                conn,
                unit_id=job_id,
                lease_token=claim.lease_token,
                park_on_exhaustion=False,
                backoff_base_seconds=0,
            )
        assert state == "queued"


async def test_fenced_saver_accepts_exact_token_and_stale_writes_mutate_nothing(
    pg,
    saver_pool,
):
    job_id = uuid4()
    async with pg.acquire() as conn:
        await _insert_stateless_job(conn, job_id)
    claim = await claim_worker_batch(
        pg,
        pod_name="checkpoint-pod-a",
        affinity_grace_seconds=0,
    )
    assert claim is not None

    saver = FencedAsyncPostgresSaver(
        saver_pool,
        unit_id=str(job_id),
        lease_token=claim.lease_token,
        retry_base_seconds=0,
    )
    config = {
        "configurable": {
            "thread_id": str(job_id),
            "checkpoint_ns": "",
        }
    }
    live_checkpoint, live_versions = _checkpoint(blob_value="live", version=1)
    async with _lease(job_id, claim.lease_token):
        next_config = await saver.aput(
            config,
            live_checkpoint,
            {"source": "loop", "step": 1, "parents": {}},
            live_versions,
        )
        await saver.aput_writes(
            next_config,
            [("worker_probe", {"accepted": True})],
            "live-task",
        )

    async with pg.acquire() as conn:
        before = await _checkpoint_snapshot(conn)
        await conn.execute(
            """
            UPDATE run_queue
            SET lease_token = lease_token + 1,
                leased_by = 'checkpoint-pod-b'
            WHERE unit_id = $1 AND state = 'leased'
            """,
            job_id,
        )

    stale_checkpoint, stale_versions = _checkpoint(blob_value="stale", version=2)
    async with _lease(job_id, claim.lease_token) as stale_handle:
        with pytest.raises(LeaseLostError):
            await saver.aput(
                next_config,
                stale_checkpoint,
                {"source": "loop", "step": 2, "parents": {}},
                stale_versions,
            )
        assert stale_handle.lost.is_set()

    # Use a fresh old-token handle so aput_writes reaches PostgreSQL's fence
    # independently instead of short-circuiting on the first lost Event.
    async with _lease(job_id, claim.lease_token) as stale_writes_handle:
        with pytest.raises(LeaseLostError):
            await saver.aput_writes(
                next_config,
                [("worker_probe", {"accepted": False})],
                "stale-task",
            )
        assert stale_writes_handle.lost.is_set()

    async with pg.acquire() as conn:
        after = await _checkpoint_snapshot(conn)
        counts = await conn.fetchrow(
            """
            SELECT (SELECT count(*) FROM checkpoints) AS checkpoints,
                   (SELECT count(*) FROM checkpoint_blobs) AS blobs,
                   (SELECT count(*) FROM checkpoint_writes) AS writes
            """
        )
    assert before == after
    assert tuple(counts) == (1, 1, 1)


async def test_permanent_delete_waits_for_saver_then_fences_and_strictly_prunes(
    pg,
    saver_pool,
    monkeypatch,
):
    monkeypatch.setenv("CHECKPOINTER_BACKEND", "postgres")
    job_id = uuid4()
    async with pg.acquire() as conn:
        await _insert_stateless_job(conn, job_id)
    claim = await claim_worker_batch(
        pg,
        pod_name="delete-fence-pod",
        affinity_grace_seconds=0,
    )
    assert claim is not None

    saver = FencedAsyncPostgresSaver(
        saver_pool,
        unit_id=str(job_id),
        lease_token=claim.lease_token,
        retry_base_seconds=0,
    )
    config = {"configurable": {"thread_id": str(job_id), "checkpoint_ns": ""}}
    checkpoint, versions = _checkpoint(blob_value="before-delete", version=1)
    async with _lease(job_id, claim.lease_token):
        next_config = await saver.aput(
            config,
            checkpoint,
            {"source": "loop", "step": 1, "parents": {}},
            versions,
        )

    db = _postgres_db_from_pool(pg)
    prepare_task = None
    async with _lease(job_id, claim.lease_token):
        async with saver._cursor(pipeline=True) as cursor:
            await cursor.execute("SELECT 42")
            prepare_task = asyncio.create_task(
                db.prepare_stateless_job_for_delete(str(job_id))
            )
            await asyncio.sleep(0.05)
            assert not prepare_task.done()

    assert prepare_task is not None
    assert await asyncio.wait_for(prepare_task, timeout=3)

    async with pg.acquire() as conn:
        queue = await conn.fetchrow(
            "SELECT state, lease_token, leased_by, leased_until "
            "FROM run_queue WHERE unit_id = $1",
            job_id,
        )
        job = await conn.fetchrow(
            "SELECT status, assigned_agent_id, context FROM jobs WHERE id = $1",
            job_id,
        )
        counts = await conn.fetchrow(
            """
            SELECT (SELECT count(*) FROM checkpoints WHERE thread_id = $1::text),
                   (SELECT count(*) FROM checkpoint_blobs WHERE thread_id = $1::text),
                   (SELECT count(*) FROM checkpoint_writes WHERE thread_id = $1::text)
            """,
            str(job_id),
        )

    assert queue["state"] == "done"
    assert queue["lease_token"] == claim.lease_token + 1
    assert queue["leased_by"] is None
    assert queue["leased_until"] is None
    assert job["status"] == "cancelled"
    assert job["assigned_agent_id"] is None
    job_context = job["context"]
    if isinstance(job_context, str):
        job_context = json.loads(job_context)
    assert job_context["_stateless_delete_pending"] is True
    assert tuple(counts) == (0, 0, 0)

    assert not await db.queue_stateless_job_for_resume(
        str(job_id),
        {"queued_feedback": "must not revive deleting work"},
        expected_status="cancelled",
    )
    async with pg.acquire() as conn:
        after_resume = await conn.fetchrow(
            "SELECT state, lease_token FROM run_queue WHERE unit_id = $1",
            job_id,
        )
    assert after_resume["state"] == "done"
    assert after_resume["lease_token"] == claim.lease_token + 1

    late_checkpoint, late_versions = _checkpoint(blob_value="late", version=2)
    async with _lease(job_id, claim.lease_token):
        with pytest.raises(LeaseLostError):
            await saver.aput(
                next_config,
                late_checkpoint,
                {"source": "loop", "step": 2, "parents": {}},
                late_versions,
            )

    assert await db.delete_job(str(job_id), prepared_stateless=True)
    async with pg.acquire() as conn:
        assert not await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM jobs WHERE id = $1)", job_id
        )
        assert not await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM run_queue WHERE unit_id = $1)", job_id
        )


async def test_stale_stateless_critic_hard_fences_and_prunes_before_parent_unstick(
    pg,
    saver_pool,
    monkeypatch,
):
    monkeypatch.setenv("CHECKPOINTER_BACKEND", "postgres")
    parent_id = uuid4()
    critic_id = uuid4()
    async with pg.acquire() as conn:
        await conn.execute(
            "INSERT INTO jobs (id, status, execution_lane) "
            "VALUES ($1, 'failed', 'pinned')",
            parent_id,
        )
        await conn.execute(
            """
            INSERT INTO jobs (
                id, parent_job_id, status, execution_lane, context
            )
            VALUES ($1, $2, 'paused', 'stateless', $3::jsonb)
            """,
            critic_id,
            parent_id,
            json.dumps({"verification_target": str(parent_id)}),
        )
        await enqueue_worker_batch(conn, job_id=critic_id, fair_key="critic-test")

    claim = await claim_worker_batch(
        pg,
        pod_name="stale-critic-pod",
        affinity_grace_seconds=0,
    )
    assert claim is not None
    saver = FencedAsyncPostgresSaver(
        saver_pool,
        unit_id=str(critic_id),
        lease_token=claim.lease_token,
        retry_base_seconds=0,
    )
    config = {"configurable": {"thread_id": str(critic_id), "checkpoint_ns": ""}}
    checkpoint, versions = _checkpoint(blob_value="critic-live", version=1)
    async with _lease(critic_id, claim.lease_token):
        await saver.aput(
            config,
            checkpoint,
            {"source": "loop", "step": 1, "parents": {}},
            versions,
        )

    db = _postgres_db_from_pool(pg)
    assert await db.cancel_and_settle_stale_stateless_verification_subjob(
        str(critic_id)
    )

    async with pg.acquire() as conn:
        queue = await conn.fetchrow(
            "SELECT state, lease_token, leased_by FROM run_queue WHERE unit_id = $1",
            critic_id,
        )
        critic = await conn.fetchrow(
            "SELECT status, context FROM jobs WHERE id = $1",
            critic_id,
        )
        counts = await conn.fetchrow(
            """
            SELECT (SELECT count(*) FROM checkpoints WHERE thread_id = $1::text),
                   (SELECT count(*) FROM checkpoint_blobs WHERE thread_id = $1::text),
                   (SELECT count(*) FROM checkpoint_writes WHERE thread_id = $1::text)
            """,
            str(critic_id),
        )

    assert queue["state"] == "done"
    assert queue["lease_token"] == claim.lease_token + 1
    assert queue["leased_by"] is None
    assert critic["status"] == "cancelled"
    critic_context = critic["context"]
    if isinstance(critic_context, str):
        critic_context = json.loads(critic_context)
    assert "_stateless_cancel_cleanup_pending" not in critic_context
    assert tuple(counts) == (0, 0, 0)


async def test_queue_steal_waits_for_saver_fence_transaction(pg, saver_pool):
    job_id = uuid4()
    async with pg.acquire() as conn:
        await _insert_stateless_job(conn, job_id)
    claim = await claim_worker_batch(
        pg,
        pod_name="fence-pod",
        affinity_grace_seconds=0,
    )
    assert claim is not None

    async with pg.acquire() as conn:
        await conn.execute(
            "UPDATE run_queue SET leased_until = now() - interval '1 minute' "
            "WHERE unit_id = $1",
            job_id,
        )

    saver = FencedAsyncPostgresSaver(
        saver_pool,
        unit_id=str(job_id),
        lease_token=claim.lease_token,
    )
    steal_conn = await asyncpg.connect(DSN, timeout=10)
    observer = await asyncpg.connect(DSN, timeout=10)
    steal_task = None
    try:
        async with _lease(job_id, claim.lease_token):
            async with saver._cursor(pipeline=True) as cursor:
                await cursor.execute("SELECT 42")
                steal_task = asyncio.create_task(
                    reap_expired(
                        steal_conn,
                        unit_kind=UNIT_KIND_WORKER_BATCH,
                        grace_seconds=0,
                        max_rows=1,
                        backoff_base_seconds=0,
                        jitter=0,
                    )
                )

                async def _steal_is_lock_waiting() -> bool:
                    while True:
                        wait_type = await observer.fetchval(
                            "SELECT wait_event_type FROM pg_stat_activity "
                            "WHERE pid = $1",
                            steal_conn.get_server_pid(),
                        )
                        if wait_type == "Lock":
                            return True
                        if steal_task.done():
                            return False
                        await asyncio.sleep(0.01)

                assert await asyncio.wait_for(_steal_is_lock_waiting(), timeout=2)
                assert not steal_task.done()

        stolen = await asyncio.wait_for(steal_task, timeout=2)
    finally:
        if steal_task is not None and not steal_task.done():
            steal_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await steal_task
        await observer.close()
        await steal_conn.close()

    assert len(stolen) == 1
    assert stolen[0].unit_id == job_id
    assert stolen[0].previous_lease_token == claim.lease_token
    assert stolen[0].lease_token == claim.lease_token + 1
    assert stolen[0].state == "queued"


async def test_transient_retry_refences_and_rejects_intervening_steal(
    pg,
    saver_pool,
):
    job_id = uuid4()
    async with pg.acquire() as conn:
        await _insert_stateless_job(conn, job_id)
    claim = await claim_worker_batch(
        pg,
        pod_name="retry-pod-a",
        affinity_grace_seconds=0,
    )
    assert claim is not None

    saver = FencedAsyncPostgresSaver(
        saver_pool,
        unit_id=str(job_id),
        lease_token=claim.lease_token,
        retry_attempts=2,
        retry_base_seconds=0,
    )
    attempts = 0

    async def _write_with_intervening_steal():
        nonlocal attempts
        attempts += 1
        try:
            async with saver._cursor(pipeline=True) as cursor:
                await cursor.execute(
                    "INSERT INTO retry_probe (unit_id, attempt) VALUES (%s, %s)",
                    (str(job_id), attempts),
                )
                if attempts == 1:
                    raise OperationalError("injected transient after fence")
        finally:
            if attempts == 1:
                async with pg.acquire() as conn:
                    await conn.execute(
                        """
                        UPDATE run_queue
                        SET lease_token = lease_token + 1,
                            leased_by = 'retry-pod-b'
                        WHERE unit_id = $1 AND state = 'leased'
                        """,
                        job_id,
                    )

    async with _lease(job_id, claim.lease_token) as handle:
        with pytest.raises(LeaseLostError):
            await saver._retry_write("injected-probe", _write_with_intervening_steal)
        assert handle.lost.is_set()

    async with pg.acquire() as conn:
        probe_count = await conn.fetchval(
            "SELECT count(*) FROM retry_probe WHERE unit_id = $1",
            job_id,
        )
    assert attempts == 2
    assert probe_count == 0
