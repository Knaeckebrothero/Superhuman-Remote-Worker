"""Real-Postgres proofs for M4 safety and operator completion resolution."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio
from testcontainers.postgres import PostgresContainer

from orchestrator.services.completion_command_resolution import (
    CompletionCommandResolution,
    CompletionResolutionConflict,
)
from orchestrator.services.completion_effect_policy import COMPLETION_EFFECT_PLAN
from orchestrator.services.completion_monitor import CompletionMonitor
from shared.workspace_contract import (
    WORKSPACE_DISPATCH_AUTHORITY_CONTEXT_KEY,
    pinned_dispatch_authority_jsonb_sql,
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
async def pg(pg_dsn, _schema_applied):
    pool = await asyncpg.create_pool(pg_dsn, min_size=1, max_size=8, timeout=10)
    async with pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE run_queue, job_completion_sweep_actions, completion_effects, "
            "completion_finalizer_leases, job_completion_commands, jobs CASCADE"
        )
    try:
        yield pool
    finally:
        await pool.close()


async def _job(conn, *, status: str = "processing") -> UUID:
    return await conn.fetchval(
        "INSERT INTO jobs (description, status, execution_lane) "
        "VALUES ('M4 resolution proof', $1, 'pinned') RETURNING id",
        status,
    )


async def _agent(conn, *, status: str = "working") -> UUID:
    return await conn.fetchval(
        "INSERT INTO agents (config_name, hostname, status) "
        "VALUES ('developer', $1, $2) RETURNING id",
        f"resolution-{uuid4().hex[:10]}",
        status,
    )


async def _assign_pinned_agent(conn, job_id: UUID, agent_id: UUID, *, lease: str):
    """Attach an agent and lease the way the dispatcher's claim CAS does.

    Landing a pinned job on processing-with-an-agent is a dispatch boundary,
    and migration 0175 refuses one that carries no matching authority marker.
    The marker SQL comes from the same builder the real claim uses so this
    fixture cannot drift away from what the fence accepts.
    """
    # now() is transaction-stable, so the marker and the column it must match
    # evaluate the same lease expression to the same instant.
    authority_sql = pinned_dispatch_authority_jsonb_sql(
        agent_expr="$2::uuid", lease_expr=lease
    )
    await conn.execute(
        f"""
        UPDATE jobs
           SET assigned_agent_id = $2::uuid,
               lease_expires_at = {lease},
               context = jsonb_set(
                   COALESCE(context, '{{}}'::jsonb),
                   '{{{WORKSPACE_DISPATCH_AUTHORITY_CONTEXT_KEY}}}',
                   {authority_sql},
                   true
               )
         WHERE id = $1
        """,
        job_id,
        agent_id,
    )


async def _command(
    conn,
    job_id: UUID,
    *,
    report_seq: int = 1,
    state: str = "pending",
    reorder: bool = False,
    attempts: int = 0,
    max_attempts: int = 5,
    deadline_seconds: float = 3_600,
    lease_seconds: float | None = None,
    reported_age_seconds: float = 0,
    code_version: str = "resolution-test",
) -> UUID:
    error_code = "operator_parked" if state == "parked" else None
    finalizing_by = "live-owner" if state == "finalizing" and lease_seconds else None
    command_id = await conn.fetchval(
        """
        INSERT INTO job_completion_commands (
            job_id, report_seq, client_report_id, payload, payload_digest,
            accepted_job_status, origin, requested_by, state, attempts,
            max_attempts, run_after, lease_expires_at, deadline_at,
            finalizing_by, code_version, error_code, status_reorder_enabled,
            reported_at
        ) VALUES (
            $1::uuid, $2::bigint, $3::uuid, '{}'::jsonb, 'resolution-digest',
            'processing', 'operator', 'resolution-real-pg', $4::text, $5::int,
            $6::int, now()-interval '1 second',
            CASE WHEN $7::float8 IS NULL THEN NULL
                 ELSE now()+make_interval(secs => $7::float8) END,
            now()+make_interval(secs => $8::float8), $9::text,
            $10::text, $11::text, $12::boolean,
            now()-make_interval(secs => $13::float8)
        )
        RETURNING id
        """,
        job_id,
        report_seq,
        uuid4(),
        state,
        attempts,
        max_attempts,
        lease_seconds,
        deadline_seconds,
        finalizing_by,
        code_version,
        error_code,
        reorder,
        reported_age_seconds,
    )
    await conn.execute(
        "UPDATE jobs SET completion_seq_hwm=GREATEST(completion_seq_hwm,$2) "
        "WHERE id=$1",
        job_id,
        report_seq,
    )
    return command_id


async def _effect(
    conn,
    command_id: UUID,
    *,
    name: str,
    state: str = "pending",
    live_seconds: float | None = -1,
    attempts: int = 1,
    detail: dict | None = None,
) -> None:
    await conn.execute(
        """
        INSERT INTO completion_effects (
            producer_kind, producer_id, effect_name, effect_group, state,
            attempts, max_attempts, intent_at, complete_by, completed_at,
            detail, error_code
        ) VALUES (
            'job_completion', $1::uuid, $2::text, 'test-group', $3::text,
            $4::int, 5, now(),
            CASE WHEN $5::float8 IS NULL THEN NULL
                 ELSE now()+make_interval(secs => $5::float8) END,
            CASE WHEN $3::text IN ('done','superseded') THEN now() ELSE NULL END,
            $6::jsonb, CASE WHEN $3::text='pending' THEN 'prior_error' ELSE NULL END
        )
        """,
        command_id,
        name,
        state,
        attempts,
        live_seconds,
        json.dumps(detail or {}),
    )


async def _pending_action(conn, job_id: UUID, command_id: UUID) -> None:
    attempt = await conn.fetchval(
        "UPDATE jobs SET completion_sweep_attempt_hwm="
        "completion_sweep_attempt_hwm+1 WHERE id=$1 "
        "RETURNING completion_sweep_attempt_hwm",
        job_id,
    )
    await conn.execute(
        """
        INSERT INTO job_completion_sweep_actions (
            job_id, attempt, command_id, command_attempt, route, source
        ) VALUES ($1, $2, $3, 0, 'resume_finalizer', 'resolution-test')
        """,
        job_id,
        attempt,
        command_id,
    )


@pytest.mark.asyncio
async def test_safety_net_supersedes_old_terminal_without_callbacks(pg):
    async with pg.acquire() as conn:
        job_id = await _job(conn, status="completed")
        command_id = await _command(
            conn, job_id, reported_age_seconds=600, deadline_seconds=3_600
        )
        await _effect(conn, command_id, name="late_callback_guard", state="done")
        await _effect(conn, command_id, name="workspace_archive_teardown")
        await _pending_action(conn, job_id, command_id)

    result = await CompletionCommandResolution(pg).preclaim_command(command_id)

    assert result.disposition == "superseded"
    assert result.superseded_effects == ("workspace_archive_teardown",)
    async with pg.acquire() as conn:
        command = await conn.fetchrow(
            "SELECT state, outcome, finalizing_by, lease_expires_at "
            "FROM job_completion_commands WHERE id=$1",
            command_id,
        )
        outcome = json.loads(command["outcome"])
        assert command["state"] == "superseded"
        assert command["finalizing_by"] is None
        assert command["lease_expires_at"] is None
        assert outcome["callbacks"] is False
        assert outcome["executed"] is False
        assert outcome["accepted_job_status"] == "processing"
        assert outcome["superseded_effects"] == ["workspace_archive_teardown"]
        assert "main_status_write" in outcome["unstarted_effects"]
        assert set(outcome["abandoned_effects"]) == set(
            outcome["superseded_effects"] + outcome["unstarted_effects"]
        )
        teardown = await conn.fetchrow(
            "SELECT state, complete_by, detail FROM completion_effects "
            "WHERE producer_id=$1 AND effect_name='workspace_archive_teardown'",
            command_id,
        )
        assert teardown["state"] == "superseded"
        assert teardown["complete_by"] is None
        assert json.loads(teardown["detail"])["output"] == {
            "callbacks": False,
            "disposition": "safety_net_superseded",
            "executed": False,
            "reason": "safety_net_legacy_terminal",
        }
        assert (
            await conn.fetchval(
                "SELECT state FROM job_completion_sweep_actions WHERE command_id=$1",
                command_id,
            )
            == "done"
        )


@pytest.mark.asyncio
async def test_safety_net_never_touches_persisted_reorder_tail(pg):
    async with pg.acquire() as conn:
        job_id = await _job(conn, status="completed")
        command_id = await _command(
            conn, job_id, reorder=True, reported_age_seconds=86_400
        )
        await _effect(conn, command_id, name="workspace_archive_teardown")

    result = await CompletionCommandResolution(pg).preclaim_command(command_id)

    assert result.disposition == "not_eligible"
    assert result.reason == "persisted_reorder_tail"
    async with pg.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT state FROM job_completion_commands WHERE id=$1", command_id
            )
            == "pending"
        )
        assert (
            await conn.fetchval(
                "SELECT state FROM completion_effects WHERE producer_id=$1", command_id
            )
            == "pending"
        )


@pytest.mark.asyncio
async def test_safety_net_fresh_terminal_entry_only_gets_grace(pg):
    async with pg.acquire() as conn:
        job_id = await _job(conn, status="completed")
        command_id = await _command(conn, job_id, reported_age_seconds=1)

    result = await CompletionCommandResolution(
        pg, safety_net_grace_seconds=120
    ).preclaim_command(command_id)

    assert result.disposition == "not_eligible"
    assert result.reason == "fresh_terminal_grace"


@pytest.mark.asyncio
async def test_safety_net_stale_entry_only_nonterminal_is_superseded(pg):
    async with pg.acquire() as conn:
        job_id = await _job(conn, status="processing")
        command_id = await _command(conn, job_id, deadline_seconds=-1)

    result = await CompletionCommandResolution(pg).preclaim_command(command_id)

    assert result.disposition == "superseded"
    assert result.reason == "safety_net_stale_entry_only"


@pytest.mark.asyncio
async def test_active_s36_is_absolute_hold_after_every_clock_expires(pg):
    async with pg.acquire() as conn:
        job_id = await _job(conn, status="completed")
        command_id = await _command(
            conn,
            job_id,
            state="parked",
            attempts=5,
            deadline_seconds=-1,
            code_version="job-completion-v2",
        )
        await _effect(
            conn,
            command_id,
            name="workspace_archive_teardown",
            detail={"teardown_authorization": {"active": True, "report_seq": 1}},
        )

    resolution = CompletionCommandResolution(pg)
    safety = await resolution.preclaim_command(command_id)
    assert safety.disposition == "held"
    assert safety.reason == "workspace_teardown_authorized"
    with pytest.raises(
        CompletionResolutionConflict, match="workspace_teardown_authorized"
    ):
        await resolution.unpark(command_id, actor="operator@example.test")


@pytest.mark.asyncio
async def test_nonlocking_authority_read_avoids_effect_command_deadlock(pg):
    async with pg.acquire() as seed:
        job_id = await _job(seed, status="completed")
        command_id = await _command(
            seed,
            job_id,
            state="finalizing",
            lease_seconds=120,
            reported_age_seconds=600,
        )
        await _effect(
            seed,
            command_id,
            name="workspace_archive_teardown",
            live_seconds=60,
        )

    async with pg.acquire() as effect_owner:
        async with effect_owner.transaction():
            await effect_owner.fetchrow(
                "SELECT * FROM completion_effects WHERE producer_id=$1 FOR UPDATE",
                command_id,
            )
            result = await asyncio.wait_for(
                CompletionCommandResolution(pg).preclaim_command(command_id),
                timeout=1,
            )
            assert result.disposition == "held"
            assert result.reason == "command_owner_live"


@pytest.mark.asyncio
async def test_unpark_rearms_command_effect_and_nonlive_action(pg):
    async with pg.acquire() as conn:
        job_id = await _job(conn)
        command_id = await _command(
            conn,
            job_id,
            state="parked",
            attempts=5,
            deadline_seconds=-1,
            code_version="job-completion-v2",
        )
        await _effect(
            conn,
            command_id,
            name="terminal_merge_change_record",
            attempts=5,
            live_seconds=None,
        )
        await _pending_action(conn, job_id, command_id)

    result = await CompletionCommandResolution(
        pg, command_deadline_seconds=7_200
    ).unpark(command_id, actor="operator@example.test")

    assert result.state == "pending"
    assert result.reset_effects == ("terminal_merge_change_record",)
    async with pg.acquire() as conn:
        command = await conn.fetchrow(
            "SELECT state, attempts, error_code, code_version, "
            "deadline_at > now()+interval "
            "'119 minutes' AS deadline_reset FROM job_completion_commands "
            "WHERE id=$1",
            command_id,
        )
        effect = await conn.fetchrow(
            "SELECT attempts, error_code, complete_by, run_after <= now() AS runnable "
            "FROM completion_effects WHERE producer_id=$1",
            command_id,
        )
        assert dict(command) == {
            "state": "pending",
            "attempts": 0,
            "error_code": None,
            "code_version": "job-completion-v2",
            "deadline_reset": True,
        }
        assert dict(effect) == {
            "attempts": 0,
            "error_code": None,
            "complete_by": None,
            "runnable": True,
        }
        assert (
            await conn.fetchval(
                "SELECT state FROM job_completion_sweep_actions WHERE command_id=$1",
                command_id,
            )
            == "done"
        )


@pytest.mark.asyncio
async def test_force_resolve_writes_status_and_complete_static_skipped_plan(pg):
    incident = AsyncMock()
    async with pg.acquire() as conn:
        job_id = await _job(conn)
        command_id = await _command(conn, job_id, state="pending")
        await _effect(conn, command_id, name="late_callback_guard", state="done")
        await _effect(conn, command_id, name="terminal_merge_change_record")

    result = await CompletionCommandResolution(pg, alert=incident).force_resolve(
        command_id,
        expected_state="pending",
        terminal_status="completed",
        actor="operator@example.test",
        reason="delivery dependency permanently unavailable",
    )

    assert result.state == "force_resolved"
    assert result.terminal_status == "completed"
    assert "terminal_merge_change_record" in result.abandoned_effects
    incident.assert_awaited_once()
    async with pg.acquire() as conn:
        job = await conn.fetchrow(
            "SELECT status, completed_at FROM jobs WHERE id=$1", job_id
        )
        command = await conn.fetchrow(
            "SELECT state, outcome FROM job_completion_commands WHERE id=$1",
            command_id,
        )
        outcome = json.loads(command["outcome"])
        assert job["status"] == "completed"
        assert job["completed_at"] is not None
        assert command["state"] == "force_resolved"
        assert outcome["incident"] is True
        assert outcome["callbacks"] is False
        assert outcome["accepted_job_status"] == "processing"
        assert outcome["superseded_effects"] == ["terminal_merge_change_record"]
        plan_names = {effect.name for effect in COMPLETION_EFFECT_PLAN}
        assert set(outcome["unstarted_effects"]) == plan_names - {
            "late_callback_guard",
            "terminal_merge_change_record",
        }
        assert set(outcome["abandoned_effects"]) == plan_names - {"late_callback_guard"}


@pytest.mark.asyncio
async def test_force_resolve_rejects_conflicting_canonical_terminal_status(pg):
    async with pg.acquire() as conn:
        job_id = await _job(conn, status="completed")
        command_id = await _command(conn, job_id, state="pending")

    with pytest.raises(CompletionResolutionConflict, match="terminal_status_conflict"):
        await CompletionCommandResolution(pg).force_resolve(
            command_id,
            expected_state="pending",
            terminal_status="failed",
            actor="operator@example.test",
            reason="must not reverse a canonical terminal decision",
        )


@pytest.mark.asyncio
async def test_other_command_delivery_marker_holds_until_its_db_clock_expiry(pg):
    async with pg.acquire() as conn:
        job_id = await _job(conn)
        command_id = await _command(conn, job_id, state="parked")
        other_command_id = uuid4()
        await conn.execute(
            """
            UPDATE jobs
            SET context=jsonb_build_object(
                '_completion_control_claim',
                jsonb_build_object(
                    'version', 1,
                    'claim_id', $2::text,
                    'source', 'completion_delivery',
                    'expected_status', 'processing',
                    'expected_lane', 'pinned',
                    'fence_kind', 'completion_command',
                    'fence_value', $2::text,
                    'expires_epoch', extract(epoch FROM now()+interval '1 hour')
                )
            )
            WHERE id=$1
            """,
            job_id,
            str(other_command_id),
        )

    resolution = CompletionCommandResolution(pg)
    with pytest.raises(CompletionResolutionConflict, match="control_owner_live"):
        await resolution.unpark(command_id, actor="operator@example.test")

    async with pg.acquire() as conn:
        await conn.execute(
            """
            UPDATE jobs
            SET context=jsonb_set(
                context,
                '{_completion_control_claim,expires_epoch}',
                to_jsonb(extract(epoch FROM now()-interval '1 second'))
            )
            WHERE id=$1
            """,
            job_id,
        )

    result = await resolution.unpark(command_id, actor="operator@example.test")
    assert result.state == "pending"


@pytest.mark.asyncio
async def test_same_command_delivery_marker_is_adopted_by_unpark(pg):
    async with pg.acquire() as conn:
        job_id = await _job(conn)
        command_id = await _command(conn, job_id, state="parked")
        await conn.execute(
            """
            UPDATE jobs
            SET context=jsonb_build_object(
                '_completion_control_claim',
                jsonb_build_object(
                    'version', 1,
                    'claim_id', $2::text,
                    'source', 'completion_delivery',
                    'expected_status', 'processing',
                    'expected_lane', 'pinned',
                    'fence_kind', 'completion_command',
                    'fence_value', $2::text,
                    'expires_epoch', extract(epoch FROM now()+interval '1 hour')
                )
            )
            WHERE id=$1
            """,
            job_id,
            str(command_id),
        )

    result = await CompletionCommandResolution(pg).unpark(
        command_id, actor="operator@example.test"
    )

    assert result.state == "pending"
    async with pg.acquire() as conn:
        context = json.loads(
            await conn.fetchval("SELECT context FROM jobs WHERE id=$1", job_id)
        )
    assert context["_completion_control_claim"]["claim_id"] == str(command_id)


@pytest.mark.asyncio
async def test_same_command_delivery_marker_is_exact_cleared_by_force(pg):
    async with pg.acquire() as conn:
        job_id = await _job(conn)
        command_id = await _command(conn, job_id, state="parked")
        await conn.execute(
            """
            UPDATE jobs
            SET context=jsonb_build_object(
                '_completion_control_claim',
                jsonb_build_object(
                    'version', 1,
                    'claim_id', $2::text,
                    'source', 'completion_delivery',
                    'expected_status', 'processing',
                    'expected_lane', 'pinned',
                    'fence_kind', 'completion_command',
                    'fence_value', $2::text,
                    'expires_epoch', extract(epoch FROM now()+interval '1 hour')
                )
            )
            WHERE id=$1
            """,
            job_id,
            str(command_id),
        )

    result = await CompletionCommandResolution(pg).force_resolve(
        command_id,
        expected_state="parked",
        terminal_status="failed",
        actor="operator@example.test",
        reason="operator explicitly abandons the delivery tail",
    )

    assert result.state == "force_resolved"
    async with pg.acquire() as conn:
        marker_present = await conn.fetchval(
            "SELECT context ? '_completion_control_claim' FROM jobs WHERE id=$1",
            job_id,
        )
    assert marker_present is False


@pytest.mark.asyncio
async def test_stale_pinned_assignment_does_not_block_parked_unpark(pg):
    async with pg.acquire() as conn:
        agent_id = await _agent(conn, status="ready")
        job_id = await _job(conn)
        await _assign_pinned_agent(
            conn, job_id, agent_id, lease="now()+interval '1 hour'"
        )
        command_id = await _command(conn, job_id, state="parked")
        await conn.execute(
            "UPDATE job_completion_commands SET origin='agent', "
            "accepted_agent_id=$2 WHERE id=$1",
            command_id,
            agent_id,
        )

    result = await CompletionCommandResolution(pg).unpark(
        command_id, actor="operator@example.test"
    )
    assert result.state == "pending"


@pytest.mark.asyncio
async def test_expired_pinned_job_lease_allows_parked_force(pg):
    async with pg.acquire() as conn:
        agent_id = await _agent(conn)
        job_id = await _job(conn)
        await conn.execute(
            "UPDATE agents SET current_job_id=$2 WHERE id=$1", agent_id, job_id
        )
        await _assign_pinned_agent(
            conn, job_id, agent_id, lease="now()-interval '1 second'"
        )
        command_id = await _command(conn, job_id, state="parked")
        await conn.execute(
            "UPDATE job_completion_commands SET origin='agent', "
            "accepted_agent_id=$2 WHERE id=$1",
            command_id,
            agent_id,
        )

    result = await CompletionCommandResolution(pg).force_resolve(
        command_id,
        expected_state="parked",
        terminal_status="failed",
        actor="operator@example.test",
        reason="the pinned execution lease expired",
    )
    assert result.state == "force_resolved"


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_status", ["working", "draining"])
async def test_exact_live_pinned_executor_holds_operator_resolution(pg, agent_status):
    async with pg.acquire() as conn:
        agent_id = await _agent(conn, status=agent_status)
        job_id = await _job(conn)
        await conn.execute(
            "UPDATE agents SET current_job_id=$2 WHERE id=$1", agent_id, job_id
        )
        await _assign_pinned_agent(
            conn, job_id, agent_id, lease="now()+interval '1 hour'"
        )
        command_id = await _command(conn, job_id, state="parked")
        await conn.execute(
            "UPDATE job_completion_commands SET origin='agent', "
            "accepted_agent_id=$2 WHERE id=$1",
            command_id,
            agent_id,
        )

    resolution = CompletionCommandResolution(pg)
    with pytest.raises(CompletionResolutionConflict, match="pinned_executor_live"):
        await resolution.unpark(command_id, actor="operator@example.test")
    with pytest.raises(CompletionResolutionConflict, match="pinned_executor_live"):
        await resolution.force_resolve(
            command_id,
            expected_state="parked",
            terminal_status="failed",
            actor="operator@example.test",
            reason="executor must be quiesced before manual resolution",
        )


@pytest.mark.asyncio
async def test_operator_resolution_holds_until_stateless_lease_expires(pg):
    async with pg.acquire() as conn:
        job_id = await _job(conn)
        await conn.execute(
            "UPDATE jobs SET execution_lane='stateless' WHERE id=$1", job_id
        )
        await conn.execute(
            """
            INSERT INTO run_queue (
                unit_id, unit_kind, state, lease_token, leased_by,
                leased_until, input_seq, consumed_seq
            ) VALUES (
                $1, 'worker_batch', 'leased', 7, 'resolution-worker',
                now()+interval '1 hour', 1, 0
            )
            """,
            job_id,
        )
        command_id = await _command(conn, job_id, state="parked")

    resolution = CompletionCommandResolution(pg)
    with pytest.raises(CompletionResolutionConflict, match="stateless_executor_live"):
        await resolution.unpark(command_id, actor="operator@example.test")
    with pytest.raises(CompletionResolutionConflict, match="stateless_executor_live"):
        await resolution.force_resolve(
            command_id,
            expected_state="parked",
            terminal_status="failed",
            actor="operator@example.test",
            reason="worker lease must expire before manual resolution",
        )

    async with pg.acquire() as conn:
        await conn.execute(
            "UPDATE run_queue SET leased_until=now()-interval '1 second' "
            "WHERE unit_id=$1",
            job_id,
        )

    result = await resolution.force_resolve(
        command_id,
        expected_state="parked",
        terminal_status="failed",
        actor="operator@example.test",
        reason="expired worker lease proves the executor is quiescent",
    )
    assert result.state == "force_resolved"


@pytest.mark.asyncio
async def test_fifo_blocks_later_operator_resolution(pg):
    async with pg.acquire() as conn:
        job_id = await _job(conn)
        await _command(conn, job_id, report_seq=1, state="parked")
        later = await _command(conn, job_id, report_seq=2, state="parked")

    with pytest.raises(CompletionResolutionConflict, match="fifo_predecessor"):
        await CompletionCommandResolution(pg).unpark(
            later, actor="operator@example.test"
        )


@pytest.mark.asyncio
async def test_bounded_batch_selects_only_eligible_oldest_rows(pg):
    async with pg.acquire() as conn:
        eligible_job = await _job(conn, status="failed")
        eligible = await _command(
            conn, eligible_job, reported_age_seconds=600, state="pending"
        )
        fresh_job = await _job(conn, status="completed")
        fresh = await _command(conn, fresh_job, reported_age_seconds=1)
        reorder_job = await _job(conn, status="completed")
        reorder = await _command(
            conn, reorder_job, reorder=True, reported_age_seconds=600
        )

    batch = await CompletionCommandResolution(pg).reconcile_batch(limit=2)

    assert batch.scanned == 1
    assert [result.command_id for result in batch.results] == [str(eligible)]
    async with pg.acquire() as conn:
        states = {
            str(row["id"]): str(row["state"])
            for row in await conn.fetch(
                "SELECT id, state FROM job_completion_commands WHERE id=ANY($1::uuid[])",
                [fresh, reorder],
            )
        }
        assert states == {str(fresh): "pending", str(reorder): "pending"}


@pytest.mark.asyncio
async def test_monitor_samples_live_leader_and_oldest_including_parked(pg):
    async with pg.acquire() as conn:
        recent_job = await _job(conn)
        await _command(conn, recent_job, reported_age_seconds=10)
        old_job = await _job(conn)
        old_command = await _command(
            conn, old_job, state="parked", reported_age_seconds=4_000
        )
        await conn.execute(
            "INSERT INTO completion_finalizer_leases "
            "(lease_name,leader_id,elected_at,expires_at) "
            "VALUES ('job_completion','monitor-test',now(),now()+interval '1 minute')"
        )

    sample = await CompletionMonitor(pg, lambda _alert: None).sample()

    assert sample.live_finalizer_leaders == 1
    assert sample.oldest_command_id == str(old_command)
    assert sample.oldest_job_id == str(old_job)
    assert sample.oldest_state == "parked"
    assert sample.oldest_age_seconds is not None
    assert sample.oldest_age_seconds >= 3_999


@pytest.mark.asyncio
async def test_monitor_samples_oldest_runnable_worker_by_effective_db_time(pg):
    async with pg.acquire() as conn:
        oldest = uuid4()
        newer = uuid4()
        rows = [
            (oldest, "worker_batch", "queued", -400.0, -700.0),
            (newer, "worker_batch", "queued", -100.0, -200.0),
            (uuid4(), "worker_batch", "queued", 600.0, -10_000.0),
            (uuid4(), "session_turn", "queued", -900.0, -900.0),
            (uuid4(), "worker_batch", "parked", -900.0, -900.0),
            (uuid4(), "worker_batch", "leased", -900.0, -900.0),
            (uuid4(), "worker_batch", "done", -900.0, -900.0),
        ]
        for unit_id, kind, state, run_after_delta, queued_delta in rows:
            await conn.execute(
                """
                INSERT INTO run_queue (
                    unit_id, unit_kind, state, run_after, queued_at,
                    leased_by, leased_until
                ) VALUES (
                    $1, $2, $3,
                    clock_timestamp()+make_interval(secs => $4::float8),
                    clock_timestamp()+make_interval(secs => $5::float8),
                    CASE WHEN $3='leased' THEN 'other-pod' ELSE NULL END,
                    CASE WHEN $3='leased' THEN
                        clock_timestamp()+interval '5 minutes' ELSE NULL END
                )
                """,
                unit_id,
                kind,
                state,
                run_after_delta,
                queued_delta,
            )

    sample = await CompletionMonitor(
        pg,
        lambda _alert: None,
        completion_commands_enabled=False,
    ).sample()

    assert sample.oldest_worker_unit_id == str(oldest)
    assert sample.oldest_worker_state == "queued"
    assert sample.oldest_worker_runnable_at is not None
    assert sample.oldest_worker_age_seconds is not None
    # Effective runnable time is max(queued_at, run_after): about 400s, not
    # the row's 700s queue residence.
    assert 399 <= sample.oldest_worker_age_seconds <= 405
