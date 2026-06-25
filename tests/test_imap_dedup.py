"""Test the atomic inbound-email claim (M1 HA — imap dedup).

The imap poller is leader-gated, but leader election has no fencing: during a
partition / Postgres failover two replicas can briefly both poll IMAP. The
claim must be safe when two pollers race the same inbound Message-ID: exactly
one wins and routes the reply; the reply is never injected into a job twice.
Mirrors tests/test_job_claim.py — a real Postgres (testcontainers) with a
minimal processed_inbound_emails table matching migration 0037.
"""
import asyncio

import pytest
import pytest_asyncio
from testcontainers.postgres import PostgresContainer

from orchestrator.database.postgres import PostgresDB

MSG_ID = "<abc123@mail.example.com>"


@pytest.fixture(scope="module")
def pg_dsn():
    with PostgresContainer("postgres:16") as pg:
        yield pg.get_connection_url().replace("postgresql+psycopg2", "postgresql")


@pytest_asyncio.fixture
async def db(pg_dsn):
    d = PostgresDB(connection_string=pg_dsn)
    await d.connect()
    async with d.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS processed_inbound_emails (
                email_message_id text PRIMARY KEY,
                processed_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        await conn.execute("TRUNCATE processed_inbound_emails")
    yield d
    await d.close()


@pytest.mark.asyncio
async def test_claim_inbound_email_is_atomic_exactly_one_wins(db):
    r1, r2 = await asyncio.gather(
        db.claim_inbound_email(MSG_ID),
        db.claim_inbound_email(MSG_ID),
    )
    assert sorted([r1, r2]) == [False, True], (
        "two concurrent pollers must not both claim the same inbound email"
    )
    async with db.acquire() as conn:
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM processed_inbound_emails WHERE email_message_id = $1",
            MSG_ID,
        )
    assert count == 1


@pytest.mark.asyncio
async def test_claim_inbound_email_rejects_already_claimed(db):
    assert await db.claim_inbound_email(MSG_ID) is True
    # A second poll of the same email (now or a later cycle) loses the claim.
    assert await db.claim_inbound_email(MSG_ID) is False


@pytest.mark.asyncio
async def test_distinct_emails_each_claim(db):
    assert await db.claim_inbound_email("<one@x>") is True
    assert await db.claim_inbound_email("<two@x>") is True
