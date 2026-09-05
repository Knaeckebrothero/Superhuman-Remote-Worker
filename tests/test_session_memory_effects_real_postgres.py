"""Real-Postgres ownership and retention proofs for session-turn effects."""

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
from orchestrator.services.session_memory_effects import SessionMemoryEffectDrain

ROOT = Path(__file__).resolve().parents[1]
APP_MIGRATIONS = ROOT / "src" / "orchestrator" / "database" / "migrations" / "app"
SESSION_COLUMNS = APP_MIGRATIONS / "0145_session_turn_memory_effects.sql"
SESSION_DRAIN_INDEX = APP_MIGRATIONS / "0146_completion_effects_session_drain.notx.sql"


@pytest.fixture(scope="module")
def pg_dsn():
    with PostgresContainer("postgres:16") as postgres:
        yield postgres.get_connection_url().replace("postgresql+psycopg2", "postgresql")


@pytest_asyncio.fixture(scope="module")
async def _schema_applied(pg_dsn):
    connection = await asyncpg.connect(pg_dsn)
    try:
        await connection.execute(
            """
            CREATE TABLE completion_effects (
                producer_kind text NOT NULL,
                producer_id uuid NOT NULL,
                scope_id uuid,
                effect_name text NOT NULL,
                effect_group text NOT NULL,
                state text NOT NULL DEFAULT 'pending',
                attempts int NOT NULL DEFAULT 0,
                max_attempts int NOT NULL DEFAULT 5,
                run_after timestamptz NOT NULL DEFAULT now(),
                created_at timestamptz NOT NULL DEFAULT now(),
                intent_at timestamptz,
                complete_by timestamptz,
                completed_at timestamptz,
                detail jsonb NOT NULL DEFAULT '{}'::jsonb,
                error_code text,
                PRIMARY KEY (producer_kind, producer_id, effect_name)
            );
            CREATE TABLE job_completion_commands (
                id uuid PRIMARY KEY,
                marker text NOT NULL
            );
            CREATE TABLE threads (
                id uuid PRIMARY KEY,
                execution_lane text NOT NULL DEFAULT 'pinned',
                status text NOT NULL DEFAULT 'ended',
                metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
                agent_id uuid,
                control_admission_agent_id uuid,
                runtime_attach_token uuid,
                runtime_generation uuid NOT NULL DEFAULT gen_random_uuid(),
                runtime_retirement_token uuid,
                runtime_retirement_permanent boolean,
                runtime_retirement_authorized_at timestamptz,
                runtime_retirement_context jsonb,
                runtime_retirement_local_quiescence jsonb,
                runtime_retirement_external_cleanup jsonb,
                runtime_authority_exposed boolean NOT NULL DEFAULT false
            );
            CREATE TABLE agents (
                id uuid PRIMARY KEY,
                thread_id uuid
            );
            CREATE TABLE run_queue (
                unit_id uuid PRIMARY KEY,
                unit_kind text,
                state text,
                lease_token bigint
            );
            CREATE TABLE docker_workspace_leases (
                owner_kind text,
                owner_id uuid,
                status text,
                quarantine_reason text,
                updated_at timestamptz
            );
            CREATE TABLE cloud_ro_mounts (
                thread_id uuid,
                status text
            );
            CREATE TABLE thread_agent_pod_provision_intents (
                thread_id uuid,
                status text
            );
            CREATE TABLE thread_agent_workspace_claims (
                thread_id uuid,
                status text
            );
            CREATE TABLE thread_turn_commits (thread_id uuid);
            CREATE TABLE thread_rewinds (thread_id uuid);
            CREATE TABLE thread_messages (thread_id uuid);
            CREATE TABLE jobs (
                id uuid PRIMARY KEY,
                created_by_thread_id uuid REFERENCES threads(id)
                    ON DELETE SET NULL,
                wake_on_complete boolean NOT NULL DEFAULT false,
                wake_state text NOT NULL DEFAULT 'none' CHECK (
                    wake_state IN (
                        'none', 'pending', 'sending', 'sent', 'dead',
                        'undeliverable'
                    )
                ),
                wake_claimed_at timestamptz,
                updated_at timestamptz NOT NULL DEFAULT now()
            );
            """
        )
        await connection.execute(SESSION_COLUMNS.read_text())
        await connection.execute(SESSION_DRAIN_INDEX.read_text())
    finally:
        await connection.close()


@pytest_asyncio.fixture
async def pool(pg_dsn, _schema_applied):
    database_pool = await asyncpg.create_pool(
        pg_dsn, min_size=1, max_size=8, timeout=10
    )
    try:
        yield database_pool
    finally:
        await database_pool.close()


@pytest_asyncio.fixture
async def db(pool):
    async with pool.acquire() as connection:
        await connection.execute(
            "TRUNCATE completion_effects, job_completion_commands, "
            "docker_workspace_leases, cloud_ro_mounts, "
            "thread_agent_pod_provision_intents, "
            "thread_agent_workspace_claims, thread_turn_commits, "
            "thread_rewinds, thread_messages, jobs, run_queue, agents, threads"
        )
    database = PostgresDB.__new__(PostgresDB)
    database._pool = pool
    return database


async def _insert_session_effect(
    pool,
    *,
    producer_id: UUID | None = None,
    state: str = "pending",
    attempts: int = 0,
    max_attempts: int = 5,
    age: str = "0 seconds",
    scope_id: UUID | None = None,
) -> UUID:
    producer_id = producer_id or uuid4()
    async with pool.acquire() as connection:
        await connection.execute(
            """
            INSERT INTO completion_effects (
                producer_kind, producer_id, scope_id, effect_name,
                effect_group, state, attempts, max_attempts,
                run_after, created_at, completed_at, detail
            ) VALUES (
                'session_turn', $1, $2, 'final_memory_extraction',
                'memory_extraction', $3, $4, $5,
                now(), now() - $6::text::interval,
                CASE WHEN $3 = 'pending' THEN NULL ELSE now() END,
                jsonb_build_object(
                    'input_message_id', $7::uuid,
                    'turn_number', 3,
                    'boundary_seq', 7,
                    'end_seq', 9,
                    'memory_scope_kind', 'thread',
                    'memory_scope_id', $2::uuid
                )
            )
            """,
            producer_id,
            scope_id or uuid4(),
            state,
            attempts,
            max_attempts,
            age,
            uuid4(),
        )
    return producer_id


@pytest.mark.asyncio
async def test_session_memory_migrations_add_nullable_identity_and_nonpartial_index(
    pool,
) -> None:
    async with pool.acquire() as connection:
        columns = {
            row["column_name"]: (row["is_nullable"], row["data_type"])
            for row in await connection.fetch(
                "SELECT column_name, is_nullable, data_type "
                "FROM information_schema.columns "
                "WHERE table_schema='public' "
                "AND ((table_name='thread_messages' "
                "AND column_name='turn_execution_id') "
                "OR (table_name='completion_effects' "
                "AND column_name='claimed_by'))"
            )
        }
        index_def = await connection.fetchval(
            "SELECT indexdef FROM pg_indexes WHERE schemaname='public' "
            "AND indexname='idx_completion_effects_session_drain'"
        )

    assert columns == {
        "turn_execution_id": ("YES", "uuid"),
        "claimed_by": ("YES", "uuid"),
    }
    assert "(producer_kind, state, run_after, created_at)" in index_def
    assert " WHERE " not in index_def.upper()


@pytest.mark.asyncio
async def test_two_drains_claim_and_execute_one_terminal_effect(db, pool) -> None:
    producer_id = await _insert_session_effect(pool)
    callback_calls = 0

    async def executor(_effect):
        nonlocal callback_calls
        callback_calls += 1
        await asyncio.sleep(0.05)
        return {"stored": 1}

    first = SessionMemoryEffectDrain(db, executor)
    second = SessionMemoryEffectDrain(db, executor)
    results = await asyncio.gather(first.drain_once(), second.drain_once())

    assert sum(result.claimed for result in results) == 1
    assert sum(result.done for result in results) == 1
    assert callback_calls == 1
    async with pool.acquire() as connection:
        row = await connection.fetchrow(
            "SELECT state, attempts, claimed_by, complete_by, detail "
            "FROM completion_effects "
            "WHERE producer_kind='session_turn' AND producer_id=$1",
            producer_id,
        )
    assert row["state"] == "done"
    assert row["attempts"] == 1
    assert row["claimed_by"] is None
    assert row["complete_by"] is None
    detail = (
        json.loads(row["detail"]) if isinstance(row["detail"], str) else row["detail"]
    )
    assert detail["turn_number"] == 3
    assert detail["output"] == {"stored": 1}


@pytest.mark.asyncio
async def test_finish_and_retry_require_exact_live_db_clock_owner(db, pool) -> None:
    producer_id = await _insert_session_effect(pool, max_attempts=2)
    first_owner = str(uuid4())
    claimed = await db.claim_session_memory_effects(
        claimed_by=first_owner, limit=1, lease_seconds=30
    )
    assert len(claimed) == 1

    assert not await db.finish_session_memory_effect(
        producer_id=str(producer_id),
        effect_name="final_memory_extraction",
        claimed_by=str(uuid4()),
        detail={"output": {}},
    )
    assert (
        await db.retry_session_memory_effect(
            producer_id=str(producer_id),
            effect_name="final_memory_extraction",
            claimed_by=first_owner,
            error_code="temporary",
            backoff_seconds=60,
        )
        == "pending"
    )
    assert not await db.claim_session_memory_effects(
        claimed_by=str(uuid4()), limit=1, lease_seconds=30
    )

    async with pool.acquire() as connection:
        await connection.execute(
            "UPDATE completion_effects SET run_after=now() "
            "WHERE producer_kind='session_turn' AND producer_id=$1",
            producer_id,
        )
    second_owner = str(uuid4())
    claimed = await db.claim_session_memory_effects(
        claimed_by=second_owner, limit=1, lease_seconds=30
    )
    assert claimed[0]["attempts"] == 2
    assert (
        await db.retry_session_memory_effect(
            producer_id=str(producer_id),
            effect_name="final_memory_extraction",
            claimed_by=second_owner,
            error_code="still_unavailable",
            backoff_seconds=60,
        )
        == "dead"
    )
    async with pool.acquire() as connection:
        row = await connection.fetchrow(
            "SELECT state, completed_at, error_code FROM completion_effects "
            "WHERE producer_kind='session_turn' AND producer_id=$1",
            producer_id,
        )
    assert row["state"] == "dead"
    assert row["completed_at"] is not None
    assert row["error_code"] == "still_unavailable"


@pytest.mark.asyncio
async def test_expired_final_attempt_is_boundedly_reconciled_dead(db, pool) -> None:
    producer_id = await _insert_session_effect(pool, attempts=3, max_attempts=3)
    async with pool.acquire() as connection:
        await connection.execute(
            "UPDATE completion_effects SET claimed_by=$2, "
            "complete_by=now() - interval '1 second' "
            "WHERE producer_kind='session_turn' AND producer_id=$1",
            producer_id,
            uuid4(),
        )

    assert not await db.claim_session_memory_effects(
        claimed_by=str(uuid4()), limit=1, lease_seconds=30
    )
    async with pool.acquire() as connection:
        row = await connection.fetchrow(
            "SELECT state, claimed_by, completed_at, error_code "
            "FROM completion_effects "
            "WHERE producer_kind='session_turn' AND producer_id=$1",
            producer_id,
        )
    assert row["state"] == "dead"
    assert row["claimed_by"] is None
    assert row["completed_at"] is not None
    assert row["error_code"] == "attempt_budget_exhausted"


@pytest.mark.asyncio
async def test_session_retention_is_bounded_and_never_prunes_commands(db, pool) -> None:
    old_done = await _insert_session_effect(pool, state="done", age="2 days")
    old_dead = await _insert_session_effect(pool, state="dead", age="8 days")
    recent_dead = await _insert_session_effect(pool, state="dead", age="2 days")
    command_id = uuid4()
    async with pool.acquire() as connection:
        await connection.execute(
            "INSERT INTO job_completion_commands (id, marker) VALUES ($1, 'keep')",
            command_id,
        )
        await connection.execute(
            """
            INSERT INTO completion_effects (
                producer_kind, producer_id, scope_id, effect_name,
                effect_group, state, created_at, completed_at
            ) VALUES (
                'job_completion', $1, $1, 'job_effect',
                'job_group', 'done', now() - interval '30 days', now()
            )
            """,
            command_id,
        )

    # LIMIT is an actual delete bound, not merely a fetch bound.
    assert (
        await db.prune_session_memory_effects(
            batch_limit=1,
            done_retention_seconds=86400,
            dead_retention_seconds=604800,
        )
        == 1
    )
    assert (
        await db.prune_session_memory_effects(
            batch_limit=1,
            done_retention_seconds=86400,
            dead_retention_seconds=604800,
        )
        == 1
    )
    async with pool.acquire() as connection:
        remaining = {
            (str(row["producer_kind"]), row["producer_id"])
            for row in await connection.fetch(
                "SELECT producer_kind, producer_id FROM completion_effects"
            )
        }
        command = await connection.fetchval(
            "SELECT marker FROM job_completion_commands WHERE id=$1", command_id
        )
    assert ("session_turn", old_done) not in remaining
    assert ("session_turn", old_dead) not in remaining
    assert ("session_turn", recent_dead) in remaining
    assert ("job_completion", command_id) in remaining
    assert command == "keep"


@pytest.mark.asyncio
async def test_permanent_delete_retains_pending_effect_source(db, pool) -> None:
    thread_id = uuid4()
    async with pool.acquire() as connection:
        await connection.execute(
            "INSERT INTO threads (id, execution_lane, status) "
            "VALUES ($1, 'pinned', 'ended')",
            thread_id,
        )
        await connection.execute(
            "INSERT INTO thread_messages (thread_id) VALUES ($1)", thread_id
        )
    producer_id = await _insert_session_effect(pool, scope_id=thread_id)

    assert await db.has_unfinished_session_memory_effects(str(thread_id))
    with pytest.raises(RuntimeError, match="waits for final-memory extraction"):
        await db.delete_thread(str(thread_id))

    async with pool.acquire() as connection:
        assert await connection.fetchval(
            "SELECT EXISTS(SELECT 1 FROM threads WHERE id=$1)", thread_id
        )
        assert await connection.fetchval(
            "SELECT EXISTS(SELECT 1 FROM thread_messages WHERE thread_id=$1)",
            thread_id,
        )
        await connection.execute(
            "UPDATE completion_effects SET state='done', completed_at=now() "
            "WHERE producer_kind='session_turn' AND producer_id=$1",
            producer_id,
        )

    assert not await db.has_unfinished_session_memory_effects(str(thread_id))
    await db.delete_thread(str(thread_id))
    async with pool.acquire() as connection:
        assert not await connection.fetchval(
            "SELECT EXISTS(SELECT 1 FROM threads WHERE id=$1)", thread_id
        )
        assert not await connection.fetchval(
            "SELECT EXISTS(SELECT 1 FROM thread_messages WHERE thread_id=$1)",
            thread_id,
        )
