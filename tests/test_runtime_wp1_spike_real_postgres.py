"""Executable R4/WP1 design checks, without a production engine or migration.

The toy store below is deliberately test-only. It exercises PostgreSQL rollback,
replay and lock behavior and runs the *current* worker claimant against a proposed
DB gate. It does not certify a future adapter, selector, effect protocol or k3d
rollout. See the WP1 contract note for those separate implementation gates.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
from testcontainers.postgres import PostgresContainer

from orchestrator.database.migrate import run_migrations
from shared.worker_queue import claim_worker_batch, enqueue_worker_batch

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.asyncio


@pytest.fixture(scope="module")
def pg_dsn():
    with PostgresContainer("postgres:16") as postgres:
        yield postgres.get_connection_url().replace("postgresql+psycopg2", "postgresql")


@pytest_asyncio.fixture(scope="module")
async def migrated_dsn(pg_dsn):
    async with asyncpg.create_pool(pg_dsn, min_size=1, max_size=2) as pool:
        await run_migrations(pool, ROOT / "src/orchestrator/database/migrations/app")
        async with pool.acquire() as conn:
            await conn.execute(
                (ROOT / "tests/fixtures/runtime_wp1_spike.sql").read_text()
            )
    return pg_dsn


@pytest_asyncio.fixture
async def pool(migrated_dsn):
    async with asyncpg.create_pool(migrated_dsn, min_size=1, max_size=3) as pool:
        async with pool.acquire() as conn:
            # The module's owned database has no external users or work.
            await conn.execute("TRUNCATE run_queue, jobs CASCADE")
        yield pool


async def _seed(conn, *, legacy=False):
    job_id = await conn.fetchval(
        "INSERT INTO jobs (description, execution_lane) "
        "VALUES ('WP1 contract spike', 'stateless') RETURNING id"
    )
    if not legacy:
        await conn.execute(
            "INSERT INTO wp1_spike.contracts VALUES ($1, 'react', 1)", job_id
        )
        await conn.execute("INSERT INTO wp1_spike.state (job_id) VALUES ($1)", job_id)
    await enqueue_worker_batch(conn, job_id=job_id, fair_key="wp1-test")
    return job_id


async def _claim(conn, job_id):
    """Prototype: lock queue -> job, mint receipt, then publish this lease."""
    async with conn.transaction():
        token = await conn.fetchval(
            "SELECT lease_token + 1 FROM run_queue WHERE unit_id=$1 FOR UPDATE",
            job_id,
        )
        await conn.fetchval("SELECT id FROM jobs WHERE id=$1 FOR UPDATE", job_id)
        await conn.execute(
            "INSERT INTO wp1_spike.claim_receipts "
            "VALUES ($1, $2, 'new-worker', txid_current())",
            job_id,
            token,
        )
        await conn.execute(
            "UPDATE run_queue SET state='leased', lease_token=$2, "
            "leased_by='new-worker', leased_until=now() + interval '60 seconds' "
            "WHERE unit_id=$1",
            job_id,
            token,
        )
        return token


class Refused(Exception):
    pass


async def _fence(conn, job_id, token):
    exact = await conn.fetchval(
        "SELECT 1 FROM run_queue WHERE unit_id=$1 AND unit_kind='worker_batch' "
        "AND state='leased' AND lease_token=$2 FOR SHARE",
        job_id,
        token,
    )
    if not exact:
        raise Refused("authority")


async def _commit(
    conn, job_id, token, operation_id, *, expected=0, text="accepted", fail=False
):
    """Small transaction spike; not an application persistence implementation."""
    request_hash = hashlib.sha256(
        json.dumps([expected, text], separators=(",", ":")).encode()
    ).hexdigest()
    async with conn.transaction():
        await _fence(conn, job_id, token)
        await conn.fetchval("SELECT id FROM jobs WHERE id=$1 FOR KEY SHARE", job_id)
        state = await conn.fetchrow(
            "SELECT * FROM wp1_spike.state WHERE job_id=$1 FOR UPDATE", job_id
        )
        prior = await conn.fetchrow(
            "SELECT * FROM wp1_spike.commits WHERE job_id=$1 AND operation_id=$2",
            job_id,
            operation_id,
        )
        if prior:
            if prior["request_hash"] != request_hash:
                raise Refused("operation payload changed")
            return prior["revision"]
        if state["revision"] != expected:
            raise Refused("revision")
        await conn.execute(
            "INSERT INTO wp1_spike.messages VALUES ($1, $2, $3, $4::jsonb)",
            job_id,
            str(operation_id),
            state["next_seq"],
            json.dumps({"role": "human", "content": text}),
        )
        if fail:
            raise Refused("after message before envelope")
        revision = await conn.fetchval(
            "UPDATE wp1_spike.state SET revision=revision+1, next_seq=next_seq+1, "
            "envelope=jsonb_build_object('absorbed_input', $2::text) "
            "WHERE job_id=$1 RETURNING revision",
            job_id,
            str(operation_id),
        )
        await conn.execute(
            "INSERT INTO wp1_spike.commits VALUES ($1, $2, $3, $4)",
            job_id,
            operation_id,
            request_hash,
            revision,
        )
        return revision


async def test_atomic_message_envelope_and_receipt_rollback(pool):
    async with pool.acquire() as conn:
        job_id = await _seed(conn)
        token = await _claim(conn, job_id)
        with pytest.raises(Refused, match="after message"):
            await _commit(conn, job_id, token, uuid4(), fail=True)
        assert await conn.fetchval("SELECT count(*) FROM wp1_spike.messages") == 0
        assert await conn.fetchval("SELECT count(*) FROM wp1_spike.commits") == 0
        state = await conn.fetchrow("SELECT * FROM wp1_spike.state")
        assert (state["revision"], state["next_seq"], state["envelope"]) == (0, 1, "{}")


async def test_committed_but_unacknowledged_replay_and_conflict(pool):
    async with pool.acquire() as conn:
        job_id = await _seed(conn)
        token = await _claim(conn, job_id)
        operation_id = uuid4()
        assert await _commit(conn, job_id, token, operation_id) == 1
        # Simulate a lost acknowledgement and a successor retrying the same ID.
        successor = await _claim(conn, job_id)
        assert await _commit(conn, job_id, successor, operation_id) == 1
        with pytest.raises(Refused, match="operation payload changed"):
            await _commit(conn, job_id, successor, operation_id, text="different")
        with pytest.raises(Refused, match="revision"):
            await _commit(conn, job_id, successor, uuid4())
        with pytest.raises(Refused, match="authority"):
            await _commit(conn, job_id, token, operation_id)
        assert await conn.fetchval("SELECT count(*) FROM wp1_spike.messages") == 1
        assert await conn.fetchval("SELECT count(*) FROM wp1_spike.commits") == 1
        assert await conn.fetchval("SELECT revision FROM wp1_spike.state") == 1


@pytest.mark.parametrize("completion_commands", [False, True])
async def test_current_unaware_claimant_is_fenced_but_legacy_job_still_runs(
    pool, completion_commands
):
    async with pool.acquire() as conn:
        job_id = await _seed(conn)
    with pytest.raises(asyncpg.CheckViolationError, match="fresh compatible claim"):
        await claim_worker_batch(
            pool, pod_name="old-worker", completion_commands_enabled=completion_commands
        )
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT state FROM run_queue") == "queued"
        assert await conn.fetchval("SELECT lease_token FROM run_queue") == 0
        assert await conn.fetchval("SELECT status::text FROM jobs") == "created"
        await conn.execute("DELETE FROM run_queue WHERE unit_id=$1", job_id)
        legacy_id = await _seed(conn, legacy=True)
    legacy_claim = await claim_worker_batch(
        pool, pod_name="old-worker", completion_commands_enabled=completion_commands
    )
    assert legacy_claim is not None
    assert legacy_claim.unit_id == legacy_id


async def test_contract_immutable_and_old_receipt_cannot_authorize_new_claim(pool):
    async with pool.acquire() as conn:
        job_id = await _seed(conn)
        with pytest.raises(asyncpg.CheckViolationError, match="immutable"):
            await conn.execute("UPDATE wp1_spike.contracts SET state_version=2")
        token = await _claim(conn, job_id)
        await conn.execute(
            "UPDATE run_queue SET leased_until=now() + interval '120 seconds'"
        )
        await conn.execute(
            "UPDATE run_queue SET state='queued', leased_by=NULL, leased_until=NULL"
        )
        with pytest.raises(asyncpg.CheckViolationError, match="fresh compatible claim"):
            await conn.execute(
                "UPDATE run_queue SET state='leased', leased_by='new-worker', "
                "leased_until=now() + interval '60 seconds'"
            )
        with pytest.raises(asyncpg.CheckViolationError, match="fresh compatible claim"):
            await claim_worker_batch(conn, pod_name="old-worker")
        assert await _claim(conn, job_id) == token + 1


async def test_claim_rotation_waits_for_fenced_write_transaction(pool):
    async with pool.acquire() as writer, pool.acquire() as claimant:
        job_id = await _seed(writer)
        token = await _claim(writer, job_id)
        claimant_pid = await claimant.fetchval("SELECT pg_backend_pid()")
        async with writer.transaction():
            await _fence(writer, job_id, token)
            task = asyncio.create_task(_claim(claimant, job_id))
            try:
                async with asyncio.timeout(5):
                    while not await writer.fetchval(
                        "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                        "WHERE pid=$1 AND wait_event_type='Lock')",
                        claimant_pid,
                    ):
                        await writer.execute("SELECT pg_stat_clear_snapshot()")
                        await asyncio.sleep(0.01)
                assert not task.done()
                assert await _commit(writer, job_id, token, uuid4()) == 1
            except BaseException:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise
        async with asyncio.timeout(5):
            assert await task == token + 1
        with pytest.raises(Refused, match="authority"):
            await _commit(writer, job_id, token, uuid4(), expected=1)
        assert await writer.fetchval("SELECT revision FROM wp1_spike.state") == 1
