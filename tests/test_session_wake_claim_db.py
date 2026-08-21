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

Design: knowledge-base/knowledge/features/session_wake_on_job_completion.md.
"""

import asyncio
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from testcontainers.postgres import PostgresContainer

from database.postgres import PostgresDB

_UID = uuid.UUID("bbbbbbbb-0000-0000-0000-000000000001")
_TID = uuid.UUID("bbbbbbbb-0000-0000-0000-000000000002")
_OTHER_TID = uuid.UUID("bbbbbbbb-0000-0000-0000-000000000003")
_MIGRATIONS = (
    Path(__file__).parents[1] / "orchestrator" / "database" / "migrations" / "app"
)


@pytest.fixture(scope="module")
def pg_dsn():
    with PostgresContainer("postgres:16") as pg:
        yield pg.get_connection_url().replace("postgresql+psycopg2", "postgresql")


@pytest_asyncio.fixture
async def db(pg_dsn):
    """Minimal real-FK session/wake schema carrying the production SQL shape.

    Mirrors migrations 0070/0071 rather than replaying the full migration chain
    — including the partial index and ``ON DELETE SET NULL`` FK, so both claim
    planning and hard-delete settlement use real PostgreSQL lock/FK semantics.
    """
    d = PostgresDB(connection_string=pg_dsn)
    await d.connect()
    async with d.acquire() as conn:
        await conn.execute(
            "DROP FUNCTION IF EXISTS "
            "public.settle_job_wakes_before_thread_delete() CASCADE"
        )
        await conn.execute("DROP TABLE IF EXISTS jobs CASCADE")
        await conn.execute("DROP TABLE IF EXISTS docker_workspace_leases CASCADE")
        await conn.execute("DROP TABLE IF EXISTS completion_effects CASCADE")
        await conn.execute("DROP TABLE IF EXISTS thread_turn_commits CASCADE")
        await conn.execute("DROP TABLE IF EXISTS thread_rewinds CASCADE")
        await conn.execute("DROP TABLE IF EXISTS thread_messages CASCADE")
        await conn.execute("DROP TABLE IF EXISTS threads CASCADE")
        await conn.execute(
            """
            CREATE TABLE threads (
                id uuid PRIMARY KEY,
                execution_lane text NOT NULL DEFAULT 'pinned',
                status text NOT NULL DEFAULT 'ended',
                metadata jsonb NOT NULL DEFAULT '{}'::jsonb
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE docker_workspace_leases (
                owner_kind text NOT NULL,
                owner_id uuid NOT NULL,
                status text NOT NULL,
                quarantine_reason text,
                updated_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        await conn.execute(
            """
            CREATE TABLE completion_effects (
                producer_kind text NOT NULL,
                producer_id uuid NOT NULL,
                effect_name text NOT NULL,
                effect_group text NOT NULL,
                scope_id uuid NOT NULL,
                state text NOT NULL,
                created_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        await conn.execute("CREATE TABLE thread_turn_commits (thread_id uuid)")
        await conn.execute("CREATE TABLE thread_rewinds (thread_id uuid)")
        await conn.execute("CREATE TABLE thread_messages (thread_id uuid)")
        await conn.execute(
            """
            CREATE TABLE jobs (
                id uuid PRIMARY KEY,
                description text,
                config_name text DEFAULT 'worker_base',
                status text NOT NULL,
                user_id uuid,
                project_id uuid,
                expert_id uuid,
                freeze_data jsonb,
                error_message text,
                created_by_thread_id uuid REFERENCES threads(id)
                    ON DELETE SET NULL,
                wake_on_complete boolean NOT NULL DEFAULT false,
                wake_state text NOT NULL DEFAULT 'none',
                wake_claimed_at timestamptz,
                wake_attempts integer NOT NULL DEFAULT 0,
                wake_notified_status text,
                wake_delivery_id uuid,
                wake_delivery_claim_attempt integer,
                updated_at timestamptz NOT NULL DEFAULT now(),
                CONSTRAINT jobs_wake_state_known CHECK (
                    wake_state IN (
                        'none','pending','sending','sent','dead','undeliverable'))
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
        await conn.execute("TRUNCATE jobs, threads")
    yield d
    await d.close()


async def _mk(db, *, status="completed", wake=True, thread=_TID, **cols) -> str:
    job_id = uuid.uuid4()
    async with db.acquire() as conn:
        if thread is not None:
            await conn.execute(
                "INSERT INTO threads (id) VALUES ($1) ON CONFLICT DO NOTHING",
                thread,
            )
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
            "SELECT wake_state, wake_claimed_at, wake_attempts, "
            "wake_notified_status "
            "FROM jobs WHERE id = $1",
            uuid.UUID(job_id),
        )
    return dict(row)


async def _creator(db, job_id) -> uuid.UUID | None:
    async with db.acquire() as conn:
        return await conn.fetchval(
            "SELECT created_by_thread_id FROM jobs WHERE id = $1",
            uuid.UUID(job_id),
        )


# --------------------------------------------------------------------------


def test_undeliverable_constraint_swap_and_validation_are_separate_migrations():
    install = (_MIGRATIONS / "0150_job_wake_undeliverable.sql").read_text()
    validate = (_MIGRATIONS / "0151_job_wake_undeliverable_validate.sql").read_text()

    assert "'undeliverable'" in install
    assert "NOT VALID" in install
    assert "VALIDATE CONSTRAINT jobs_wake_state_known" not in install
    assert "created_by_thread_id IS NULL" not in install
    assert "depends-on:    0150_job_wake_undeliverable.sql" in validate
    assert "created_by_thread_id IS NULL" in validate
    assert "wake_state IN ('none', 'pending', 'sending')" in validate
    assert "VALIDATE CONSTRAINT jobs_wake_state_known" in validate


def test_thread_delete_guard_is_schema_qualified_and_exactly_scoped():
    guard = (_MIGRATIONS / "0154_thread_delete_wake_guard.sql").read_text()

    assert "depends-on:    0153_thread_permission_lease_comment.sql" in guard
    assert "CREATE FUNCTION public.settle_job_wakes_before_thread_delete()" in guard
    assert "SET search_path = pg_catalog, public" in guard
    assert "UPDATE public.jobs" in guard
    assert "created_by_thread_id = OLD.id" in guard
    assert "wake_on_complete" in guard
    assert "wake_state IN ('none', 'pending', 'sending')" in guard
    assert "BEFORE DELETE ON public.threads" in guard


def test_forward_orphan_convergence_is_exactly_guarded_and_ordered():
    convergence = (_MIGRATIONS / "0155_job_wake_orphan_convergence.sql").read_text()

    assert "depends-on:    0154_thread_delete_wake_guard.sql" in convergence
    assert "wake_on_complete" in convergence
    assert "created_by_thread_id IS NULL" in convergence
    assert "wake_state IN ('none', 'pending', 'sending')" in convergence
    assert "wake_state = 'undeliverable'" in convergence
    assert "wake_claimed_at = NULL" in convergence


@pytest.mark.asyncio
async def test_undeliverable_migration_backfills_only_legacy_open_orphans(db):
    install = (_MIGRATIONS / "0150_job_wake_undeliverable.sql").read_text()
    validate = (_MIGRATIONS / "0151_job_wake_undeliverable_validate.sql").read_text()
    async with db.acquire() as conn:
        await conn.execute("ALTER TABLE jobs DROP CONSTRAINT jobs_wake_state_known")
        await conn.execute(
            "ALTER TABLE jobs ADD CONSTRAINT jobs_wake_state_known CHECK ("
            "wake_state IN ('none','pending','sending','sent','dead'))"
        )
        await conn.execute(
            "INSERT INTO threads (id) VALUES ($1)",
            _TID,
        )

        ids = {
            name: uuid.uuid4()
            for name in (
                "none",
                "pending",
                "sending",
                "sent",
                "dead",
                "opted_out",
                "live",
            )
        }
        for state in ("none", "pending", "sending", "sent", "dead"):
            await conn.execute(
                "INSERT INTO jobs (id, description, status, user_id, "
                "wake_on_complete, wake_state, wake_claimed_at) "
                "VALUES ($1, 'legacy', 'completed', $2, true, $3, now())",
                ids[state],
                _UID,
                state,
            )
        await conn.execute(
            "INSERT INTO jobs (id, description, status, user_id, "
            "wake_on_complete, wake_state) "
            "VALUES ($1, 'ordinary', 'completed', $2, false, 'pending')",
            ids["opted_out"],
            _UID,
        )
        await conn.execute(
            "INSERT INTO jobs (id, description, status, user_id, "
            "created_by_thread_id, wake_on_complete, wake_state) "
            "VALUES ($1, 'live', 'completed', $2, $3, true, 'pending')",
            ids["live"],
            _UID,
            _TID,
        )

        await conn.execute(install)
        await conn.execute(validate)
        rows = await conn.fetch(
            "SELECT id, wake_state, wake_claimed_at FROM jobs WHERE id = ANY($1)",
            list(ids.values()),
        )
        constraint_valid = await conn.fetchval(
            "SELECT convalidated FROM pg_constraint "
            "WHERE conrelid = 'jobs'::regclass "
            "AND conname = 'jobs_wake_state_known'"
        )

    states = {str(row["id"]): dict(row) for row in rows}
    for name in ("none", "pending", "sending"):
        assert states[str(ids[name])]["wake_state"] == "undeliverable"
        assert states[str(ids[name])]["wake_claimed_at"] is None
    assert states[str(ids["sent"])]["wake_state"] == "sent"
    assert states[str(ids["dead"])]["wake_state"] == "dead"
    assert states[str(ids["opted_out"])]["wake_state"] == "pending"
    assert states[str(ids["live"])]["wake_state"] == "pending"
    assert constraint_valid is True


@pytest.mark.asyncio
async def test_raw_old_code_delete_is_guarded_before_real_fk_null(db):
    guard = (_MIGRATIONS / "0154_thread_delete_wake_guard.sql").read_text()
    async with db.acquire() as conn:
        await conn.execute(guard)

    pending = await _mk(db)
    await db.mark_job_wake_pending(pending, "completed")
    sent = await _mk(db)
    await db.mark_job_wake_pending(sent, "completed")
    await db.claim_pending_job_wakes()
    await db.finish_job_wake(sent, "completed")
    other = await _mk(db, thread=_OTHER_TID)
    await db.mark_job_wake_pending(other, "completed")

    async with db.acquire() as conn:
        # Models an old orchestrator or an operator issuing DELETE directly,
        # with no new application-side settlement statement.
        await conn.execute("DELETE FROM threads WHERE id = $1", _TID)

    assert (await _state(db, pending))["wake_state"] == "undeliverable"
    assert await _creator(db, pending) is None
    assert (await _state(db, sent))["wake_state"] == "sent"
    assert (await _state(db, other))["wake_state"] == "pending"
    assert await _creator(db, other) == _OTHER_TID


@pytest.mark.asyncio
async def test_post_0151_pre_0154_orphan_converges_forward_in_0155(db):
    """Model the rollout interval exactly.

    The expanded/validated state constraint represents 0151 already applied.
    Old code then deletes a thread before 0154's trigger exists, so the real FK
    nulls every creator. Installing 0154 cannot repair historical rows; 0155
    must retire only the opted-in open wake states left in that finite window.
    """
    guard = (_MIGRATIONS / "0154_thread_delete_wake_guard.sql").read_text()
    convergence = (_MIGRATIONS / "0155_job_wake_orphan_convergence.sql").read_text()
    ids = {
        state: await _mk(db, wake_state=state)
        for state in ("none", "pending", "sending", "sent", "dead")
    }
    ordinary = await _mk(db, thread=None, wake=False, wake_state="pending")
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE jobs SET wake_claimed_at = now() WHERE id = ANY($1::uuid[])",
            [uuid.UUID(job_id) for job_id in (*ids.values(), ordinary)],
        )

        # Pre-0154 old-code delete: there is no guard yet, so the real FK alone
        # handles the backreference and creates the rollout-window orphans.
        await conn.execute("DELETE FROM threads WHERE id = $1", _TID)
        await conn.execute(guard)

    for state, job_id in ids.items():
        assert await _creator(db, job_id) is None
        assert (await _state(db, job_id))["wake_state"] == state

    async with db.acquire() as conn:
        await conn.execute(convergence)

    for state in ("none", "pending", "sending"):
        row = await _state(db, ids[state])
        assert row["wake_state"] == "undeliverable"
        assert row["wake_claimed_at"] is None
    for state in ("sent", "dead"):
        row = await _state(db, ids[state])
        assert row["wake_state"] == state
        assert row["wake_claimed_at"] is not None
    ordinary_row = await _state(db, ordinary)
    assert ordinary_row["wake_state"] == "pending"
    assert ordinary_row["wake_claimed_at"] is not None


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
    assert not [r for r in await db.claim_pending_job_wakes() if str(r["id"]) == job_id]

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
    assert not [r for r in await db.claim_pending_job_wakes() if str(r["id"]) == job_id]


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

    assert not [r for r in await db.claim_pending_job_wakes() if str(r["id"]) == job_id]
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
    assert not [r for r in await db.claim_pending_job_wakes() if str(r["id"]) == job_id]
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


@pytest.mark.asyncio
@pytest.mark.parametrize("wake_state", ["none", "pending", "sending"])
async def test_hard_delete_atomically_retires_every_owed_wake_state(db, wake_state):
    job_id = await _mk(db)
    if wake_state in {"pending", "sending"}:
        await db.mark_job_wake_pending(job_id, "completed")
    if wake_state == "sending":
        claimed = await db.claim_pending_job_wakes()
        assert [str(row["id"]) for row in claimed] == [job_id]

    await db.delete_thread(str(_TID))

    state = await _state(db, job_id)
    assert state["wake_state"] == "undeliverable"
    assert await _creator(db, job_id) is None, "the real FK did not SET NULL"
    assert not [
        row for row in await db.claim_pending_job_wakes() if str(row["id"]) == job_id
    ]
    assert await db.mark_job_wake_pending(job_id, "completed") is False
    assert (await db.get_job_wake_stats())["undeliverable"] == 1


@pytest.mark.asyncio
async def test_hard_delete_scopes_retirement_to_exact_thread_and_open_states(db):
    pending = await _mk(db)
    await db.mark_job_wake_pending(pending, "completed")
    delivered = await _mk(db)
    await db.mark_job_wake_pending(delivered, "completed")
    await db.claim_pending_job_wakes()
    await db.finish_job_wake(delivered, "completed")
    other = await _mk(db, thread=_OTHER_TID)
    await db.mark_job_wake_pending(other, "completed")

    await db.delete_thread(str(_TID))

    assert (await _state(db, pending))["wake_state"] == "undeliverable"
    assert (await _state(db, delivered))["wake_state"] == "sent"
    assert (await _state(db, other))["wake_state"] == "pending"
    assert await _creator(db, other) == _OTHER_TID


@pytest.mark.asyncio
@pytest.mark.parametrize("settle_kind", ["finish", "release"])
async def test_hard_delete_wins_against_late_sender_settlement(db, settle_kind):
    """Pause DELETE after wake retirement but before FK null/commit.

    The sender's late finish blocks behind the deleting transaction, then must
    lose its ``wake_state='sending'`` CAS after PostgreSQL rechecks the row.
    This is the production claim/delete race, including the real FK trigger.
    """
    job_id = await _mk(db)
    await db.mark_job_wake_pending(job_id, "completed")
    assert [str(row["id"]) for row in await db.claim_pending_job_wakes()] == [job_id]

    lock_key = 8675309150
    async with db.acquire() as blocker:
        await blocker.execute("SELECT pg_advisory_lock($1)", lock_key)
        async with db.acquire() as setup:
            await setup.execute(
                """
                CREATE OR REPLACE FUNCTION pause_thread_delete_for_wake_test()
                RETURNS trigger AS $$
                BEGIN
                    PERFORM pg_advisory_lock(8675309150);
                    PERFORM pg_advisory_unlock(8675309150);
                    RETURN OLD;
                END;
                $$ LANGUAGE plpgsql
                """
            )
            await setup.execute(
                "CREATE TRIGGER pause_thread_delete_for_wake_test "
                "BEFORE DELETE ON threads FOR EACH ROW "
                "EXECUTE FUNCTION pause_thread_delete_for_wake_test()"
            )

        delete_task = asyncio.create_task(db.delete_thread(str(_TID)))
        for _ in range(200):
            async with db.acquire() as observer:
                waiting = await observer.fetchval(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM pg_stat_activity
                        WHERE query LIKE 'DELETE FROM threads%'
                          AND wait_event_type = 'Lock'
                    )
                    """
                )
            if waiting:
                break
            await asyncio.sleep(0.01)
        else:
            delete_task.cancel()
            await delete_task
            pytest.fail("hard delete never reached the post-retirement barrier")

        if settle_kind == "finish":
            settle_task = asyncio.create_task(db.finish_job_wake(job_id, "completed"))
            query_fragment = "SET wake_state = 'sent'"
        else:
            settle_task = asyncio.create_task(db.release_job_wake(job_id))
            query_fragment = "SET wake_state = CASE"
        for _ in range(200):
            async with db.acquire() as observer:
                sender_waiting = await observer.fetchval(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM pg_stat_activity
                        WHERE position($1 in query) > 0
                          AND wait_event_type = 'Lock'
                    )
                    """,
                    query_fragment,
                )
            if sender_waiting:
                break
            await asyncio.sleep(0.01)
        else:
            settle_task.cancel()
            await settle_task
            delete_task.cancel()
            await delete_task
            pytest.fail("late sender never contended with hard delete")
        await blocker.execute("SELECT pg_advisory_unlock($1)", lock_key)

    try:
        await delete_task
        settled = await settle_task
        if settle_kind == "finish":
            assert settled is False
        else:
            assert settled == "undeliverable"
    finally:
        async with db.acquire() as cleanup:
            await cleanup.execute(
                "DROP TRIGGER IF EXISTS pause_thread_delete_for_wake_test ON threads"
            )
            await cleanup.execute(
                "DROP FUNCTION IF EXISTS pause_thread_delete_for_wake_test()"
            )

    assert (await _state(db, job_id))["wake_state"] == "undeliverable"
    assert await _creator(db, job_id) is None
