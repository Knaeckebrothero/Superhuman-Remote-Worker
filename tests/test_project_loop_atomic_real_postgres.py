"""Real-Postgres contention proof for atomic project-loop advancement."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
from testcontainers.postgres import PostgresContainer

from orchestrator.database.postgres import PostgresDB
from orchestrator.services.project_loop_atomic import (
    LoopAdvanceExpectation,
    LoopAdvanceMutation,
    materialize_loop_advance_atomic,
    plan_loop_advance,
)

SCHEMA_FILE = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "orchestrator"
    / "database"
    / "schema_current.sql"
)


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
            "TRUNCATE project_loops, job_datasources, jobs, projects CASCADE"
        )
    try:
        yield store
    finally:
        await store.close()


async def _seed_turn(db: PostgresDB):
    loop_id = uuid4()
    member_id = uuid4()
    project_id = uuid4()
    campaign = {
        "id": "campaign-atomic",
        "cursor": 1,
        "status": "active",
        "stages": [{"role": "developer"}, {"role": "critic"}],
    }
    async with db.acquire() as conn:
        await conn.execute(
            "INSERT INTO projects (id, name) VALUES ($1, 'atomic loop')",
            project_id,
        )
        await conn.execute(
            """
            INSERT INTO jobs (
                id, description, status, project_id, execution_lane, context
            ) VALUES (
                $1, 'finished campaign member', 'completed', $2, 'pinned',
                $3::jsonb
            )
            """,
            member_id,
            project_id,
            json.dumps(
                {
                    "loop_id": str(loop_id),
                    "loop_role": "developer",
                    "loop_iteration": 1,
                    "loop_seq_index": 1,
                    "loop_remaining": 5,
                }
            ),
        )
        await conn.execute(
            """
            INSERT INTO project_loops (
                id, project_id, status, role_sequence, seq_index,
                max_iterations, remaining_iterations,
                max_consecutive_failures, current_job_id,
                current_stage_jobs, total_jobs_run,
                scheduling, campaign, campaign_history
            ) VALUES (
                $1, $2, 'running', $3::jsonb, 1,
                6, 5, 3, $4::uuid,
                jsonb_build_array(($4::uuid)::text), 1,
                'campaign', $5::jsonb, '[]'::jsonb
            )
            """,
            loop_id,
            project_id,
            json.dumps(["critic", "developer"]),
            member_id,
            json.dumps(campaign),
        )
    loop = await db.get_project_loop(str(loop_id))
    assert loop is not None
    states = await db.get_loop_stage_member_statuses([str(member_id)])
    return loop, states, campaign, member_id


@pytest.mark.asyncio
async def test_two_contenders_commit_one_campaign_successor_set(db):
    loop, states, campaign, member_id = await _seed_turn(db)
    expected = LoopAdvanceExpectation.from_rows(loop, states)
    next_campaign = {**campaign, "cursor": 2, "stages_done": 1}
    mutation = LoopAdvanceMutation(
        stage="critic",
        seq_index=1,
        remaining_iterations=4,
        consecutive_failures=0,
        last_error=None,
        campaign_changed=True,
        campaign=next_campaign,
        extra_context={
            "loop_campaign_id": "campaign-atomic",
            "loop_campaign_index": 1,
        },
        replay={"provision": True, "dispatch": True},
    )

    async def contend():
        return await materialize_loop_advance_atomic(
            db,
            loop_id=str(loop["id"]),
            member_job_id=str(member_id),
            expected=expected,
            mutation=mutation,
            backlog_block="prepared outside transaction",
            history_block="prepared outside transaction",
        )

    first, second = await asyncio.gather(contend(), contend())
    winners = [result for result in (first, second) if result["won"]]
    losers = [result for result in (first, second) if not result["won"]]
    assert len(winners) == 1
    assert len(losers) == 1
    assert losers[0]["reason"] == "loop_world_changed"
    successor_ids = winners[0]["spawned_job_ids"]
    assert len(successor_ids) == 1

    async with db.acquire() as conn:
        persisted_loop = await conn.fetchrow(
            "SELECT current_job_id, current_stage_jobs, total_jobs_run, "
            "remaining_iterations, campaign FROM project_loops WHERE id=$1",
            loop["id"],
        )
        count = await conn.fetchval(
            "SELECT count(*) FROM jobs WHERE context->>'loop_id'=$1 AND id <> $2",
            str(loop["id"]),
            member_id,
        )
        successor = await conn.fetchrow(
            "SELECT status, context FROM jobs WHERE id=$1",
            successor_ids[0],
        )

    assert count == 1
    assert str(persisted_loop["current_job_id"]) == successor_ids[0]
    stage = persisted_loop["current_stage_jobs"]
    if isinstance(stage, str):
        stage = json.loads(stage)
    assert stage == successor_ids
    assert int(persisted_loop["total_jobs_run"]) == 2
    assert int(persisted_loop["remaining_iterations"]) == 4
    persisted_campaign = persisted_loop["campaign"]
    if isinstance(persisted_campaign, str):
        persisted_campaign = json.loads(persisted_campaign)
    assert persisted_campaign == next_campaign
    context = successor["context"]
    if isinstance(context, str):
        context = json.loads(context)
    assert successor["status"] == "created"
    assert context["loop_campaign_id"] == "campaign-atomic"
    assert context["loop_campaign_index"] == 1
    assert context["cloud_baseline"]["state"] == "seeding"

    predecessor = await db.get_job(str(member_id))
    predecessor_context = predecessor["context"]
    if isinstance(predecessor_context, str):
        predecessor_context = json.loads(predecessor_context)
    marker = predecessor_context["_project_loop_advance_handoff"]
    assert marker["state"] == "pending"
    assert marker["output"] == {"applicable": True, **winners[0]}

    # Two commandless sweeper leaders see the same descriptor, but the exact
    # DB-clock lease admits only one external-tail owner.
    claimant_ids = [f"sweeper-{uuid4()}", f"sweeper-{uuid4()}"]
    claims = await asyncio.gather(
        *(
            db.claim_project_loop_handoff(
                str(member_id),
                expected_output=marker["output"],
                claimant_id=claimant_id,
                lease_seconds=120,
            )
            for claimant_id in claimant_ids
        )
    )
    assert sorted(claims) == [False, True]
    winner_claimant = claimant_ids[claims.index(True)]

    handoff_result = {"actions": ["persisted exact successor"]}
    # DB-clock expiry is an ownership boundary. The old claimant cannot renew
    # or acknowledge after expiry; a new exact-output claimant takes over.
    async with db.acquire() as conn:
        await conn.execute(
            """
            UPDATE jobs
            SET context=jsonb_set(
                context,
                '{_project_loop_advance_handoff,claim_expires_epoch}',
                to_jsonb(extract(epoch FROM now()) - 1),
                false
            )
            WHERE id=$1
            """,
            member_id,
        )
    assert (
        await db.renew_project_loop_handoff(
            str(member_id),
            expected_output=marker["output"],
            claimant_id=winner_claimant,
            lease_seconds=120,
        )
        is False
    )
    with pytest.raises(RuntimeError, match="marker changed"):
        await db.finish_project_loop_handoff(
            str(member_id),
            expected_output=marker["output"],
            result=handoff_result,
            claimant_id=winner_claimant,
        )
    takeover_claimant = f"sweeper-{uuid4()}"
    assert await db.claim_project_loop_handoff(
        str(member_id),
        expected_output=marker["output"],
        claimant_id=takeover_claimant,
        lease_seconds=120,
    )
    assert (
        await db.finish_project_loop_handoff(
            str(member_id),
            expected_output=marker["output"],
            result=handoff_result,
            claimant_id=takeover_claimant,
        )
        == handoff_result
    )
    # Response loss after the marker UPDATE returns the stored result without
    # reopening the external tail.
    assert (
        await db.finish_project_loop_handoff(
            str(member_id),
            expected_output=marker["output"],
            result={"actions": ["must not replace"]},
            claimant_id=claimant_ids[1 - claims.index(True)],
        )
        == handoff_result
    )


@pytest.mark.asyncio
async def test_vector_ttl_turn_ledger_survives_response_loss(db, monkeypatch):
    """INSERT-ledger + TTL UPDATE are one vector transaction and replay once."""

    migration = (
        Path(__file__).resolve().parents[1]
        / "src/orchestrator/database/migrations/vector/0018_project_loop_ttl_effects.sql"
    ).read_text()
    project_id, loop_id, member_id = uuid4(), uuid4(), uuid4()
    async with db.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS knowledge_index (
                id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                project_id uuid NOT NULL,
                status text NOT NULL,
                remaining_cycles int
            )
            """
        )
        # Migration is deliberately idempotent; freshness runs replay it from
        # zero and an already-live Tilt deployment must also remain safe.
        await conn.execute(migration)
        await conn.execute(migration)
        await conn.execute(
            "INSERT INTO knowledge_index (project_id,status,remaining_cycles) "
            "VALUES ($1,'active',3)",
            project_id,
        )

    import orchestrator.main

    monkeypatch.setattr(orchestrator.main, "vector_db", db)
    args = {
        "loop_id": str(loop_id),
        "project_id": str(project_id),
        "completed_member_id": str(member_id),
        "total_jobs_run": 4,
    }
    assert await orchestrator.main._decrement_project_loop_kb_ttl_once(**args) is True
    # Models a crash after vector COMMIT but before handoff/effect ack.
    assert await orchestrator.main._decrement_project_loop_kb_ttl_once(**args) is False

    async with db.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT remaining_cycles FROM knowledge_index WHERE project_id=$1",
                project_id,
            )
            == 2
        )
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM project_loop_ttl_effects "
                "WHERE loop_id=$1 AND total_jobs_run=4",
                loop_id,
            )
            == 1
        )

    with pytest.raises(RuntimeError, match="different turn"):
        await orchestrator.main._decrement_project_loop_kb_ttl_once(
            **{**args, "completed_member_id": str(uuid4())}
        )


@pytest.mark.asyncio
async def test_loop_notification_response_loss_dedups_bell_and_sse(db, monkeypatch):
    loop, _states, _campaign, member_id = await _seed_turn(db)
    user_id = uuid4()
    async with db.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (id,display_name) VALUES ($1,'Loop Owner')", user_id
        )

    import orchestrator.main
    from orchestrator.services.notification_feed import notification_feed

    monkeypatch.setattr(orchestrator.main, "postgres_db", db)
    broadcast = MagicMock()
    monkeypatch.setattr(notification_feed, "broadcast", broadcast)
    # The bell row is a feed row: wire the singleton to this DB + feed (attrs
    # only, so nothing leaks past the test).
    from orchestrator.services.notification_service import notification_service

    monkeypatch.setattr(notification_service, "_db", db)
    monkeypatch.setattr(notification_service, "_available", True)
    monkeypatch.setattr(notification_service, "_notification_feed", notification_feed)
    monkeypatch.setattr(notification_service, "_email_service", None)
    loop = {**loop, "owner_id": user_id}
    kwargs = {
        "job_id": str(member_id),
        "event_type": "loop_campaign_disposition",
        "subject": "Campaign done",
        "message": "The durable bell is the recovery source.",
        "dedup_turn_identity": f"{member_id}:2",
        "note_id": "planned:0",
    }
    await orchestrator.main._notify_loop_event(loop, **kwargs)
    # Models a response loss after durable insert + SSE but before handoff ack.
    await orchestrator.main._notify_loop_event(loop, **kwargs)

    async with db.acquire() as conn:
        # The durable bell row is a feed row now (unified notification system):
        # keyed on the turn identity, so the replay lands on the same row.
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM notifications "
                "WHERE recipient_id=$1 AND category='loop_event' AND subject=$2",
                user_id,
                kwargs["subject"],
            )
            == 1
        )
    broadcast.assert_called_once()

    with pytest.raises(RuntimeError, match="different payload"):
        await orchestrator.main._notify_loop_event(
            loop,
            **{**kwargs, "message": "identity drift"},
        )


@pytest.mark.asyncio
async def test_multibyte_campaign_handoff_descriptor_stays_below_effect_cap(db):
    """A 20 KiB+ campaign label cannot inflate the persisted replay marker."""

    loop, states, campaign, member_id = await _seed_turn(db)
    huge = "界" * 20_000
    campaign = {**campaign, "title": huge}
    async with db.acquire() as conn:
        await conn.execute(
            "UPDATE project_loops SET campaign=$2::jsonb WHERE id=$1",
            loop["id"],
            json.dumps(campaign, ensure_ascii=False),
        )
    loop = await db.get_project_loop(str(loop["id"]))
    mutation = plan_loop_advance(
        loop,
        completed_job={"id": str(member_id)},
        completed_context={
            "loop_id": str(loop["id"]),
            "loop_campaign_id": "campaign-atomic",
            "loop_campaign_index": 0,
        },
        member_states=states,
        failed=False,
        member_error=None,
        deadline_passed=False,
    )
    result = await materialize_loop_advance_atomic(
        db,
        loop_id=str(loop["id"]),
        member_job_id=str(member_id),
        expected=LoopAdvanceExpectation.from_rows(loop, states),
        mutation=mutation,
        backlog_block="prepared",
        history_block="prepared",
    )
    assert result["won"] is True

    async with db.acquire() as conn:
        marker_bytes = await conn.fetchval(
            "SELECT pg_column_size(context->'_project_loop_advance_handoff') "
            "FROM jobs WHERE id=$1",
            member_id,
        )
        marker = await conn.fetchval(
            "SELECT context->'_project_loop_advance_handoff' FROM jobs WHERE id=$1",
            member_id,
        )
    if isinstance(marker, str):
        marker = json.loads(marker)
    assert marker_bytes < 8 * 1024
    assert (
        len(json.dumps(marker["output"], ensure_ascii=False).encode("utf-8")) < 8 * 1024
    )
    assert len(marker["output"]["replay"]["action"]["label"].encode("utf-8")) <= 256
    assert huge not in json.dumps(marker, ensure_ascii=False)
