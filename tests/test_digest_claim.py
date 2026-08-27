"""Test the atomic quiet-hours digest claim (M1 HA — notification digest).

The quiet-hours digest loop is leader-gated, but during the transient
dual-leader window two loops can both read a user's pending notifications and
both send the digest email. Claiming the pending set atomically
(delivered_at NULL → NOW, RETURNING) must let exactly one loop win, so the
digest is never sent twice. Mirrors tests/test_job_claim.py.
"""

import asyncio
from uuid import UUID

import pytest
import pytest_asyncio
from testcontainers.postgres import PostgresContainer

from orchestrator.database.postgres import PostgresDB

USER = "33333333-3333-3333-3333-333333333333"


@pytest.fixture(scope="module")
def pg_dsn():
    with PostgresContainer("postgres:16") as pg:
        yield pg.get_connection_url().replace("postgresql+psycopg2", "postgresql")


@pytest_asyncio.fixture
async def db(pg_dsn):
    d = PostgresDB(connection_string=pg_dsn)
    await d.connect()
    async with d.acquire() as conn:
        # Minimal notification_queue (no FKs) mirroring 0001_initial.sql.
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS notification_queue (
                id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                user_id uuid NOT NULL,
                job_id uuid,
                thread_id varchar(12),
                subject text NOT NULL,
                message text NOT NULL,
                channels jsonb NOT NULL DEFAULT '{}',
                queued_at timestamptz DEFAULT now(),
                delivered_at timestamptz
            )
            """
        )
        await conn.execute("TRUNCATE notification_queue")
        for i in range(3):
            await conn.execute(
                "INSERT INTO notification_queue (user_id, subject, message) "
                "VALUES ($1, $2, $3)",
                UUID(USER),
                f"subject {i}",
                f"message {i}",
            )
    yield d
    await d.close()


async def _pending_count(db) -> int:
    async with db.acquire() as conn:
        return await conn.fetchval(
            "SELECT COUNT(*) FROM notification_queue "
            "WHERE user_id = $1 AND delivered_at IS NULL",
            UUID(USER),
        )


@pytest.mark.asyncio
async def test_digest_claim_is_disjoint_exactly_one_wins(db):
    c1, c2 = await asyncio.gather(
        db.claim_pending_notifications(USER),
        db.claim_pending_notifications(USER),
    )
    # The 3 pending rows are partitioned across the two callers with no overlap
    # (in practice one claims all three, the other none) — never double-sent.
    ids1 = {str(r["id"]) for r in c1}
    ids2 = {str(r["id"]) for r in c2}
    assert ids1.isdisjoint(ids2), "a notification must not be claimed by both loops"
    assert len(ids1) + len(ids2) == 3
    assert await _pending_count(db) == 0  # all claimed
    # A subsequent claim finds nothing.
    assert await db.claim_pending_notifications(USER) == []


@pytest.mark.asyncio
async def test_unmark_releases_claim_for_retry(db):
    claimed = await db.claim_pending_notifications(USER)
    assert len(claimed) == 3
    assert await _pending_count(db) == 0
    # Simulate dispatch failure → release the claim.
    n = await db.unmark_notifications_delivered([str(r["id"]) for r in claimed])
    assert n == 3
    assert await _pending_count(db) == 3  # eligible again
