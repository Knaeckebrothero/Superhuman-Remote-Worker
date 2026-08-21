"""Real-PostgreSQL authority and race proofs for persistent pod recycling."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio
from testcontainers.postgres import PostgresContainer

from orchestrator.database.postgres import PostgresDB
from orchestrator.security import crypto
from orchestrator.services import runtime_actor
from orchestrator.services.persistent_provisioner import (
    PersistentPodCreateResult,
    PersistentPodCreateStatus,
)
from orchestrator.services.persistent_recycler import (
    PersistentPodObservation,
    PersistentThreadRecycler,
)
from src.shared.persistent_input_delivery import (
    InputDeliveryAuthorityLost,
    claim_pending_input_deliveries,
    lock_runtime_authority,
    mark_input_delivery_queued,
    persist_input_delivery,
    transition_input_delivery,
)

SCHEMA_FILE = (
    Path(__file__).resolve().parents[1]
    / "orchestrator"
    / "database"
    / "schema_current.sql"
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
    monkeypatch.setenv("APP_ENCRYPTION_KEY", "P" * 32)
    crypto.reset_cipher_cache()
    store = PostgresDB(
        connection_string=pg_dsn,
        min_connections=1,
        max_connections=10,
    )
    await store.connect()
    async with store.acquire() as conn:
        await conn.execute(
            "TRUNCATE runtime_actor_access_tokens, runtime_actor_grants, "
            "runtime_actor_bootstraps, job_message_routes, session_wake_events, "
            "project_officers, threads, agents, project_members, projects, "
            "users CASCADE"
        )
    try:
        yield store
    finally:
        await store.close()
        crypto.reset_cipher_cache()


class FakeProvisioner:
    expected_build_sha = "new-build"
    image_ref = "example.test/agent:sha-new-build"
    is_available = True

    def __init__(self):
        self.current: dict | None = None
        self.create_calls = 0
        self.created_targets: list[str | None] = []
        self.deleted_uids: list[str] = []
        self.fail_creates = False

    async def get_pod_status(self, thread_id: str):
        return dict(self.current) if self.current else None

    async def delete_agent_pod_exact(self, thread_id: str, *, expected_pod_uid: str):
        self.deleted_uids.append(expected_pod_uid)
        if self.current and self.current.get("pod_uid") == expected_pod_uid:
            self.current = None
        return True

    async def create_agent_pod(
        self,
        thread_id: str,
        *,
        config_name: str,
        lifecycle_generation: str,
        target_image_ref: str | None = None,
    ):
        self.create_calls += 1
        self.created_targets.append(target_image_ref)
        if self.fail_creates:
            return PersistentPodCreateResult(
                PersistentPodCreateStatus.FAILED,
                f"persistent-{thread_id[:12]}",
                failure_class="injected_create_failure",
            )
        build = (
            target_image_ref.rsplit(":sha-", 1)[-1]
            if target_image_ref and ":sha-" in target_image_ref
            else self.expected_build_sha
        )
        uid = f"replacement-{lifecycle_generation[:8]}"
        self.current = _pod_status(
            thread_id,
            uid=uid,
            build=build,
            generation=lifecycle_generation,
            ready=False,
        )
        return PersistentPodCreateResult(
            PersistentPodCreateStatus.CREATED,
            f"persistent-{thread_id[:12]}",
            pod_uid=uid,
            build_sha=build,
        )


def _pod_status(
    thread_id: str,
    *,
    uid: str,
    build: str,
    generation: str | None = None,
    ready: bool = True,
):
    labels = {
        "srw/component": "persistent-agent",
        "srw/thread-id": thread_id,
        "srw/build-sha": build,
    }
    if generation:
        labels["srw/recycle-generation"] = generation
    return {
        "thread_id": thread_id,
        "pod_name": f"persistent-{thread_id[:12]}",
        "pod_uid": uid,
        "build_sha": build,
        "phase": "Running",
        "ready": ready,
        "terminating": False,
        "labels": labels,
    }


async def _seed(db: PostgresDB, *, preexisting_hold: dict | None = None):
    ids = {key: str(uuid4()) for key in ("user", "project", "thread", "agent")}
    metadata = {
        "config_override": {"officer": {"enabled": True, "hold": preexisting_hold}},
        "agent_pod": {
            "pod_name": f"persistent-{ids['thread'][:12]}",
            "pod_uid": "old-pod",
            "observed_build_sha": "old-build",
        },
    }
    async with db.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, display_name, email) VALUES ($1, 'owner', $2)",
            UUID(ids["user"]),
            f"{ids['user']}@example.test",
        )
        await conn.execute(
            "INSERT INTO projects (id, name) VALUES ($1, 'recycle proof')",
            UUID(ids["project"]),
        )
        await conn.execute(
            "INSERT INTO project_members (project_id,user_id,role) "
            "VALUES ($1,$2,'owner')",
            UUID(ids["project"]),
            UUID(ids["user"]),
        )
        await conn.execute(
            "INSERT INTO threads "
            "(id,user_id,project_id,status,execution_lane,config_name,metadata) "
            "VALUES ($1,$2,$3,'active','pinned','centurion',$4::jsonb)",
            UUID(ids["thread"]),
            UUID(ids["user"]),
            UUID(ids["project"]),
            json.dumps(metadata),
        )
        await conn.execute(
            "INSERT INTO agents "
            "(id,config_name,hostname,pod_ip,pod_uid,status,agent_mode,thread_id,last_heartbeat) "
            "VALUES ($1,'centurion',$2,'127.0.0.1','old-pod','session','persistent',$3,now())",
            UUID(ids["agent"]),
            f"persistent-{ids['thread'][:12]}",
            UUID(ids["thread"]),
        )
        await conn.execute(
            "UPDATE threads SET agent_id=$2 WHERE id=$1",
            UUID(ids["thread"]),
            UUID(ids["agent"]),
        )
        await conn.execute(
            "INSERT INTO project_officers (project_id,thread_id) VALUES ($1,$2)",
            UUID(ids["project"]),
            UUID(ids["thread"]),
        )
    actor = await runtime_actor.mint_thread_runtime_actor(
        db, thread_id=ids["thread"], agent_id=ids["agent"]
    )
    ids["old_access"] = actor.access_credential
    return ids


async def _recycle_state(db: PostgresDB, thread_id: str):
    row = await db.get_thread(thread_id)
    metadata = row["metadata"]
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    return metadata["agent_pod"]["recycle"], metadata


async def _bind_replacement_agent(
    db: PostgresDB, *, thread_id: str, pod_uid: str
) -> tuple[str, runtime_actor.RuntimeActorContext]:
    agent_id = str(uuid4())
    async with db.acquire() as conn:
        await conn.execute(
            "INSERT INTO agents "
            "(id,config_name,hostname,pod_ip,pod_uid,status,agent_mode,thread_id,last_heartbeat) "
            "VALUES ($1,'centurion',$2,'127.0.0.2',$3,'session','persistent',$4,now())",
            UUID(agent_id),
            f"persistent-{thread_id[:12]}",
            pod_uid,
            UUID(thread_id),
        )
        await conn.execute(
            "UPDATE threads SET agent_id=$2,status='active' WHERE id=$1",
            UUID(thread_id),
            UUID(agent_id),
        )
    actor = await runtime_actor.mint_thread_runtime_actor(
        db, thread_id=thread_id, agent_id=agent_id
    )
    return agent_id, actor


@pytest.mark.asyncio
async def test_turn_boundary_recycle_preserves_thread_and_replaces_authority(db):
    ids = await _seed(db)
    assert await db.enqueue_session_wake_event(
        ids["thread"],
        source="legate",
        dedup_key="recycle-continuity",
        payload={"message": "survives pod replacement"},
        project_id=ids["project"],
    )
    provisioner = FakeProvisioner()
    provisioner.current = _pod_status(ids["thread"], uid="old-pod", build="old-build")
    recycler = PersistentThreadRecycler(db=db, provisioner=provisioner)
    old = PersistentPodObservation.from_status(ids["thread"], provisioner.current)

    requested = await recycler.request_and_reconcile(
        thread_id=ids["thread"],
        reason="image_drift",
        expected_build_sha="new-build",
        observation=old,
        expected_project_id=ids["project"],
    )
    assert requested.phase == "awaiting_old_pod_exit"
    state, metadata = await _recycle_state(db, ids["thread"])
    hold = metadata["config_override"]["officer"]["hold"]
    assert hold["kind"] == "maintenance"
    assert "thread_id" not in hold
    assert state["hold_owned"] is True
    acknowledgement = await recycler.acknowledge_parked_boundary(
        thread_id=ids["thread"], agent_id=None
    )
    assert acknowledgement.acknowledged is True

    # The exact old object has disappeared after the runtime's clean exit.
    provisioner.current = None
    await recycler.request_and_reconcile(
        thread_id=ids["thread"],
        reason="image_drift",
        expected_build_sha="new-build",
        expected_project_id=ids["project"],
    )
    state, _ = await _recycle_state(db, ids["thread"])
    assert state["phase"] == "awaiting_replacement"
    assert provisioner.create_calls == 1

    successor = str(uuid4())
    new_uid = provisioner.current["pod_uid"]
    async with db.acquire() as conn:
        await conn.execute(
            "INSERT INTO agents "
            "(id,config_name,hostname,pod_ip,pod_uid,status,agent_mode,thread_id,last_heartbeat) "
            "VALUES ($1,'centurion',$2,'127.0.0.2',$3,'session','persistent',$4,now())",
            UUID(successor),
            f"persistent-{ids['thread'][:12]}",
            new_uid,
            UUID(ids["thread"]),
        )
        await conn.execute(
            "UPDATE threads SET agent_id=$2,status='active' WHERE id=$1",
            UUID(ids["thread"]),
            UUID(successor),
        )
    successor_actor = await runtime_actor.mint_thread_runtime_actor(
        db, thread_id=ids["thread"], agent_id=successor
    )
    generation = state["generation"]
    provisioner.current = _pod_status(
        ids["thread"],
        uid=new_uid,
        build="new-build",
        generation=generation,
        ready=True,
    )
    completed = await recycler.request_and_reconcile(
        thread_id=ids["thread"],
        reason="image_drift",
        expected_build_sha="new-build",
        expected_project_id=ids["project"],
    )
    assert completed.phase == "complete"
    row = await db.get_thread(ids["thread"])
    assert str(row["agent_id"]) == successor
    _, metadata = await _recycle_state(db, ids["thread"])
    assert metadata["config_override"]["officer"]["hold"] is None
    assert metadata["agent_pod"]["pod_uid"] == new_uid

    with pytest.raises(Exception):
        await runtime_actor._actor_for_access(db, ids["old_access"])
    current = await runtime_actor._actor_for_access(
        db, successor_actor.access_credential
    )
    assert current.thread_id == ids["thread"]
    async with db.acquire() as conn:
        live_grants = await conn.fetchval(
            "SELECT count(*) FROM runtime_actor_grants "
            "WHERE thread_id=$1 AND agent_id=$2 AND revoked_at IS NULL",
            UUID(ids["thread"]),
            UUID(successor),
        )
        post_thread = await conn.fetchval(
            "SELECT thread_id FROM project_officers WHERE project_id=$1",
            UUID(ids["project"]),
        )
        pending_wakes = await conn.fetchval(
            "SELECT count(*) FROM session_wake_events "
            "WHERE thread_id=$1 AND dedup_key='recycle-continuity'",
            UUID(ids["thread"]),
        )
    assert live_grants == 1
    assert str(post_thread) == ids["thread"]
    assert pending_wakes == 1


@pytest.mark.asyncio
async def test_concurrent_missing_pod_recovery_uses_one_generation_and_create(db):
    ids = await _seed(db)
    provisioner = FakeProvisioner()
    recycler = PersistentThreadRecycler(db=db, provisioner=provisioner)

    results = await asyncio.gather(
        *(
            recycler.request_and_reconcile(
                thread_id=ids["thread"],
                reason="missing_pod",
                expected_build_sha="new-build",
                expected_project_id=ids["project"],
            )
            for _ in range(4)
        )
    )
    state, _ = await _recycle_state(db, ids["thread"])
    assert len({r.generation for r in results if r.generation}) == 1
    assert state["phase"] == "awaiting_replacement"
    assert provisioner.create_calls == 1


@pytest.mark.asyncio
async def test_raw_delete_wake_rejection_survives_hold_and_replacement(db):
    """The observed ordering: wake claim precedes the 60s lifecycle tick.

    The terminating runtime refuses that first delivery, so the outbox row is
    released rather than stamped sent.  Missing-pod reconciliation then owns a
    maintenance hold; after exact replacement authority is healthy, the same
    durable delivery id is claimed and can be settled once.
    """

    ids = await _seed(db)
    assert await db.enqueue_session_wake_event(
        ids["thread"],
        source="timer",
        dedup_key="timer",
        payload={"minutes": 30, "reason": "raw deletion race"},
        project_id=ids["project"],
    )

    claimed = await db.claim_pending_session_wake_events(
        debounce_seconds_by_source={"timer": 0}
    )
    assert len(claimed) == 1
    first = await db.assign_session_wake_delivery_groups([int(claimed[0]["id"])])
    assert len(first) == 1
    delivery_id = first[0]["delivery_id"]
    await db.release_session_wake_events([int(first[0]["id"])])

    provisioner = FakeProvisioner()
    recycler = PersistentThreadRecycler(db=db, provisioner=provisioner)
    missing = await recycler.request_and_reconcile(
        thread_id=ids["thread"],
        reason="missing_pod",
        expected_build_sha="new-build",
        expected_project_id=ids["project"],
    )
    assert missing.phase == "awaiting_replacement"
    state, metadata = await _recycle_state(db, ids["thread"])
    hold = metadata["config_override"]["officer"]["hold"]
    assert hold["kind"] == "maintenance"
    assert "thread_id" not in hold
    assert (
        await db.claim_pending_session_wake_events(
            debounce_seconds_by_source={"timer": 0}
        )
        == []
    )

    new_uid = provisioner.current["pod_uid"]
    successor, actor = await _bind_replacement_agent(
        db, thread_id=ids["thread"], pod_uid=new_uid
    )
    provisioner.current = _pod_status(
        ids["thread"],
        uid=new_uid,
        build="new-build",
        generation=state["generation"],
        ready=True,
    )
    completed = await recycler.request_and_reconcile(
        thread_id=ids["thread"],
        reason="missing_pod",
        expected_build_sha="new-build",
        expected_project_id=ids["project"],
    )
    assert completed.phase == "complete"
    assert str((await db.get_thread(ids["thread"]))["agent_id"]) == successor
    recovered_actor = await runtime_actor._actor_for_access(db, actor.access_credential)
    assert recovered_actor.thread_id == ids["thread"]
    assert recovered_actor.caller_kind == "officer"

    retried = await db.claim_pending_session_wake_events(
        debounce_seconds_by_source={"timer": 0}
    )
    assert len(retried) == 1
    second = await db.assign_session_wake_delivery_groups([int(retried[0]["id"])])
    assert [row["delivery_id"] for row in second] == [delivery_id]
    runtime_generation = uuid4()
    async with db.acquire() as conn:
        async with conn.transaction():
            delivery = await persist_input_delivery(
                conn,
                thread_id=ids["thread"],
                delivery_id=delivery_id,
                role="event",
                content="replacement executes the retained wake",
                source="officer_wake",
                turn_number=1,
                agent_id=successor,
                pod_uid=new_uid,
                runtime_generation=runtime_generation,
            )
            assert await mark_input_delivery_queued(
                conn,
                delivery_id=delivery_id,
                agent_id=successor,
                pod_uid=new_uid,
                runtime_generation=runtime_generation,
                claim_generation=int(delivery["claim_generation"]),
            )
            assert await transition_input_delivery(
                conn,
                delivery_id=delivery_id,
                agent_id=successor,
                pod_uid=new_uid,
                runtime_generation=runtime_generation,
                claim_generation=int(delivery["claim_generation"]),
                transition="admitted",
                turn_number=1,
            )
    await db.finish_session_wake_events([int(second[0]["id"])])
    assert (
        await db.claim_pending_session_wake_events(
            debounce_seconds_by_source={"timer": 0}
        )
        == []
    )


@pytest.mark.asyncio
async def test_concurrent_delivery_identity_and_transcript_accept_are_once(db):
    ids = await _seed(db)
    for key in ("first", "second"):
        assert await db.enqueue_session_wake_event(
            ids["thread"],
            source="test",
            dedup_key=key,
            payload={"summary": key},
            project_id=ids["project"],
        )

    left, right = await asyncio.gather(
        db.claim_pending_session_wake_events(),
        db.claim_pending_session_wake_events(),
    )
    claimed = [*left, *right]
    assert len(claimed) == 2
    claimed_ids = [int(row["id"]) for row in claimed]

    assigned_a, assigned_b = await asyncio.gather(
        db.assign_session_wake_delivery_groups(claimed_ids),
        db.assign_session_wake_delivery_groups(claimed_ids),
    )
    delivery_ids_a = {row["delivery_id"] for row in assigned_a}
    delivery_ids_b = {row["delivery_id"] for row in assigned_b}
    assert len(delivery_ids_a) == 1
    assert delivery_ids_a == delivery_ids_b
    delivery_id = next(iter(delivery_ids_a))
    assert (
        len(await db.get_session_wake_delivery_group(ids["thread"], delivery_id)) == 2
    )

    runtime_generation = uuid4()

    async def persist_once():
        async with db.acquire() as conn:
            async with conn.transaction():
                return await persist_input_delivery(
                    conn,
                    thread_id=ids["thread"],
                    delivery_id=delivery_id,
                    role="event",
                    content="same accepted wake",
                    source="officer_wake",
                    turn_number=1,
                    agent_id=ids["agent"],
                    pod_uid="old-pod",
                    runtime_generation=runtime_generation,
                )

    first, retry = await asyncio.gather(persist_once(), persist_once())
    assert sorted((first["transcript_inserted"], retry["transcript_inserted"])) == [
        False,
        True,
    ]
    assert first["claim_generation"] == retry["claim_generation"] == 1
    async with db.acquire() as conn:
        async with conn.transaction():
            rerendered = await persist_input_delivery(
                conn,
                thread_id=ids["thread"],
                delivery_id=delivery_id,
                role="event",
                content="newer sitrep text must not replace accepted input",
                source="officer_wake",
                turn_number=2,
                agent_id=ids["agent"],
                pod_uid="old-pod",
                runtime_generation=runtime_generation,
            )
    assert rerendered["content"] == "same accepted wake"
    async with db.acquire() as conn:
        count = await conn.fetchval(
            "SELECT count(*) FROM thread_messages "
            "WHERE thread_id=$1 AND role='event' "
            "AND content='same accepted wake'",
            UUID(ids["thread"]),
        )
    assert count == 1


@pytest.mark.asyncio
async def test_input_persist_crash_successor_reclaim_and_stale_owner_fence(db):
    """INSERT-without-queue is reclaimable; the predecessor cannot settle it."""

    ids = await _seed(db)
    delivery_id = uuid4()
    old_runtime = uuid4()
    new_runtime = uuid4()

    async with db.acquire() as conn:
        async with conn.transaction():
            first = await persist_input_delivery(
                conn,
                thread_id=ids["thread"],
                delivery_id=delivery_id,
                role="event",
                content="durable before process death",
                source="officer_wake",
                turn_number=1,
                agent_id=ids["agent"],
                pod_uid="old-pod",
                runtime_generation=old_runtime,
            )
    assert first["state"] == "owned"
    assert first["transcript_inserted"] is True

    async with db.acquire() as conn:
        async with conn.transaction():
            reclaimed = await claim_pending_input_deliveries(
                conn,
                thread_id=ids["thread"],
                agent_id=ids["agent"],
                pod_uid="old-pod",
                runtime_generation=new_runtime,
            )
    assert len(reclaimed) == 1
    assert int(reclaimed[0]["claim_generation"]) == 2

    async with db.acquire() as conn:
        async with conn.transaction():
            assert not await transition_input_delivery(
                conn,
                delivery_id=delivery_id,
                agent_id=ids["agent"],
                pod_uid="old-pod",
                runtime_generation=old_runtime,
                claim_generation=1,
                transition="settled",
            )
            assert await mark_input_delivery_queued(
                conn,
                delivery_id=delivery_id,
                agent_id=ids["agent"],
                pod_uid="old-pod",
                runtime_generation=new_runtime,
                claim_generation=2,
            )
            assert await transition_input_delivery(
                conn,
                delivery_id=delivery_id,
                agent_id=ids["agent"],
                pod_uid="old-pod",
                runtime_generation=new_runtime,
                claim_generation=2,
                transition="admitted",
                turn_number=1,
            )
            assert await transition_input_delivery(
                conn,
                delivery_id=delivery_id,
                agent_id=ids["agent"],
                pod_uid="old-pod",
                runtime_generation=new_runtime,
                claim_generation=2,
                transition="settled",
            )

    async with db.acquire() as conn:
        counts = await conn.fetchrow(
            "SELECT count(*) AS total, count(*) FILTER (WHERE state='settled') "
            "AS settled FROM thread_input_deliveries WHERE delivery_id=$1",
            delivery_id,
        )
        transcript = await conn.fetchval(
            "SELECT count(*) FROM thread_messages message JOIN "
            "thread_input_deliveries delivery ON delivery.message_id=message.id "
            "WHERE delivery.delivery_id=$1",
            delivery_id,
        )
    assert dict(counts) == {"total": 1, "settled": 1}
    assert transcript == 1


@pytest.mark.asyncio
async def test_wake_outbox_refuses_transcript_only_then_accepts_admission(db):
    ids = await _seed(db)
    assert await db.enqueue_session_wake_event(
        ids["thread"],
        source="timer",
        dedup_key="execution-boundary",
        payload={"minutes": 30},
        project_id=ids["project"],
    )
    claimed = await db.claim_pending_session_wake_events(
        debounce_seconds_by_source={"timer": 0}
    )
    assigned = await db.assign_session_wake_delivery_groups([int(claimed[0]["id"])])
    delivery_id = assigned[0]["delivery_id"]
    runtime_generation = uuid4()
    async with db.acquire() as conn:
        async with conn.transaction():
            delivery = await persist_input_delivery(
                conn,
                thread_id=ids["thread"],
                delivery_id=delivery_id,
                role="event",
                content="timer wake",
                source="officer_wake",
                turn_number=1,
                agent_id=ids["agent"],
                pod_uid="old-pod",
                runtime_generation=runtime_generation,
            )

    with pytest.raises(asyncpg.CheckViolationError):
        await db.finish_session_wake_events([int(assigned[0]["id"])])

    async with db.acquire() as conn:
        async with conn.transaction():
            assert await mark_input_delivery_queued(
                conn,
                delivery_id=delivery_id,
                agent_id=ids["agent"],
                pod_uid="old-pod",
                runtime_generation=runtime_generation,
                claim_generation=int(delivery["claim_generation"]),
            )
            assert await transition_input_delivery(
                conn,
                delivery_id=delivery_id,
                agent_id=ids["agent"],
                pod_uid="old-pod",
                runtime_generation=runtime_generation,
                claim_generation=int(delivery["claim_generation"]),
                transition="admitted",
                turn_number=1,
            )
    await db.finish_session_wake_events([int(assigned[0]["id"])])
    async with db.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT state FROM session_wake_events WHERE id=$1",
                int(assigned[0]["id"]),
            )
            == "sent"
        )


@pytest.mark.asyncio
async def test_job_wake_outbox_requires_durable_provider_admission(db):
    ids = await _seed(db)
    thread_id, agent_id, job_id = uuid4(), uuid4(), uuid4()
    async with db.acquire() as conn:
        await conn.execute(
            "INSERT INTO threads "
            "(id,user_id,project_id,status,execution_lane,config_name,metadata) "
            "VALUES ($1,$2,$3,'active','pinned','base','{}'::jsonb)",
            thread_id,
            UUID(ids["user"]),
            UUID(ids["project"]),
        )
        await conn.execute(
            "INSERT INTO agents "
            "(id,config_name,hostname,pod_ip,pod_uid,status,agent_mode,thread_id) "
            "VALUES ($1,'base',$2,'127.0.0.3','plain-pod','session',"
            "'persistent',$3)",
            agent_id,
            f"persistent-{str(thread_id)[:12]}",
            thread_id,
        )
        await conn.execute(
            "UPDATE threads SET agent_id=$2 WHERE id=$1", thread_id, agent_id
        )
        await conn.execute(
            "INSERT INTO jobs "
            "(id,description,status,user_id,project_id,created_by_thread_id,"
            "wake_on_complete,wake_state) "
            "VALUES ($1,'execution boundary','completed',$2,$3,$4,true,'pending')",
            job_id,
            UUID(ids["user"]),
            UUID(ids["project"]),
            thread_id,
        )

    claimed = [row for row in await db.claim_pending_job_wakes() if row["id"] == job_id]
    assert len(claimed) == 1
    with pytest.raises(asyncpg.CheckViolationError):
        await db.finish_job_wake(str(job_id), "completed")

    async with db.acquire() as conn:
        delivery_id = await conn.fetchval(
            "SELECT wake_delivery_id FROM jobs WHERE id=$1", job_id
        )
        async with conn.transaction():
            delivery = await persist_input_delivery(
                conn,
                thread_id=thread_id,
                delivery_id=delivery_id,
                role="event",
                content="execute this wake once",
                source="officer_wake",
                turn_number=1,
                agent_id=agent_id,
                pod_uid="plain-pod",
                runtime_generation=uuid4(),
            )
            runtime_generation = delivery["owner_runtime_generation"]
            assert await mark_input_delivery_queued(
                conn,
                delivery_id=delivery_id,
                agent_id=agent_id,
                pod_uid="plain-pod",
                runtime_generation=runtime_generation,
                claim_generation=int(delivery["claim_generation"]),
            )
            assert await transition_input_delivery(
                conn,
                delivery_id=delivery_id,
                agent_id=agent_id,
                pod_uid="plain-pod",
                runtime_generation=runtime_generation,
                claim_generation=int(delivery["claim_generation"]),
                transition="admitted",
                turn_number=1,
            )
    assert await db.finish_job_wake(str(job_id), "completed") is True


@pytest.mark.asyncio
async def test_pre_0174_claimers_fail_before_session_or_job_network_delivery(db):
    """The mixed-version fence is tied to each claim attempt, not old state."""

    ids = await _seed(db)
    assert await db.enqueue_session_wake_event(
        ids["thread"],
        source="timer",
        dedup_key="rolling-fence",
        payload={"minutes": 30},
        project_id=ids["project"],
    )
    async with db.acquire() as conn:
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "UPDATE session_wake_events SET state='sending', "
                "claimed_at=now(), attempts=attempts+1 "
                "WHERE thread_id=$1 AND dedup_key='rolling-fence'",
                UUID(ids["thread"]),
            )
        event_state = await conn.fetchrow(
            "SELECT state, attempts FROM session_wake_events "
            "WHERE thread_id=$1 AND dedup_key='rolling-fence'",
            UUID(ids["thread"]),
        )
    assert dict(event_state) == {"state": "pending", "attempts": 0}

    claimed_events = await db.claim_pending_session_wake_events(
        debounce_seconds_by_source={"timer": 0}
    )
    assert len(claimed_events) == 1
    claimed_payload = claimed_events[0]["payload"]
    if isinstance(claimed_payload, str):
        claimed_payload = json.loads(claimed_payload)
    assert claimed_payload["_delivery_claim_attempt"] == 1
    UUID(claimed_payload["_delivery_id"])

    job_id = uuid4()
    async with db.acquire() as conn:
        await conn.execute(
            "INSERT INTO jobs "
            "(id,description,status,user_id,project_id,created_by_thread_id,"
            "wake_on_complete,wake_state) "
            "VALUES ($1,'rolling claim','completed',$2,$3,$4,true,'pending')",
            job_id,
            UUID(ids["user"]),
            UUID(ids["project"]),
            UUID(ids["thread"]),
        )
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "UPDATE jobs SET wake_state='sending', wake_claimed_at=now(), "
                "wake_attempts=wake_attempts+1 WHERE id=$1",
                job_id,
            )
        job_state = await conn.fetchrow(
            "SELECT wake_state, wake_attempts FROM jobs WHERE id=$1", job_id
        )
    assert dict(job_state) == {"wake_state": "pending", "wake_attempts": 0}

    claimed_jobs = [
        row for row in await db.claim_pending_job_wakes() if row["id"] == job_id
    ]
    assert len(claimed_jobs) == 1
    async with db.acquire() as conn:
        job_claim = await conn.fetchrow(
            "SELECT wake_delivery_id, wake_delivery_claim_attempt, wake_attempts "
            "FROM jobs WHERE id=$1",
            job_id,
        )
    assert job_claim["wake_delivery_id"] is not None
    assert job_claim["wake_delivery_claim_attempt"] == job_claim["wake_attempts"] == 1


@pytest.mark.asyncio
async def test_replacement_binding_steals_once_and_old_agent_cannot_mutate(db):
    ids = await _seed(db)
    delivery_id = uuid4()
    old_runtime = uuid4()
    async with db.acquire() as conn:
        async with conn.transaction():
            await persist_input_delivery(
                conn,
                thread_id=ids["thread"],
                delivery_id=delivery_id,
                role="human",
                content="retain direct input",
                source="direct_human",
                turn_number=1,
                agent_id=ids["agent"],
                pod_uid="old-pod",
                runtime_generation=old_runtime,
            )

    successor, _actor = await _bind_replacement_agent(
        db, thread_id=ids["thread"], pod_uid="new-pod"
    )
    successor_runtime = uuid4()
    async with db.acquire() as conn:
        async with conn.transaction():
            rows = await claim_pending_input_deliveries(
                conn,
                thread_id=ids["thread"],
                agent_id=successor,
                pod_uid="new-pod",
                runtime_generation=successor_runtime,
            )
    assert len(rows) == 1
    assert int(rows[0]["claim_generation"]) == 2

    async with db.acquire() as conn:
        async with conn.transaction():
            with pytest.raises(InputDeliveryAuthorityLost):
                await lock_runtime_authority(
                    conn,
                    thread_id=ids["thread"],
                    agent_id=ids["agent"],
                    pod_uid="old-pod",
                )


@pytest.mark.asyncio
async def test_generic_session_pod_is_not_misclassified_as_missing_legacy_pod(db):
    ids = await _seed(db)
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agents SET hostname='srw-agent-s-generic' WHERE id=$1",
            UUID(ids["agent"]),
        )
    provisioner = FakeProvisioner()
    recycler = PersistentThreadRecycler(db=db, provisioner=provisioner)

    result = await recycler.request_and_reconcile(
        thread_id=ids["thread"],
        reason="operator_requested",
        expected_build_sha="new-build",
        expected_project_id=ids["project"],
    )

    assert result.state == "blocked"
    assert result.failure_class == "unsupported_pod_authority"
    assert provisioner.create_calls == 0
    assert str((await db.get_thread(ids["thread"]))["agent_id"]) == ids["agent"]


@pytest.mark.asyncio
async def test_preexisting_maintenance_hold_is_never_claimed_or_cleared(db):
    original = {
        "kind": "maintenance",
        "since": "2026-08-20T00:00:00+00:00",
        "note": "operator maintenance",
    }
    ids = await _seed(db, preexisting_hold=original)
    provisioner = FakeProvisioner()
    provisioner.current = _pod_status(ids["thread"], uid="old-pod", build="old-build")
    recycler = PersistentThreadRecycler(db=db, provisioner=provisioner)
    await recycler.request_and_reconcile(
        thread_id=ids["thread"],
        reason="image_drift",
        expected_build_sha="new-build",
        expected_project_id=ids["project"],
    )
    state, metadata = await _recycle_state(db, ids["thread"])
    assert state["hold_owned"] is False
    assert state["preexisting_hold"] is True
    assert metadata["config_override"]["officer"]["hold"] == original


@pytest.mark.asyncio
async def test_conference_hold_blocks_recycle_without_mutating_its_authority(db):
    conference_thread = str(uuid4())
    original = {
        "kind": "conference",
        "thread_id": conference_thread,
        "since": "2026-08-20T00:00:00+00:00",
    }
    ids = await _seed(db, preexisting_hold=original)
    provisioner = FakeProvisioner()
    provisioner.current = _pod_status(ids["thread"], uid="old-pod", build="old-build")
    recycler = PersistentThreadRecycler(db=db, provisioner=provisioner)

    result = await recycler.request_and_reconcile(
        thread_id=ids["thread"],
        reason="image_drift",
        expected_build_sha="new-build",
        expected_project_id=ids["project"],
    )

    assert result.state == "blocked"
    assert result.failure_class == "conference_hold"
    row = await db.get_thread(ids["thread"])
    metadata = row["metadata"]
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    assert metadata["config_override"]["officer"]["hold"] == original
    assert "recycle" not in metadata["agent_pod"]
    assert provisioner.create_calls == 0


@pytest.mark.asyncio
async def test_retryable_failure_keeps_hold_and_pages_once_before_convergence(db):
    ids = await _seed(db)
    provisioner = FakeProvisioner()
    provisioner.fail_creates = True
    pages: list[tuple[str, str, str]] = []

    async def notify(project_id: str, thread_id: str, failure_class: str):
        pages.append((project_id, thread_id, failure_class))
        return True

    recycler = PersistentThreadRecycler(
        db=db, provisioner=provisioner, failure_notifier=notify
    )
    failed = await recycler.request_and_reconcile(
        thread_id=ids["thread"],
        reason="missing_pod",
        expected_build_sha="new-build",
        expected_project_id=ids["project"],
    )
    assert failed.phase == "failed_retryable"
    assert pages == [(ids["project"], ids["thread"], "injected_create_failure")]
    _, metadata = await _recycle_state(db, ids["thread"])
    assert metadata["config_override"]["officer"]["hold"]["kind"] == "maintenance"

    # Immediate replicas respect backoff and do not page or create again.
    await asyncio.gather(
        *(
            recycler.request_and_reconcile(
                thread_id=ids["thread"],
                reason="missing_pod",
                expected_build_sha="new-build",
                expected_project_id=ids["project"],
            )
            for _ in range(3)
        )
    )
    assert provisioner.create_calls == 1
    assert len(pages) == 1

    async with db.acquire() as conn:
        await conn.execute(
            """
            UPDATE threads
               SET metadata = jsonb_set(
                   metadata,
                   '{agent_pod,recycle,next_retry_at}',
                   to_jsonb((now() - interval '1 minute')::text))
             WHERE id=$1
            """,
            UUID(ids["thread"]),
        )
    provisioner.fail_creates = False
    retried = await recycler.request_and_reconcile(
        thread_id=ids["thread"],
        reason="missing_pod",
        expected_build_sha="new-build",
        expected_project_id=ids["project"],
    )
    assert retried.phase == "awaiting_replacement"
    assert provisioner.create_calls == 2
    assert len(pages) == 1


@pytest.mark.asyncio
async def test_unsettled_old_runtime_times_out_without_forced_deletion(db):
    ids = await _seed(db)
    provisioner = FakeProvisioner()
    provisioner.current = _pod_status(ids["thread"], uid="old-pod", build="old-build")
    pages: list[tuple[str, str, str]] = []

    async def notify(project_id: str, thread_id: str, failure_class: str):
        pages.append((project_id, thread_id, failure_class))
        return True

    recycler = PersistentThreadRecycler(
        db=db, provisioner=provisioner, failure_notifier=notify
    )
    await recycler.request_and_reconcile(
        thread_id=ids["thread"],
        reason="image_drift",
        expected_build_sha="new-build",
        expected_project_id=ids["project"],
    )
    async with db.acquire() as conn:
        await conn.execute(
            """
            UPDATE threads
               SET metadata = jsonb_set(
                   metadata,
                   '{agent_pod,recycle,drain_wait_started_at}',
                   to_jsonb((now() - interval '6 minutes')::text))
             WHERE id=$1
            """,
            UUID(ids["thread"]),
        )
    failed = await recycler.request_and_reconcile(
        thread_id=ids["thread"],
        reason="image_drift",
        expected_build_sha="new-build",
        expected_project_id=ids["project"],
    )
    assert failed.phase == "failed_retryable"
    assert failed.failure_class == "drain_boundary_timeout"
    assert provisioner.deleted_uids == []
    assert pages == [(ids["project"], ids["thread"], "drain_boundary_timeout")]
    _, metadata = await _recycle_state(db, ids["thread"])
    assert metadata["config_override"]["officer"]["hold"]["kind"] == "maintenance"


@pytest.mark.asyncio
async def test_reciprocal_uid_mismatch_holds_and_pages_without_mutation(db):
    ids = await _seed(db)
    provisioner = FakeProvisioner()
    provisioner.current = _pod_status(
        ids["thread"], uid="foreign-pod", build="new-build"
    )
    pages: list[tuple[str, str, str]] = []

    async def notify(project_id: str, thread_id: str, failure_class: str):
        pages.append((project_id, thread_id, failure_class))
        return True

    recycler = PersistentThreadRecycler(
        db=db, provisioner=provisioner, failure_notifier=notify
    )
    results = await asyncio.gather(
        *(
            recycler.request_and_reconcile(
                thread_id=ids["thread"],
                reason="authority_mismatch",
                expected_build_sha="new-build",
                expected_project_id=ids["project"],
            )
            for _ in range(3)
        )
    )
    assert {result.phase for result in results} == {"blocked"}
    assert pages == [(ids["project"], ids["thread"], "reciprocal_binding_mismatch")]
    row = await db.get_thread(ids["thread"])
    assert str(row["agent_id"]) == ids["agent"]
    state, metadata = await _recycle_state(db, ids["thread"])
    assert state["last_failure"]["class"] == "reciprocal_binding_mismatch"
    assert metadata["config_override"]["officer"]["hold"]["kind"] == "maintenance"
    assert provisioner.create_calls == 0
    assert provisioner.deleted_uids == []


@pytest.mark.asyncio
async def test_decommission_or_new_incarnation_cannot_be_revived(db):
    ids = await _seed(db)
    provisioner = FakeProvisioner()
    recycler = PersistentThreadRecycler(db=db, provisioner=provisioner)
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE threads SET status='ended' WHERE id=$1",
            UUID(ids["thread"]),
        )
        await conn.execute(
            "UPDATE project_officers SET thread_id=NULL WHERE project_id=$1",
            UUID(ids["project"]),
        )
    ended = await recycler.request_and_reconcile(
        thread_id=ids["thread"],
        reason="missing_pod",
        expected_build_sha="new-build",
        expected_project_id=ids["project"],
    )
    assert ended.state == "cancelled"
    assert provisioner.create_calls == 0

    successor_thread = uuid4()
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE threads SET status='active' WHERE id=$1",
            UUID(ids["thread"]),
        )
        await conn.execute(
            "INSERT INTO threads (id,project_id,status,execution_lane,metadata) "
            "VALUES ($1,$2,'active','pinned',"
            '\'{"config_override":{"officer":{"enabled":true}}}\'::jsonb)',
            successor_thread,
            UUID(ids["project"]),
        )
        await conn.execute(
            "UPDATE project_officers SET thread_id=$2 WHERE project_id=$1",
            UUID(ids["project"]),
            successor_thread,
        )
    stale = await recycler.request_and_reconcile(
        thread_id=ids["thread"],
        reason="missing_pod",
        expected_build_sha="new-build",
        expected_project_id=ids["project"],
    )
    assert stale.state == "cancelled"
    assert provisioner.create_calls == 0


@pytest.mark.asyncio
async def test_headerless_and_asserted_parked_boundary_use_locked_generation(db):
    ids = await _seed(db)
    provisioner = FakeProvisioner()
    provisioner.current = _pod_status(ids["thread"], uid="old-pod", build="old-build")
    recycler = PersistentThreadRecycler(db=db, provisioner=provisioner)
    await recycler.request_and_reconcile(
        thread_id=ids["thread"],
        reason="image_drift",
        expected_build_sha="new-build",
        observation=PersistentPodObservation.from_status(
            ids["thread"], provisioner.current
        ),
        expected_project_id=ids["project"],
    )

    spoofed = await recycler.acknowledge_parked_boundary(
        thread_id=ids["thread"], agent_id=str(uuid4())
    )
    assert spoofed.active_generation is True
    assert spoofed.acknowledged is False
    assert (await db.get_thread(ids["thread"]))["status"] == "active"

    headerless = await recycler.acknowledge_parked_boundary(
        thread_id=ids["thread"], agent_id=None
    )
    assert headerless.acknowledged is True
    assert (await db.get_thread(ids["thread"]))["status"] == "suspended"


@pytest.mark.asyncio
async def test_headerless_ordinary_persistent_generation_needs_no_workspace(db):
    ids = await _seed(db)
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE threads SET metadata = jsonb_set("
            "metadata, '{config_override,officer,enabled}', 'false'::jsonb) "
            "WHERE id=$1",
            UUID(ids["thread"]),
        )
        await conn.execute(
            "DELETE FROM project_officers WHERE project_id=$1",
            UUID(ids["project"]),
        )
    provisioner = FakeProvisioner()
    provisioner.current = _pod_status(ids["thread"], uid="old-pod", build="old-build")
    recycler = PersistentThreadRecycler(db=db, provisioner=provisioner)
    await recycler.request_and_reconcile(
        thread_id=ids["thread"],
        reason="operator_test",
        expected_build_sha="new-build",
        observation=PersistentPodObservation.from_status(
            ids["thread"], provisioner.current
        ),
        expected_project_id=ids["project"],
    )

    acknowledgement = await recycler.acknowledge_parked_boundary(
        thread_id=ids["thread"], agent_id=None
    )
    assert acknowledgement.active_generation is True
    assert acknowledgement.acknowledged is True


@pytest.mark.asyncio
async def test_notification_claim_crash_reclaims_once_and_success_is_terminal(db):
    ids = await _seed(db)
    provisioner = FakeProvisioner()
    provisioner.fail_creates = True
    unpaged = PersistentThreadRecycler(db=db, provisioner=provisioner)
    failed = await unpaged.request_and_reconcile(
        thread_id=ids["thread"],
        reason="missing_pod",
        expected_build_sha="new-build",
        expected_project_id=ids["project"],
    )
    assert failed.phase == "failed_retryable"
    state, _ = await _recycle_state(db, ids["thread"])
    generation = state["generation"]

    # Simulate process death after durable claim and before provider settlement.
    assert await unpaged._claim_notification(ids["thread"], generation, "dead-owner")
    async with db.acquire() as conn:
        await conn.execute(
            """
            UPDATE threads
               SET metadata = jsonb_set(
                   metadata,
                   '{agent_pod,recycle,notification,claim_expires_at}',
                   to_jsonb((now() - interval '1 minute')::text))
             WHERE id=$1
            """,
            UUID(ids["thread"]),
        )

    entered = asyncio.Event()
    release = asyncio.Event()
    pages: list[tuple[str, str, str]] = []

    async def notify(project_id: str, thread_id: str, failure_class: str):
        pages.append((project_id, thread_id, failure_class))
        entered.set()
        await release.wait()
        return True

    first = PersistentThreadRecycler(
        db=db, provisioner=provisioner, failure_notifier=notify
    )
    second = PersistentThreadRecycler(
        db=db, provisioner=provisioner, failure_notifier=notify
    )
    first_task = asyncio.create_task(
        first.request_and_reconcile(
            thread_id=ids["thread"],
            reason="missing_pod",
            expected_build_sha="new-build",
            expected_project_id=ids["project"],
        )
    )
    await entered.wait()
    await second.request_and_reconcile(
        thread_id=ids["thread"],
        reason="missing_pod",
        expected_build_sha="new-build",
        expected_project_id=ids["project"],
    )
    assert len(pages) == 1
    release.set()
    await first_task

    await asyncio.gather(
        *(
            second.request_and_reconcile(
                thread_id=ids["thread"],
                reason="missing_pod",
                expected_build_sha="new-build",
                expected_project_id=ids["project"],
            )
            for _ in range(3)
        )
    )
    assert len(pages) == 1
    state, _ = await _recycle_state(db, ids["thread"])
    assert state["notification"]["state"] == "delivered"


@pytest.mark.asyncio
async def test_failed_notification_retries_after_bounded_backoff(db):
    ids = await _seed(db)
    provisioner = FakeProvisioner()
    provisioner.fail_creates = True
    deliveries = [False, True]
    attempts = 0

    async def notify(_project_id: str, _thread_id: str, _failure_class: str):
        nonlocal attempts
        attempts += 1
        return deliveries.pop(0)

    recycler = PersistentThreadRecycler(
        db=db, provisioner=provisioner, failure_notifier=notify
    )
    await recycler.request_and_reconcile(
        thread_id=ids["thread"],
        reason="missing_pod",
        expected_build_sha="new-build",
        expected_project_id=ids["project"],
    )
    state, _ = await _recycle_state(db, ids["thread"])
    assert state["notification"]["state"] == "failed"
    assert state["notification"]["next_retry_at"]
    assert attempts == 1

    await recycler.request_and_reconcile(
        thread_id=ids["thread"],
        reason="missing_pod",
        expected_build_sha="new-build",
        expected_project_id=ids["project"],
    )
    assert attempts == 1
    async with db.acquire() as conn:
        await conn.execute(
            """
            UPDATE threads
               SET metadata = jsonb_set(
                   metadata,
                   '{agent_pod,recycle,notification,next_retry_at}',
                   to_jsonb((now() - interval '1 minute')::text))
             WHERE id=$1
            """,
            UUID(ids["thread"]),
        )
    await recycler.request_and_reconcile(
        thread_id=ids["thread"],
        reason="missing_pod",
        expected_build_sha="new-build",
        expected_project_id=ids["project"],
    )
    state, _ = await _recycle_state(db, ids["thread"])
    assert attempts == 2
    assert state["notification"]["state"] == "delivered"


@pytest.mark.asyncio
async def test_officer_replacement_requires_exact_current_grant(db):
    ids = await _seed(db)
    provisioner = FakeProvisioner()
    recycler = PersistentThreadRecycler(db=db, provisioner=provisioner)
    await recycler.request_and_reconcile(
        thread_id=ids["thread"],
        reason="missing_pod",
        expected_build_sha="new-build",
        expected_project_id=ids["project"],
    )
    state, _ = await _recycle_state(db, ids["thread"])
    uid = provisioner.current["pod_uid"]
    agent_id, _ = await _bind_replacement_agent(
        db, thread_id=ids["thread"], pod_uid=uid
    )
    provisioner.current = _pod_status(
        ids["thread"],
        uid=uid,
        build="new-build",
        generation=state["generation"],
        ready=True,
    )
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE runtime_actor_grants SET caller_kind='worker' "
            "WHERE agent_id=$1 AND revoked_at IS NULL",
            UUID(agent_id),
        )
    refused = await recycler.request_and_reconcile(
        thread_id=ids["thread"],
        reason="missing_pod",
        expected_build_sha="new-build",
        expected_project_id=ids["project"],
    )
    assert refused.phase == "awaiting_replacement"
    _, metadata = await _recycle_state(db, ids["thread"])
    assert metadata["config_override"]["officer"]["hold"] is not None

    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE runtime_actor_grants SET caller_kind='officer' "
            "WHERE agent_id=$1 AND revoked_at IS NULL",
            UUID(agent_id),
        )
    accepted = await recycler.request_and_reconcile(
        thread_id=ids["thread"],
        reason="missing_pod",
        expected_build_sha="new-build",
        expected_project_id=ids["project"],
    )
    assert accepted.phase == "complete"


@pytest.mark.asyncio
async def test_two_desired_image_changes_chain_without_releasing_hold(db):
    ids = await _seed(db)
    provisioner = FakeProvisioner()
    provisioner.current = _pod_status(ids["thread"], uid="old-pod", build="old-build")
    recycler = PersistentThreadRecycler(db=db, provisioner=provisioner)
    await recycler.request_and_reconcile(
        thread_id=ids["thread"],
        reason="image_drift",
        expected_build_sha="new-build",
        observation=PersistentPodObservation.from_status(
            ids["thread"], provisioner.current
        ),
        expected_project_id=ids["project"],
    )

    async def drain_and_provision() -> tuple[dict, str]:
        acknowledgement = await recycler.acknowledge_parked_boundary(
            thread_id=ids["thread"], agent_id=None
        )
        assert acknowledgement.acknowledged
        provisioner.current = None
        await recycler.request_and_reconcile(
            thread_id=ids["thread"],
            reason="image_drift",
            expected_build_sha=provisioner.expected_build_sha,
            expected_project_id=ids["project"],
        )
        state, metadata = await _recycle_state(db, ids["thread"])
        assert metadata["config_override"]["officer"]["hold"] is not None
        assert state["phase"] == "awaiting_replacement"
        return state, provisioner.current["pod_uid"]

    first, first_uid = await drain_and_provision()
    provisioner.expected_build_sha = "build-two"
    provisioner.image_ref = "example.test/agent:sha-build-two"
    await _bind_replacement_agent(db, thread_id=ids["thread"], pod_uid=first_uid)
    provisioner.current = _pod_status(
        ids["thread"],
        uid=first_uid,
        build="new-build",
        generation=first["generation"],
        ready=True,
    )
    chained_two = await recycler.request_and_reconcile(
        thread_id=ids["thread"],
        reason="image_drift",
        expected_build_sha="build-two",
        expected_project_id=ids["project"],
    )
    assert chained_two.phase == "awaiting_old_pod_exit"
    state_two, metadata = await _recycle_state(db, ids["thread"])
    assert state_two["expected_build_sha"] == "build-two"
    assert metadata["config_override"]["officer"]["hold"] is not None

    provisioner.expected_build_sha = "build-three"
    provisioner.image_ref = "example.test/agent:sha-build-three"
    second, second_uid = await drain_and_provision()
    assert second["expected_build_sha"] == "build-two"
    await _bind_replacement_agent(db, thread_id=ids["thread"], pod_uid=second_uid)
    provisioner.current = _pod_status(
        ids["thread"],
        uid=second_uid,
        build="build-two",
        generation=second["generation"],
        ready=True,
    )
    chained_three = await recycler.request_and_reconcile(
        thread_id=ids["thread"],
        reason="image_drift",
        expected_build_sha="build-three",
        expected_project_id=ids["project"],
    )
    assert chained_three.phase == "awaiting_old_pod_exit"
    state_three, metadata = await _recycle_state(db, ids["thread"])
    assert state_three["expected_build_sha"] == "build-three"
    assert metadata["config_override"]["officer"]["hold"] is not None

    third, third_uid = await drain_and_provision()
    await _bind_replacement_agent(db, thread_id=ids["thread"], pod_uid=third_uid)
    provisioner.current = _pod_status(
        ids["thread"],
        uid=third_uid,
        build="build-three",
        generation=third["generation"],
        ready=True,
    )
    complete = await recycler.request_and_reconcile(
        thread_id=ids["thread"],
        reason="image_drift",
        expected_build_sha="build-three",
        expected_project_id=ids["project"],
    )
    assert complete.phase == "complete"
    _, metadata = await _recycle_state(db, ids["thread"])
    assert metadata["config_override"]["officer"]["hold"] is None
    assert provisioner.created_targets == [
        "example.test/agent:sha-new-build",
        "example.test/agent:sha-build-two",
        "example.test/agent:sha-build-three",
    ]
