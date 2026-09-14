"""Real-Postgres proof of the push-ownership fence (step 4a).

Replays the whole app migration chain into a scratch container database,
seeds one stateless thread with a leased run_queue row and one armed
generation, and drives the hand-off → fence → adoption → progress →
acknowledgement path through the real statements. Lock-manager and
``jsonb`` semantics are what a fake connection cannot represent.

Skips without a container runtime (same gate as
tests/test_subagent_thread_migration.py).
"""

from __future__ import annotations

import json
import pathlib
import re
from uuid import uuid4

import pytest

asyncpg = pytest.importorskip("asyncpg")

from orchestrator.database.migrate import run_migrations  # noqa: E402
from shared.cloud_sync_generations import (  # noqa: E402
    CloudSyncScope,
    acknowledge_cloud_sync_generation,
    adopt_push_ownership,
    arm_cloud_sync_generations,
    cloud_sync_writer_is_current,
    hand_off_push_ownership,
    heartbeat_push_owner,
    load_cloud_sync_requirements,
    pending_push_state,
    record_push_failure,
    record_push_progress,
)

MIGRATIONS = (
    pathlib.Path(__file__).resolve().parents[1]
    / "src"
    / "orchestrator"
    / "database"
    / "migrations"
    / "app"
)
WORKSPACE = "ws-incarnation-1"
SCOPE = "c" * 64


@pytest.fixture(scope="module")
def scratch_pg_dsn() -> str:
    testcontainers = pytest.importorskip("testcontainers.postgres")
    try:
        container = testcontainers.PostgresContainer("postgres:16")
        container.start()
    except Exception as exc:  # no container runtime on this box
        pytest.skip(f"no container runtime for the push-ownership proof: {exc}")
    try:
        yield re.sub(
            r"^postgresql\+\w+://", "postgresql://", container.get_connection_url()
        )
    finally:
        container.stop()


def _swap_db(dsn: str, dbname: str) -> str:
    head, _, tail = dsn.rpartition("/")
    query = "?" + tail.split("?", 1)[1] if "?" in tail else ""
    return f"{head}/{dbname}{query}"


async def _seed(conn, *, thread_id, user_id, lease_token: int) -> None:
    await conn.execute(
        "INSERT INTO users (id, display_name) VALUES ($1, 'owner')", user_id
    )
    await conn.execute(
        """
        INSERT INTO threads (id, user_id, title, status, execution_lane, metadata)
        VALUES ($1, $2, 'push proof', 'active', 'stateless', $3::jsonb)
        """,
        thread_id,
        user_id,
        json.dumps(
            {"_workspace_binding": {"kind": "virtual", "generation": WORKSPACE}}
        ),
    )
    await conn.execute(
        """
        INSERT INTO run_queue (unit_id, unit_kind, state, lease_token, leased_by,
                               leased_until, input_seq, consumed_seq,
                               input_delivery_capable_lease_token)
        VALUES ($1, 'session_turn', 'leased', $2, 'pod-a',
                now() + interval '60 seconds', 10, 9, $2)
        """,
        thread_id,
        lease_token,
    )


@pytest.mark.asyncio
async def test_hand_off_fence_adoption_progress_and_push_ack(
    scratch_pg_dsn: str,
) -> None:
    dbname = f"push_ownership_{uuid4().hex[:12]}"
    admin = await asyncpg.connect(scratch_pg_dsn)
    try:
        await admin.execute(f'CREATE DATABASE "{dbname}"')
    finally:
        await admin.close()
    pool = await asyncpg.create_pool(
        _swap_db(scratch_pg_dsn, dbname), min_size=1, max_size=4
    )
    thread_id = uuid4()
    user_id = uuid4()
    try:
        await run_migrations(pool, MIGRATIONS)
        async with pool.acquire() as conn:
            await _seed(conn, thread_id=thread_id, user_id=user_id, lease_token=7)

            # Arm one generation under lease 7 (what turn start does).
            armed = await arm_cloud_sync_generations(
                conn,
                thread_id=thread_id,
                lease_token=7,
                scopes=[
                    CloudSyncScope(
                        mount_id="legacy-session",
                        workspace_generation=WORKSPACE,
                        sync_scope_sha256=SCOPE,
                    )
                ],
            )
            req = armed["legacy-session"]
            assert req.required_generation == 7 and req.push_owner_token == 0

            # Nothing handed off yet: no push token is current.
            assert not await cloud_sync_writer_is_current(
                conn,
                kind="push",
                thread_id=thread_id,
                workspace_generation=WORKSPACE,
                push_owner_token=1,
            )

            # Hand-off under the live lease → token 1, owner stamped.
            token = await hand_off_push_ownership(
                conn,
                thread_id=thread_id,
                lease_token=7,
                workspace_generation=WORKSPACE,
                pod_name="pod-a",
                pod_uid="uid-a",
            )
            assert token == 1
            assert await cloud_sync_writer_is_current(
                conn,
                kind="push",
                thread_id=thread_id,
                workspace_generation=WORKSPACE,
                push_owner_token=1,
            )
            # A hand-off with a stale lease is a no-op (fenced by run_queue).
            assert (
                await hand_off_push_ownership(
                    conn,
                    thread_id=thread_id,
                    lease_token=6,
                    workspace_generation=WORKSPACE,
                    pod_name="x",
                    pod_uid="y",
                )
                is None
            )

            # The owner records progress + heartbeats under its token.
            assert await record_push_progress(
                conn,
                thread_id=thread_id,
                mount_id="legacy-session",
                push_owner_token=1,
                files={
                    "a.md": {
                        "sha256": "a" * 64,
                        "size": 3,
                        "remote_etag": "e1",
                        "state": "uploaded",
                    }
                },
                planned=3,
            )
            assert await record_push_progress(
                conn,
                thread_id=thread_id,
                mount_id="legacy-session",
                push_owner_token=1,
                files={
                    "b.md": {
                        "sha256": "b" * 64,
                        "size": 4,
                        "remote_etag": "e2",
                        "state": "uploaded",
                    }
                },
            )
            assert await heartbeat_push_owner(
                conn, thread_id=thread_id, push_owner_token=1
            )
            state = await pending_push_state(conn, thread_id=thread_id)
            assert state == {
                "pending": 1,
                "uploaded": 2,
                "total": 3,
                "owner_alive": True,
                "failed": False,
            }

            # The unit completes (lease gone); the push token is still the fence.
            await conn.execute(
                "UPDATE run_queue SET state='done', leased_by=NULL, leased_until=NULL "
                "WHERE unit_id=$1",
                thread_id,
            )
            assert await cloud_sync_writer_is_current(
                conn,
                kind="push",
                thread_id=thread_id,
                workspace_generation=WORKSPACE,
                push_owner_token=1,
            )
            assert not await cloud_sync_writer_is_current(
                conn,
                kind="lease",
                thread_id=thread_id,
                workspace_generation=WORKSPACE,
                lease_token=7,
            )

            # The thread's next claim (lease 8) adopts: token bumps to 2, the old
            # owner is fenced out, and the successor reads the durable progress.
            await conn.execute(
                "UPDATE run_queue SET state='leased', lease_token=8, leased_by='pod-b', "
                "leased_until=now() + interval '60 seconds', "
                "input_delivery_capable_lease_token=8 WHERE unit_id=$1",
                thread_id,
            )
            adopted = await adopt_push_ownership(
                conn,
                thread_id=thread_id,
                lease_token=8,
                workspace_generation=WORKSPACE,
                pod_name="pod-b",
                pod_uid="uid-b",
            )
            assert set(adopted["legacy-session"]["files"]) == {"a.md", "b.md"}
            assert adopted["legacy-session"]["planned"] == 3
            assert not await cloud_sync_writer_is_current(
                conn,
                kind="push",
                thread_id=thread_id,
                workspace_generation=WORKSPACE,
                push_owner_token=1,
            )
            assert not await record_push_progress(
                conn,
                thread_id=thread_id,
                mount_id="legacy-session",
                push_owner_token=1,
                files={},
            )
            assert not await heartbeat_push_owner(
                conn, thread_id=thread_id, push_owner_token=1
            )
            assert await record_push_failure(
                conn, thread_id=thread_id, push_owner_token=2, error="late failure"
            )
            assert (await pending_push_state(conn, thread_id=thread_id))[
                "failed"
            ] is True

            # The successor's own writes are lease-fenced; its acknowledgement
            # under lease 8 closes the generation, after which no push token
            # (old or new) is current: nothing is left to write.
            loaded = await load_cloud_sync_requirements(
                conn, thread_id=thread_id, lease_token=8, workspace_generation=WORKSPACE
            )
            assert loaded["legacy-session"].push_owner_token == 2
            assert await acknowledge_cloud_sync_generation(
                conn,
                thread_id=thread_id,
                lease_token=8,
                mount_id="legacy-session",
                generation=7,
                workspace_generation=WORKSPACE,
                sync_scope_sha256=SCOPE,
                baseline_sha256=req.baseline_sha256,
            )
            assert not await cloud_sync_writer_is_current(
                conn,
                kind="push",
                thread_id=thread_id,
                workspace_generation=WORKSPACE,
                push_owner_token=2,
            )
            assert (await pending_push_state(conn, thread_id=thread_id))["pending"] == 0

            # Re-arming the next generation (lease 8) resets progress but keeps
            # the token monotonic.
            rearmed = await arm_cloud_sync_generations(
                conn,
                thread_id=thread_id,
                lease_token=8,
                scopes=[
                    CloudSyncScope(
                        mount_id="legacy-session",
                        workspace_generation=WORKSPACE,
                        sync_scope_sha256=SCOPE,
                    )
                ],
            )
            assert rearmed["legacy-session"].required_generation == 8
            assert rearmed["legacy-session"].push_progress == {
                "planned": None,
                "files": {},
            }
            assert rearmed["legacy-session"].push_owner_token == 2

            # A handed-off push can acknowledge under its push token alone —
            # the path a completed unit's off-slot transmit takes.
            token = await hand_off_push_ownership(
                conn,
                thread_id=thread_id,
                lease_token=8,
                workspace_generation=WORKSPACE,
                pod_name="pod-b",
                pod_uid="uid-b",
            )
            assert token == 3
            await conn.execute(
                "UPDATE run_queue SET state='done', leased_by=NULL WHERE unit_id=$1",
                thread_id,
            )
            assert await acknowledge_cloud_sync_generation(
                conn,
                thread_id=thread_id,
                lease_token=8,
                mount_id="legacy-session",
                generation=8,
                workspace_generation=WORKSPACE,
                sync_scope_sha256=SCOPE,
                baseline_sha256=rearmed["legacy-session"].baseline_sha256,
                push_owner_token=3,
            )
            assert (await pending_push_state(conn, thread_id=thread_id))["pending"] == 0
    finally:
        await pool.close()
        admin = await asyncpg.connect(scratch_pg_dsn)
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')
        finally:
            await admin.close()
