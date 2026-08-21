"""Real-PostgreSQL P0 proofs for 24/7 Officer runtime authorization."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio
from fastapi import HTTPException
from starlette.datastructures import Headers
from testcontainers.postgres import PostgresContainer

from orchestrator.database.postgres import PostgresDB
from orchestrator.security import crypto
from orchestrator.services import runtime_actor as service
from orchestrator.services import runtime_actor_verification as verification
from src.shared import persistent_input_delivery as input_delivery
from src.shared.runtime_actor import (
    RUNTIME_ACTOR_MAINTENANCE_PHASE_HEADER,
    RUNTIME_ACTOR_MAINTENANCE_PHASE_PRE_TURN,
    RUNTIME_ACTOR_REFRESH_HEADER,
)


SCHEMA_FILE = (
    Path(__file__).resolve().parents[1]
    / "orchestrator"
    / "database"
    / "schema_current.sql"
)


def _json(value):
    return json.loads(value) if isinstance(value, str) else value


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
    monkeypatch.setenv("APP_ENCRYPTION_KEY", "R" * 32)
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
            "runtime_actor_bootstraps, session_wake_events, project_officers, "
            "threads, agents, project_members, projects, users CASCADE"
        )
    try:
        yield store
    finally:
        await store.close()
        crypto.reset_cipher_cache()


def _refresh_request(token: str, *, pre_turn: bool = True) -> MagicMock:
    request = MagicMock()
    request.method = "POST"
    raw_headers = [(RUNTIME_ACTOR_REFRESH_HEADER.lower().encode(), token.encode())]
    if pre_turn:
        raw_headers.append(
            (
                RUNTIME_ACTOR_MAINTENANCE_PHASE_HEADER.lower().encode(),
                RUNTIME_ACTOR_MAINTENANCE_PHASE_PRE_TURN.encode(),
            )
        )
    request.headers = Headers(raw=raw_headers)
    request.url.path = "/api/runtime-actors/refresh"
    request.client = None
    return request


async def _seed_officer(db: PostgresDB) -> dict[str, str]:
    ids = {
        "project_id": str(uuid4()),
        "thread_id": str(uuid4()),
        "agent_id": str(uuid4()),
        "user_id": str(uuid4()),
    }
    async with db.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id, display_name, email) VALUES ($1, $2, $3)",
            UUID(ids["user_id"]),
            "Officer owner",
            f"runtime-{ids['user_id']}@example.test",
        )
        await conn.execute(
            "INSERT INTO projects (id, name) VALUES ($1, $2)",
            UUID(ids["project_id"]),
            "runtime actor proof",
        )
        await conn.execute(
            "INSERT INTO project_members (project_id, user_id, role) "
            "VALUES ($1, $2, 'owner')",
            UUID(ids["project_id"]),
            UUID(ids["user_id"]),
        )
        await conn.execute(
            """
            INSERT INTO threads (
                id, user_id, project_id, status, execution_lane, metadata
            )
            VALUES ($1, $2, $3, 'active', 'pinned',
                    '{"config_override":{"officer":{"enabled":true}}}'::jsonb)
            """,
            UUID(ids["thread_id"]),
            UUID(ids["user_id"]),
            UUID(ids["project_id"]),
        )
        await conn.execute(
            """
            INSERT INTO agents (
                id, config_name, hostname, pod_ip, status, agent_mode,
                thread_id, last_heartbeat
            )
            VALUES ($1, 'persistent_defaults', 'officer-proof', '127.0.0.1',
                    'session', 'persistent', $2, now())
            """,
            UUID(ids["agent_id"]),
            UUID(ids["thread_id"]),
        )
        await conn.execute(
            "UPDATE threads SET agent_id = $2 WHERE id = $1",
            UUID(ids["thread_id"]),
            UUID(ids["agent_id"]),
        )
        await conn.execute(
            "INSERT INTO project_officers (project_id, thread_id) VALUES ($1, $2)",
            UUID(ids["project_id"]),
            UUID(ids["thread_id"]),
        )
    actor = await service.mint_thread_runtime_actor(
        db,
        thread_id=ids["thread_id"],
        agent_id=ids["agent_id"],
    )
    assert actor.refresh_credential and actor.access_credential
    ids["refresh_token"] = actor.refresh_credential
    ids["access_token"] = actor.access_credential
    return ids


async def _grant(db: PostgresDB, token: str):
    async with db.acquire() as conn:
        return await conn.fetchrow(
            "SELECT * FROM runtime_actor_grants WHERE refresh_token_hash = $1 "
            "OR previous_refresh_token_hash = $1",
            service._digest(token),
        )


async def _replace_officer_agent(
    db: PostgresDB, ids: dict[str, str]
) -> tuple[str, service.RuntimeActorContext]:
    successor = str(uuid4())
    async with db.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO agents (
                id, config_name, hostname, pod_ip, status, agent_mode,
                thread_id, last_heartbeat
            ) VALUES ($1, 'persistent_defaults', 'replacement', '127.0.0.2',
                      'session', 'persistent', $2, now())
            """,
            UUID(successor),
            UUID(ids["thread_id"]),
        )
        await conn.execute(
            "UPDATE agents SET status = 'offline' WHERE id = $1",
            UUID(ids["agent_id"]),
        )
        await conn.execute(
            "UPDATE threads SET agent_id = $2 WHERE id = $1",
            UUID(ids["thread_id"]),
            UUID(successor),
        )
    actor = await service.mint_thread_runtime_actor(
        db,
        thread_id=ids["thread_id"],
        agent_id=successor,
    )
    return successor, actor


@pytest.mark.asyncio
async def test_watchdog_renews_across_multiple_idle_ttls_and_access_stays_valid(db):
    ids = await _seed_officer(db)
    grant = await _grant(db, ids["refresh_token"])
    first_expiry = grant["refresh_expires_at"]

    first_maintenance = first_expiry - timedelta(hours=5)
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agents SET last_heartbeat = $2 WHERE id = $1",
            UUID(ids["agent_id"]),
            first_maintenance,
        )
    first = await service.maintain_current_officer_runtime(
        db,
        project_id=ids["project_id"],
        thread_id=ids["thread_id"],
        now=first_maintenance,
    )
    assert first.authorized and first.state == "renewed"

    grant = await _grant(db, ids["refresh_token"])
    second_maintenance = grant["refresh_expires_at"] - timedelta(hours=5)
    assert second_maintenance > first_expiry
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agents SET last_heartbeat = $2 WHERE id = $1",
            UUID(ids["agent_id"]),
            second_maintenance,
        )
        await conn.execute(
            "UPDATE runtime_actor_access_tokens SET expires_at = $2 "
            "WHERE token_hash = $1",
            service._digest(ids["access_token"]),
            second_maintenance + timedelta(minutes=5),
        )
    second = await service.maintain_current_officer_runtime(
        db,
        project_id=ids["project_id"],
        thread_id=ids["thread_id"],
        now=second_maintenance,
    )

    assert second.authorized and second.state == "renewed"
    assert second_maintenance > first_expiry
    accessed = await service._actor_for_access(db, ids["access_token"])
    assert accessed.caller_kind == "officer"
    assert accessed.project_id == ids["project_id"]


@pytest.mark.asyncio
async def test_watchdog_adopts_exactly_one_valid_legacy_grant(db):
    ids = await _seed_officer(db)
    async with db.acquire() as conn:
        await conn.execute(
            "ALTER TABLE runtime_actor_grants DISABLE TRIGGER "
            "trg_runtime_actor_grants_officer_agent_binding"
        )
        try:
            await conn.execute(
                "UPDATE runtime_actor_grants SET agent_id = NULL, "
                "refresh_expires_at = now() + interval '5 minutes'"
            )
        finally:
            await conn.execute(
                "ALTER TABLE runtime_actor_grants ENABLE TRIGGER "
                "trg_runtime_actor_grants_officer_agent_binding"
            )
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "UPDATE runtime_actor_grants SET last_refreshed_at = now() "
                "WHERE agent_id IS NULL"
            )

    result = await service.maintain_current_officer_runtime(
        db,
        project_id=ids["project_id"],
        thread_id=ids["thread_id"],
    )

    assert result.authorized and result.state == "renewed"
    async with db.acquire() as conn:
        rows = await conn.fetch(
            "SELECT refresh_token_hash, agent_id, revoked_at "
            "FROM runtime_actor_grants ORDER BY created_at"
        )
    assert len(rows) == 1
    assert rows[0]["revoked_at"] is None
    assert str(rows[0]["agent_id"]) == ids["agent_id"]


@pytest.mark.asyncio
async def test_watchdog_refuses_multiple_unbound_legacy_grants_without_guessing(db):
    ids = await _seed_officer(db)
    legacy_token = "srr_" + "L" * 43
    async with db.acquire() as conn:
        await conn.execute(
            "ALTER TABLE runtime_actor_grants DISABLE TRIGGER "
            "trg_runtime_actor_grants_officer_agent_binding"
        )
        try:
            await conn.execute("UPDATE runtime_actor_grants SET agent_id = NULL")
            await conn.execute(
                """
                INSERT INTO runtime_actor_grants (
                    refresh_token_hash, caller_kind, user_id, project_id,
                    project_role, thread_id, officer_incarnation,
                    refresh_expires_at, created_at
                ) VALUES ($1, 'officer', $2, $3, 'owner', $4, 0,
                          now() + interval '5 minutes', now() + interval '1 day')
                """,
                service._digest(legacy_token),
                UUID(ids["user_id"]),
                UUID(ids["project_id"]),
                UUID(ids["thread_id"]),
            )
        finally:
            await conn.execute(
                "ALTER TABLE runtime_actor_grants ENABLE TRIGGER "
                "trg_runtime_actor_grants_officer_agent_binding"
            )

    result = await service.maintain_current_officer_runtime(
        db,
        project_id=ids["project_id"],
        thread_id=ids["thread_id"],
    )

    assert not result.authorized
    assert result.failure_code == "ambiguous_legacy_grants"
    with pytest.raises(HTTPException) as credential_bearing:
        await service.refresh_runtime_actor_request(
            db, _refresh_request(ids["refresh_token"])
        )
    assert credential_bearing.value.detail["code"] == "ambiguous_legacy_grants"
    async with db.acquire() as conn:
        rows = await conn.fetch(
            "SELECT agent_id, revoked_at FROM runtime_actor_grants ORDER BY id"
        )
        incident = _json(
            await conn.fetchval(
                "SELECT state->'runtime_actor_incident' FROM project_officers"
            )
        )
    assert len(rows) == 2
    assert all(row["agent_id"] is None for row in rows)
    assert all(row["revoked_at"] is None for row in rows)
    assert incident["failure_class"] == "ambiguous_legacy_grants"
    assert "credential" not in json.dumps(incident).lower()


@pytest.mark.asyncio
async def test_expired_current_incarnation_recovers_after_orchestrator_restart(
    db, pg_dsn
):
    ids = await _seed_officer(db)
    async with db.acquire() as conn:
        # Reproduce the already-deployed 0161 row: migration 0171 cannot infer
        # a pod/agent authority during DDL, so legacy Officer grants start
        # unbound and only the exact current runtime may adopt them.
        await conn.execute(
            "ALTER TABLE runtime_actor_grants DISABLE TRIGGER "
            "trg_runtime_actor_grants_officer_agent_binding"
        )
        try:
            await conn.execute(
                "UPDATE runtime_actor_grants SET agent_id = NULL, "
                "refresh_expires_at = now() - interval '1 hour'"
            )
        finally:
            await conn.execute(
                "ALTER TABLE runtime_actor_grants ENABLE TRIGGER "
                "trg_runtime_actor_grants_officer_agent_binding"
            )
    # A fresh service instance represents an orchestrator restart. The Officer
    # pod and its hidden refresh bearer are deliberately unchanged. The
    # credential-independent watchdog restores only this exact live binding;
    # no pod restart or model-selected privileged call is needed.
    restarted = PostgresDB(
        connection_string=pg_dsn,
        min_connections=1,
        max_connections=4,
    )
    await restarted.connect()
    try:
        maintenance = await service.maintain_current_officer_runtime(
            restarted,
            project_id=ids["project_id"],
            thread_id=ids["thread_id"],
        )
        assert maintenance.authorized and maintenance.state == "recovered"
        async with restarted.acquire() as conn:
            handoff = await conn.fetchrow(
                "SELECT refresh_expires_at, refresh_rotation_required, agent_id "
                "FROM runtime_actor_grants"
            )
        assert handoff["refresh_expires_at"] > datetime.now(timezone.utc)
        assert handoff["refresh_rotation_required"] is True
        assert str(handoff["agent_id"]) == ids["agent_id"]
        # A pre-0171 replica would slide last_refreshed_at without rotating.
        # The database fence makes that mixed-version write fail closed.
        async with restarted.acquire() as conn:
            with pytest.raises(asyncpg.CheckViolationError):
                await conn.execute(
                    "UPDATE runtime_actor_grants "
                    "SET last_refreshed_at = now(), "
                    "refresh_expires_at = now() + interval '1 day'"
                )

        # The unchanged, already-running Officer can now make its next normal
        # refresh. Recovery forces rotation, after which the old bearer has
        # only the documented ambiguous-response overlap.
        recovered = await service.refresh_runtime_actor_request(
            restarted, _refresh_request(ids["refresh_token"])
        )
    finally:
        await restarted.close()

    assert recovered.refresh_credential != ids["refresh_token"]
    assert recovered.access_credential
    with pytest.raises(service.RuntimeActorCredentialError) as old_access:
        await service._actor_for_access(db, ids["access_token"])
    assert old_access.value.code == "invalid_credential"
    current = await service._actor_for_access(db, recovered.access_credential)
    assert current.project_id == ids["project_id"]
    async with db.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT credential_generation, agent_id, "
            "refresh_handoff_ciphertext, refresh_handoff_acknowledged_at "
            "FROM runtime_actor_grants"
        )
        incident = await conn.fetchval(
            "SELECT state->'runtime_actor_incident' FROM project_officers"
        )
    assert row["credential_generation"] == 2
    assert str(row["agent_id"]) == ids["agent_id"]
    assert row["refresh_handoff_ciphertext"]
    assert recovered.refresh_credential not in row["refresh_handoff_ciphertext"]
    assert row["refresh_handoff_acknowledged_at"] is None
    # An expired-but-otherwise-current binding is recovered as liveness work,
    # so there was no failure incident to settle.
    assert incident is None
    acknowledged = await service.refresh_runtime_actor_request(
        db, _refresh_request(recovered.refresh_credential)
    )
    assert acknowledged.refresh_credential == recovered.refresh_credential
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE runtime_actor_grants "
            "SET previous_refresh_valid_until = now() - interval '1 second'"
        )
    with pytest.raises(HTTPException) as old_bearer:
        await service.refresh_runtime_actor_request(
            db, _refresh_request(ids["refresh_token"])
        )
    assert old_bearer.value.detail["code"] == "invalid_credential"


@pytest.mark.asyncio
async def test_rotation_redelivers_one_generation_after_two_lost_responses_and_restart(
    db, pg_dsn
):
    ids = await _seed_officer(db)
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE runtime_actor_grants SET refresh_expires_at = "
            "now() - interval '1 hour'"
        )

    # Response 1 is committed and discarded by the transport/client.
    lost_one = await service.refresh_runtime_actor_request(
        db, _refresh_request(ids["refresh_token"])
    )
    rotated_token = lost_one.refresh_credential
    assert rotated_token != ids["refresh_token"]
    async with db.acquire() as conn:
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "UPDATE runtime_actor_grants SET last_refreshed_at = "
                "now() + interval '1 second'"
            )

    # Simulate retry after the old fixed 120-second window. An unacknowledged
    # handoff has no delivery-derived deadline and must re-deliver the same
    # encrypted generation, not rotate again. Response 2 is discarded too.
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE runtime_actor_grants SET previous_refresh_valid_until = "
            "now() - interval '1 hour'"
        )
    lost_two = await service.refresh_runtime_actor_request(
        db, _refresh_request(ids["refresh_token"])
    )
    assert lost_two.refresh_credential == rotated_token

    restarted = PostgresDB(
        connection_string=pg_dsn,
        min_connections=1,
        max_connections=4,
    )
    await restarted.connect()
    try:
        delivered = await service.refresh_runtime_actor_request(
            restarted, _refresh_request(ids["refresh_token"])
        )
        assert delivered.refresh_credential == rotated_token
        acknowledged = await service.refresh_runtime_actor_request(
            restarted, _refresh_request(rotated_token)
        )
        assert acknowledged.refresh_credential == rotated_token
    finally:
        await restarted.close()

    async with db.acquire() as conn:
        state = await conn.fetchrow(
            """
            SELECT credential_generation, refresh_token_hash,
                   previous_refresh_token_hash,
                   refresh_handoff_ciphertext,
                   refresh_handoff_acknowledged_at,
                   (SELECT count(*) FROM runtime_actor_access_tokens) AS access_count
              FROM runtime_actor_grants
            """
        )
    assert state["credential_generation"] == 2
    assert state["refresh_token_hash"] == service._digest(rotated_token)
    assert state["previous_refresh_token_hash"] == service._digest(ids["refresh_token"])
    assert state["refresh_handoff_ciphertext"]
    assert rotated_token not in state["refresh_handoff_ciphertext"]
    assert state["refresh_handoff_acknowledged_at"] is not None
    assert state["access_count"] == 1

    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE runtime_actor_grants SET previous_refresh_valid_until = "
            "now() - interval '1 second'"
        )
    with pytest.raises(HTTPException) as predecessor:
        await service.refresh_runtime_actor_request(
            db, _refresh_request(ids["refresh_token"])
        )
    assert predecessor.value.detail["code"] == "invalid_credential"


@pytest.mark.asyncio
async def test_expired_worker_does_not_gain_officer_recovery(db):
    actor = await service.mint_worker_runtime_actor(db, project_id=None, user_id=None)
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE runtime_actor_grants SET refresh_expires_at = now() - interval '1 hour' "
            "WHERE caller_kind = 'worker'"
        )

    with pytest.raises(HTTPException) as denied:
        await service.refresh_runtime_actor_request(
            db, _refresh_request(actor.refresh_credential)
        )
    assert denied.value.detail["code"] == "expired_credential"


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_status", ["offline", "failed", "draining"])
async def test_unavailable_agent_is_routine_lifecycle_state_not_authorization_incident(
    db, agent_status
):
    ids = await _seed_officer(db)
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agents SET status = $2, "
            "last_heartbeat = now() - interval '1 hour' WHERE id = $1",
            UUID(ids["agent_id"]),
            agent_status,
        )

    outcome = await service.maintain_current_officer_runtime(
        db,
        project_id=ids["project_id"],
        thread_id=ids["thread_id"],
    )

    assert not outcome.authorized
    assert outcome.state == "lifecycle_pending"
    assert not outcome.notification_due
    async with db.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT state->'runtime_actor_incident' FROM project_officers"
            )
            is None
        )


@pytest.mark.asyncio
async def test_retiring_current_agent_immediately_fences_access_and_refresh(db):
    ids = await _seed_officer(db)
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agents SET status = 'draining' WHERE id = $1",
            UUID(ids["agent_id"]),
        )

    with pytest.raises(service.RuntimeActorCredentialError) as access_denied:
        await service._actor_for_access(db, ids["access_token"])
    assert access_denied.value.code == "runtime_not_current"
    with pytest.raises(HTTPException) as refresh_denied:
        await service.refresh_runtime_actor_request(
            db, _refresh_request(ids["refresh_token"])
        )
    assert refresh_denied.value.detail["code"] == "runtime_not_current"


@pytest.mark.asyncio
async def test_deleting_current_agent_revokes_authority_and_retains_uuid_snapshot(db):
    ids = await _seed_officer(db)

    assert await db.delete_agent(ids["agent_id"])

    async with db.acquire() as conn:
        grant = await conn.fetchrow(
            "SELECT agent_id, revoked_at FROM runtime_actor_grants"
        )
        thread_agent = await conn.fetchval(
            "SELECT agent_id FROM threads WHERE id = $1", UUID(ids["thread_id"])
        )
    assert str(grant["agent_id"]) == ids["agent_id"]
    assert grant["revoked_at"] is not None
    assert thread_agent is None
    with pytest.raises(service.RuntimeActorCredentialError) as access_denied:
        await service._actor_for_access(db, ids["access_token"])
    assert access_denied.value.code == "revoked_credential"
    with pytest.raises(HTTPException) as refresh_denied:
        await service.refresh_runtime_actor_request(
            db, _refresh_request(ids["refresh_token"])
        )
    assert refresh_denied.value.detail["code"] == "revoked_credential"


@pytest.mark.asyncio
async def test_agent_deletion_racing_refresh_has_no_post_delete_authority(db):
    ids = await _seed_officer(db)

    refreshed, deleted = await asyncio.gather(
        service.refresh_runtime_actor_request(
            db, _refresh_request(ids["refresh_token"])
        ),
        db.delete_agent(ids["agent_id"]),
        return_exceptions=True,
    )

    assert deleted is True
    if not isinstance(refreshed, Exception):
        with pytest.raises(service.RuntimeActorCredentialError):
            await service._actor_for_access(db, refreshed.access_credential)
    else:
        assert isinstance(refreshed, HTTPException)
    async with db.acquire() as conn:
        grant = await conn.fetchrow(
            "SELECT agent_id, revoked_at FROM runtime_actor_grants"
        )
    assert str(grant["agent_id"]) == ids["agent_id"]
    assert grant["revoked_at"] is not None


@pytest.mark.asyncio
async def test_deleting_revoked_officer_predecessor_preserves_grant_audit(db):
    ids = await _seed_officer(db)
    successor, _actor = await _replace_officer_agent(db, ids)

    assert await db.delete_agent(ids["agent_id"])

    async with db.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM agents WHERE id = $1", UUID(ids["agent_id"])
            )
            == 0
        )
        predecessor = await conn.fetchrow(
            "SELECT agent_id, revoked_at FROM runtime_actor_grants WHERE agent_id = $1",
            UUID(ids["agent_id"]),
        )
        current = await conn.fetchrow(
            "SELECT agent_id, revoked_at FROM runtime_actor_grants WHERE agent_id = $1",
            UUID(successor),
        )
    assert str(predecessor["agent_id"]) == ids["agent_id"]
    assert predecessor["revoked_at"] is not None
    assert current["revoked_at"] is None
    async with db.acquire() as conn:
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "UPDATE runtime_actor_grants SET agent_id = NULL WHERE agent_id = $1",
                UUID(ids["agent_id"]),
            )


@pytest.mark.asyncio
async def test_offline_gc_batch_with_officer_predecessor_deletes_unrelated_agents(db):
    ids = await _seed_officer(db)
    await _replace_officer_agent(db, ids)
    unrelated = uuid4()
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE agents SET last_heartbeat = now() - interval '2 days' "
            "WHERE id = $1",
            UUID(ids["agent_id"]),
        )
        await conn.execute(
            """
            INSERT INTO agents (
                id, config_name, hostname, pod_ip, status, agent_mode,
                last_heartbeat
            ) VALUES ($1, 'general', 'unrelated-offline', '127.0.0.9',
                      'offline', 'worker', now() - interval '2 days')
            """,
            unrelated,
        )

    assert await db.gc_offline_agents(retention_hours=24) == 2

    async with db.acquire() as conn:
        remaining = await conn.fetchval(
            "SELECT count(*) FROM agents WHERE id = ANY($1::uuid[])",
            [UUID(ids["agent_id"]), unrelated],
        )
        predecessor = await conn.fetchrow(
            "SELECT agent_id, revoked_at FROM runtime_actor_grants WHERE agent_id = $1",
            UUID(ids["agent_id"]),
        )
    assert remaining == 0
    assert str(predecessor["agent_id"]) == ids["agent_id"]
    assert predecessor["revoked_at"] is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fence",
    [
        "stale-agent",
        "decommissioned",
        "ended",
        "foreign-project",
        "orphan",
        "old-incarnation",
        "pod-churn",
    ],
)
async def test_expired_recovery_fails_for_noncurrent_authority(db, fence):
    ids = await _seed_officer(db)
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE runtime_actor_grants SET refresh_expires_at = now() - interval '1 hour'"
        )
        if fence == "stale-agent":
            await conn.execute(
                "UPDATE agents SET last_heartbeat = now() - interval '1 hour'"
            )
        elif fence == "decommissioned":
            await conn.execute("UPDATE project_officers SET thread_id = NULL")
        elif fence == "ended":
            await conn.execute("UPDATE threads SET status = 'ended'")
        elif fence == "foreign-project":
            foreign_project = uuid4()
            await conn.execute(
                "INSERT INTO projects (id, name) VALUES ($1, 'foreign')",
                foreign_project,
            )
            await conn.execute(
                "UPDATE runtime_actor_grants SET project_id = $1",
                foreign_project,
            )
        elif fence == "orphan":
            await conn.execute("UPDATE threads SET agent_id = NULL")
        elif fence == "old-incarnation":
            await conn.execute(
                "UPDATE project_officers SET incarnations = "
                "jsonb_build_array(jsonb_build_object('thread_id', thread_id))"
            )
        elif fence == "pod-churn":
            successor = uuid4()
            await conn.execute(
                """
                INSERT INTO agents (
                    id, config_name, hostname, pod_ip, status, agent_mode,
                    thread_id, last_heartbeat
                ) VALUES ($1, 'persistent_defaults', 'successor', '127.0.0.2',
                          'session', 'persistent', $2, now())
                """,
                successor,
                UUID(ids["thread_id"]),
            )
            await conn.execute(
                "UPDATE threads SET agent_id = $2 WHERE id = $1",
                UUID(ids["thread_id"]),
                successor,
            )

    watchdog = await service.maintain_current_officer_runtime(
        db,
        project_id=ids["project_id"],
        thread_id=ids["thread_id"],
    )
    assert not watchdog.authorized

    with pytest.raises(HTTPException) as denied:
        await service.refresh_runtime_actor_request(
            db, _refresh_request(ids["refresh_token"])
        )
    assert denied.value.detail["code"] == "runtime_not_current"


@pytest.mark.asyncio
async def test_concurrent_watchdog_recovery_is_one_idempotent_authority(db):
    ids = await _seed_officer(db)
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE runtime_actor_grants SET refresh_expires_at = "
            "now() - interval '1 hour'"
        )

    outcomes = await asyncio.gather(
        service.maintain_current_officer_runtime(
            db,
            project_id=ids["project_id"],
            thread_id=ids["thread_id"],
        ),
        service.maintain_current_officer_runtime(
            db,
            project_id=ids["project_id"],
            thread_id=ids["thread_id"],
        ),
    )

    assert sorted(result.state for result in outcomes) == ["current", "recovered"]
    assert all(result.authorized for result in outcomes)
    async with db.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT count(*) FILTER (WHERE revoked_at IS NULL) AS grants, "
            "max(credential_generation) AS generation, "
            "bool_and(refresh_rotation_required) AS rotation_required "
            "FROM runtime_actor_grants"
        )
    assert dict(row) == {
        "grants": 1,
        "generation": 1,
        "rotation_required": True,
    }


@pytest.mark.asyncio
async def test_pod_replacement_revokes_the_predecessor_grant_for_old_replicas(db):
    ids = await _seed_officer(db)
    successor = uuid4()
    async with db.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO agents (
                id, config_name, hostname, pod_ip, status, agent_mode,
                thread_id, last_heartbeat
            ) VALUES ($1, 'persistent_defaults', 'replacement', '127.0.0.2',
                      'session', 'persistent', $2, now())
            """,
            successor,
            UUID(ids["thread_id"]),
        )
        await conn.execute(
            "UPDATE agents SET status = 'offline' WHERE id = $1",
            UUID(ids["agent_id"]),
        )
        await conn.execute(
            "UPDATE threads SET agent_id = $2 WHERE id = $1",
            UUID(ids["thread_id"]),
            successor,
        )

    replacement = await service.mint_thread_runtime_actor(
        db,
        thread_id=ids["thread_id"],
        agent_id=str(successor),
    )

    async with db.acquire() as conn:
        rows = await conn.fetch(
            "SELECT agent_id, revoked_at FROM runtime_actor_grants "
            "ORDER BY created_at, id"
        )
    assert len(rows) == 2
    assert str(rows[0]["agent_id"]) == ids["agent_id"]
    assert rows[0]["revoked_at"] is not None
    assert str(rows[1]["agent_id"]) == str(successor)
    assert rows[1]["revoked_at"] is None
    with pytest.raises(HTTPException) as old_refresh:
        await service.refresh_runtime_actor_request(
            db, _refresh_request(ids["refresh_token"])
        )
    assert old_refresh.value.detail["code"] == "revoked_credential"
    assert (
        await service._actor_for_access(db, replacement.access_credential)
    ).project_id == ids["project_id"]


@pytest.mark.asyncio
async def test_concurrent_expired_recovery_redelivers_one_authoritative_generation(db):
    ids = await _seed_officer(db)
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE runtime_actor_grants SET refresh_expires_at = now() - interval '1 hour'"
        )

    outcomes = await asyncio.gather(
        service.refresh_runtime_actor_request(
            db, _refresh_request(ids["refresh_token"])
        ),
        service.refresh_runtime_actor_request(
            db, _refresh_request(ids["refresh_token"])
        ),
    )

    async with db.acquire() as conn:
        counts = await conn.fetchrow(
            "SELECT count(*) FILTER (WHERE revoked_at IS NULL) AS grants, "
            "(SELECT count(*) FROM runtime_actor_access_tokens) AS access, "
            "max(credential_generation) AS generation "
            "FROM runtime_actor_grants"
        )
    assert dict(counts) == {"grants": 1, "access": 1, "generation": 2}
    assert len({outcome.refresh_credential for outcome in outcomes}) == 1
    usable = 0
    for outcome in outcomes:
        try:
            await service._actor_for_access(db, outcome.access_credential)
        except service.RuntimeActorCredentialError:
            pass
        else:
            usable += 1
    assert usable == 1
    redelivered = await service.refresh_runtime_actor_request(
        db, _refresh_request(ids["refresh_token"])
    )
    assert redelivered.refresh_credential == outcomes[0].refresh_credential


@pytest.mark.asyncio
async def test_incident_and_operator_page_claim_are_deduplicated_across_replicas(db):
    ids = await _seed_officer(db)
    async with db.acquire() as conn:
        await conn.execute("DELETE FROM runtime_actor_grants")
    observed = datetime.now(timezone.utc)

    first, second = await asyncio.gather(
        service.maintain_current_officer_runtime(
            db,
            project_id=ids["project_id"],
            thread_id=ids["thread_id"],
            now=observed,
        ),
        service.maintain_current_officer_runtime(
            db,
            project_id=ids["project_id"],
            thread_id=ids["thread_id"],
            now=observed,
        ),
    )
    claimed = [result for result in (first, second) if result.notification_due]
    assert len(claimed) == 1
    assert claimed[0].notification_claim_id

    settled = await asyncio.gather(
        service.settle_officer_runtime_incident_notification(
            db,
            project_id=ids["project_id"],
            thread_id=ids["thread_id"],
            officer_incarnation=0,
            notification_claim_id=claimed[0].notification_claim_id,
            delivered=True,
        ),
        service.settle_officer_runtime_incident_notification(
            db,
            project_id=ids["project_id"],
            thread_id=ids["thread_id"],
            officer_incarnation=0,
            notification_claim_id=claimed[0].notification_claim_id,
            delivered=True,
        ),
    )
    assert sorted(settled) == [False, True]
    async with db.acquire() as conn:
        incident = await conn.fetchval(
            "SELECT state->'runtime_actor_incident' FROM project_officers"
        )
    incident = _json(incident)
    assert incident["attempt_count"] == 1
    assert incident["notification"]["attempt_count"] == 1
    assert incident["notification"]["state"] == "delivered"


@pytest.mark.asyncio
async def test_one_recovery_wake_then_suppression_and_recovery_rearms_outbox(db):
    ids = await _seed_officer(db)
    async with db.acquire() as conn:
        await conn.execute("DELETE FROM runtime_actor_grants")
        await conn.execute(
            """
            INSERT INTO session_wake_events (
                thread_id, source, dedup_key, payload, fire_at, state
            ) VALUES ($1, 'officer_timer', 'runtime-test', '{}'::jsonb,
                      now() + interval '1 hour', 'pending')
            """,
            UUID(ids["thread_id"]),
        )
    await service.maintain_current_officer_runtime(
        db,
        project_id=ids["project_id"],
        thread_id=ids["thread_id"],
    )

    first = await service.admit_officer_wake_for_runtime(
        db, project_id=ids["project_id"], thread_id=ids["thread_id"]
    )
    second = await service.admit_officer_wake_for_runtime(
        db, project_id=ids["project_id"], thread_id=ids["thread_id"]
    )
    assert first == (True, None)
    assert second[0] is False and second[1] is not None

    recovered = await service.mint_thread_runtime_actor(
        db,
        thread_id=ids["thread_id"],
        agent_id=ids["agent_id"],
    )
    assert recovered.access_credential
    async with db.acquire() as conn:
        state = await conn.fetchval(
            "SELECT state->'runtime_actor_incident'->>'status' FROM project_officers"
        )
        fire_at = await conn.fetchval(
            "SELECT fire_at FROM session_wake_events WHERE dedup_key = 'runtime-test'"
        )
    assert state == "resolved"
    assert fire_at <= datetime.now(timezone.utc) + timedelta(seconds=1)


@pytest.mark.asyncio
async def test_decommission_racing_recovery_cannot_leave_stale_authority(db):
    ids = await _seed_officer(db)
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE runtime_actor_grants SET refresh_expires_at = now() - interval '1 hour'"
        )

    async def _decommission():
        async with db.acquire() as conn:
            async with conn.transaction():
                await conn.fetchrow(
                    "SELECT project_id FROM project_officers "
                    "WHERE project_id = $1 FOR UPDATE",
                    UUID(ids["project_id"]),
                )
                await conn.fetchrow(
                    "SELECT id FROM threads WHERE id = $1 FOR UPDATE",
                    UUID(ids["thread_id"]),
                )
                await conn.execute(
                    "UPDATE project_officers SET thread_id = NULL "
                    "WHERE project_id = $1",
                    UUID(ids["project_id"]),
                )
                await conn.execute(
                    "UPDATE threads SET status = 'ended' WHERE id = $1",
                    UUID(ids["thread_id"]),
                )

    recovered, _ = await asyncio.gather(
        service.refresh_runtime_actor_request(
            db, _refresh_request(ids["refresh_token"])
        ),
        _decommission(),
        return_exceptions=True,
    )
    if isinstance(recovered, Exception):
        assert isinstance(recovered, HTTPException)
    else:
        with pytest.raises(service.RuntimeActorCredentialError):
            await service._current_actor(db, recovered)
    async with db.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT thread_id FROM project_officers WHERE project_id = $1",
                UUID(ids["project_id"]),
            )
            is None
        )


@pytest.mark.asyncio
async def test_direct_end_racing_watchdog_recovery_cannot_leave_live_authority(db):
    ids = await _seed_officer(db)
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE runtime_actor_grants SET refresh_expires_at = "
            "now() - interval '1 hour'"
        )

    recovery, _ = await asyncio.gather(
        service.maintain_current_officer_runtime(
            db,
            project_id=ids["project_id"],
            thread_id=ids["thread_id"],
        ),
        db.end_thread(ids["thread_id"]),
    )

    assert recovery.state in {"recovered", "not_current"}
    async with db.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT status FROM threads WHERE id = $1",
                UUID(ids["thread_id"]),
            )
            == "ended"
        )
    with pytest.raises(HTTPException) as denied:
        await service.refresh_runtime_actor_request(
            db, _refresh_request(ids["refresh_token"])
        )
    assert denied.value.detail["code"] == "runtime_not_current"


@pytest.mark.asyncio
async def test_recommission_racing_recovery_fences_the_old_incarnation(db):
    ids = await _seed_officer(db)
    successor_thread = uuid4()
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE runtime_actor_grants SET refresh_expires_at = now() - interval '1 hour'"
        )
        await conn.execute(
            """
            INSERT INTO threads (
                id, user_id, project_id, status, execution_lane, metadata
            ) VALUES ($1, $2, $3, 'active', 'pinned',
                    '{"config_override":{"officer":{"enabled":true}}}'::jsonb)
            """,
            successor_thread,
            UUID(ids["user_id"]),
            UUID(ids["project_id"]),
        )

    async def _recommission():
        async with db.acquire() as conn:
            async with conn.transaction():
                await conn.fetchrow(
                    "SELECT project_id FROM project_officers "
                    "WHERE project_id = $1 FOR UPDATE",
                    UUID(ids["project_id"]),
                )
                await conn.fetchrow(
                    "SELECT id FROM threads WHERE id = $1 FOR UPDATE",
                    UUID(ids["thread_id"]),
                )
                await conn.execute(
                    """
                    UPDATE project_officers
                       SET thread_id = $2,
                           incarnations = incarnations ||
                               jsonb_build_array(
                                   jsonb_build_object('thread_id', $3::text)
                               )
                     WHERE project_id = $1
                    """,
                    UUID(ids["project_id"]),
                    successor_thread,
                    ids["thread_id"],
                )
                await conn.execute(
                    "UPDATE threads SET status = 'ended' WHERE id = $1",
                    UUID(ids["thread_id"]),
                )

    recovered, _ = await asyncio.gather(
        service.refresh_runtime_actor_request(
            db, _refresh_request(ids["refresh_token"])
        ),
        _recommission(),
        return_exceptions=True,
    )
    if isinstance(recovered, Exception):
        assert isinstance(recovered, HTTPException)
    else:
        with pytest.raises(service.RuntimeActorCredentialError):
            await service._current_actor(db, recovered)
    async with db.acquire() as conn:
        current_thread = await conn.fetchval(
            "SELECT thread_id FROM project_officers WHERE project_id = $1",
            UUID(ids["project_id"]),
        )
    assert current_thread == successor_thread


@pytest.mark.asyncio
async def test_vacant_post_retains_operator_visible_incident(db):
    ids = await _seed_officer(db)
    async with db.acquire() as conn:
        await conn.execute("DELETE FROM runtime_actor_grants")
    failed = await service.maintain_current_officer_runtime(
        db,
        project_id=ids["project_id"],
        thread_id=ids["thread_id"],
    )
    assert not failed.authorized
    async with db.acquire() as conn:
        await conn.execute(
            """
            UPDATE threads
               SET metadata = jsonb_set(
                    metadata,
                    '{officer_state,runtime_actor_incident}',
                    '{"status":"resolved","forged":true}'::jsonb,
                    true)
             WHERE id = $1
            """,
            UUID(ids["thread_id"]),
        )
    handoff = await db.decommission_project_officer(
        ids["project_id"], ids["thread_id"], reason="runtime incident proof"
    )
    assert handoff["transitioned"]
    async with db.acquire() as conn:
        incident = await conn.fetchval(
            "SELECT state->'runtime_actor_incident' FROM project_officers"
        )
    incident = _json(incident)
    assert incident["status"] == "open"
    assert incident["thread_id"] == ids["thread_id"]
    assert "forged" not in incident
    assert "credential" not in str(incident).lower()


@pytest.mark.asyncio
async def test_successor_does_not_inherit_or_get_suppressed_by_old_incident(db):
    ids = await _seed_officer(db)
    async with db.acquire() as conn:
        await conn.execute("DELETE FROM runtime_actor_grants")
    await service.maintain_current_officer_runtime(
        db,
        project_id=ids["project_id"],
        thread_id=ids["thread_id"],
    )
    await db.decommission_project_officer(
        ids["project_id"], ids["thread_id"], reason="successor proof"
    )
    successor = uuid4()
    async with db.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO threads (
                id, user_id, project_id, status, execution_lane, metadata
            ) VALUES ($1, $2, $3, 'active', 'pinned',
                    '{"config_override":{"officer":{"enabled":true}}}'::jsonb)
            """,
            successor,
            UUID(ids["user_id"]),
            UUID(ids["project_id"]),
        )
    registered = await db.register_project_officer_thread(
        ids["project_id"], str(successor), commission_continuity=True
    )
    assert registered is not None
    successor_row = await db.get_thread(str(successor))
    successor_metadata = _json(successor_row["metadata"])
    assert "runtime_actor_incident" not in (
        successor_metadata.get("officer_state") or {}
    )
    post = await db.get_project_officer(ids["project_id"])
    assert post["state"]["runtime_actor_incident"]["status"] == "superseded"
    assert (
        post["state"]["runtime_actor_incident"]["resolution"] == "incarnation_changed"
    )
    assert await service.admit_officer_wake_for_runtime(
        db,
        project_id=ids["project_id"],
        thread_id=str(successor),
    ) == (True, None)


@pytest.mark.asyncio
async def test_verification_plan_stays_post_only_across_decommission_recommission(db):
    ids = await _seed_officer(db)
    started = datetime.now(timezone.utc)
    plan = await verification.create_plan(
        db,
        enabled=True,
        project_id=ids["project_id"],
        idempotency_key=str(uuid4()),
        exercise="maintenance_failure",
        created_by=ids["user_id"],
        expires_in_seconds=300,
        now=started,
    )
    # Generic runtime-authored state cannot replace the authoritative plan,
    # and a forged thread projection cannot be harvested into the Post.
    await db.merge_project_officer_state(
        ids["project_id"],
        {
            "runtime_actor_verification": {"forged": True},
            "safe_runtime_note": {"preserved": True},
        },
    )
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE threads SET metadata = jsonb_set(metadata, "
            "'{officer_state,runtime_actor_verification}', "
            "'{\"forged\":true}'::jsonb, true) WHERE id = $1",
            UUID(ids["thread_id"]),
        )
    durable = await db.get_project_officer(ids["project_id"])
    assert durable["state"]["runtime_actor_verification"]["plan_id"] == plan["plan_id"]
    assert durable["state"]["safe_runtime_note"] == {"preserved": True}

    handoff = await db.decommission_project_officer(
        ids["project_id"], ids["thread_id"], reason="verification scope proof"
    )
    assert handoff["transitioned"]
    post_after_handoff = await db.get_project_officer(ids["project_id"])
    assert "forged" not in post_after_handoff["state"]["runtime_actor_verification"]

    successor = uuid4()
    async with db.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO threads (
                id, user_id, project_id, status, execution_lane, metadata
            ) VALUES ($1, $2, $3, 'active', 'pinned',
                      '{"config_override":{"officer":{"enabled":true}}}'::jsonb)
            """,
            successor,
            UUID(ids["user_id"]),
            UUID(ids["project_id"]),
        )
    registered = await db.register_project_officer_thread(
        ids["project_id"], str(successor), commission_continuity=True
    )
    assert registered is not None
    successor_row = await db.get_thread(str(successor))
    successor_state = _json(successor_row["metadata"]).get("officer_state") or {}
    assert "runtime_actor_verification" not in successor_state
    assert successor_state["safe_runtime_note"] == {"preserved": True}

    invalidated = await verification.get_plan(
        db,
        enabled=True,
        project_id=ids["project_id"],
        now=started + timedelta(seconds=1),
    )
    assert invalidated["state"] == "invalidated"
    assert invalidated["finish_reason"] == "binding_changed"


@pytest.mark.asyncio
async def test_verification_longevity_crosses_two_logical_windows_then_refreshes(db):
    ids = await _seed_officer(db)
    started = datetime.now(timezone.utc)
    plan = await verification.create_plan(
        db,
        enabled=True,
        project_id=ids["project_id"],
        idempotency_key=str(uuid4()),
        exercise="longevity",
        created_by=ids["user_id"],
        expires_in_seconds=600,
        logical_window_seconds=30,
        now=started,
    )

    first = await service.maintain_current_officer_runtime(
        db,
        project_id=ids["project_id"],
        thread_id=ids["thread_id"],
        now=started + timedelta(seconds=31),
        verification_enabled=True,
    )
    second = await service.maintain_current_officer_runtime(
        db,
        project_id=ids["project_id"],
        thread_id=ids["thread_id"],
        now=started + timedelta(seconds=62),
        verification_enabled=True,
    )
    waiting = await verification.get_plan(
        db,
        enabled=True,
        project_id=ids["project_id"],
        now=started + timedelta(seconds=62),
    )

    assert first.authorized and first.state == "renewed"
    assert second.authorized and second.state == "renewed"
    assert waiting["plan_id"] == plan["plan_id"]
    assert waiting["state"] == "awaiting_ack"
    assert waiting["progress"]["windows_completed"] == 2

    exchanged = await service.refresh_runtime_actor_exchange(
        db,
        _refresh_request(ids["refresh_token"]),
        verification_enabled=True,
        now=started + timedelta(seconds=63),
    )
    assert exchanged.actor is not None
    assert not exchanged.response_lost
    assert (
        await service._actor_for_access(db, exchanged.actor.access_credential)
    ).project_id == ids["project_id"]
    completed = await verification.get_plan(
        db,
        enabled=True,
        project_id=ids["project_id"],
        now=started + timedelta(seconds=64),
    )
    assert completed["state"] == "completed"
    assert completed["progress"]["authority_refresh_observed"] is True


@pytest.mark.asyncio
async def test_verification_two_committed_losses_redeliver_one_generation(db, pg_dsn):
    ids = await _seed_officer(db)
    started = datetime.now(timezone.utc)
    plan = await verification.create_plan(
        db,
        enabled=True,
        project_id=ids["project_id"],
        idempotency_key=str(uuid4()),
        exercise="response_loss",
        created_by=ids["user_id"],
        expires_in_seconds=900,
        response_losses=2,
        response_loss_gap_seconds=125,
        now=started,
    )

    lost_one = await service.refresh_runtime_actor_exchange(
        db,
        _refresh_request(ids["refresh_token"]),
        verification_enabled=True,
        now=started,
    )
    assert lost_one.response_lost and lost_one.actor is not None
    rotated = lost_one.actor.refresh_credential
    assert rotated != ids["refresh_token"]

    too_early = await service.refresh_runtime_actor_exchange(
        db,
        _refresh_request(ids["refresh_token"]),
        verification_enabled=True,
        now=started + timedelta(seconds=120),
    )
    assert too_early.actor is None
    assert too_early.retryable_failure_code == "verification_response_loss_spacing"

    # A fresh service pool proves that neither the fault count nor handoff
    # redelivery depends on process memory.
    restarted = PostgresDB(
        connection_string=pg_dsn,
        min_connections=1,
        max_connections=4,
    )
    await restarted.connect()
    try:
        lost_two = await service.refresh_runtime_actor_exchange(
            restarted,
            _refresh_request(ids["refresh_token"]),
            verification_enabled=True,
            now=started + timedelta(seconds=126),
        )
        assert lost_two.response_lost and lost_two.actor is not None
        assert lost_two.actor.refresh_credential == rotated

        delivered = await service.refresh_runtime_actor_exchange(
            restarted,
            _refresh_request(ids["refresh_token"]),
            verification_enabled=True,
            now=started + timedelta(seconds=127),
        )
        assert delivered.actor is not None and not delivered.response_lost
        assert delivered.actor.refresh_credential == rotated
        acknowledged = await service.refresh_runtime_actor_exchange(
            restarted,
            _refresh_request(rotated),
            verification_enabled=True,
            now=started + timedelta(seconds=128),
        )
        assert acknowledged.actor is not None and not acknowledged.response_lost
    finally:
        await restarted.close()

    completed = await verification.get_plan(
        db,
        enabled=True,
        project_id=ids["project_id"],
        now=started + timedelta(seconds=129),
    )
    assert completed["plan_id"] == plan["plan_id"]
    assert completed["state"] == "completed"
    assert completed["progress"] == {
        "losses_committed": 2,
        "next_loss_not_before": (started + timedelta(seconds=251)).isoformat(),
        "redelivery_observed": True,
        "acknowledgement_observed": True,
        "last_loss_committed_at": (started + timedelta(seconds=126)).isoformat(),
    }
    async with db.acquire() as conn:
        counts = await conn.fetchrow(
            "SELECT max(credential_generation) AS generation, "
            "(SELECT count(*) FROM runtime_actor_access_tokens) AS access "
            "FROM runtime_actor_grants WHERE revoked_at IS NULL"
        )
    assert dict(counts) == {"generation": 2, "access": 1}


@pytest.mark.asyncio
async def test_verification_failure_waits_for_real_preturn_maintenance(db):
    ids = await _seed_officer(db)
    started = datetime.now(timezone.utc)
    await verification.create_plan(
        db,
        enabled=True,
        project_id=ids["project_id"],
        idempotency_key=str(uuid4()),
        exercise="maintenance_failure",
        created_by=ids["user_id"],
        expires_in_seconds=300,
        now=started,
    )

    # A periodic heartbeat may maintain the exact grant, but it cannot consume
    # the deliberate fault before the persistent loop has persisted input.
    heartbeat = await service.refresh_runtime_actor_exchange(
        db,
        _refresh_request(ids["refresh_token"], pre_turn=False),
        verification_enabled=True,
        now=started,
    )
    assert heartbeat.actor is not None
    armed = await verification.get_plan(
        db, enabled=True, project_id=ids["project_id"], now=started
    )
    assert armed["state"] == "armed"
    assert armed["attempt_count"] == 0
    async with db.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT state ? 'runtime_actor_incident' FROM project_officers"
            )
            is False
        )

    preturn = await service.refresh_runtime_actor_exchange(
        db,
        _refresh_request(ids["refresh_token"], pre_turn=True),
        verification_enabled=True,
        now=started + timedelta(seconds=1),
    )
    assert preturn.actor is None
    assert preturn.retryable_failure_code == "verification_maintenance_failure"
    faulted = await verification.get_plan(
        db,
        enabled=True,
        project_id=ids["project_id"],
        now=started + timedelta(seconds=1),
    )
    assert faulted["state"] == "faulted"
    assert faulted["attempt_count"] == 1


@pytest.mark.asyncio
async def test_verification_maintenance_failure_pages_once_and_recovers_wake(db):
    ids = await _seed_officer(db)
    # Keep the injected protocol clock just behind the database clock so the
    # wake re-armed by recovery is immediately claimable without changing the
    # database clock or raw grant timestamps.
    started = datetime.now(timezone.utc) - timedelta(seconds=65)
    async with db.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO session_wake_events (
                thread_id, source, dedup_key, payload, fire_at, state
            ) VALUES ($1, 'officer_timer', 'verification-failure', '{}'::jsonb,
                      $2, 'pending')
            """,
            UUID(ids["thread_id"]),
            started + timedelta(hours=1),
        )
    plan = await verification.create_plan(
        db,
        enabled=True,
        project_id=ids["project_id"],
        idempotency_key=str(uuid4()),
        exercise="maintenance_failure",
        created_by=ids["user_id"],
        expires_in_seconds=900,
        now=started,
    )

    failed = await service.refresh_runtime_actor_exchange(
        db,
        _refresh_request(ids["refresh_token"]),
        verification_enabled=True,
        now=started,
    )
    assert failed.actor is None
    assert failed.retryable_failure_code == "verification_maintenance_failure"
    assert await service.admit_officer_wake_for_runtime(
        db,
        project_id=ids["project_id"],
        thread_id=ids["thread_id"],
        now=started + timedelta(seconds=1),
    ) == (True, None)
    assert not (
        await service.admit_officer_wake_for_runtime(
            db,
            project_id=ids["project_id"],
            thread_id=ids["thread_id"],
            now=started + timedelta(seconds=2),
        )
    )[0]

    contenders = await asyncio.gather(
        service.maintain_current_officer_runtime(
            db,
            project_id=ids["project_id"],
            thread_id=ids["thread_id"],
            now=started + timedelta(seconds=61),
            verification_enabled=True,
        ),
        service.maintain_current_officer_runtime(
            db,
            project_id=ids["project_id"],
            thread_id=ids["thread_id"],
            now=started + timedelta(seconds=61),
            verification_enabled=True,
        ),
    )
    assert sum(result.notification_due for result in contenders) == 1
    claim = next(result for result in contenders if result.notification_due)
    assert await service.settle_officer_runtime_incident_notification(
        db,
        project_id=ids["project_id"],
        thread_id=ids["thread_id"],
        officer_incarnation=0,
        notification_claim_id=claim.notification_claim_id,
        delivered=True,
        now=started + timedelta(seconds=62),
    )

    recovering = await verification.transition_plan(
        db,
        enabled=True,
        project_id=ids["project_id"],
        plan_id=plan["plan_id"],
        action="recover",
        actor_id=ids["user_id"],
        now=started + timedelta(seconds=63),
    )
    assert recovering["state"] == "recovering"
    recovered = await service.refresh_runtime_actor_exchange(
        db,
        _refresh_request(ids["refresh_token"]),
        verification_enabled=True,
        now=started + timedelta(seconds=64),
    )
    assert recovered.actor is not None
    final = await verification.get_plan(
        db,
        enabled=True,
        project_id=ids["project_id"],
        now=started + timedelta(seconds=65),
    )
    assert final["state"] == "completed"
    assert final["progress"]["recovery_observed"] is True
    async with db.acquire() as conn:
        incident = _json(
            await conn.fetchval(
                "SELECT state->'runtime_actor_incident' FROM project_officers"
            )
        )
        wake_at = await conn.fetchval(
            "SELECT fire_at FROM session_wake_events "
            "WHERE dedup_key = 'verification-failure'"
        )
    assert incident["status"] == "resolved"
    assert incident["notification"]["attempt_count"] == 1
    assert wake_at <= started + timedelta(seconds=64)

    claimed = await db.claim_pending_session_wake_events(limit=10)
    assert len(claimed) == 1
    assert claimed[0]["dedup_key"] == "verification-failure"
    assigned = await db.assign_session_wake_delivery_groups([int(claimed[0]["id"])])
    assert len(assigned) == 1
    delivery_id = assigned[0]["delivery_id"]
    pod_uid = "verification-runtime-pod"
    runtime_generation = uuid4()
    async with db.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "UPDATE agents SET pod_uid = $2 WHERE id = $1",
                UUID(ids["agent_id"]),
                pod_uid,
            )
            owned = await input_delivery.persist_input_delivery(
                conn,
                thread_id=ids["thread_id"],
                delivery_id=delivery_id,
                role="event",
                content="verification wake",
                source="officer_wake",
                turn_number=None,
                agent_id=ids["agent_id"],
                pod_uid=pod_uid,
                runtime_generation=runtime_generation,
            )
            assert await input_delivery.mark_input_delivery_queued(
                conn,
                delivery_id=delivery_id,
                agent_id=ids["agent_id"],
                pod_uid=pod_uid,
                runtime_generation=runtime_generation,
                claim_generation=int(owned["claim_generation"]),
            )
            assert await input_delivery.transition_input_delivery(
                conn,
                delivery_id=delivery_id,
                agent_id=ids["agent_id"],
                pod_uid=pod_uid,
                runtime_generation=runtime_generation,
                claim_generation=int(owned["claim_generation"]),
                transition="admitted",
                turn_number=1,
            )
    await db.finish_session_wake_events([int(claimed[0]["id"])])
    async with db.acquire() as conn:
        async with conn.transaction():
            assert await input_delivery.transition_input_delivery(
                conn,
                delivery_id=delivery_id,
                agent_id=ids["agent_id"],
                pod_uid=pod_uid,
                runtime_generation=runtime_generation,
                claim_generation=int(owned["claim_generation"]),
                transition="settled",
            )
    assert await db.claim_pending_session_wake_events(limit=10) == []
    async with db.acquire() as conn:
        settled = await conn.fetchrow(
            "SELECT wake.state, wake.attempts, "
            "wake.payload->>'_delivery_id' AS delivery_id, "
            "input.state AS input_state FROM session_wake_events wake "
            "JOIN thread_input_deliveries input "
            "ON input.delivery_id::text = wake.payload->>'_delivery_id' "
            "WHERE wake.dedup_key = 'verification-failure'"
        )
    assert dict(settled) == {
        "state": "sent",
        "attempts": 1,
        "delivery_id": delivery_id,
        "input_state": "settled",
    }


@pytest.mark.asyncio
async def test_verification_plan_invalidates_on_binding_change_without_secrets(db):
    ids = await _seed_officer(db)
    started = datetime.now(timezone.utc)
    plan = await verification.create_plan(
        db,
        enabled=True,
        project_id=ids["project_id"],
        idempotency_key=str(uuid4()),
        exercise="maintenance_failure",
        created_by=ids["user_id"],
        expires_in_seconds=300,
        now=started,
    )
    await _replace_officer_agent(db, ids)

    invalidated = await verification.get_plan(
        db,
        enabled=True,
        project_id=ids["project_id"],
        now=started + timedelta(seconds=1),
    )
    assert invalidated["plan_id"] == plan["plan_id"]
    assert invalidated["state"] == "invalidated"
    assert invalidated["created_by"] == ids["user_id"]
    assert len(invalidated["request_digest"]) == 64
    assert "grant_id" not in invalidated
    serialized = json.dumps(invalidated)
    assert ids["refresh_token"] not in serialized
    assert ids["access_token"] not in serialized
    assert "ciphertext" not in serialized


@pytest.mark.asyncio
async def test_verification_is_disabled_and_attempt_bounded(db):
    ids = await _seed_officer(db)
    with pytest.raises(verification.RuntimeVerificationPlanError) as disabled:
        await verification.create_plan(
            db,
            enabled=False,
            project_id=ids["project_id"],
            idempotency_key=str(uuid4()),
            exercise="maintenance_failure",
            created_by=ids["user_id"],
            expires_in_seconds=300,
        )
    assert disabled.value.code == "verification_disabled"

    started = datetime.now(timezone.utc)
    await verification.create_plan(
        db,
        enabled=True,
        project_id=ids["project_id"],
        idempotency_key=str(uuid4()),
        exercise="maintenance_failure",
        created_by=ids["user_id"],
        expires_in_seconds=300,
        now=started,
    )
    for offset in range(20):
        outcome = await service.refresh_runtime_actor_exchange(
            db,
            _refresh_request(ids["refresh_token"]),
            verification_enabled=True,
            now=started + timedelta(seconds=offset),
        )
        assert outcome.actor is None
    exhausted = await service.refresh_runtime_actor_exchange(
        db,
        _refresh_request(ids["refresh_token"]),
        verification_enabled=True,
        now=started + timedelta(seconds=21),
    )
    assert exhausted.actor is not None
    final = await verification.get_plan(
        db,
        enabled=True,
        project_id=ids["project_id"],
        now=started + timedelta(seconds=22),
    )
    assert final["state"] == "exhausted"


@pytest.mark.asyncio
async def test_verification_response_loss_is_single_winner_under_contention(db):
    ids = await _seed_officer(db)
    started = datetime.now(timezone.utc)
    await verification.create_plan(
        db,
        enabled=True,
        project_id=ids["project_id"],
        idempotency_key=str(uuid4()),
        exercise="response_loss",
        created_by=ids["user_id"],
        expires_in_seconds=300,
        response_losses=1,
        response_loss_gap_seconds=0,
        now=started,
    )

    outcomes = await asyncio.gather(
        service.refresh_runtime_actor_exchange(
            db,
            _refresh_request(ids["refresh_token"]),
            verification_enabled=True,
            now=started,
        ),
        service.refresh_runtime_actor_exchange(
            db,
            _refresh_request(ids["refresh_token"]),
            verification_enabled=True,
            now=started,
        ),
    )

    assert sum(outcome.response_lost for outcome in outcomes) == 1
    delivered = next(outcome for outcome in outcomes if not outcome.response_lost)
    assert delivered.actor is not None
    state = await verification.get_plan(
        db, enabled=True, project_id=ids["project_id"], now=started
    )
    assert state["progress"]["losses_committed"] == 1
    assert state["progress"]["redelivery_observed"] is True
    async with db.acquire() as conn:
        counts = await conn.fetchrow(
            "SELECT max(credential_generation) AS generation, "
            "(SELECT count(*) FROM runtime_actor_access_tokens) AS access "
            "FROM runtime_actor_grants WHERE project_id = $1 AND revoked_at IS NULL",
            UUID(ids["project_id"]),
        )
    assert dict(counts) == {"generation": 2, "access": 1}


@pytest.mark.asyncio
async def test_verification_plan_is_idempotent_expires_and_cannot_replay(db):
    ids = await _seed_officer(db)
    started = datetime.now(timezone.utc)
    identity = str(uuid4())
    first = await verification.create_plan(
        db,
        enabled=True,
        project_id=ids["project_id"],
        idempotency_key=identity,
        exercise="maintenance_failure",
        created_by=ids["user_id"],
        expires_in_seconds=120,
        now=started,
    )
    replay = await verification.create_plan(
        db,
        enabled=True,
        project_id=ids["project_id"],
        idempotency_key=identity,
        exercise="maintenance_failure",
        created_by=ids["user_id"],
        expires_in_seconds=120,
        now=started + timedelta(seconds=1),
    )
    assert replay["plan_id"] == first["plan_id"]
    assert first["replayed"] is False
    assert replay["replayed"] is True
    assert replay["created_by"] == ids["user_id"]
    assert replay["created_at"] == first["created_at"]
    assert len(replay["request_digest"]) == 64

    expired = await verification.get_plan(
        db,
        enabled=True,
        project_id=ids["project_id"],
        now=started + timedelta(seconds=121),
    )
    assert expired["state"] == "expired"
    replay_after_expiry = await verification.create_plan(
        db,
        enabled=True,
        project_id=ids["project_id"],
        idempotency_key=identity,
        exercise="maintenance_failure",
        created_by=ids["user_id"],
        expires_in_seconds=120,
        now=started + timedelta(seconds=122),
    )
    assert replay_after_expiry["plan_id"] == first["plan_id"]
    assert replay_after_expiry["replayed"] is True
    assert replay_after_expiry["state"] == "expired"
    fresh = await verification.create_plan(
        db,
        enabled=True,
        project_id=ids["project_id"],
        idempotency_key=str(uuid4()),
        exercise="maintenance_failure",
        created_by=ids["user_id"],
        expires_in_seconds=120,
        now=started + timedelta(seconds=122),
    )
    assert fresh["plan_id"] != first["plan_id"]
    assert fresh["replayed"] is False


@pytest.mark.asyncio
async def test_verification_create_idempotency_normalizes_and_conflicts_loudly(db):
    ids = await _seed_officer(db)
    started = datetime.now(timezone.utc)
    identity = str(uuid4())
    first = await verification.create_plan(
        db,
        enabled=True,
        project_id=ids["project_id"],
        idempotency_key=identity,
        exercise="response_loss",
        created_by=ids["user_id"],
        expires_in_seconds=900,
        now=started,
    )
    normalized_replay = await verification.create_plan(
        db,
        enabled=True,
        project_id=ids["project_id"],
        idempotency_key=identity,
        exercise="response_loss",
        created_by=ids["user_id"],
        expires_in_seconds=900,
        response_losses=1,
        response_loss_gap_seconds=125,
        now=started + timedelta(seconds=1),
    )
    assert normalized_replay["plan_id"] == first["plan_id"]
    assert normalized_replay["request_digest"] == first["request_digest"]
    assert normalized_replay["replayed"] is True

    conflicts = [
        {
            "exercise": "response_loss",
            "created_by": ids["user_id"],
            "expires_in_seconds": 900,
            "response_losses": 2,
            "response_loss_gap_seconds": 125,
        },
        {
            "exercise": "longevity",
            "created_by": ids["user_id"],
            "expires_in_seconds": 900,
            "logical_window_seconds": 60,
        },
        {
            "exercise": "response_loss",
            "created_by": ids["user_id"],
            "expires_in_seconds": 901,
            "response_losses": 1,
            "response_loss_gap_seconds": 125,
        },
        {
            "exercise": "response_loss",
            "created_by": str(uuid4()),
            "expires_in_seconds": 900,
            "response_losses": 1,
            "response_loss_gap_seconds": 125,
        },
    ]
    for request in conflicts:
        with pytest.raises(verification.RuntimeVerificationPlanError) as conflict:
            await verification.create_plan(
                db,
                enabled=True,
                project_id=ids["project_id"],
                idempotency_key=identity,
                now=started + timedelta(seconds=2),
                **request,
            )
        assert conflict.value.status_code == 409
        assert conflict.value.code == "idempotency_conflict"

    disarmed = await verification.transition_plan(
        db,
        enabled=True,
        project_id=ids["project_id"],
        plan_id=first["plan_id"],
        action="disarm",
        actor_id=ids["user_id"],
        now=started + timedelta(seconds=3),
    )
    assert disarmed["state"] == "disarmed"
    terminal_replay = await verification.create_plan(
        db,
        enabled=True,
        project_id=ids["project_id"],
        idempotency_key=identity,
        exercise="response_loss",
        created_by=ids["user_id"],
        expires_in_seconds=900,
        response_losses=1,
        response_loss_gap_seconds=125,
        now=started + timedelta(seconds=4),
    )
    assert terminal_replay["state"] == "disarmed"
    assert terminal_replay["replayed"] is True
    assert terminal_replay["disarmed_by"] == ids["user_id"]
    assert terminal_replay["disarmed_at"] == disarmed["disarmed_at"]


@pytest.mark.asyncio
async def test_verification_recover_and_disarm_replay_preserve_first_actor(db):
    ids = await _seed_officer(db)
    started = datetime.now(timezone.utc)
    first_admin = str(uuid4())
    later_admin = str(uuid4())
    plan = await verification.create_plan(
        db,
        enabled=True,
        project_id=ids["project_id"],
        idempotency_key=str(uuid4()),
        exercise="maintenance_failure",
        created_by=ids["user_id"],
        expires_in_seconds=300,
        now=started,
    )
    failed = await service.refresh_runtime_actor_exchange(
        db,
        _refresh_request(ids["refresh_token"]),
        verification_enabled=True,
        now=started + timedelta(seconds=1),
    )
    assert failed.retryable_failure_code == "verification_maintenance_failure"

    # Simulate a committed transition whose first HTTP response was lost.
    accepted = await verification.transition_plan(
        db,
        enabled=True,
        project_id=ids["project_id"],
        plan_id=plan["plan_id"],
        action="recover",
        actor_id=first_admin,
        now=started + timedelta(seconds=2),
    )
    retried = await verification.transition_plan(
        db,
        enabled=True,
        project_id=ids["project_id"],
        plan_id=plan["plan_id"],
        action="recover",
        actor_id=later_admin,
        now=started + timedelta(seconds=3),
    )
    assert accepted["replayed"] is False
    assert retried["replayed"] is True
    assert retried["recovery_requested_by"] == first_admin
    assert retried["recovery_requested_at"] == accepted["recovery_requested_at"]

    disarmed = await verification.transition_plan(
        db,
        enabled=True,
        project_id=ids["project_id"],
        plan_id=plan["plan_id"],
        action="disarm",
        actor_id=later_admin,
        now=started + timedelta(seconds=4),
    )
    disarm_replay = await verification.transition_plan(
        db,
        enabled=True,
        project_id=ids["project_id"],
        plan_id=plan["plan_id"],
        action="disarm",
        actor_id=first_admin,
        now=started + timedelta(seconds=5),
    )
    assert disarmed["state"] == "disarmed"
    assert disarmed["disarmed_by"] == later_admin
    assert disarm_replay["replayed"] is True
    assert disarm_replay["disarmed_by"] == later_admin
    assert disarm_replay["disarmed_at"] == disarmed["disarmed_at"]


@pytest.mark.asyncio
async def test_verification_concurrent_recover_has_one_attributed_winner(db):
    ids = await _seed_officer(db)
    started = datetime.now(timezone.utc)
    actors = [str(uuid4()), str(uuid4())]
    plan = await verification.create_plan(
        db,
        enabled=True,
        project_id=ids["project_id"],
        idempotency_key=str(uuid4()),
        exercise="maintenance_failure",
        created_by=ids["user_id"],
        expires_in_seconds=300,
        now=started,
    )
    await service.refresh_runtime_actor_exchange(
        db,
        _refresh_request(ids["refresh_token"]),
        verification_enabled=True,
        now=started + timedelta(seconds=1),
    )

    outcomes = await asyncio.gather(
        *(
            verification.transition_plan(
                db,
                enabled=True,
                project_id=ids["project_id"],
                plan_id=plan["plan_id"],
                action="recover",
                actor_id=actor,
                now=started + timedelta(seconds=2),
            )
            for actor in actors
        )
    )

    assert sorted(outcome["replayed"] for outcome in outcomes) == [False, True]
    assert {outcome["state"] for outcome in outcomes} == {"recovering"}
    assert len({outcome["recovery_requested_by"] for outcome in outcomes}) == 1
    assert outcomes[0]["recovery_requested_at"] == outcomes[1]["recovery_requested_at"]
    assert outcomes[0]["recovery_requested_by"] in actors


@pytest.mark.asyncio
@pytest.mark.parametrize("lifecycle", ["hold", "end", "decommission", "incarnation"])
async def test_verification_plan_tracks_officer_lifecycle_without_crossing_scope(
    db, lifecycle
):
    ids = await _seed_officer(db)
    started = datetime.now(timezone.utc)
    plan = await verification.create_plan(
        db,
        enabled=True,
        project_id=ids["project_id"],
        idempotency_key=str(uuid4()),
        exercise="maintenance_failure",
        created_by=ids["user_id"],
        expires_in_seconds=300,
        now=started,
    )
    if lifecycle == "hold":
        await db.set_project_officer_hold(
            ids["project_id"],
            expected_thread_id=ids["thread_id"],
            hold={
                "kind": "maintenance",
                "since": started.isoformat(),
                "note": "verification lifecycle proof",
            },
        )
        held = await db.get_thread(ids["thread_id"])
        assert _json(held["metadata"])["config_override"]["officer"]["hold"] == {
            "kind": "maintenance",
            "since": started.isoformat(),
            "note": "verification lifecycle proof",
        }
    elif lifecycle == "end":
        await db.end_thread(ids["thread_id"])
    elif lifecycle == "decommission":
        handoff = await db.decommission_project_officer(
            ids["project_id"],
            ids["thread_id"],
            reason="verification lifecycle proof",
        )
        assert handoff["transitioned"]
    else:
        await db.append_project_officer_incarnation(
            ids["project_id"],
            {
                "thread_id": ids["thread_id"],
                "commissioned_at": started.isoformat(),
                "decommissioned_at": started.isoformat(),
                "reason": "verification lifecycle proof",
            },
        )

    observed = await verification.get_plan(
        db,
        enabled=True,
        project_id=ids["project_id"],
        now=started + timedelta(seconds=1),
    )
    assert observed["plan_id"] == plan["plan_id"]
    assert observed["state"] == ("armed" if lifecycle == "hold" else "invalidated")


@pytest.mark.asyncio
async def test_verification_does_not_touch_workers_or_an_unrelated_officer(db):
    targeted = await _seed_officer(db)
    unrelated = await _seed_officer(db)
    started = datetime.now(timezone.utc)
    await verification.create_plan(
        db,
        enabled=True,
        project_id=targeted["project_id"],
        idempotency_key=str(uuid4()),
        exercise="response_loss",
        created_by=targeted["user_id"],
        expires_in_seconds=300,
        response_losses=1,
        response_loss_gap_seconds=0,
        now=started,
    )

    other = await service.refresh_runtime_actor_exchange(
        db,
        _refresh_request(unrelated["refresh_token"]),
        verification_enabled=True,
        now=started,
    )
    assert other.actor is not None and not other.response_lost

    worker = await service.mint_worker_runtime_actor(db, project_id=None, user_id=None)
    worker_exchange = await service.refresh_runtime_actor_exchange(
        db,
        _refresh_request(worker.refresh_credential),
        verification_enabled=True,
        now=started,
    )
    assert worker_exchange.actor is not None
    assert worker_exchange.actor.caller_kind == "worker"
    assert not worker_exchange.response_lost
