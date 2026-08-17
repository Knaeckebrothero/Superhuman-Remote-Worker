"""Real-Postgres tests: outage-paused critics survive the staleness reap.

``cancel_stale_verification_subjobs``'s staleness arm cancels any agentless
``('created','paused')`` critic whose ``updated_at`` is older than 6h. An
outage-paused critic waiting out a >6h LLM cooldown (within the 12h pause
budget) has an untouched row — it was being cancelled mid-pause. The staleness
arm now exempts a critic that is ``paused`` with a live outage wake
(``context.llm_outage.next_retry_at`` newer than a grace horizon); an overdue
wake (outage sweeper broken) and the parent-terminal arm still reap.
knowledge-base/knowledge/features/llm_outage_subjob_resilience.md (#7).

Uses testcontainers (real Postgres) — the predicate's NULL semantics
(critics without llm_outage state) can't be trusted to mocks.
"""

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
import pytest_asyncio
from testcontainers.postgres import PostgresContainer

from orchestrator.database.postgres import PostgresDB

PARENT = "11111111-1111-1111-1111-111111111111"


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
            CREATE TABLE IF NOT EXISTS jobs (
                id uuid PRIMARY KEY,
                parent_job_id uuid,
                status text NOT NULL,
                execution_lane text NOT NULL DEFAULT 'pinned',
                assigned_agent_id uuid,
                context jsonb,
                created_at timestamptz DEFAULT now(),
                updated_at timestamptz DEFAULT now()
            )
            """
        )
        await conn.execute("TRUNCATE jobs")
    yield d
    await d.close()


async def _seed_parent(db, status="reviewing"):
    async with db.acquire() as conn:
        await conn.execute(
            "INSERT INTO jobs (id, status) VALUES ($1, $2)",
            PARENT,
            status,
        )


async def _seed_critic(
    db,
    *,
    status="paused",
    stale_hours_ago=7,
    wake_in_s=None,
    execution_lane="pinned",
):
    """Insert an agentless critic; wake_in_s sets llm_outage.next_retry_at
    relative to now (negative = already woke)."""
    ctx = {"verification_target": PARENT}
    if wake_in_s is not None:
        ctx["llm_outage"] = {
            "attempt": 2,
            "next_retry_at": (
                datetime.now(timezone.utc) + timedelta(seconds=wake_in_s)
            ).isoformat(),
        }
    critic_id = str(uuid4())
    async with db.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO jobs (
                id, parent_job_id, status, context, updated_at, execution_lane
            )
            VALUES ($1, $2, $3, $4::jsonb,
                    CURRENT_TIMESTAMP - make_interval(hours => $5::int), $6)
            """,
            critic_id,
            PARENT,
            status,
            json.dumps(ctx),
            stale_hours_ago,
            execution_lane,
        )
    return critic_id


async def _status(db, job_id):
    async with db.acquire() as conn:
        row = await conn.fetchrow("SELECT status FROM jobs WHERE id = $1", job_id)
    return row["status"]


@pytest.mark.asyncio
async def test_outage_paused_critic_with_future_wake_survives_staleness(db):
    # 7h-stale paused critic waiting out a cooldown that wakes in 2h — within
    # the 12h budget, must NOT be reaped mid-pause.
    await _seed_parent(db)
    critic = await _seed_critic(db, stale_hours_ago=7, wake_in_s=2 * 3600)
    assert await db.cancel_stale_verification_subjobs(stale_hours=6) == 0
    assert await _status(db, critic) == "paused"


@pytest.mark.asyncio
async def test_recently_woken_critic_within_grace_survives(db):
    # Wake 5min past (claim→dispatch pickup window) — still legitimate.
    await _seed_parent(db)
    critic = await _seed_critic(db, stale_hours_ago=7, wake_in_s=-300)
    assert await db.cancel_stale_verification_subjobs(stale_hours=6) == 0
    assert await _status(db, critic) == "paused"


@pytest.mark.asyncio
async def test_overdue_paused_critic_is_still_reaped(db):
    # Wake 2h past + still paused = the outage sweeper broke — reap, never
    # park forever.
    await _seed_parent(db)
    critic = await _seed_critic(db, stale_hours_ago=9, wake_in_s=-2 * 3600)
    assert await db.cancel_stale_verification_subjobs(stale_hours=6) == 1
    assert await _status(db, critic) == "cancelled"


@pytest.mark.asyncio
async def test_paused_critic_without_outage_state_is_still_reaped(db):
    # NULL-semantics guard: no llm_outage on context → the exemption must not
    # swallow the ordinary orphaned-critic reap.
    await _seed_parent(db)
    critic = await _seed_critic(db, stale_hours_ago=7, wake_in_s=None)
    assert await db.cancel_stale_verification_subjobs(stale_hours=6) == 1
    assert await _status(db, critic) == "cancelled"


@pytest.mark.asyncio
async def test_parent_terminal_reaps_even_a_legit_paused_critic(db):
    # The parent-terminal arm is untouched: a subjob under a dead parent can
    # never proceed, cooldown or not.
    await _seed_parent(db, status="failed")
    critic = await _seed_critic(db, stale_hours_ago=0, wake_in_s=2 * 3600)
    assert await db.cancel_stale_verification_subjobs(stale_hours=6) == 1
    assert await _status(db, critic) == "cancelled"


@pytest.mark.asyncio
async def test_created_stale_critic_unchanged(db):
    await _seed_parent(db)
    critic = await _seed_critic(db, status="created", stale_hours_ago=7)
    assert await db.cancel_stale_verification_subjobs(stale_hours=6) == 1
    assert await _status(db, critic) == "cancelled"


@pytest.mark.asyncio
async def test_stale_waiting_critic_is_reaped(db):
    # Task 10: 'waiting' critics are orphans of the retired inter-round
    # parking mechanism (knowledge-base/knowledge/issues/stale_critic_waiting_status_escapes_
    # reaper.md) — nothing legitimately parks a critic in 'waiting' between
    # rounds any more (a fresh critic is spawned every round instead), so an
    # agentless 'waiting' critic past the staleness horizon must be reaped
    # exactly like 'created', even while its parent is still alive.
    await _seed_parent(db)
    critic = await _seed_critic(db, status="waiting", stale_hours_ago=7)
    assert await db.cancel_stale_verification_subjobs(stale_hours=6) == 1
    assert await _status(db, critic) == "cancelled"


@pytest.mark.asyncio
async def test_fresh_waiting_critic_survives_staleness(db):
    # Mirror of test_created_stale_critic_unchanged's age gate: a 'waiting'
    # critic younger than the staleness horizon is left alone (parent-
    # terminal arm aside) — this is an age fallback, not "reap on sight".
    await _seed_parent(db)
    critic = await _seed_critic(db, status="waiting", stale_hours_ago=1)
    assert await db.cancel_stale_verification_subjobs(stale_hours=6) == 0
    assert await _status(db, critic) == "waiting"


@pytest.mark.asyncio
async def test_stateless_critic_is_selected_not_bulk_cancelled(db):
    await _seed_parent(db, status="failed")
    critic = await _seed_critic(
        db,
        status="paused",
        stale_hours_ago=0,
        execution_lane="stateless",
    )

    assert await db.cancel_stale_verification_subjobs(stale_hours=6) == 0
    assert await _status(db, critic) == "paused"
    assert await db.list_stale_stateless_verification_subjobs() == [critic]
