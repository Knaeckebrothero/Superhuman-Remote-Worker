"""Real-Postgres behavioral proof for the ledger-aware `unstick_reviewing_parents`.

Task 10 (knowledge-base/knowledge/superpowers/sdd/2026-07-27-verification-fail-closed/task-10-brief.md)
replaces the watchdog's old "every critic child is failed/cancelled" gate —
broken as of the fail-closed verification design, since every round (approved
AND returned) now freezes its critic as an ordinary `completed` subjob
(Task 8), so a stale round-1 critic permanently blocks the old gate forever.

This file exists because a purely textual check on `_UNSTICK_REVIEWING_SQL`
(see tests/test_stale_verification_sweeper.py) cannot prove the replacement
condition is actually *correct*: the brief's own illustrative SQL — "does the
newest ledger entry have a recorded verdict" — is a tautology once any round
has ever been recorded (every appended round always carries a non-null
verdict by construction; see `_record_verification_round_impl` in
orchestrator/main.py), so on its own it can NEVER distinguish "the live
critic just recorded and the handler hasn't caught up yet" (must not fire)
from "a NEWER critic died without recording anything, while an OLD round's
verdict is still sitting there stale" (must fire) — which is exactly the
scenario this task exists to fix. The implementation here instead compares
the ledger against the MOST RECENTLY SPAWNED critic child specifically (see
the `_UNSTICK_REVIEWING_SQL` comment in orchestrator/database/postgres.py).
These tests exercise that distinction against a real server, with real rows,
not mocks.

Uses testcontainers + the full `schema_current.sql` snapshot (matching
tests/test_sweeps_real_postgres.py's schema source) so FK columns
(parent_job_id, user_id, config_name, created_at) all exist for real —
mirrors tests/test_stale_verification_outage_exemption.py's rationale for why
this predicate needs a real server: NULL semantics and JSON operator
propagation aren't safe to trust to mocks.
"""

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
    Path(__file__).resolve().parent.parent
    / "src"
    / "orchestrator"
    / "database"
    / "schema_current.sql"
)

GRACE_MINUTES = 30


@pytest.fixture(scope="module")
def pg_dsn():
    with PostgresContainer("postgres:16") as pg:
        yield pg.get_connection_url().replace("postgresql+psycopg2", "postgresql")


@pytest_asyncio.fixture(scope="module")
async def _schema_applied(pg_dsn):
    # Applied once per container — schema_current.sql creates ~50 tables,
    # functions, and triggers; re-running it per test would be needlessly
    # slow. Individual tests TRUNCATE instead (see `db` fixture below).
    conn = await asyncpg.connect(pg_dsn)
    try:
        await conn.execute(SCHEMA_FILE.read_text())
    finally:
        await conn.close()
    return True


@pytest_asyncio.fixture
async def db(pg_dsn, _schema_applied):
    d = PostgresDB(connection_string=pg_dsn, min_connections=1, max_connections=3)
    await d.connect()
    async with d.acquire() as conn:
        await conn.execute("TRUNCATE jobs CASCADE")
    yield d
    await d.close()


async def _insert_job(
    db,
    *,
    job_id,
    status,
    parent_job_id=None,
    context=None,
    updated_minutes_ago=0,
    created_minutes_ago=0,
):
    async with db.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO jobs
                (id, description, status, parent_job_id, context,
                 config_name, created_at, updated_at)
            VALUES
                ($1, 'ledger watchdog test', $2, $3, $4::jsonb, 'developer',
                 CURRENT_TIMESTAMP - make_interval(mins => $5::int),
                 CURRENT_TIMESTAMP - make_interval(mins => $6::int))
            """,
            job_id,
            status,
            parent_job_id,
            json.dumps(context or {}),
            created_minutes_ago,
            updated_minutes_ago,
        )


async def _status(db, job_id):
    async with db.acquire() as conn:
        row = await conn.fetchrow("SELECT status FROM jobs WHERE id = $1", job_id)
    return row["status"]


async def _insert_completion_command(db, job_id, *, route_case):
    state = "parked" if route_case == "parked" else "finalizing"
    lease_interval = (
        "interval '5 minutes'" if route_case == "live" else "interval '-1 minute'"
    )
    async with db.acquire() as conn:
        await conn.execute(
            f"""
            INSERT INTO job_completion_commands (
                job_id, report_seq, client_report_id, payload, payload_digest,
                accepted_job_status, origin, requested_by, state, attempts,
                lease_expires_at, deadline_at, finalizing_by, code_version,
                error_code
            ) VALUES (
                $1, 1, $2, '{{}}'::jsonb, 'watchdog-route', 'reviewing',
                'operator', 'watchdog-test', $3, 1,
                CASE WHEN $3 = 'finalizing' THEN now() + {lease_interval} END,
                now() + interval '1 hour',
                CASE WHEN $3 = 'finalizing' THEN 'watchdog-finalizer' END,
                'gate3-v1',
                CASE WHEN $3 = 'parked' THEN 'operator_hold' END
            )
            """,
            job_id,
            uuid4(),
            state,
        )


def _round(round_num, critic_job_id, verdict):
    """Minimal round record — only the fields the watchdog's SQL reads."""
    return {"round": round_num, "critic_job_id": str(critic_job_id), "verdict": verdict}


@pytest.mark.asyncio
async def test_fires_when_round_two_critic_dies_after_round_one_resolved(db):
    """The bug this task fixes: round 1 returned (critic 1 recorded + is
    `completed`, same as an approval under Task 8's design), the target did
    more work and re-entered 'reviewing', a round-2 critic spawned and died
    (`failed`) WITHOUT ever recording anything. The old "all children
    failed/cancelled" gate would stay blocked forever by critic 1's
    `completed` status. The new gate must fire.
    """
    target = uuid4()
    critic1 = uuid4()
    critic2 = uuid4()

    await _insert_job(
        db,
        job_id=target,
        status="reviewing",
        updated_minutes_ago=45,  # past the 30min grace
        context={"verification_rounds": [_round(1, critic1, "returned")]},
    )
    # Old, already-resolved critic — terminal, and (post Task 8) `completed`.
    await _insert_job(
        db,
        job_id=critic1,
        status="completed",
        parent_job_id=target,
        context={"verification_target": str(target)},
        created_minutes_ago=60,
    )
    # Fresh critic for round 2 — spawned after critic 1, died without
    # recording a verdict.
    await _insert_job(
        db,
        job_id=critic2,
        status="failed",
        parent_job_id=target,
        context={"verification_target": str(target)},
        created_minutes_ago=40,
    )

    rows = await db.unstick_reviewing_parents(grace_minutes=GRACE_MINUTES)

    assert [str(r["id"]) for r in rows] == [str(target)]
    assert rows[0]["config_name"] == "developer"
    assert await _status(db, target) == "pending_review"


@pytest.mark.asyncio
async def test_does_not_fire_while_freshly_recorded_verdict_awaits_handler(db):
    """Race-safety proof: the critic that just went terminal DID record its
    verdict — `_handle_critic_verdict_on_complete` (main.py) owns applying
    that outcome to the target and must not be pre-empted, no matter how much
    time has passed since. This is the scenario the task explicitly calls
    out: "an over-eager watchdog that escalates a target whose critic just
    approved it would be a regression worse than the bug being fixed."

    `updated_at` is set far past grace deliberately — in the live system
    `append_verification_round` bumps the target's `updated_at` at the moment
    it records (recording happens strictly before the critic's own job
    reaches 'completed'; see `_record_verification_round_impl`'s docstring),
    so the grace timer alone would normally also protect this exact window.
    Backdating it here isolates the SECOND, independent safety net — the
    ledger check — for the case where the handler is delayed well past
    grace (e.g. a restart between the ledger write and the handler running).
    """
    target = uuid4()
    critic1 = uuid4()

    await _insert_job(
        db,
        job_id=target,
        status="reviewing",
        updated_minutes_ago=45,
        context={"verification_rounds": [_round(1, critic1, "approved")]},
    )
    await _insert_job(
        db,
        job_id=critic1,
        status="completed",
        parent_job_id=target,
        context={"verification_target": str(target)},
        created_minutes_ago=50,
    )

    rows = await db.unstick_reviewing_parents(grace_minutes=GRACE_MINUTES)

    assert rows == []
    assert await _status(db, target) == "reviewing"


@pytest.mark.asyncio
async def test_fires_when_no_critic_ever_spawned(db):
    """Parity with the old query's documented "(or none exists)" case: a
    target sitting in 'reviewing' past grace with zero critic children ever
    spawned (e.g. `_trigger_verification_on_complete` never ran) is
    stranded, not merely "awaiting a fresh critic"."""
    target = uuid4()
    await _insert_job(
        db, job_id=target, status="reviewing", updated_minutes_ago=45, context={}
    )

    rows = await db.unstick_reviewing_parents(grace_minutes=GRACE_MINUTES)

    assert [str(r["id"]) for r in rows] == [str(target)]
    assert await _status(db, target) == "pending_review"


@pytest.mark.asyncio
async def test_does_not_fire_while_critic_still_processing(db):
    """A live critic — even past grace — must never be pre-empted."""
    target = uuid4()
    critic = uuid4()
    await _insert_job(
        db,
        job_id=target,
        status="reviewing",
        updated_minutes_ago=45,
        context={},
    )
    await _insert_job(
        db,
        job_id=critic,
        status="processing",
        parent_job_id=target,
        context={"verification_target": str(target)},
        created_minutes_ago=10,
    )

    rows = await db.unstick_reviewing_parents(grace_minutes=GRACE_MINUTES)

    assert rows == []
    assert await _status(db, target) == "reviewing"


@pytest.mark.asyncio
async def test_does_not_fire_while_critic_waiting_for_reply(db):
    """Defence in depth: 'waiting_for_reply' should now be UNREACHABLE for a
    critic, and the watchdog must still handle it if it ever isn't.

    It used to be reachable: `config/worker_base.yaml` sets
    `tools.communication: [send_message]`; `config/experts/critic/config.yaml`
    never overrides the `communication` key, and `deep_merge`
    (src/core/loader.py) merges the `tools` dict by key rather than replacing
    it wholesale, so the critic inherited `send_message`. A blocking-mode
    `send_message` call sets the CALLER's own job status to
    'waiting_for_reply' (orchestrator/main.py's send_message handler), and
    NOTHING reaps that state — `communication.blocking_timeout_hours` has no
    implementation — so the target sat in 'reviewing' forever.

    That is now closed upstream: `_critic_config_override` stamps
    `tools.communication: []`. This case is kept because the cost of one
    unreachable branch is far below the cost of a critic silently parked
    there again: it is alive, blocked on a human reply — not dead — and the
    watchdog must not treat it as absent just because 'waiting_for_reply'
    isn't 'waiting'.
    """
    target = uuid4()
    critic = uuid4()
    await _insert_job(
        db,
        job_id=target,
        status="reviewing",
        updated_minutes_ago=45,
        context={},
    )
    await _insert_job(
        db,
        job_id=critic,
        status="waiting_for_reply",
        parent_job_id=target,
        context={"verification_target": str(target)},
        created_minutes_ago=10,
    )

    rows = await db.unstick_reviewing_parents(grace_minutes=GRACE_MINUTES)

    assert rows == []
    assert await _status(db, target) == "reviewing"


@pytest.mark.asyncio
async def test_does_not_fire_before_grace_elapsed(db):
    """A dead, unrecorded critic doesn't strand the target instantly — the
    grace floor still applies."""
    target = uuid4()
    critic = uuid4()
    await _insert_job(
        db,
        job_id=target,
        status="reviewing",
        updated_minutes_ago=5,  # well within the 30min grace
        context={},
    )
    await _insert_job(
        db,
        job_id=critic,
        status="failed",
        parent_job_id=target,
        context={"verification_target": str(target)},
        created_minutes_ago=4,
    )

    rows = await db.unstick_reviewing_parents(grace_minutes=GRACE_MINUTES)

    assert rows == []
    assert await _status(db, target) == "reviewing"


@pytest.mark.asyncio
async def test_fires_when_latest_critic_is_pending_review_unrecorded(db):
    """Bonus regression coverage for a related, separately-documented wedge
    (knowledge-base/knowledge/issues/critic_failure_leaves_parent_job_stuck_reviewing.md, "2026-
    07-16 — new wedge variant"): a critic that freezes into `pending_review`
    (e.g. a non-outage subjob freeze) is ACTIONABLE, not live —
    `_CRITIC_ACTIONABLE_STATUSES` in orchestrator/main.py already treats it
    that way. It must not block the watchdog either.
    """
    target = uuid4()
    critic = uuid4()
    await _insert_job(
        db,
        job_id=target,
        status="reviewing",
        updated_minutes_ago=45,
        context={},
    )
    await _insert_job(
        db,
        job_id=critic,
        status="pending_review",
        parent_job_id=target,
        context={"verification_target": str(target)},
        created_minutes_ago=40,
    )

    rows = await db.unstick_reviewing_parents(grace_minutes=GRACE_MINUTES)

    assert [str(r["id"]) for r in rows] == [str(target)]
    assert await _status(db, target) == "pending_review"


@pytest.mark.asyncio
async def test_scholar_and_delegation_children_are_ignored(db):
    """`parent_job_id` alone also matches scholars/delegation children —
    only rows with `context->>'verification_target'` set count as critics.
    A live (non-terminal) non-critic child must not block the watchdog, and
    must not satisfy the "some critic recorded" ledger check either.
    """
    target = uuid4()
    scholar = uuid4()
    await _insert_job(
        db,
        job_id=target,
        status="reviewing",
        updated_minutes_ago=45,
        context={},
    )
    await _insert_job(
        db,
        job_id=scholar,
        status="processing",  # would block if mis-classified as a critic
        parent_job_id=target,
        context={},  # no verification_target — not a critic
        created_minutes_ago=10,
    )

    rows = await db.unstick_reviewing_parents(grace_minutes=GRACE_MINUTES)

    assert [str(r["id"]) for r in rows] == [str(target)]
    assert await _status(db, target) == "pending_review"


@pytest.mark.asyncio
async def test_survives_a_json_null_verification_rounds(db):
    """A jsonb `null` at context->'verification_rounds' is NOT SQL NULL, so
    COALESCE does not replace it and `jsonb_array_elements('null'::jsonb)`
    raises "cannot extract elements from a scalar". That aborts the whole
    statement, the sweeper tick dies with it, and the stranded-target watchdog
    stops working SYSTEM-WIDE — for every target, not just this row.

    Reachable today: a caller could seed `verification_rounds` through the
    public POST /api/jobs context (that hole is closed separately), and any
    future writer that stores a null lands here too. The predicate must
    type-check the value rather than trust its shape.
    """
    target = uuid4()
    await _insert_job(
        db,
        job_id=target,
        status="reviewing",
        updated_minutes_ago=45,
        context={"verification_rounds": None},
    )

    # Must not raise — that is the entire point of this test.
    rows = await db.unstick_reviewing_parents(grace_minutes=GRACE_MINUTES)

    assert [str(r["id"]) for r in rows] == [str(target)]
    assert await _status(db, target) == "pending_review"


@pytest.mark.asyncio
async def test_survives_a_non_array_verification_rounds(db):
    """Same class, other scalars/objects: anything that is not a jsonb array
    must be treated as "no ledger", never fed to jsonb_array_elements."""
    for bad in ('"oops"', "42", '{"round": 1}'):
        target = uuid4()
        async with db.acquire() as conn:
            await conn.execute("TRUNCATE jobs CASCADE")
            await conn.execute(
                """
                INSERT INTO jobs
                    (id, description, status, context, config_name,
                     created_at, updated_at)
                VALUES
                    ($1, 'bad ledger', 'reviewing',
                     jsonb_build_object('verification_rounds', $2::jsonb),
                     'developer', CURRENT_TIMESTAMP,
                     CURRENT_TIMESTAMP - make_interval(mins => 45))
                """,
                target,
                bad,
            )

        rows = await db.unstick_reviewing_parents(grace_minutes=GRACE_MINUTES)

        assert [str(r["id"]) for r in rows] == [str(target)], f"failed for {bad}"


# ---------------------------------------------------------------------------
# has_live_verification_critic — the duplicate-spawn guard's predicate.
#
# Lives here rather than in tests/test_atomic_job_context.py because it needs
# real `parent_job_id` + `context` columns, which only the full
# schema_current.sql fixture above provides. Same "NULL semantics and JSON
# operator propagation aren't safe to trust to mocks" rationale as the rest of
# this module.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status", ["created", "processing", "paused", "waiting", "waiting_for_reply"]
)
async def test_live_critic_is_detected(db, status):
    target = uuid4()
    critic = uuid4()
    await _insert_job(db, job_id=target, status="reviewing")
    await _insert_job(
        db,
        job_id=critic,
        status=status,
        parent_job_id=target,
        context={"verification_target": str(target)},
    )

    assert await db.has_live_verification_critic(str(target)) is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status", ["completed", "failed", "cancelled", "pending_review"]
)
async def test_finished_critic_is_not_live(db, status):
    target = uuid4()
    critic = uuid4()
    await _insert_job(db, job_id=target, status="reviewing")
    await _insert_job(
        db,
        job_id=critic,
        status=status,
        parent_job_id=target,
        context={"verification_target": str(target)},
    )

    assert await db.has_live_verification_critic(str(target)) is False


@pytest.mark.asyncio
async def test_no_children_is_not_live(db):
    target = uuid4()
    await _insert_job(db, job_id=target, status="reviewing")

    assert await db.has_live_verification_critic(str(target)) is False


@pytest.mark.asyncio
async def test_live_scholar_child_is_not_a_critic(db):
    """`parent_job_id` alone also matches scholars and delegation children —
    only rows carrying `context->>'verification_target'` count, or a job with
    a live scholar could never get its first critic."""
    target = uuid4()
    scholar = uuid4()
    await _insert_job(db, job_id=target, status="reviewing")
    await _insert_job(
        db,
        job_id=scholar,
        status="processing",
        parent_job_id=target,
        context={"scholar_target": str(target)},
    )

    assert await db.has_live_verification_critic(str(target)) is False


@pytest.mark.asyncio
async def test_invalid_uuid_is_reported_live(db):
    """Fail CLOSED: an unusable id must not read as "no critic exists" and
    licence a spawn."""
    assert await db.has_live_verification_critic("not-a-uuid") is True


@pytest.mark.asyncio
async def test_critic_round_lookup_returns_only_the_exact_round(db):
    target = uuid4()
    round_zero_critic = uuid4()
    round_one_critic = uuid4()
    await _insert_job(db, job_id=target, status="reviewing")
    await _insert_job(
        db,
        job_id=round_zero_critic,
        status="processing",
        parent_job_id=target,
        context={
            "verification_target": str(target),
            "verification_round": 0,
        },
    )
    await _insert_job(
        db,
        job_id=round_one_critic,
        status="created",
        parent_job_id=target,
        context={
            "verification_target": str(target),
            "verification_round": 1,
        },
    )

    found = await db.get_verification_critic_for_round(str(target), 1)

    assert found is not None
    assert str(found["id"]) == str(round_one_critic)
    assert await db.get_verification_critic_for_round(str(target), 2) is None


@pytest.mark.asyncio
async def test_cas_does_not_touch_non_reviewing_targets(db):
    """The CAS (`WHERE p.status = 'reviewing'`) — a target already moved on
    (e.g. the verdict handler won the race and flipped it to something else)
    must be left untouched even if every other condition would match."""
    target = uuid4()
    critic = uuid4()
    await _insert_job(
        db,
        job_id=target,
        status="processing",  # NOT reviewing — handler already moved it on
        updated_minutes_ago=45,
        context={},
    )
    await _insert_job(
        db,
        job_id=critic,
        status="failed",
        parent_job_id=target,
        context={"verification_target": str(target)},
        created_minutes_ago=40,
    )

    rows = await db.unstick_reviewing_parents(grace_minutes=GRACE_MINUTES)

    assert rows == []
    assert await _status(db, target) == "processing"


# ---------------------------------------------------------------------------
# Wall-clock arm (unstick_reviewing_parents_wallclock) — the LIVE-critic
# backstop, fix direction 2 of
# knowledge-history/done/rejected_verdict_livelocks_critic_and_wedges_parent.md.
# ---------------------------------------------------------------------------

WALLCLOCK_MINUTES = 60


@pytest.mark.asyncio
async def test_wallclock_escalates_live_critic_past_ceiling(db):
    """A parent stuck in 'reviewing' for the whole ceiling while its critic is
    ALIVE (the livelocked-critic shape: 189 iterations / 105 min in the field
    case) escalates with the distinct did-not-render-a-verdict message —
    exactly the case the dead-critic arm deliberately never touches."""
    target = uuid4()
    critic = uuid4()
    await _insert_job(
        db,
        job_id=target,
        status="reviewing",
        updated_minutes_ago=75,  # past the 60min ceiling
        context={},
    )
    await _insert_job(
        db,
        job_id=critic,
        status="processing",  # alive the whole time
        parent_job_id=target,
        context={"verification_target": str(target)},
        created_minutes_ago=75,
    )

    # The dead-critic arm must decline (live critic exists) …
    assert await db.unstick_reviewing_parents(grace_minutes=GRACE_MINUTES) == []
    assert await _status(db, target) == "reviewing"

    # … while the wall-clock arm fires with its own message.
    rows = await db.unstick_reviewing_parents_wallclock(WALLCLOCK_MINUTES)

    assert [str(r["id"]) for r in rows] == [str(target)]
    assert await _status(db, target) == "pending_review"
    async with db.acquire() as conn:
        msg = await conn.fetchval(
            "SELECT error_message FROM jobs WHERE id = $1", target
        )
    assert "critic did not render a verdict in 60 minutes" in msg
    assert "critic pipeline died" not in msg


@pytest.mark.asyncio
async def test_wallclock_leaves_healthy_long_review_under_ceiling(db):
    """A live review younger than the ceiling is never pre-empted."""
    target = uuid4()
    critic = uuid4()
    await _insert_job(
        db,
        job_id=target,
        status="reviewing",
        updated_minutes_ago=45,  # past the dead-arm grace, under the ceiling
        context={},
    )
    await _insert_job(
        db,
        job_id=critic,
        status="processing",
        parent_job_id=target,
        context={"verification_target": str(target)},
        created_minutes_ago=45,
    )

    assert await db.unstick_reviewing_parents_wallclock(WALLCLOCK_MINUTES) == []
    assert await _status(db, target) == "reviewing"


@pytest.mark.asyncio
async def test_wallclock_ignores_dead_critic_parents(db):
    """No live critic → that is the dead-critic arm's case, not this one;
    the two arms stay disjoint so each wedge gets its own message."""
    target = uuid4()
    critic = uuid4()
    await _insert_job(
        db,
        job_id=target,
        status="reviewing",
        updated_minutes_ago=120,
        context={},
    )
    await _insert_job(
        db,
        job_id=critic,
        status="failed",
        parent_job_id=target,
        context={"verification_target": str(target)},
        created_minutes_ago=120,
    )

    assert await db.unstick_reviewing_parents_wallclock(WALLCLOCK_MINUTES) == []
    assert await _status(db, target) == "reviewing"


@pytest.mark.asyncio
@pytest.mark.parametrize("wallclock", [False, True])
@pytest.mark.parametrize(
    ("route_case", "expected_status"),
    [
        ("live", "reviewing"),
        ("expired", "pending_review"),
        ("parked", "pending_review"),
    ],
)
async def test_watchdogs_defer_only_to_live_finalizer_lease(
    db, wallclock, route_case, expected_status
):
    target = uuid4()
    await _insert_job(
        db,
        job_id=target,
        status="reviewing",
        updated_minutes_ago=120,
        context={},
    )
    if wallclock:
        critic = uuid4()
        await _insert_job(
            db,
            job_id=critic,
            status="processing",
            parent_job_id=target,
            context={"verification_target": str(target)},
            created_minutes_ago=120,
        )
    await _insert_completion_command(db, target, route_case=route_case)

    if wallclock:
        await db.unstick_reviewing_parents_wallclock(
            WALLCLOCK_MINUTES, completion_commands_enabled=True
        )
    else:
        await db.unstick_reviewing_parents(
            GRACE_MINUTES, completion_commands_enabled=True
        )

    assert await _status(db, target) == expected_status


@pytest.mark.asyncio
@pytest.mark.parametrize("wallclock", [False, True])
@pytest.mark.parametrize(
    ("route_case", "expected_status"),
    [
        ("live", "reviewing"),
        ("expired", "pending_review"),
        ("parked", "pending_review"),
    ],
)
async def test_watchdogs_defer_to_live_critic_finalizer_lease_only(
    db, wallclock, route_case, expected_status
):
    target = uuid4()
    critic = uuid4()
    await _insert_job(
        db,
        job_id=target,
        status="reviewing",
        updated_minutes_ago=120,
        context={},
    )
    await _insert_job(
        db,
        job_id=critic,
        status="processing" if wallclock else "failed",
        parent_job_id=target,
        context={"verification_target": str(target)},
        created_minutes_ago=120,
    )
    await _insert_completion_command(db, critic, route_case=route_case)

    if wallclock:
        await db.unstick_reviewing_parents_wallclock(
            WALLCLOCK_MINUTES, completion_commands_enabled=True
        )
    else:
        await db.unstick_reviewing_parents(
            GRACE_MINUTES, completion_commands_enabled=True
        )

    assert await _status(db, target) == expected_status
