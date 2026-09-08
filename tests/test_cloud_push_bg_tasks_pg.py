"""Real PostgreSQL proofs of idle-push scheduling, bounded retries and fencing."""

from __future__ import annotations

import asyncio
import json
from uuid import UUID, uuid4

import pytest
import pytest_asyncio

from tests.test_cloud_sync_push_ownership_pg import (
    MIGRATIONS,
    SCOPE,
    WORKSPACE,
    _seed,
    _swap_db,
    scratch_pg_dsn as scratch_pg_dsn,
)
from orchestrator.database.migrate import run_migrations
from shared import cloud_push_tasks as tasks
from shared.cloud_sync_generations import (
    CloudSyncScope,
    adopt_push_ownership,
    arm_cloud_sync_generations,
    cloud_sync_writer_is_current,
    hand_off_push_ownership,
    record_push_progress,
)
from shared.run_queue import claim_unit, complete_unit, enqueue_unit, unpark_unit
from shared.run_queue.queries import list_parked

asyncpg = pytest.importorskip("asyncpg")


@pytest_asyncio.fixture
async def pg(request):
    dsn = request.getfixturevalue("scratch_pg_dsn")
    name = f"bg_push_{uuid4().hex[:12]}"
    admin = await asyncpg.connect(dsn)
    await admin.execute(f'CREATE DATABASE "{name}"')
    pool = await asyncpg.create_pool(_swap_db(dsn, name), min_size=1, max_size=4)
    try:
        await run_migrations(pool, MIGRATIONS)
        yield pool
    finally:
        await pool.close()
        await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        await admin.close()


async def pending_thread(pg):
    thread = uuid4()
    async with pg.acquire() as conn:
        await _seed(conn, thread_id=thread, user_id=uuid4(), lease_token=1)
        await arm_cloud_sync_generations(
            conn,
            thread_id=thread,
            lease_token=1,
            scopes=[
                CloudSyncScope(
                    mount_id="legacy-session",
                    workspace_generation=WORKSPACE,
                    sync_scope_sha256=SCOPE,
                )
            ],
        )
        await hand_off_push_ownership(
            conn,
            thread_id=thread,
            lease_token=1,
            workspace_generation=WORKSPACE,
            pod_name="dead-owner",
            pod_uid=str(uuid4()),
        )
        assert (
            await complete_unit(conn, unit_id=thread, lease_token=1, consumed_seq=10)
            == "done"
        )
        await conn.execute(
            "UPDATE thread_cloud_sync_generations SET push_heartbeat_at=now()-interval '100 seconds' WHERE thread_id=$1",
            thread,
        )
    return thread


async def scheduled(pg):
    thread = await pending_thread(pg)
    (candidate,) = await tasks.stale_pending_pushes(pg)
    async with pg.acquire() as conn:
        unit = await tasks.enqueue_cloud_push(conn, candidate)
    claim = await tasks.claim_bg_task(pg, pod_name="successor")
    assert str(claim.unit_id) == unit
    return thread, claim


@pytest.mark.asyncio
async def test_concurrent_sweeps_dedup_and_failures_stay_bounded(pg):
    thread = await pending_thread(pg)
    (candidate,) = await tasks.stale_pending_pushes(pg)

    async def enqueue():
        async with pg.acquire() as conn:
            return await tasks.enqueue_cloud_push(conn, candidate)

    results = await asyncio.gather(enqueue(), enqueue())
    assert sum(result is not None for result in results) == 1
    unit = next(result for result in results if result)
    await pg.execute(
        "UPDATE run_queue SET max_attempts=2 WHERE unit_id=$1",
        UUID(unit),
    )
    for expected in ("queued", "parked"):
        claim = await tasks.claim_bg_task(pg, pod_name="successor")
        assert (
            await tasks.fail_bg_task(pg, claim, error="cloud unavailable") == expected
        )
        await pg.execute(
            "UPDATE run_queue SET run_after=now() WHERE unit_id=$1", claim.unit_id
        )
    assert await tasks.stale_pending_pushes(pg) == []
    assert await tasks.claim_bg_task(pg, pod_name="successor") is None
    (parked,) = await list_parked(pg)
    assert str(parked["thread_id"]) == str(thread)
    assert parked["park_reason"] == "cloud_push_failed"
    assert (
        await pg.fetchval("SELECT state FROM run_queue WHERE unit_id=$1", thread)
        == "done"
    )
    assert await unpark_unit(pg, unit_id=unit)
    assert await tasks.claim_bg_task(pg, pod_name="successor") is not None


@pytest.mark.asyncio
async def test_bg_adoption_resumes_progress_and_new_session_fences_it(pg):
    thread, claim = await scheduled(pg)
    await record_push_progress(
        pg,
        thread_id=thread,
        mount_id="legacy-session",
        push_owner_token=1,
        files={
            "already.txt": {
                "state": "uploaded",
                "sha256": "a" * 64,
                "size": 5,
                "remote_etag": '"v1"',
            }
        },
    )
    # A progress checkpoint renews heartbeat. Simulate the owner then dying.
    await pg.execute(
        "UPDATE thread_cloud_sync_generations SET push_heartbeat_at=now()-interval '100 seconds' WHERE thread_id=$1",
        thread,
    )
    async with pg.acquire() as conn:
        requirements = await tasks.adopt_bg_push(
            conn,
            unit_id=claim.unit_id,
            lease_token=claim.lease_token,
            pod_name="successor",
            pod_uid=str(uuid4()),
            workspace_generation=WORKSPACE,
        )
    req = requirements["legacy-session"]
    assert req.push_owner_token == 2
    assert "already.txt" in req.push_progress["files"]
    assert await tasks.bg_push_lease_is_current(
        pg, unit_id=claim.unit_id, lease_token=claim.lease_token, pod_name="successor"
    )
    await enqueue_unit(pg, unit_id=thread, unit_kind="session_turn", input_seq=11)
    interactive = await claim_unit(
        pg, unit_kind="session_turn", pod_name="interactive", affinity_grace_seconds=0
    )
    assert interactive is not None
    await adopt_push_ownership(
        pg,
        thread_id=thread,
        lease_token=interactive.lease_token,
        workspace_generation=WORKSPACE,
        pod_name="interactive",
        pod_uid=str(uuid4()),
    )
    assert not await cloud_sync_writer_is_current(
        pg,
        kind="push",
        thread_id=thread,
        workspace_generation=WORKSPACE,
        push_owner_token=req.push_owner_token,
    )
    # Cleanup of the losing BG writer must not fence the newer session owner.
    await tasks.abandon_bg_push(
        pg, thread_id=thread, push_owner_token=req.push_owner_token
    )
    assert (
        await pg.fetchval(
            "SELECT push_owner_token FROM thread_cloud_sync_generations WHERE thread_id=$1",
            thread,
        )
        == 3
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("blocker", ["session", "live_push", "retirement", "workspace"])
async def test_adoption_refuses_changed_authority(pg, blocker):
    thread, claim = await scheduled(pg)
    if blocker == "session":
        await pg.execute("UPDATE run_queue SET state='queued' WHERE unit_id=$1", thread)
    elif blocker == "live_push":
        await pg.execute(
            "UPDATE thread_cloud_sync_generations SET push_heartbeat_at=now() WHERE thread_id=$1",
            thread,
        )
    elif blocker == "retirement":
        await pg.execute(
            "UPDATE threads SET metadata=metadata || $2::jsonb WHERE id=$1",
            thread,
            json.dumps({"_stateless_claim_retirement": {}}),
        )
    else:
        await pg.execute(
            "UPDATE threads SET metadata=jsonb_set(metadata,'{_workspace_binding,generation}','\"changed\"') WHERE id=$1",
            thread,
        )
    async with pg.acquire() as conn:
        with pytest.raises(RuntimeError):
            await tasks.adopt_bg_push(
                conn,
                unit_id=claim.unit_id,
                lease_token=claim.lease_token,
                pod_name="successor",
                pod_uid=str(uuid4()),
                workspace_generation=WORKSPACE,
            )
    assert (
        await pg.fetchval(
            "SELECT push_owner_token FROM thread_cloud_sync_generations WHERE thread_id=$1",
            thread,
        )
        == 1
    )


@pytest.mark.asyncio
async def test_completed_generation_obsoletes_task_without_adoption(pg):
    thread, claim = await scheduled(pg)
    await pg.execute(
        "UPDATE thread_cloud_sync_generations SET acknowledged_generation=required_generation WHERE thread_id=$1",
        thread,
    )
    async with pg.acquire() as conn:
        assert (
            await tasks.adopt_bg_push(
                conn,
                unit_id=claim.unit_id,
                lease_token=claim.lease_token,
                pod_name="successor",
                pod_uid=str(uuid4()),
                workspace_generation=WORKSPACE,
            )
            == {}
        )
    assert await tasks.complete_bg_task(pg, claim) == "done"
    assert (
        await pg.fetchval(
            "SELECT push_owner_token FROM thread_cloud_sync_generations WHERE thread_id=$1",
            thread,
        )
        == 1
    )


@pytest.mark.asyncio
async def test_background_lease_loss_and_deferral_preserve_fences(pg):
    thread, claim = await scheduled(pg)
    assert (
        await tasks.fail_bg_task(pg, claim, error="session busy", deferred=True)
        == "queued"
    )
    assert (
        await pg.fetchval(
            "SELECT attempts_since_completion FROM run_queue WHERE unit_id=$1",
            claim.unit_id,
        )
        == 0
    )
    assert not await tasks.bg_push_lease_is_current(
        pg, unit_id=claim.unit_id, lease_token=claim.lease_token, pod_name="successor"
    )
    await pg.execute(
        "UPDATE run_queue SET run_after=now() WHERE unit_id=$1", claim.unit_id
    )
    newer = await tasks.claim_bg_task(pg, pod_name="successor")
    assert newer.lease_token > claim.lease_token
    assert await tasks.complete_bg_task(pg, claim) is None
    async with pg.acquire() as conn:
        with pytest.raises(RuntimeError):
            await tasks.adopt_bg_push(
                conn,
                unit_id=claim.unit_id,
                lease_token=claim.lease_token,
                pod_name="successor",
                pod_uid=str(uuid4()),
                workspace_generation=WORKSPACE,
            )
    assert await tasks.complete_bg_task(pg, newer) == "done"
