"""The session-wake outbox claim, against a real Postgres (testcontainers).

The claim is the mechanism the whole feature rests on, and every property that
makes it correct is a *Postgres* property — Read Committed re-evaluation under
FOR UPDATE, SKIP LOCKED disjointness, IS DISTINCT FROM's NULL handling. Mocking
those would test the mock. So these run against a real server.

What each property buys, concretely:

  * **exactly one winner** — the send is not idempotent. The agent's /api/input
    mints a fresh message id per call and unconditionally enqueues, so a double
    delivery is a visible duplicate in the user's transcript plus a second paid
    LLM turn.
  * **re-claim past the visibility timeout** — this is why losing the
    opportunistic post-commit send is harmless, and therefore why the direct
    POST can be a latency optimization rather than the mechanism. Without it a
    SIGKILL mid-rollout silently loses the wake.
  * **the backstop arm** — there is no terminal-state choke point in this
    codebase; dispatch-time failures, the LLM-outage fail path and VM-upgrade
    expiry all go terminal with a direct DB write and no hook. The claim finds
    those by status, so a missed hook costs a tick, not a completion.
  * **per-status dedup** — pending_review → completed via approve is a second,
    legitimate wake, and it must not be suppressed by the first.

Design: docs/features/session_wake_on_job_completion.md.
"""

import uuid

import pytest
import pytest_asyncio
from testcontainers.postgres import PostgresContainer

from database.postgres import PostgresDB

_UID = uuid.UUID("bbbbbbbb-0000-0000-0000-000000000001")
_TID = uuid.UUID("bbbbbbbb-0000-0000-0000-000000000002")
_OTHER_TID = uuid.UUID("bbbbbbbb-0000-0000-0000-000000000003")


@pytest.fixture(scope="module")
def pg_dsn():
    with PostgresContainer("postgres:16") as pg:
        yield pg.get_connection_url().replace("postgresql+psycopg2", "postgresql")


@pytest_asyncio.fixture
async def db(pg_dsn):
    """A `jobs` table carrying exactly the columns the claim reads/writes.

    Mirrors migrations 0070/0071 rather than replaying the full migration chain
    — including the partial index, so the claim's literal-SQL predicate contract
    is exercised the way production plans it.
    """
    d = PostgresDB(connection_string=pg_dsn)
    await d.connect()
    async with d.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id uuid PRIMARY KEY,
                description text,
                config_name text DEFAULT 'worker_base',
                status text NOT NULL,
                user_id uuid,
                project_id uuid,
                expert_id uuid,
                freeze_data jsonb,
                error_message text,
                created_by_thread_id uuid,
                wake_on_complete boolean NOT NULL DEFAULT false,
                wake_state text NOT NULL DEFAULT 'none',
                wake_claimed_at timestamptz,
                wake_attempts integer NOT NULL DEFAULT 0,
                wake_notified_status text,
                updated_at timestamptz NOT NULL DEFAULT now(),
                CONSTRAINT jobs_wake_state_known CHECK (
                    wake_state IN ('none','pending','sending','sent','dead'))
            )
            """
        )
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_jobs_wake_pending
                ON jobs (updated_at)
                WHERE wake_on_complete
                  AND created_by_thread_id IS NOT NULL
                  AND wake_state IN ('none','pending','sending')
            """
        )
        # Production has a BEFORE UPDATE trigger maintaining updated_at; the
        # claim's ORDER BY depends on it, so reproduce it here.
        await conn.execute(
            """
            CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $$
            BEGIN NEW.updated_at = now(); RETURN NEW; END;
            $$ LANGUAGE plpgsql
            """
        )
        await conn.execute("DROP TRIGGER IF EXISTS jobs_updated_at ON jobs")
        await conn.execute(
            "CREATE TRIGGER jobs_updated_at BEFORE UPDATE ON jobs "
            "FOR EACH ROW EXECUTE FUNCTION set_updated_at()"
        )
        await conn.execute("TRUNCATE jobs")
    yield d
    await d.close()


async def _mk(db, *, status="completed", wake=True, thread=_TID, **cols) -> str:
    job_id = uuid.uuid4()
    async with db.acquire() as conn:
        await conn.execute(
            "INSERT INTO jobs (id, description, status, user_id, "
            "created_by_thread_id, wake_on_complete) VALUES ($1,'task',$2,$3,$4,$5)",
            job_id,
            status,
            _UID,
            thread,
            wake,
        )
        for col, val in cols.items():
            await conn.execute(f"UPDATE jobs SET {col} = $2 WHERE id = $1", job_id, val)
    return str(job_id)


async def _state(db, job_id) -> dict:
    async with db.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT wake_state, wake_attempts, wake_notified_status "
            "FROM jobs WHERE id = $1",
            uuid.UUID(job_id),
        )
    return dict(row)


# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_concurrent_claimers_yield_exactly_one_winner(db):
    """SKIP LOCKED plus Read Committed re-evaluation under FOR UPDATE. A double
    delivery is a duplicate transcript message and a second paid LLM turn."""
    job_id = await _mk(db)
    assert await db.mark_job_wake_pending(job_id, "completed") is True

    a, b = await db.claim_pending_job_wakes(), await db.claim_pending_job_wakes()

    winners = [r for r in (*a, *b) if str(r["id"]) == job_id]
    assert len(winners) == 1, "the wake was claimed twice"
    assert (await _state(db, job_id))["wake_state"] == "sending"


@pytest.mark.asyncio
async def test_a_crashed_sender_is_re_claimed_after_the_visibility_timeout(db):
    """The arm the durability argument rests on: a replica that died holding the
    claim must not park the wake forever."""
    job_id = await _mk(db)
    await db.mark_job_wake_pending(job_id, "completed")
    await db.claim_pending_job_wakes()

    # Inside the window: still exclusively the (now dead) claimer's.
    assert not [
        r for r in await db.claim_pending_job_wakes() if str(r["id"]) == job_id
    ]

    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE jobs SET wake_claimed_at = now() - interval '10 minutes' "
            "WHERE id = $1",
            uuid.UUID(job_id),
        )

    again = [r for r in await db.claim_pending_job_wakes() if str(r["id"]) == job_id]
    assert len(again) == 1
    assert again[0]["wake_attempts"] == 2, "each re-claim must burn an attempt"


@pytest.mark.asyncio
async def test_backstop_claims_a_terminal_job_whose_hook_never_ran(db):
    """Dispatch-time failures mark a job terminal with a direct DB write and no
    hook of any kind. Found by status, not by having been enqueued."""
    job_id = await _mk(db, status="failed")  # wake_state stays 'none'
    assert (await _state(db, job_id))["wake_state"] == "none"

    claimed = [r for r in await db.claim_pending_job_wakes() if str(r["id"]) == job_id]
    assert len(claimed) == 1
    assert claimed[0]["status"] == "failed"


@pytest.mark.asyncio
async def test_a_still_running_job_is_never_claimed(db):
    job_id = await _mk(db, status="processing")
    assert not [
        r for r in await db.claim_pending_job_wakes() if str(r["id"]) == job_id
    ]


@pytest.mark.asyncio
async def test_jobs_without_a_session_creator_are_invisible_to_the_claim(db):
    """Cockpit and automation jobs vastly outnumber session-created ones; if
    they were claimable the sweeper would deliver wakes to nobody."""
    no_thread = await _mk(db, thread=None)
    opted_out = await _mk(db, wake=False)

    ids = {str(r["id"]) for r in await db.claim_pending_job_wakes()}
    assert no_thread not in ids and opted_out not in ids


@pytest.mark.asyncio
async def test_same_terminal_status_is_delivered_once_but_a_new_one_wakes_again(db):
    """(job_id, terminal_status) is the dedup key, not job_id: approve flipping
    pending_review → completed is news the session wants."""
    job_id = await _mk(db, status="pending_review")
    assert await db.mark_job_wake_pending(job_id, "pending_review") is True
    await db.claim_pending_job_wakes()
    await db.finish_job_wake(job_id, "pending_review")

    assert await db.mark_job_wake_pending(job_id, "pending_review") is False

    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE jobs SET status = 'completed' WHERE id = $1", uuid.UUID(job_id)
        )
    assert await db.mark_job_wake_pending(job_id, "completed") is True


@pytest.mark.asyncio
async def test_a_delivered_wake_leaves_the_claimable_set(db):
    job_id = await _mk(db)
    await db.mark_job_wake_pending(job_id, "completed")
    await db.claim_pending_job_wakes()
    await db.finish_job_wake(job_id, "completed")

    assert not [
        r for r in await db.claim_pending_job_wakes() if str(r["id"]) == job_id
    ]
    assert (await _state(db, job_id))["wake_notified_status"] == "completed"


@pytest.mark.asyncio
async def test_release_retries_under_the_cap_and_buries_at_it(db):
    job_id = await _mk(db)
    await db.mark_job_wake_pending(job_id, "completed")
    await db.claim_pending_job_wakes()

    assert await db.release_job_wake(job_id, max_attempts=8) == "pending"

    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE jobs SET wake_attempts = 8 WHERE id = $1", uuid.UUID(job_id)
        )
    await db.claim_pending_job_wakes()
    assert await db.release_job_wake(job_id, max_attempts=8) == "dead"

    # Burying is what stops one unreachable session starving live wakes behind
    # it in the claim's ORDER BY — so a dead row must stay out.
    assert not [
        r for r in await db.claim_pending_job_wakes() if str(r["id"]) == job_id
    ]
    assert (await db.get_job_wake_stats())["dead"] >= 1


@pytest.mark.asyncio
async def test_a_dead_wake_is_not_re_armed_by_a_later_terminal_transition(db):
    """'dead' means an operator needs to look; silently re-arming would hide it."""
    job_id = await _mk(db)
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE jobs SET wake_state = 'dead' WHERE id = $1", uuid.UUID(job_id)
        )
    assert await db.mark_job_wake_pending(job_id, "cancelled") is False


@pytest.mark.asyncio
async def test_settle_calls_only_touch_a_row_this_caller_claimed(db):
    """finish/release are guarded on wake_state='sending' so a late settle from
    a timed-out sender cannot stomp the row a second claimer now owns."""
    job_id = await _mk(db)
    await db.mark_job_wake_pending(job_id, "completed")

    await db.finish_job_wake(job_id, "completed")  # never claimed
    assert (await _state(db, job_id))["wake_state"] == "pending"

    assert await db.release_job_wake(job_id) == "dead"  # no row → treated as dead
    assert (await _state(db, job_id))["wake_state"] == "pending"


@pytest.mark.asyncio
async def test_claim_orders_oldest_owed_first_and_honours_the_limit(db):
    async with db.acquire() as conn:
        await conn.execute("TRUNCATE jobs")
    first = await _mk(db)
    second = await _mk(db)
    await db.mark_job_wake_pending(first, "completed")
    await db.mark_job_wake_pending(second, "completed")

    batch = await db.claim_pending_job_wakes(limit=1)
    assert [str(r["id"]) for r in batch] == [first]


@pytest.mark.asyncio
async def test_thread_job_counts_scope_to_the_creating_thread(db):
    async with db.acquire() as conn:
        await conn.execute("TRUNCATE jobs")
    await _mk(db, status="completed")
    await _mk(db, status="failed")
    await _mk(db, status="processing")
    await _mk(db, status="completed", thread=_OTHER_TID)

    counts = await db.get_thread_job_counts(str(_TID))

    assert counts["total"] == 3
    assert counts["finished"] == 2
    assert counts["running"] == 1
    assert counts["failed"] == 1
