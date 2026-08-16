"""Test the atomic permission-email 'sent' claim (M1 HA — notify sweeper).

thread_permission_notify_sweeper is leader-gated, but during the transient
dual-leader window two sweepers can both pass the racy NOT-EXISTS dedup and
both send the "approval needed" email. claim_sent_notification must make the
'sent' marker a single claimable slot per (request_id, kind) via the partial
unique index, so exactly one sweeper sends. Mirrors tests/test_job_claim.py.
"""

import asyncio
from uuid import UUID

import pytest
import pytest_asyncio
from testcontainers.postgres import PostgresContainer

from orchestrator.database.postgres import PostgresDB
from services.headless_notifications import (
    claim_sent_notification,
    downgrade_sent_claim,
)

THREAD = "44444444-4444-4444-4444-444444444444"
REQ = "55555555-5555-5555-5555-555555555555"
REQ2 = "66666666-6666-6666-6666-666666666666"


@pytest.fixture(scope="module")
def pg_dsn():
    with PostgresContainer("postgres:16") as pg:
        yield pg.get_connection_url().replace("postgresql+psycopg2", "postgresql")


@pytest_asyncio.fixture
async def db(pg_dsn):
    d = PostgresDB(connection_string=pg_dsn)
    await d.connect()
    async with d.acquire() as conn:
        # Minimal thread_notifications (no FKs) + the partial unique index from
        # migration 0038.
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS thread_notifications (
                id bigserial PRIMARY KEY,
                thread_id uuid NOT NULL,
                request_id uuid,
                kind text NOT NULL,
                sent_at timestamptz NOT NULL DEFAULT now(),
                delivery_status text,
                email_to text,
                metadata jsonb NOT NULL DEFAULT '{}'::jsonb
            )
            """
        )
        await conn.execute("TRUNCATE thread_notifications")
        await conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_tn_sent_request_kind "
            "ON thread_notifications (request_id, kind) "
            "WHERE delivery_status = 'sent'"
        )
    yield d
    await d.close()


async def _sent_count(db, request_id: str) -> int:
    async with db.acquire() as conn:
        return await conn.fetchval(
            "SELECT COUNT(*) FROM thread_notifications "
            "WHERE request_id = $1 AND delivery_status = 'sent'",
            UUID(request_id),
        )


@pytest.mark.asyncio
async def test_sent_claim_is_atomic_exactly_one_wins(db):
    r1, r2 = await asyncio.gather(
        claim_sent_notification(
            db, thread_id=THREAD, request_id=REQ, kind="permission_pending"
        ),
        claim_sent_notification(
            db, thread_id=THREAD, request_id=REQ, kind="permission_pending"
        ),
    )
    won = [r for r in (r1, r2) if r is not None]
    lost = [r for r in (r1, r2) if r is None]
    assert len(won) == 1 and len(lost) == 1, "two sweepers must not both send"
    assert await _sent_count(db, REQ) == 1


@pytest.mark.asyncio
async def test_sent_claim_rejects_already_claimed(db):
    assert (
        await claim_sent_notification(
            db, thread_id=THREAD, request_id=REQ, kind="permission_pending"
        )
        is not None
    )
    assert (
        await claim_sent_notification(
            db, thread_id=THREAD, request_id=REQ, kind="permission_pending"
        )
        is None
    )


@pytest.mark.asyncio
async def test_distinct_requests_each_claim(db):
    assert (
        await claim_sent_notification(
            db, thread_id=THREAD, request_id=REQ, kind="permission_pending"
        )
        is not None
    )
    assert (
        await claim_sent_notification(
            db, thread_id=THREAD, request_id=REQ2, kind="permission_pending"
        )
        is not None
    )


@pytest.mark.asyncio
async def test_downgrade_frees_the_slot(db):
    claim_id = await claim_sent_notification(
        db, thread_id=THREAD, request_id=REQ, kind="permission_pending"
    )
    assert claim_id is not None
    # Send failed → downgrade to 'failed', freeing the unique slot.
    await downgrade_sent_claim(db, claim_id)
    assert await _sent_count(db, REQ) == 0
    # The slot is reclaimable (no longer a 'sent' row blocking it).
    assert (
        await claim_sent_notification(
            db, thread_id=THREAD, request_id=REQ, kind="permission_pending"
        )
        is not None
    )
