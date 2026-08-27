"""Atomic sudo-request claim (HA / M2-L4 NATS replica-safety).

NATS sudo requests fan out to BOTH orchestrator replicas (no queue group), so
on_sudo_request runs twice. _insert_request claims the request on its unique
NATS reply subject (migration 0040) so exactly one replica inserts the row and
acts. Mirrors tests/test_notify_dedup.py.
"""

import asyncio

import pytest
import pytest_asyncio
from testcontainers.postgres import PostgresContainer

from database.postgres import PostgresDB
from services.sudo_gate import SudoGateService

JOB = "11111111-1111-1111-1111-111111111111"
REPLY = "_INBOX.aaaaaaaaaaaaaaaaaaaaaa"
REPLY2 = "_INBOX.bbbbbbbbbbbbbbbbbbbbbb"


@pytest.fixture(scope="module")
def pg_dsn():
    with PostgresContainer("postgres:16") as pg:
        yield pg.get_connection_url().replace("postgresql+psycopg2", "postgresql")


@pytest_asyncio.fixture
async def gate(pg_dsn):
    d = PostgresDB(connection_string=pg_dsn)
    await d.connect()
    async with d.acquire() as conn:
        # Minimal sudo_approval_requests (no FK to jobs; status as plain text to
        # avoid the sudo_request_status enum) + the partial unique index from
        # migration 0040 (DDL kept identical to the migration).
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sudo_approval_requests (
                id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                job_id             uuid,
                thread_id          uuid,
                vm_name            varchar(255) NOT NULL,
                command            text NOT NULL,
                arguments          text[] DEFAULT '{}',
                working_directory  text,
                requesting_user    varchar(255) NOT NULL,
                target_user        varchar(255) NOT NULL DEFAULT 'root',
                status             text NOT NULL DEFAULT 'pending',
                requested_at       timestamptz NOT NULL DEFAULT now(),
                expires_at         timestamptz NOT NULL DEFAULT (now() + interval '300 seconds'),
                nats_reply_subject text,
                metadata           jsonb DEFAULT '{}',
                CHECK (num_nonnulls(job_id, thread_id) = 1)
            )
            """
        )
        await conn.execute("TRUNCATE sudo_approval_requests")
        await conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_sudo_request_reply_subject "
            "ON sudo_approval_requests (nats_reply_subject) "
            "WHERE nats_reply_subject IS NOT NULL"
        )
    g = SudoGateService()
    g._db = d
    yield g
    await d.close()


async def _row_count(gate, reply: str) -> int:
    async with gate._db.acquire() as conn:
        return await conn.fetchval(
            "SELECT COUNT(*) FROM sudo_approval_requests WHERE nats_reply_subject = $1",
            reply,
        )


async def _claim(gate, reply, user="agent"):
    return await gate._insert_request(
        job_id=JOB,
        vm_name="vm1",
        command="rm",
        arguments=["-rf", "/tmp/x"],
        cwd="/tmp",
        requesting_user=user,
        target_user="root",
        nats_reply_subject=reply,
        metadata={"k": "v"},
    )


@pytest.mark.asyncio
async def test_claim_atomic_exactly_one_wins(gate):
    r1, r2 = await asyncio.gather(_claim(gate, REPLY), _claim(gate, REPLY))
    won = [r for r in (r1, r2) if r is not None]
    lost = [r for r in (r1, r2) if r is None]
    assert len(won) == 1 and len(lost) == 1, "two replicas must not both insert"
    assert await _row_count(gate, REPLY) == 1


@pytest.mark.asyncio
async def test_distinct_replies_each_claim(gate):
    assert await _claim(gate, REPLY) is not None
    assert await _claim(gate, REPLY2) is not None


@pytest.mark.asyncio
async def test_null_reply_unconstrained(gate):
    # vm_upgrade-style requests carry no reply subject — both must insert
    # (NULLs are distinct under the partial index).
    assert await _claim(gate, None) is not None
    assert await _claim(gate, None) is not None


@pytest.mark.asyncio
async def test_db_error_raises_not_none(gate):
    # A genuine DB failure (NOT NULL violation on requesting_user) must RAISE so
    # the caller denies rather than silently dropping. Pre-change this returned
    # None (swallowed); the new contract raises. This is the red test.
    with pytest.raises(Exception):
        await _claim(gate, "_INBOX.cccccccccccc", user=None)
