"""Real-PostgreSQL proofs for Officer Post admission and handoff authority.

The mocked suites prove endpoint plumbing; this module proves the properties
that depend on PostgreSQL row locks and rollback semantics:

* manual and automatic creates linearize on one stable project_officers row;
* final-slot and ticket-claim races insert exactly one job;
* stale preparation cannot cross hold/config/decommission/recommission;
* predecessor jobs occupy successor capacity through durable lineage; and
* every decommission substep rolls back as one unit, while a committed retry
  is idempotent and exposes durable (not delivered) route fallback intent.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio
from testcontainers.postgres import PostgresContainer

from orchestrator.database.postgres import OfficerPostLifecycleConflict, PostgresDB
from services.officer_admission import (
    OfficerAdmissionConflict,
    SlotAdmissionError,
    admit_and_create_job,
    admit_and_create_job_in_transaction,
    prepare_officer_admission,
)

SCHEMA_FILE = (
    Path(__file__).resolve().parents[1]
    / "orchestrator"
    / "database"
    / "schema_current.sql"
)

DECOMMISSION_STEPS = (
    "post_locked",
    "thread_locked",
    "in_flight_checked",
    "state_harvested",
    "wake_rows_locked",
    "wake_entries_folded",
    "wakes_cleared",
    "routes_fallback_staged",
    "incarnation_appended",
    "post_unlinked",
    "thread_disabled",
)


@pytest.fixture(scope="module")
def pg_dsn():
    try:
        container = PostgresContainer("postgres:15")
        container.start()
    except Exception as exc:
        pytest.skip(f"local Postgres container unavailable: {exc}")
    try:
        yield container.get_connection_url().replace(
            "postgresql+psycopg2", "postgresql"
        )
    finally:
        container.stop()


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
        max_connections=10,
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


def _roster(*, count: int = 1) -> dict:
    return {
        "line": {
            "count": count,
            "category": "executor",
            "model": "MiniMax-M3",
            "backend": "sandbox",
        }
    }


async def _seed_post(
    db: PostgresDB,
    *,
    count: int = 1,
    auto_pull: bool = False,
    post_state: dict | None = None,
    officer_state: dict | None = None,
) -> dict[str, str]:
    project_id = uuid4()
    thread_id = uuid4()
    runtime_officer = {
        "enabled": True,
        "auto_pull": auto_pull,
        "slots": _roster(count=count),
    }
    metadata = {
        "config_override": {"officer": runtime_officer},
        "officer_state": officer_state or {},
    }
    durable_config = {"officer": {"slots": _roster(count=count)}}
    async with db.acquire() as conn:
        await conn.execute(
            "INSERT INTO projects (id, name) VALUES ($1, 'post tx proof')",
            project_id,
        )
        await conn.execute(
            """
            INSERT INTO threads (id, project_id, status, metadata)
            VALUES ($1, $2, 'active', $3::jsonb)
            """,
            thread_id,
            project_id,
            json.dumps(metadata),
        )
        await conn.execute(
            """
            INSERT INTO project_officers (
                project_id, thread_id, config_override, state
            ) VALUES ($1, $2, $3::jsonb, $4::jsonb)
            """,
            project_id,
            thread_id,
            json.dumps(durable_config),
            json.dumps(post_state or {}),
        )
    return {"project_id": str(project_id), "thread_id": str(thread_id)}


async def _seed_successor(db: PostgresDB, seed: dict[str, str], *, count: int = 1):
    thread_id = uuid4()
    metadata = {
        "config_override": {
            "officer": {
                "enabled": True,
                "auto_pull": False,
                "slots": _roster(count=count),
            }
        }
    }
    async with db.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO threads (id, project_id, status, metadata)
            VALUES ($1, $2, 'active', $3::jsonb)
            """,
            thread_id,
            UUID(seed["project_id"]),
            json.dumps(metadata),
        )
    return str(thread_id)


async def _prepare(
    db: PostgresDB,
    seed: dict[str, str],
    *,
    auto_pull: bool = False,
):
    return await prepare_officer_admission(
        db,
        project_id=seed["project_id"],
        thread_id=seed["thread_id"],
        requested_slot="line",
        require_auto_pull=auto_pull,
        expected_category="executor" if auto_pull else None,
    )


def _job_kwargs(label: str) -> dict:
    return {
        "description": label,
        "config_name": "worker_base",
        "config_override": {"autonomy": "full"},
        "context": {},
        "wake_on_complete": True,
    }


async def _job_count(db: PostgresDB) -> int:
    async with db.acquire() as conn:
        return int(await conn.fetchval("SELECT COUNT(*) FROM jobs"))


async def _seed_officer_job(
    db: PostgresDB,
    seed: dict[str, str],
    *,
    thread_id: str,
    status: str,
    label: str,
) -> dict:
    job = await db.create_job(
        description=label,
        project_id=seed["project_id"],
        config_name="worker_base",
        created_by_thread_id=thread_id,
        context={"officer_slot": "line"},
    )
    if status != "created":
        async with db.acquire() as conn:
            await conn.execute(
                "UPDATE jobs SET status=$2 WHERE id=$1", job["id"], status
            )
    return job


async def _race(*calls):
    ready = asyncio.Event()

    async def _run(call):
        await ready.wait()
        try:
            return await call()
        except Exception as exc:  # outcome inspected by each proof
            return exc

    tasks = [asyncio.create_task(_run(call)) for call in calls]
    ready.set()
    return await asyncio.gather(*tasks)


@pytest.mark.asyncio
async def test_two_manual_creates_race_for_one_remaining_slot(db):
    seed = await _seed_post(db, count=1)
    preparation = await _prepare(db, seed)

    outcomes = await _race(
        lambda: admit_and_create_job(
            db,
            preparation=preparation,
            job_kwargs=_job_kwargs("manual A"),
            ticket_note_id="ticket-a",
        ),
        lambda: admit_and_create_job(
            db,
            preparation=preparation,
            job_kwargs=_job_kwargs("manual B"),
            ticket_note_id="ticket-b",
        ),
    )

    assert sum(isinstance(outcome, dict) for outcome in outcomes) == 1
    assert sum(isinstance(outcome, SlotAdmissionError) for outcome in outcomes) == 1
    assert await _job_count(db) == 1


@pytest.mark.asyncio
async def test_same_ticket_manual_manual_race_is_one_job_and_normal_conflict(db):
    seed = await _seed_post(db, count=2)
    preparation = await _prepare(db, seed)

    outcomes = await _race(
        *(
            lambda label=label: admit_and_create_job(
                db,
                preparation=preparation,
                job_kwargs=_job_kwargs(label),
                ticket_note_id="same-ticket",
            )
            for label in ("manual A", "manual B")
        )
    )

    conflicts = [o for o in outcomes if isinstance(o, OfficerAdmissionConflict)]
    assert sum(isinstance(outcome, dict) for outcome in outcomes) == 1
    assert len(conflicts) == 1
    assert conflicts[0].code == "ticket_claimed"
    assert await _job_count(db) == 1


@pytest.mark.asyncio
async def test_same_ticket_manual_tick_race_is_one_job_and_normal_conflict(db):
    # auto_pull=true exists only in this isolated fixture; production remains
    # release-blocked with the control disabled.
    seed = await _seed_post(db, count=2, auto_pull=True)
    manual = await _prepare(db, seed)
    tick = await _prepare(db, seed, auto_pull=True)
    ready_at = datetime.now(timezone.utc) - timedelta(seconds=1)

    outcomes = await _race(
        lambda: admit_and_create_job(
            db,
            preparation=manual,
            job_kwargs=_job_kwargs("manual"),
            ticket_note_id="manual-tick",
        ),
        lambda: admit_and_create_job(
            db,
            preparation=tick,
            job_kwargs=_job_kwargs("tick"),
            ticket_note_id="manual-tick",
            ticket_ready_at=ready_at,
        ),
    )

    conflicts = [o for o in outcomes if isinstance(o, OfficerAdmissionConflict)]
    assert sum(isinstance(outcome, dict) for outcome in outcomes) == 1
    assert len(conflicts) == 1
    assert conflicts[0].code == "ticket_claimed"
    assert await _job_count(db) == 1


@pytest.mark.asyncio
async def test_backlog_query_and_admission_reject_enabled_thread_not_holding_post(db):
    seed = await _seed_post(db)
    orphan_id = await _seed_successor(db, seed)

    commissioned = await db.list_commissioned_officer_posts_for_backlog()
    assert [str(row["id"]) for row in commissioned] == [seed["thread_id"]]
    with pytest.raises(OfficerAdmissionConflict) as exc:
        await prepare_officer_admission(
            db,
            project_id=seed["project_id"],
            thread_id=orphan_id,
            requested_slot="line",
            require_auto_pull=False,
        )
    assert exc.value.code == "stale_incarnation"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    ["hold", "disable", "roster", "decommission", "recommission"],
)
async def test_final_boundary_rejects_lifecycle_or_roster_change(db, mutation):
    seed = await _seed_post(db)
    preparation = await _prepare(db, seed)

    if mutation == "hold":
        await db.set_project_officer_hold(
            seed["project_id"],
            expected_thread_id=seed["thread_id"],
            hold={"kind": "maintenance", "since": "now", "note": "proof"},
        )
    elif mutation == "disable":
        await db.update_project_officer_post(
            seed["project_id"],
            config_updates={"officer": {"enabled": False}},
        )
    elif mutation == "roster":
        await db.update_project_officer_post(
            seed["project_id"],
            config_updates={"officer": {"slots": _roster(count=0)}},
        )
    else:
        await db.decommission_project_officer(
            seed["project_id"], seed["thread_id"], reason="proof"
        )
        if mutation == "recommission":
            successor = await _seed_successor(db, seed)
            assert await db.register_project_officer_thread(
                seed["project_id"], successor
            )

    with pytest.raises((OfficerAdmissionConflict, SlotAdmissionError)):
        await admit_and_create_job(
            db,
            preparation=preparation,
            job_kwargs=_job_kwargs(mutation),
            ticket_note_id=f"ticket-{mutation}",
        )
    assert await _job_count(db) == 0


@pytest.mark.asyncio
async def test_predecessor_job_occupies_successor_lineage_capacity(db):
    seed = await _seed_post(db, count=1)
    old_preparation = await _prepare(db, seed)
    await admit_and_create_job(
        db,
        preparation=old_preparation,
        job_kwargs=_job_kwargs("predecessor work"),
        ticket_note_id="predecessor",
    )
    await db.decommission_project_officer(
        seed["project_id"], seed["thread_id"], reason="rotate", force=True
    )
    successor = await _seed_successor(db, seed, count=1)
    assert await db.register_project_officer_thread(seed["project_id"], successor)
    successor_seed = {**seed, "thread_id": successor}
    new_preparation = await _prepare(db, successor_seed)

    with pytest.raises(SlotAdmissionError):
        await admit_and_create_job(
            db,
            preparation=new_preparation,
            job_kwargs=_job_kwargs("successor work"),
            ticket_note_id="successor",
        )
    assert await _job_count(db) == 1


@pytest.mark.asyncio
async def test_ordinary_job_insert_does_not_wait_for_officer_post_lock(db):
    seed = await _seed_post(db)
    async with db.acquire() as lock_conn:
        transaction = lock_conn.transaction()
        await transaction.start()
        try:
            await lock_conn.fetchrow(
                "SELECT project_id FROM project_officers "
                "WHERE project_id=$1 FOR UPDATE",
                UUID(seed["project_id"]),
            )
            result = await asyncio.wait_for(
                db.create_job(
                    description="ordinary session work",
                    project_id=seed["project_id"],
                    config_name="worker_base",
                ),
                timeout=1.0,
            )
        finally:
            await transaction.rollback()
    assert result["id"]


@pytest.mark.asyncio
async def test_admission_wins_then_no_force_decommission_warns_and_keeps_post(db):
    seed = await _seed_post(db, count=2)
    preparation = await _prepare(db, seed)
    inserted = asyncio.Event()
    allow_admission_commit = asyncio.Event()

    async def hold_admission_open():
        async with db.acquire() as conn:
            async with conn.transaction():
                job = await admit_and_create_job_in_transaction(
                    db,
                    conn,
                    preparation=preparation,
                    job_kwargs=_job_kwargs("admission wins"),
                    ticket_note_id="race-admission-wins",
                )
                inserted.set()
                await allow_admission_commit.wait()
                return job

    admission_task = asyncio.create_task(hold_admission_open())
    await asyncio.wait_for(inserted.wait(), timeout=2)
    handoff_task = asyncio.create_task(
        db.decommission_project_officer(
            seed["project_id"], seed["thread_id"], reason="race", force=False
        )
    )
    await asyncio.sleep(0.1)
    assert not handoff_task.done()

    allow_admission_commit.set()
    job, handoff = await asyncio.gather(admission_task, handoff_task)

    assert job["id"]
    assert handoff["transitioned"] is False
    assert handoff["blocked_by_in_flight"] is True
    assert [item["job_id"] for item in handoff["in_flight_jobs"]] == [str(job["id"])]
    post = await db.get_project_officer(seed["project_id"])
    thread = await db.get_thread(seed["thread_id"])
    assert str(post["thread_id"]) == seed["thread_id"]
    assert thread["status"] == "active"


@pytest.mark.asyncio
async def test_no_force_decommission_wins_then_racing_admission_is_rejected(db):
    seed = await _seed_post(db, count=1)
    preparation = await _prepare(db, seed)
    post_locked = asyncio.Event()
    allow_handoff = asyncio.Event()

    async def pause_after_post_lock(step):
        if step == "post_locked":
            post_locked.set()
            await allow_handoff.wait()

    handoff_task = asyncio.create_task(
        db.decommission_project_officer(
            seed["project_id"],
            seed["thread_id"],
            reason="race",
            force=False,
            fault_injector=pause_after_post_lock,
        )
    )
    await asyncio.wait_for(post_locked.wait(), timeout=2)
    admission_task = asyncio.create_task(
        admit_and_create_job(
            db,
            preparation=preparation,
            job_kwargs=_job_kwargs("handoff wins"),
            ticket_note_id="race-handoff-wins",
        )
    )
    await asyncio.sleep(0.1)
    assert not admission_task.done()

    allow_handoff.set()
    handoff, admission = await asyncio.gather(
        handoff_task, admission_task, return_exceptions=True
    )

    assert handoff["transitioned"] is True
    assert isinstance(admission, OfficerAdmissionConflict)
    assert admission.code in {"post_vacant", "stale_incarnation"}
    assert await _job_count(db) == 0


@pytest.mark.asyncio
async def test_no_force_gate_counts_every_nonterminal_status_across_lineage(db):
    seed = await _seed_post(db)
    predecessor = await _seed_successor(db, seed)
    async with db.acquire() as conn:
        await conn.execute(
            """
            UPDATE project_officers
               SET incarnations = jsonb_build_array(
                   jsonb_build_object('thread_id', $2::text))
             WHERE project_id = $1
            """,
            UUID(seed["project_id"]),
            predecessor,
        )

    nonterminal = (
        "created",
        "processing",
        "pending_review",
        "paused",
        "reviewing",
        "waiting",
        "waiting_for_reply",
    )
    for index, status in enumerate(nonterminal):
        await _seed_officer_job(
            db,
            seed,
            thread_id=predecessor if index % 2 else seed["thread_id"],
            status=status,
            label=f"lineage {status}",
        )
    for status in ("completed", "failed", "cancelled"):
        await _seed_officer_job(
            db,
            seed,
            thread_id=predecessor,
            status=status,
            label=f"terminal {status}",
        )

    handoff = await db.decommission_project_officer(
        seed["project_id"], seed["thread_id"], reason="lineage gate"
    )

    assert handoff["blocked_by_in_flight"] is True
    assert {item["status"] for item in handoff["in_flight_jobs"]} == set(nonterminal)
    assert (
        str((await db.get_project_officer(seed["project_id"]))["thread_id"])
        == seed["thread_id"]
    )


async def _seed_decommission_debris(db: PostgresDB, seed: dict[str, str]) -> str:
    job = await db.create_job(
        description="route owner",
        project_id=seed["project_id"],
        config_name="worker_base",
    )
    await db.enqueue_session_wake_event(
        seed["thread_id"],
        source="job_transition",
        dedup_key="job-transition-proof",
        payload={
            "job_id": str(job["id"]),
            "status": "completed",
            "description": "finished while retiring",
        },
        project_id=seed["project_id"],
    )
    await db.enqueue_session_wake_event(
        seed["thread_id"],
        source="timer",
        dedup_key="timer-proof",
        payload={"reason": "routine"},
        project_id=seed["project_id"],
    )
    route_id = str(uuid4())
    assert await db.create_message_route(
        {
            "route_id": route_id,
            "job_id": str(job["id"]),
            "project_id": seed["project_id"],
            "thread_id": "worker-thread",
            "policy_snapshot": {"applied": "officer_first"},
            "state": "pending_officer",
            "blocking": True,
            "officer_thread_id": seed["thread_id"],
            "officer_incarnation": 0,
            "officer_deadline": datetime.now(timezone.utc),
            "total_deadline": datetime.now(timezone.utc) + timedelta(hours=1),
            "transitions": [],
        }
    )
    return route_id


def _json(value):
    return json.loads(value) if isinstance(value, str) else value


@pytest.mark.asyncio
@pytest.mark.parametrize("fault_step", DECOMMISSION_STEPS)
async def test_decommission_fault_after_each_substep_rolls_back(db, fault_step):
    initial_post_state = {"standing": "before"}
    seed = await _seed_post(
        db,
        post_state=initial_post_state,
        officer_state={"digest": [{"subject": "harvest me"}]},
    )
    route_id = await _seed_decommission_debris(db, seed)

    def inject(step):
        if step == fault_step:
            raise RuntimeError(f"fault after {step}")

    with pytest.raises(RuntimeError, match=f"fault after {fault_step}"):
        await db.decommission_project_officer(
            seed["project_id"],
            seed["thread_id"],
            reason="fault proof",
            fault_injector=inject,
        )

    async with db.acquire() as conn:
        post = await conn.fetchrow(
            "SELECT thread_id, state, incarnations FROM project_officers "
            "WHERE project_id=$1",
            UUID(seed["project_id"]),
        )
        thread = await conn.fetchrow(
            "SELECT status, metadata FROM threads WHERE id=$1",
            UUID(seed["thread_id"]),
        )
        wakes = await conn.fetchval(
            "SELECT COUNT(*) FROM session_wake_events WHERE thread_id=$1",
            UUID(seed["thread_id"]),
        )
        route = await conn.fetchrow(
            "SELECT state, transitions, user_delivery_at "
            "FROM job_message_routes WHERE route_id=$1",
            UUID(route_id),
        )
    assert str(post["thread_id"]) == seed["thread_id"]
    assert _json(post["state"]) == initial_post_state
    assert _json(post["incarnations"]) == []
    assert thread["status"] == "active"
    assert _json(thread["metadata"])["config_override"]["officer"]["enabled"]
    assert wakes == 2
    assert route["state"] == "pending_officer"
    assert _json(route["transitions"]) == []
    assert route["user_delivery_at"] is None


@pytest.mark.asyncio
async def test_repeated_decommission_is_idempotent_and_stages_one_fallback(db):
    seed = await _seed_post(
        db,
        post_state={"standing": "before"},
        officer_state={"digest": [{"subject": "harvest me"}]},
    )
    route_id = await _seed_decommission_debris(db, seed)

    first = await db.decommission_project_officer(
        seed["project_id"], seed["thread_id"], reason="retired"
    )
    second = await db.decommission_project_officer(
        seed["project_id"], seed["thread_id"], reason="retired"
    )

    assert first["transitioned"] is True
    assert len(first["routes"]) == 1
    assert first["routes"][0]["user_delivery_at"] is None
    assert second["transitioned"] is False
    assert second["already_decommissioned"] is True
    assert second["routes"] == []
    async with db.acquire() as conn:
        post = await conn.fetchrow(
            "SELECT thread_id, state, incarnations FROM project_officers "
            "WHERE project_id=$1",
            UUID(seed["project_id"]),
        )
        thread = await conn.fetchrow(
            "SELECT status, metadata FROM threads WHERE id=$1",
            UUID(seed["thread_id"]),
        )
        wakes = await conn.fetchval(
            "SELECT COUNT(*) FROM session_wake_events WHERE thread_id=$1",
            UUID(seed["thread_id"]),
        )
        route = await conn.fetchrow(
            "SELECT state, transitions, user_delivery_at "
            "FROM job_message_routes WHERE route_id=$1",
            UUID(route_id),
        )
    state = _json(post["state"])
    incarnations = _json(post["incarnations"])
    assert post["thread_id"] is None
    assert state["standing"] == "before"
    assert state["digest"] == [{"subject": "harvest me"}]
    assert len(state["while_vacant"]) == 1
    assert len(incarnations) == 1
    assert incarnations[0]["thread_id"] == seed["thread_id"]
    assert wakes == 0
    assert route["state"] == "escalated_to_user"
    assert len(_json(route["transitions"])) == 1
    # The lifecycle transaction created durable outbox intent only. No
    # notification provider ran inside it, so acceptance remains unstamped.
    assert route["user_delivery_at"] is None
    assert thread["status"] == "ended"
    assert not _json(thread["metadata"])["config_override"]["officer"]["enabled"]


@pytest.mark.asyncio
async def test_direct_end_of_orphan_does_not_touch_occupied_post_debris(db):
    seed = await _seed_post(
        db,
        post_state={"standing": "keep"},
        officer_state={"digest": [{"subject": "current only"}]},
    )
    orphan = await _seed_successor(db, seed)
    route_id = await _seed_decommission_debris(db, seed)
    assert await db.enqueue_session_wake_event(
        orphan,
        source="job_transition",
        dedup_key="orphan-wake",
        payload={"job_id": "orphan-job", "status": "completed"},
        project_id=seed["project_id"],
    )

    retired = await db.decommission_project_officer(
        seed["project_id"],
        orphan,
        reason="direct_end",
        allow_orphan_retirement=True,
    )

    assert retired["transitioned"] is False
    assert retired["orphan_retired"] is True
    assert retired["current_thread_id"] == seed["thread_id"]
    async with db.acquire() as conn:
        post = await conn.fetchrow(
            "SELECT thread_id, state, incarnations FROM project_officers "
            "WHERE project_id=$1",
            UUID(seed["project_id"]),
        )
        current = await conn.fetchrow(
            "SELECT status, metadata FROM threads WHERE id=$1",
            UUID(seed["thread_id"]),
        )
        ended_orphan = await conn.fetchrow(
            "SELECT status, metadata FROM threads WHERE id=$1", UUID(orphan)
        )
        wake_count = await conn.fetchval(
            "SELECT COUNT(*) FROM session_wake_events WHERE thread_id=ANY($1::uuid[])",
            [UUID(seed["thread_id"]), UUID(orphan)],
        )
        route = await conn.fetchrow(
            "SELECT state, transitions FROM job_message_routes WHERE route_id=$1",
            UUID(route_id),
        )
    assert str(post["thread_id"]) == seed["thread_id"]
    assert _json(post["state"]) == {"standing": "keep"}
    assert _json(post["incarnations"]) == []
    assert current["status"] == "active"
    assert _json(current["metadata"])["config_override"]["officer"]["enabled"]
    assert ended_orphan["status"] == "ended"
    assert not _json(ended_orphan["metadata"])["config_override"]["officer"]["enabled"]
    assert wake_count == 3
    assert route["state"] == "pending_officer"
    assert _json(route["transitions"]) == []


@pytest.mark.asyncio
async def test_orphan_end_wins_vacant_post_race_and_registration_loses(db):
    seed = await _seed_post(db)
    await db.decommission_project_officer(
        seed["project_id"], seed["thread_id"], reason="vacate"
    )
    orphan = await _seed_successor(db, seed)
    assert await db.enqueue_session_wake_event(
        orphan,
        source="job_transition",
        dedup_key="preserve-orphan-wake",
        payload={"job_id": "orphan-job", "status": "completed"},
        project_id=seed["project_id"],
    )
    post_locked = asyncio.Event()
    allow_end = asyncio.Event()

    async def pause_after_post_lock(step):
        if step == "post_locked":
            post_locked.set()
            await allow_end.wait()

    end_task = asyncio.create_task(
        db.decommission_project_officer(
            seed["project_id"],
            orphan,
            reason="direct_end",
            allow_orphan_retirement=True,
            fault_injector=pause_after_post_lock,
        )
    )
    await asyncio.wait_for(post_locked.wait(), timeout=2)
    registration_task = asyncio.create_task(
        db.register_project_officer_thread(seed["project_id"], orphan)
    )
    await asyncio.sleep(0.1)
    assert not registration_task.done()

    allow_end.set()
    ended, registration = await asyncio.gather(end_task, registration_task)

    assert ended["orphan_retired"] is True
    assert registration is None
    post = await db.get_project_officer(seed["project_id"])
    thread = await db.get_thread(orphan)
    async with db.acquire() as conn:
        wake_count = await conn.fetchval(
            "SELECT COUNT(*) FROM session_wake_events WHERE thread_id=$1",
            UUID(orphan),
        )
    assert post["thread_id"] is None
    assert len(post["incarnations"]) == 1
    assert post["incarnations"][0]["thread_id"] == seed["thread_id"]
    assert thread["status"] == "ended"
    assert wake_count == 1


@pytest.mark.asyncio
async def test_commission_continuity_racing_decommission_is_preserved_and_truthful(db):
    seed = await _seed_post(db, post_state={"sitrep_fingerprints": {"old-job": "fp"}})
    await db.decommission_project_officer(
        seed["project_id"], seed["thread_id"], reason="vacate"
    )
    continuity_entry = {
        "job_id": "completed-while-vacant",
        "status": "completed",
        "description": "carry me",
        "at": datetime.now(timezone.utc).isoformat(),
    }
    await db.append_project_officer_while_vacant(seed["project_id"], [continuity_entry])
    successor = await _seed_successor(db, seed)
    post_linked = asyncio.Event()
    allow_continuity = asyncio.Event()

    async def pause_after_link(step):
        if step == "post_linked":
            post_linked.set()
            await allow_continuity.wait()

    registration_task = asyncio.create_task(
        db.register_project_officer_thread(
            seed["project_id"],
            successor,
            commission_continuity=True,
            fault_injector=pause_after_link,
        )
    )
    await asyncio.wait_for(post_linked.wait(), timeout=2)
    handoff_task = asyncio.create_task(
        db.decommission_project_officer(
            seed["project_id"], successor, reason="racing handoff", force=True
        )
    )
    await asyncio.sleep(0.1)
    assert not handoff_task.done()

    allow_continuity.set()
    registration, handoff = await asyncio.gather(registration_task, handoff_task)

    continuity = registration["commission_continuity"]
    assert continuity["brief_enqueued"] is True
    assert continuity["while_vacant"] == [continuity_entry]
    assert handoff["transitioned"] is True
    assert (
        await db.confirm_project_officer_incarnation(seed["project_id"], successor)
        is False
    )
    post = await db.get_project_officer(seed["project_id"])
    thread = await db.get_thread(successor)
    async with db.acquire() as conn:
        wake_count = await conn.fetchval(
            "SELECT COUNT(*) FROM session_wake_events WHERE thread_id=$1",
            UUID(successor),
        )
    assert post["thread_id"] is None
    assert post["state"]["while_vacant"] == [continuity_entry]
    assert post["state"]["sitrep_fingerprints"] == {"old-job": "fp"}
    assert thread["status"] == "ended"
    assert wake_count == 0


@pytest.mark.asyncio
async def test_completion_vs_commission_event_appears_exactly_once(db):
    seed = await _seed_post(db)
    await db.decommission_project_officer(
        seed["project_id"], seed["thread_id"], reason="vacate"
    )
    successor = await _seed_successor(db, seed)

    outcomes = await _race(
        lambda: db.route_project_officer_job_transition(
            seed["project_id"],
            job_id="race-job",
            status="completed",
            description="completion race",
            dedup_key="race-job:completed",
        ),
        lambda: db.register_project_officer_thread(
            seed["project_id"], successor, commission_continuity=True
        ),
    )

    assert all(isinstance(outcome, dict) for outcome in outcomes)
    post = await db.get_project_officer(seed["project_id"])
    assert str(post["thread_id"]) == successor
    assert post["state"].get("while_vacant") == []
    async with db.acquire() as conn:
        rows = await conn.fetch(
            "SELECT source, payload FROM session_wake_events "
            "WHERE thread_id=$1 ORDER BY id",
            UUID(successor),
        )
    occurrences = 0
    for row in rows:
        payload = _json(row["payload"])
        if row["source"] == "commission":
            occurrences += sum(
                1
                for entry in payload.get("while_vacant") or []
                if entry.get("job_id") == "race-job"
                and entry.get("status") == "completed"
            )
        elif row["source"] == "job_transition":
            occurrences += int(
                payload.get("job_id") == "race-job"
                and payload.get("status") == "completed"
            )
    assert occurrences == 1
    assert {row["source"] for row in rows} in (
        {"commission"},
        {"commission", "job_transition"},
    )


@pytest.mark.asyncio
async def test_losing_commission_cannot_patch_winning_officer_configuration(db):
    seed = await _seed_post(db)
    await db.decommission_project_officer(
        seed["project_id"], seed["thread_id"], reason="vacate"
    )
    initial = await db.get_project_officer(seed["project_id"])
    generation = initial["updated_at"]
    winning = await db.update_project_officer_post(
        seed["project_id"],
        config_updates={"officer": {"slots": _roster(count=2)}},
        expected_vacant_updated_at=generation,
    )
    winning_config = winning["post"]["config_override"]
    winner = await _seed_successor(db, seed, count=2)
    linked = asyncio.Event()
    allow_registration = asyncio.Event()

    async def pause_after_link(step):
        if step == "post_linked":
            linked.set()
            await allow_registration.wait()

    registration_task = asyncio.create_task(
        db.register_project_officer_thread(
            seed["project_id"],
            winner,
            expected_post_config_override=winning_config,
            fault_injector=pause_after_link,
        )
    )
    await asyncio.wait_for(linked.wait(), timeout=2)
    losing_update_task = asyncio.create_task(
        db.update_project_officer_post(
            seed["project_id"],
            config_updates={"officer": {"slots": _roster(count=9)}},
            expected_vacant_updated_at=generation,
        )
    )
    await asyncio.sleep(0.1)
    assert not losing_update_task.done()

    allow_registration.set()
    registration, losing_update = await asyncio.gather(
        registration_task, losing_update_task, return_exceptions=True
    )

    assert registration is not None
    assert isinstance(losing_update, OfficerPostLifecycleConflict)
    assert losing_update.code == "commission_generation_changed"
    post = await db.get_project_officer(seed["project_id"])
    thread = await db.get_thread(winner)
    assert post["config_override"]["officer"]["slots"]["line"]["count"] == 2
    assert (
        _json(thread["metadata"])["config_override"]["officer"]["slots"]["line"][
            "count"
        ]
        == 2
    )


@pytest.mark.asyncio
async def test_concurrent_decommission_blocks_successor_until_handoff_commit(db):
    seed = await _seed_post(db)
    successor = await _seed_successor(db, seed)
    post_locked = asyncio.Event()
    allow_commit = asyncio.Event()

    async def pause_after_post_lock(step):
        if step == "post_locked":
            post_locked.set()
            await allow_commit.wait()

    handoff_task = asyncio.create_task(
        db.decommission_project_officer(
            seed["project_id"],
            seed["thread_id"],
            reason="rotate",
            fault_injector=pause_after_post_lock,
        )
    )
    await asyncio.wait_for(post_locked.wait(), timeout=2)
    register_task = asyncio.create_task(
        db.register_project_officer_thread(seed["project_id"], successor)
    )
    await asyncio.sleep(0.1)
    assert not register_task.done()

    allow_commit.set()
    handoff, registration = await asyncio.gather(handoff_task, register_task)
    assert handoff["transitioned"] is True
    assert registration is not None
    post = await db.get_project_officer(seed["project_id"])
    assert str(post["thread_id"]) == successor
    assert len(post["incarnations"]) == 1
    assert post["incarnations"][0]["thread_id"] == seed["thread_id"]


@pytest.mark.asyncio
async def test_blocking_route_snapshot_cannot_land_after_decommission(db):
    """The route/freeze unit shares the post lock prefix with handoff.

    Once decommission owns the post, a stale officer-routing snapshot waits;
    after the handoff commits it loses cleanly and leaves no route, wake,
    message, or frozen job behind.
    """
    seed = await _seed_post(db)
    job = await db.create_job(
        description="worker awaiting route",
        project_id=seed["project_id"],
        config_name="worker_base",
    )
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE jobs SET status='processing' WHERE id=$1",
            job["id"],
        )

    post_locked = asyncio.Event()
    allow_commit = asyncio.Event()

    async def pause_after_post_lock(step):
        if step == "post_locked":
            post_locked.set()
            await allow_commit.wait()

    handoff_task = asyncio.create_task(
        db.decommission_project_officer(
            seed["project_id"],
            seed["thread_id"],
            reason="route race",
            fault_injector=pause_after_post_lock,
        )
    )
    await asyncio.wait_for(post_locked.wait(), timeout=2)

    route_id = str(uuid4())
    route_task = asyncio.create_task(
        db.create_routed_blocking_freeze(
            str(job["id"]),
            {
                "status": "waiting_for_reply",
                "freeze_type": "blocking_message",
                "thread_id": "worker-thread",
                "route_id": route_id,
            },
            route={
                "route_id": route_id,
                "job_id": str(job["id"]),
                "project_id": seed["project_id"],
                "thread_id": "worker-thread",
                "policy_snapshot": {"applied": "officer_first"},
                "state": "pending_officer",
                "officer_thread_id": seed["thread_id"],
                "officer_incarnation": 0,
                "officer_deadline": datetime.now(timezone.utc),
                "total_deadline": datetime.now(timezone.utc) + timedelta(hours=1),
                "transitions": [],
            },
            message_entry={
                "subject": "Need input",
                "message": "Which path?",
                "status": "sent",
            },
            wake={
                "thread_id": seed["thread_id"],
                "source": "worker_message",
                "dedup_key": f"route:{route_id}",
                "payload": {"route_id": route_id},
            },
            expected_lane="pinned",
        )
    )
    await asyncio.sleep(0.1)
    assert not route_task.done()

    allow_commit.set()
    handoff, routed = await asyncio.gather(handoff_task, route_task)
    assert handoff["transitioned"] is True
    assert routed is None
    async with db.acquire() as conn:
        stored_job = await conn.fetchrow(
            "SELECT status, freeze_data FROM jobs WHERE id=$1", job["id"]
        )
        routes = await conn.fetchval("SELECT COUNT(*) FROM job_message_routes")
        wakes = await conn.fetchval("SELECT COUNT(*) FROM session_wake_events")
        messages = await conn.fetchval("SELECT COUNT(*) FROM message_log")
    assert stored_job["status"] == "processing"
    assert stored_job["freeze_data"] is None
    assert routes == wakes == messages == 0
