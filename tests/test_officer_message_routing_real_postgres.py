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
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
from testcontainers.postgres import PostgresContainer

from orchestrator.database.postgres import PostgresDB

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
            "TRUNCATE job_message_routes, session_wake_events, message_log, "
            "jobs, project_officers, threads, projects CASCADE"
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
    async with db.acquire() as conn:
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
                              project_id, config_name)
            VALUES ($1, 'routing proof job', $2, 'pinned', $3, 'worker_base')
            """,
            job_id,
            job_status,
            project_id,
        )
    return {
        "project_id": str(project_id),
        "officer_thread_id": str(officer_tid),
        "job_id": str(job_id),
    }


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
