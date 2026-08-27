"""Real-Postgres proofs for the worker-message route ledger (M2/M4 races).

What the mocked suites cannot prove: the blocking-send transaction really is
all-or-nothing (failure injection leaves NO orphaned rows and NO frozen job),
and the FOR UPDATE SKIP LOCKED + CAS claims settle each route — and unblock
each job — exactly once under genuine concurrency.

Mirrors tests/test_project_loop_atomic_real_postgres.py: a throwaway
testcontainers Postgres seeded with the regenerated schema_current.sql (which
carries migration 0159's job_message_routes).
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
from testcontainers.postgres import PostgresContainer

from orchestrator.database.postgres import PostgresDB
from services import message_routing

SCHEMA_FILE = (
    Path(__file__).resolve().parents[1]
    / "orchestrator"
    / "database"
    / "schema_current.sql"
)

NOW = datetime.now(timezone.utc)


@pytest.fixture(scope="module")
def pg_dsn():
    with PostgresContainer("postgres:15") as postgres:
        yield postgres.get_connection_url().replace("postgresql+psycopg2", "postgresql")


@pytest_asyncio.fixture(scope="module")
async def _schema_applied(pg_dsn):
    conn = await asyncpg.connect(pg_dsn)
    try:
        await conn.execute(SCHEMA_FILE.read_text())
    finally:
        await conn.close()


@pytest_asyncio.fixture
async def db(pg_dsn, _schema_applied, monkeypatch):
    monkeypatch.setenv("EXPERTS_DB_ENABLED", "false")
    store = PostgresDB(
        connection_string=pg_dsn,
        min_connections=1,
        max_connections=8,
    )
    await store.connect()
    async with store.acquire() as conn:
        await conn.execute(
            "TRUNCATE message_delivery_attempts, message_delivery_intents, "
            "job_message_routes, session_wake_events, message_log, "
            "jobs, project_officers, threads, projects, users CASCADE"
        )
    try:
        yield store
    finally:
        await store.close()


async def _seed(db, *, job_status: str = "processing"):
    """Project + commissioned officer thread + post row + one pinned job."""
    project_id = uuid4()
    officer_tid = uuid4()
    job_id = uuid4()
    user_id = uuid4()
    async with db.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, display_name, email) "
            "VALUES ($1, 'Routing Owner', $2)",
            user_id,
            f"routing-{user_id}@example.test",
        )
        await conn.execute(
            "INSERT INTO projects (id, name) VALUES ($1, 'routing proof')",
            project_id,
        )
        await conn.execute(
            """
            INSERT INTO threads (id, project_id, status, metadata)
            VALUES ($1, $2, 'active',
                    '{"config_override": {"officer": {"enabled": true}}}'::jsonb)
            """,
            officer_tid,
            project_id,
        )
        await conn.execute(
            """
            INSERT INTO project_officers (project_id, thread_id,
                                          communication_policy)
            VALUES ($1, $2,
                    '{"worker_messages":"officer_first",
                      "officer_response_minutes":5}'::jsonb)
            """,
            project_id,
            officer_tid,
        )
        await conn.execute(
            """
            INSERT INTO jobs (id, description, status, execution_lane,
                              project_id, user_id, config_name)
            VALUES ($1, 'routing proof job', $2, 'pinned', $3, $4, 'worker_base')
            """,
            job_id,
            job_status,
            project_id,
            user_id,
        )
    return {
        "project_id": str(project_id),
        "officer_thread_id": str(officer_tid),
        "job_id": str(job_id),
        "user_id": str(user_id),
    }


async def _reserve(
    db: PostgresDB,
    seed: dict[str, str],
    generation: str,
    *,
    bucket: str,
    audience: str,
    job_hourly_limit: int = 5,
    internal_job_hourly_limit: int = 30,
) -> dict:
    return await db.reserve_message_delivery_intent(
        routing_generation=generation,
        route_id=generation,
        bucket=bucket,
        effective_audience=audience,
        job_id=seed["job_id"],
        project_id=seed["project_id"],
        user_id=seed["user_id"] if bucket == "human" else None,
        job_hourly_limit=job_hourly_limit,
        internal_job_hourly_limit=internal_job_hourly_limit,
    )


def _route_dict(seed, *, route_id=None, state="pending_officer", **overrides):
    route = {
        "route_id": route_id or str(uuid4()),
        "job_id": seed["job_id"],
        "project_id": seed["project_id"],
        "thread_id": "abc123",
        "policy_snapshot": {
            "worker_messages": "officer_first",
            "applied": "officer_first",
            "officer_response_minutes": 5,
        },
        "state": state,
        "officer_thread_id": seed["officer_thread_id"],
        "officer_incarnation": 0,
        "officer_deadline": NOW - timedelta(minutes=1),
        "total_deadline": NOW + timedelta(hours=24),
        "transitions": [],
    }
    route.update(overrides)
    return route


def _freeze(route):
    return {
        "status": "waiting_for_reply",
        "freeze_type": "blocking_message",
        "thread_id": route["thread_id"],
        "subject": "Need input",
        "job_id": route["job_id"],
        "route_id": route["route_id"],
        "routing": "officer_first",
    }


def _wake(seed, route):
    return {
        "thread_id": seed["officer_thread_id"],
        "source": "worker_message",
        "dedup_key": f"route:{route['route_id']}",
        "payload": {"summary": "blocking question", "route_id": route["route_id"]},
    }


_MESSAGE_ENTRY = {
    "user_id": None,
    "recipient_email": None,
    "subject": "Need input",
    "message": "Which DB should I use?",
    "status": "sent",
}


# =============================================================================
# OC-07 — durable effective-audience quota and delivery identity
# =============================================================================


@pytest.mark.asyncio
async def test_officer_internal_and_human_buckets_are_independent(db):
    seed = await _seed(db)
    internal = await _reserve(
        db,
        seed,
        str(uuid4()),
        bucket="officer_internal",
        audience="officer",
    )
    human = await _reserve(
        db,
        seed,
        str(uuid4()),
        bucket="human",
        audience="human",
    )
    assert internal["allowed"] and human["allowed"]
    async with db.acquire() as conn:
        counts = dict(
            await conn.fetchrow(
                "SELECT count(*) FILTER (WHERE bucket='human') AS human, "
                "count(*) FILTER (WHERE bucket='officer_internal') AS internal "
                "FROM message_delivery_intents"
            )
        )
    assert counts == {"human": 1, "internal": 1}


@pytest.mark.asyncio
async def test_internal_flood_limit_does_not_consume_human_quota(db):
    seed = await _seed(db)
    for _ in range(2):
        assert (
            await _reserve(
                db,
                seed,
                str(uuid4()),
                bucket="officer_internal",
                audience="officer",
                internal_job_hourly_limit=2,
            )
        )["allowed"]
    refused = await _reserve(
        db,
        seed,
        str(uuid4()),
        bucket="officer_internal",
        audience="officer",
        internal_job_hourly_limit=2,
    )
    assert refused["allowed"] is False
    assert refused["limit"] == "internal_job_hourly"
    assert (
        await _reserve(
            db,
            seed,
            str(uuid4()),
            bucket="human",
            audience="human",
            job_hourly_limit=1,
        )
    )["allowed"]


@pytest.mark.asyncio
async def test_concurrent_retries_reserve_and_attempt_once(db):
    seed = await _seed(db)
    generation = str(uuid4())
    reservations = await asyncio.gather(
        *(
            _reserve(
                db,
                seed,
                generation,
                bucket="human",
                audience="officer_and_user",
            )
            for _ in range(8)
        )
    )
    assert all(row["allowed"] for row in reservations)
    assert len({row["intent_id"] for row in reservations}) == 1
    attempts = await asyncio.gather(
        *(
            db.begin_message_delivery_attempt(reservations[0]["intent_id"])
            for _ in range(8)
        )
    )
    assert sum(bool(row["delivery_claimed"]) for row in attempts) == 1
    async with db.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM message_delivery_intents "
                "WHERE routing_generation=$1::uuid AND bucket='human'",
                generation,
            )
            == 1
        )
        assert (
            await conn.fetchval("SELECT count(*) FROM message_delivery_attempts") == 1
        )


@pytest.mark.asyncio
async def test_generation_replay_cannot_cross_job_or_project_authority(db):
    first = await _seed(db)
    second = await _seed(db)
    generation = str(uuid4())
    assert (
        await _reserve(
            db,
            first,
            generation,
            bucket="human",
            audience="human",
        )
    )["allowed"]
    with pytest.raises(ValueError, match="different job/project/user authority"):
        await _reserve(
            db,
            second,
            generation,
            bucket="human",
            audience="human",
        )


@pytest.mark.asyncio
async def test_accepted_delivery_is_sticky_and_suppresses_retry(db):
    seed = await _seed(db)
    intent = await _reserve(
        db,
        seed,
        str(uuid4()),
        bucket="human",
        audience="human",
    )
    attempt = await db.begin_message_delivery_attempt(intent["intent_id"])
    assert attempt["delivery_claimed"]
    assert await db.settle_message_delivery_attempt(
        intent["intent_id"], attempt["attempt_number"], accepted=True
    )
    replay = await db.begin_message_delivery_attempt(intent["intent_id"])
    assert replay == {
        "intent_id": intent["intent_id"],
        "delivery_claimed": False,
        "accepted": True,
    }


@pytest.mark.asyncio
async def test_failed_or_abandoned_attempt_remains_recoverable(db):
    seed = await _seed(db)
    intent = await _reserve(
        db,
        seed,
        str(uuid4()),
        bucket="human",
        audience="human",
    )
    first = await db.begin_message_delivery_attempt(intent["intent_id"])
    assert await db.settle_message_delivery_attempt(
        intent["intent_id"],
        first["attempt_number"],
        accepted=False,
        failure_class="provider_rejected",
    )
    second = await db.begin_message_delivery_attempt(intent["intent_id"])
    assert second["delivery_claimed"]
    assert second["attempt_number"] == 2
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE message_delivery_attempts SET attempted_at=now()-interval '2 minutes' "
            "WHERE intent_id=$1::uuid AND attempt_number=2",
            intent["intent_id"],
        )
    third = await db.begin_message_delivery_attempt(
        intent["intent_id"], attempt_timeout_seconds=60
    )
    assert third["delivery_claimed"]
    assert third["attempt_number"] == 3
    async with db.acquire() as conn:
        abandoned = await conn.fetchval(
            "SELECT failure_class FROM message_delivery_attempts "
            "WHERE intent_id=$1::uuid AND attempt_number=2",
            intent["intent_id"],
        )
    assert abandoned == "attempt_owner_lost"


@pytest.mark.asyncio
async def test_concurrent_escalation_delivers_and_charges_once(db):
    seed = await _seed(db)
    route = _route_dict(
        seed,
        state="escalated_to_user",
        officer_deadline=None,
        routing_generation=str(uuid4()),
        effective_audience="officer_and_user",
    )
    assert await db.create_message_route(route)
    stored = await db.get_message_route(route["route_id"])
    notifier = SimpleNamespace(
        dispatch=AsyncMock(return_value={"email": True, "email_message_id": "<x>"})
    )
    results = await asyncio.gather(
        *(
            message_routing.deliver_route_to_user(
                db, stored, reason="officer_escalated", notifier=notifier
            )
            for _ in range(8)
        )
    )
    assert sum(bool(result) for result in results) == 1
    assert notifier.dispatch.await_count == 1
    async with db.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM message_delivery_intents "
                "WHERE routing_generation=$1::uuid AND bucket='human'",
                route["routing_generation"],
            )
            == 1
        )
        assert (
            await conn.fetchval(
                "SELECT user_delivery_at IS NOT NULL FROM job_message_routes "
                "WHERE route_id=$1::uuid",
                route["route_id"],
            )
            is True
        )


@pytest.mark.asyncio
async def test_routed_blocking_freeze_commits_all_four_rows(db):
    seed = await _seed(db)
    route = _route_dict(seed)
    created = await db.create_routed_blocking_freeze(
        seed["job_id"],
        _freeze(route),
        route=route,
        message_entry=_MESSAGE_ENTRY,
        wake=_wake(seed, route),
        expected_lane="pinned",
    )
    assert created is not None
    assert created["route_id"] == route["route_id"]

    async with db.acquire() as conn:
        job = await conn.fetchrow(
            "SELECT status, freeze_data FROM jobs WHERE id = $1::uuid",
            seed["job_id"],
        )
        stored_route = await conn.fetchrow(
            "SELECT * FROM job_message_routes WHERE route_id = $1::uuid",
            route["route_id"],
        )
        wake = await conn.fetchrow(
            "SELECT * FROM session_wake_events WHERE dedup_key = $1",
            f"route:{route['route_id']}",
        )
        message = await conn.fetchrow(
            "SELECT * FROM message_log WHERE id = $1::uuid",
            created["originating_message_id"],
        )
    assert job["status"] == "waiting_for_reply"
    assert json.loads(job["freeze_data"])["route_id"] == route["route_id"]
    assert stored_route["state"] == "pending_officer"
    assert stored_route["blocking"] is True
    assert str(stored_route["originating_message_id"]) == str(message["id"])
    assert wake["state"] == "pending"
    assert message["mode"] == "blocking"


@pytest.mark.asyncio
async def test_guard_loss_rolls_back_every_row(db):
    """Failure injection: the job is not freezable → the WHOLE unit rolls
    back. No frozen job without a durable route — and no route debris
    without a frozen job."""
    seed = await _seed(db, job_status="paused")
    route = _route_dict(seed)
    created = await db.create_routed_blocking_freeze(
        seed["job_id"],
        _freeze(route),
        route=route,
        message_entry=_MESSAGE_ENTRY,
        wake=_wake(seed, route),
        expected_lane="pinned",
    )
    assert created is None
    async with db.acquire() as conn:
        job = await conn.fetchrow(
            "SELECT status, freeze_data FROM jobs WHERE id = $1::uuid",
            seed["job_id"],
        )
        routes = await conn.fetchval("SELECT COUNT(*) FROM job_message_routes")
        wakes = await conn.fetchval("SELECT COUNT(*) FROM session_wake_events")
        messages = await conn.fetchval("SELECT COUNT(*) FROM message_log")
    assert job["status"] == "paused"
    assert job["freeze_data"] is None
    assert routes == 0
    assert wakes == 0
    assert messages == 0


@pytest.mark.asyncio
async def test_lane_mismatch_rolls_back(db):
    seed = await _seed(db)
    route = _route_dict(seed)
    created = await db.create_routed_blocking_freeze(
        seed["job_id"],
        _freeze(route),
        route=route,
        message_entry=_MESSAGE_ENTRY,
        wake=_wake(seed, route),
        expected_lane="stateless",
        lease_token=7,
    )
    assert created is None
    async with db.acquire() as conn:
        assert await conn.fetchval("SELECT COUNT(*) FROM job_message_routes") == 0


@pytest.mark.asyncio
async def test_resolution_cas_settles_exactly_once_under_race(db):
    """Officer answer vs SLA escalation on one route: exactly one wins."""
    seed = await _seed(db)
    route = _route_dict(seed)
    assert await db.create_message_route({**route, "blocking": True})

    async def _officer_resolve():
        return await db.transition_message_route(
            route["route_id"],
            to_state="resolved_by_officer",
            expected_states=["pending_officer", "pending_both", "escalated_to_user"],
            actor_kind="officer",
            actor_id=seed["officer_thread_id"],
            officer_thread_id=seed["officer_thread_id"],
        )

    async def _escalate():
        return await db.transition_message_route(
            route["route_id"],
            to_state="escalated_to_user",
            expected_states=["pending_officer", "delivery_failed"],
            actor_kind="system",
            note="officer_sla_expired",
        )

    first, second = await asyncio.gather(_officer_resolve(), _escalate())
    winners = [outcome for outcome in (first, second) if outcome is not None]
    # Ordering note: if the escalation wins first, the officer resolve can
    # STILL legitimately win afterwards (escalated_to_user is in its expected
    # set — an officer answering right after escalation is valid). What can
    # never happen is zero winners or a lost-update overwrite.
    assert 1 <= len(winners) <= 2
    final = await db.get_message_route(route["route_id"])
    assert final["state"] in ("resolved_by_officer", "escalated_to_user")
    if final["state"] == "resolved_by_officer":
        assert final["resolved_by_kind"] == "officer"
    # The audit trail recorded every winning transition exactly once.
    assert len(final["transitions"]) == len(winners)


@pytest.mark.asyncio
async def test_two_reconcilers_escalate_a_due_route_once(db):
    """Two concurrent SLA claims (two reconciler replicas): one claims the
    route, the other gets nothing."""
    seed = await _seed(db)
    route = _route_dict(seed)
    assert await db.create_message_route({**route, "blocking": True})

    now = datetime.now(timezone.utc)
    first, second = await asyncio.gather(
        db.claim_officer_sla_escalations(now=now, limit=10),
        db.claim_officer_sla_escalations(now=now, limit=10),
    )
    assert len(first) + len(second) == 1
    final = await db.get_message_route(route["route_id"])
    assert final["state"] == "escalated_to_user"
    assert len(final["transitions"]) == 1


@pytest.mark.asyncio
async def test_answer_vs_total_deadline_unblocks_exactly_once(db):
    """§5.2: a user answer racing the total timeout — the job's status CAS is
    the single unblock authority; both sides observe exactly one winner."""
    seed = await _seed(db)
    route = _route_dict(
        seed,
        state="user_direct",
        officer_deadline=None,
        total_deadline=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    created = await db.create_routed_blocking_freeze(
        seed["job_id"],
        _freeze(route),
        route=route,
        message_entry=_MESSAGE_ENTRY,
        wake=None,
        expected_lane="pinned",
    )
    assert created is not None

    async def _unblock_job() -> bool:
        """The one honest unblock CAS both racers must go through."""
        async with db.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE jobs SET status = 'paused', freeze_data = NULL "
                "WHERE id = $1::uuid AND status = 'waiting_for_reply' "
                "RETURNING id",
                seed["job_id"],
            )
        return row is not None

    async def _answer() -> int:
        if not await _unblock_job():
            return 0
        await db.transition_message_route(
            route["route_id"],
            to_state="resolved_by_user",
            expected_states=[
                "pending_officer",
                "pending_both",
                "user_direct",
                "escalated_to_user",
                "delivery_failed",
            ],
            actor_kind="user",
        )
        return 1

    async def _reconciler() -> int:
        claimed = await db.claim_total_timeout_routes(
            now=datetime.now(timezone.utc), limit=10
        )
        unblocked = 0
        for claimed_route in claimed:
            job = await db.get_job(claimed_route["job_id"])
            freeze = job.get("freeze_data")
            if isinstance(freeze, str):
                freeze = json.loads(freeze)
            if (
                job["status"] == "waiting_for_reply"
                and (freeze or {}).get("route_id") == claimed_route["route_id"]
            ):
                if await _unblock_job():
                    unblocked += 1
        return unblocked

    answer_wins, reconciler_wins = await asyncio.gather(_answer(), _reconciler())
    assert answer_wins + reconciler_wins == 1

    final = await db.get_message_route(route["route_id"])
    assert final["state"] in ("resolved_by_user", "timed_out")
    async with db.acquire() as conn:
        status = await conn.fetchval(
            "SELECT status FROM jobs WHERE id = $1::uuid", seed["job_id"]
        )
    assert status == "paused"


@pytest.mark.asyncio
async def test_stranded_frozen_repair_scan_matches_generation(db):
    """Leg 2b's SQL: a timed_out route whose job is still frozen on ITS
    freeze generation is found; a mismatched generation is not."""
    seed = await _seed(db)
    route = _route_dict(seed, state="user_direct", officer_deadline=None)
    created = await db.create_routed_blocking_freeze(
        seed["job_id"],
        _freeze(route),
        route=route,
        message_entry=_MESSAGE_ENTRY,
        wake=None,
        expected_lane="pinned",
    )
    assert created is not None
    # Simulate the crash: route timed_out, resume never landed.
    updated = await db.transition_message_route(
        route["route_id"],
        to_state="timed_out",
        expected_states=["user_direct"],
        actor_kind="system",
    )
    assert updated is not None
    stranded = await db.list_timed_out_routes_still_frozen(limit=10)
    assert [r["route_id"] for r in stranded] == [route["route_id"]]

    # Re-freeze the job on a NEWER generation — the old route must vanish
    # from the scan.
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE jobs SET freeze_data = $2::jsonb WHERE id = $1::uuid",
            seed["job_id"],
            json.dumps({"thread_id": "zzz", "route_id": str(uuid4())}),
        )
    assert await db.list_timed_out_routes_still_frozen(limit=10) == []


@pytest.mark.asyncio
async def test_stale_pending_officer_scan_requires_missing_wake(db):
    seed = await _seed(db)
    route = _route_dict(seed)
    created = await db.create_routed_blocking_freeze(
        seed["job_id"],
        _freeze(route),
        route=route,
        message_entry=_MESSAGE_ENTRY,
        wake=_wake(seed, route),
        expected_lane="pinned",
    )
    assert created is not None
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE job_message_routes SET created_at = now() - interval '1 hour'"
        )
    # Healthy pending wake row → not stale.
    assert await db.list_stale_pending_officer_routes(older_than_seconds=60) == []
    # Bury the wake → the route is orphaned and must fall back.
    async with db.acquire() as conn:
        await conn.execute("UPDATE session_wake_events SET state = 'dead'")
    stale = await db.list_stale_pending_officer_routes(older_than_seconds=60)
    assert [r["route_id"] for r in stale] == [route["route_id"]]


@pytest.mark.asyncio
async def test_open_route_listing_for_sitrep(db):
    seed = await _seed(db)
    route = _route_dict(seed)
    created = await db.create_routed_blocking_freeze(
        seed["job_id"],
        _freeze(route),
        route=route,
        message_entry=_MESSAGE_ENTRY,
        wake=_wake(seed, route),
        expected_lane="pinned",
    )
    assert created is not None
    open_routes = await db.list_open_worker_message_routes(seed["project_id"])
    assert len(open_routes) == 1
    assert open_routes[0]["route_id"] == route["route_id"]
    assert open_routes[0]["subject"] == "Need input"
    assert open_routes[0]["job_status"] == "waiting_for_reply"
    # Resolve it → the inbox is clean again.
    await db.transition_message_route(
        route["route_id"],
        to_state="resolved_by_officer",
        expected_states=["pending_officer"],
        actor_kind="officer",
    )
    assert await db.list_open_worker_message_routes(seed["project_id"]) == []


# =============================================================================
# OC-04 — the resume CAS is on the route GENERATION, not just the status
# =============================================================================


@pytest.mark.asyncio
async def test_resume_refuses_a_stale_route_generation(db):
    """The ABA race, against real transactional state.

    A job waits on route A, resumes, then waits again on route B. A delayed
    actor for A still sees ``status='waiting_for_reply'`` — the old CAS would
    have let it resume B's wait, unblocking a worker whose question nobody
    answered.
    """
    seed = await _seed(db)
    route_a = _route_dict(seed)
    assert await db.create_routed_blocking_freeze(
        seed["job_id"],
        _freeze(route_a),
        route=route_a,
        message_entry=_MESSAGE_ENTRY,
        expected_lane="pinned",
    )

    # A is answered: the job resumes on its own generation.
    assert await db.queue_job_for_resume(
        seed["job_id"],
        {},
        expected_status="waiting_for_reply",
        expected_route_id=route_a["route_id"],
    )

    # The job asks again and freezes on a NEW route.
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE jobs SET status='processing' WHERE id=$1::uuid", seed["job_id"]
        )
    route_b = _route_dict(seed)
    assert await db.create_routed_blocking_freeze(
        seed["job_id"],
        _freeze(route_b),
        route=route_b,
        message_entry=_MESSAGE_ENTRY,
        expected_lane="pinned",
    )

    # The delayed actor for A arrives. Status matches; the generation does not.
    assert not await db.queue_job_for_resume(
        seed["job_id"],
        {},
        expected_status="waiting_for_reply",
        expected_route_id=route_a["route_id"],
    )

    async with db.acquire() as conn:
        job = await conn.fetchrow(
            "SELECT status, freeze_data FROM jobs WHERE id=$1::uuid", seed["job_id"]
        )
    assert job["status"] == "waiting_for_reply"
    assert json.loads(job["freeze_data"])["route_id"] == route_b["route_id"]

    # B's own actor still wins.
    assert await db.queue_job_for_resume(
        seed["job_id"],
        {},
        expected_status="waiting_for_reply",
        expected_route_id=route_b["route_id"],
    )


@pytest.mark.asyncio
async def test_reply_and_timeout_race_resolves_exactly_once(db):
    """Both actors hold the SAME generation; the database picks one winner."""
    seed = await _seed(db)
    route = _route_dict(seed)
    assert await db.create_routed_blocking_freeze(
        seed["job_id"],
        _freeze(route),
        route=route,
        message_entry=_MESSAGE_ENTRY,
        expected_lane="pinned",
    )

    results = await asyncio.gather(
        db.queue_job_for_resume(
            seed["job_id"],
            {},
            expected_status="waiting_for_reply",
            expected_route_id=route["route_id"],
        ),
        db.queue_job_for_resume(
            seed["job_id"],
            {},
            expected_status="waiting_for_reply",
            expected_route_id=route["route_id"],
        ),
    )
    assert sum(1 for r in results if r) == 1, results


@pytest.mark.asyncio
async def test_an_unrouted_freeze_keeps_the_status_only_cas(db):
    """Backwards compatibility: a freeze with no route_id (an ordinary pause,
    or a pre-routing job) must still resume on status alone."""
    seed = await _seed(db)
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE jobs SET status='waiting_for_reply', "
            'freeze_data=\'{"type":"blocking_message"}\'::jsonb '
            "WHERE id=$1::uuid",
            seed["job_id"],
        )
    assert await db.queue_job_for_resume(
        seed["job_id"], {}, expected_status="waiting_for_reply"
    )


# =============================================================================
# OC-01 — a direct blocking freeze can never outlive its route
# =============================================================================


@pytest.mark.asyncio
async def test_direct_freeze_and_route_commit_or_neither_does(db):
    """The kill-point property, stated as an invariant.

    Before this, user_direct froze the job and then created its route as
    best-effort bookkeeping. A crash in between left waiting_for_reply with no
    route — and the timeout reconciler claims ROUTES, so nothing would ever
    find it. The job waited forever, holding its backlog ticket claim and pool
    capacity with it.
    """
    seed = await _seed(db)
    route = _route_dict(seed)
    route["state"] = "user_direct"
    route["officer_thread_id"] = None
    route["officer_deadline"] = None

    created = await db.create_routed_blocking_freeze(
        seed["job_id"],
        _freeze(route),
        route=route,
        message_entry=_MESSAGE_ENTRY,
        wake=None,
        expected_lane="pinned",
    )
    assert created is not None

    async with db.acquire() as conn:
        job = await conn.fetchrow(
            "SELECT status, freeze_data FROM jobs WHERE id=$1::uuid", seed["job_id"]
        )
        stored = await conn.fetchrow(
            "SELECT state, blocking, total_deadline FROM job_message_routes "
            "WHERE route_id=$1::uuid",
            route["route_id"],
        )
    assert job["status"] == "waiting_for_reply"
    assert stored is not None, "a freeze without its route is the OC-01 defect"
    assert stored["state"] == "user_direct" and stored["blocking"] is True
    assert stored["total_deadline"] is not None, "no deadline = unrecoverable"
    assert json.loads(job["freeze_data"])["route_id"] == str(route["route_id"])


@pytest.mark.asyncio
async def test_a_lost_guard_leaves_the_job_runnable_and_writes_no_route(db):
    """The other half: if the unit cannot commit, NOTHING is written.

    There is deliberately no compensating 'unfreeze' — the only safe direction
    is to refuse, leaving the job dispatchable.
    """
    seed = await _seed(db)
    async with db.acquire() as conn:
        # Not freezable: the guard the helper checks under the row lock.
        await conn.execute(
            "UPDATE jobs SET status='completed' WHERE id=$1::uuid", seed["job_id"]
        )
    route = _route_dict(seed)
    route["state"] = "user_direct"

    created = await db.create_routed_blocking_freeze(
        seed["job_id"],
        _freeze(route),
        route=route,
        message_entry=_MESSAGE_ENTRY,
        wake=None,
        expected_lane="pinned",
    )
    assert created is None

    async with db.acquire() as conn:
        job = await conn.fetchrow(
            "SELECT status FROM jobs WHERE id=$1::uuid", seed["job_id"]
        )
        orphan = await conn.fetchval(
            "SELECT COUNT(*) FROM job_message_routes WHERE route_id=$1::uuid",
            route["route_id"],
        )
        stray_message = await conn.fetchval(
            "SELECT COUNT(*) FROM message_log WHERE thread_id=$1",
            route["thread_id"],
        )
    assert job["status"] == "completed"
    assert orphan == 0
    assert stray_message == 0, "a rolled-back unit must leave no message either"


@pytest.mark.asyncio
async def test_no_blocking_freeze_exists_without_a_route_row(db):
    """The repair/invariant query OC-01 asks for: any waiting_for_reply job
    whose freeze names a route_id that has no route row is corruption."""
    seed = await _seed(db)
    route = _route_dict(seed)
    route["state"] = "user_direct"
    assert await db.create_routed_blocking_freeze(
        seed["job_id"],
        _freeze(route),
        route=route,
        message_entry=_MESSAGE_ENTRY,
        wake=None,
        expected_lane="pinned",
    )
    async with db.acquire() as conn:
        orphans = await conn.fetchval(
            """
            SELECT COUNT(*) FROM jobs j
             WHERE j.status = 'waiting_for_reply'
               AND j.freeze_data ? 'route_id'
               AND NOT EXISTS (
                     SELECT 1 FROM job_message_routes r
                      WHERE r.route_id = (j.freeze_data->>'route_id')::uuid)
            """
        )
    assert orphans == 0


# =============================================================================
# Terminal auto-close — a dead job's routes leave every "open" surface
# =============================================================================


@pytest.mark.asyncio
async def test_cancelled_job_auto_close_closes_stamps_and_drops_counts(db):
    """The fd9cad regression shape: a blocking route escalated_to_user whose
    job was then cancelled kept showing as an open worker message in every
    sitrep (and its wake intent kept counting as a pending event) with no
    safe closure verb. Auto-close must: stamp it closed, drop it from the
    open listing and the pending wake count, keep the thread history, leave
    settled routes untouched, and be idempotent."""
    seed = await _seed(db, job_status="cancelled")
    route = _route_dict(seed, state="escalated_to_user")
    assert await db.create_message_route(route)
    # The originating worker message — history that must survive the close.
    assert await db.log_message(
        job_id=seed["job_id"],
        thread_id=route["thread_id"],
        direction="outbound",
        subject="[BLOCKER] refspec push REJECTED",
        message="3rd strike — need a human decision",
        status="sent",
        mode="blocking",
    )
    # Still-undelivered officer wake intent for the question.
    assert await db.enqueue_session_wake_event(
        seed["officer_thread_id"],
        source="worker_message",
        dedup_key=f"route:{route['route_id']}",
        payload={"route_id": route["route_id"]},
        project_id=seed["project_id"],
    )
    # An already-settled route on the same job must stay exactly as it is.
    settled = _route_dict(seed, state="resolved_by_user", thread_id="def456")
    assert await db.create_message_route(settled)

    closed = await db.close_message_routes_for_terminal_jobs(seed["job_id"])
    assert [r["route_id"] for r in closed] == [route["route_id"]]
    row = closed[0]
    assert row["state"] == "closed"
    assert row["resolved_by_kind"] == "system"
    assert row["resolved_by_id"] == "job_cancelled"
    assert row["resolved_at"] is not None
    last = row["transitions"][-1]
    assert last["from"] == "escalated_to_user"
    assert last["to"] == "closed"
    assert last["actor_kind"] == "system"
    assert last["note"] == "closed automatically: job cancelled"

    # Out of the officer inbox/sitrep listing ("Worker messages (N open)").
    assert await db.list_open_worker_message_routes(seed["project_id"]) == []
    # Out of the reply lane's open-route lookup (nothing left to act on).
    assert (
        await db.find_message_route_for_thread(
            seed["job_id"], route["thread_id"], open_only=True
        )
        is None
    )
    # The pending wake intent is retired → officer pending-event count drops.
    async with db.acquire() as conn:
        pending = await conn.fetchval(
            "SELECT COUNT(*) FROM session_wake_events "
            "WHERE thread_id = $1::uuid AND state = 'pending'",
            seed["officer_thread_id"],
        )
    assert pending == 0

    # History is preserved: the ledger row (with its stamp) and the thread.
    kept = await db.get_message_route(route["route_id"])
    assert kept["state"] == "closed"
    thread = await db.get_thread_messages(seed["job_id"], route["thread_id"])
    assert thread is not None
    assert thread["messages"][0]["message"].startswith("3rd strike")

    # The settled route was never touched.
    untouched = await db.get_message_route(settled["route_id"])
    assert untouched["state"] == "resolved_by_user"
    assert untouched["transitions"] == []

    # Idempotent: a second terminalization is a no-op.
    assert await db.close_message_routes_for_terminal_jobs(seed["job_id"]) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["completed", "failed"])
async def test_completed_and_failed_jobs_close_with_their_own_stamp(db, status):
    seed = await _seed(db, job_status=status)
    route = _route_dict(seed, state="pending_officer")
    assert await db.create_message_route(route)
    closed = await db.close_message_routes_for_terminal_jobs(seed["job_id"])
    assert len(closed) == 1
    assert closed[0]["resolved_by_id"] == f"job_{status}"
    assert closed[0]["transitions"][-1]["note"] == (
        f"closed automatically: job {status}"
    )
    assert await db.list_open_worker_message_routes(seed["project_id"]) == []


@pytest.mark.asyncio
async def test_backstop_sweep_spares_live_jobs(db):
    """The reconciler's no-job-id sweep closes only terminal jobs' routes; a
    processing job's open question must survive it."""
    dead = await _seed(db, job_status="cancelled")
    dead_route = _route_dict(dead, state="user_direct")
    assert await db.create_message_route(dead_route)
    live = await _seed(db)  # processing
    live_route = _route_dict(live, state="pending_officer")
    assert await db.create_message_route(live_route)

    closed = await db.close_message_routes_for_terminal_jobs()
    assert [r["route_id"] for r in closed] == [dead_route["route_id"]]
    survivors = await db.list_open_worker_message_routes(live["project_id"])
    assert [r["route_id"] for r in survivors] == [live_route["route_id"]]
