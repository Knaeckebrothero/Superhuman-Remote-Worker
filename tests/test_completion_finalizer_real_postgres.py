"""Real-Postgres fencing proofs for the Gate-3 completion finalizer."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio
from testcontainers.postgres import PostgresContainer

from orchestrator.services.completion_finalizer import (
    CompletionDispositionSuperseded,
    CompletionFinalizer,
    CompletionLeaseLost,
)
from orchestrator.services.job_completion_commands import accept_completion_command


SCHEMA_FILE = (
    Path(__file__).resolve().parents[1]
    / "orchestrator"
    / "database"
    / "schema_current.sql"
)


@pytest.fixture(scope="module")
def pg_dsn():
    with PostgresContainer("postgres:16") as postgres:
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
            "TRUNCATE completion_effects, completion_finalizer_leases, "
            "job_completion_commands, run_queue, jobs, agents CASCADE"
        )
    try:
        yield pool
    finally:
        await pool.close()


async def _accepted_pinned(pg, *, report_id: UUID | None = None):
    async with pg.acquire() as conn:
        agent_id = await conn.fetchval(
            "INSERT INTO agents (config_name, hostname, status) "
            "VALUES ('developer', $1, 'working') RETURNING id",
            f"finalizer-{uuid4().hex[:10]}",
        )
        job_id = await conn.fetchval(
            "INSERT INTO jobs "
            "(description, status, execution_lane, assigned_agent_id) "
            "VALUES ('finalizer test', 'processing', 'pinned', $1) RETURNING id",
            agent_id,
        )
    accepted = await accept_completion_command(
        pg,
        job_id=str(job_id),
        payload={
            "should_stop": True,
            "goal_achieved": True,
            "error": None,
            "freeze_data": None,
        },
        lease_token=None,
        agent_id=str(agent_id),
        client_report_id=str(report_id or uuid4()),
        requested_by="real-pg-finalizer-test",
    )
    return accepted, job_id, agent_id


@pytest.mark.asyncio
async def test_two_claimers_execute_one_effect_and_replay_one_outcome(pg):
    accepted, _, _ = await _accepted_pinned(pg)
    effect_calls = 0

    async def workflow(runner):
        async def effect():
            nonlocal effect_calls
            effect_calls += 1
            await asyncio.sleep(0.05)
            return {"external_id": "stable-result"}

        detail = await runner.run(name="one_effect", group="proof", callback=effect)
        return {"status": "handled", "detail": detail}

    first = CompletionFinalizer(pg, workflow=workflow, leader_id="first")
    second = CompletionFinalizer(pg, workflow=workflow, leader_id="second")
    results = await asyncio.gather(
        first.finalize_command(accepted.command_id),
        second.finalize_command(accepted.command_id),
    )

    assert effect_calls == 1
    assert any(result.disposition == "done" for result in results)
    async with pg.acquire() as conn:
        command = await conn.fetchrow(
            "SELECT state, attempts, outcome, finalizing_by, lease_expires_at "
            "FROM job_completion_commands WHERE id=$1",
            UUID(accepted.command_id),
        )
        effect = await conn.fetchrow(
            "SELECT state, attempts, detail FROM completion_effects "
            "WHERE producer_kind='job_completion' AND producer_id=$1",
            UUID(accepted.command_id),
        )
    assert command["state"] == "done"
    assert command["attempts"] == 1
    assert command["finalizing_by"] is None
    assert command["lease_expires_at"] is None
    outcome = json.loads(command["outcome"])
    effect_detail = json.loads(effect["detail"])
    assert outcome["detail"] == {"external_id": "stable-result"}
    assert effect["state"] == "done"
    assert effect["attempts"] == 1
    assert effect_detail["output"] == {"external_id": "stable-result"}

    replay = await second.finalize_command(accepted.command_id)
    assert replay.disposition == "terminal"
    assert replay.outcome == outcome
    assert effect_calls == 1


@pytest.mark.asyncio
async def test_long_s36_budget_extends_exact_term_then_shrinks_before_finish(pg):
    accepted, _, _ = await _accepted_pinned(pg)
    observed: dict[str, float] = {}

    async def workflow(runner):
        async def terminal_snapshot():
            async with pg.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT extract(epoch FROM command.lease_expires_at-now()) "
                    "AS command_remaining, "
                    "extract(epoch FROM effect.complete_by-now()) "
                    "AS effect_remaining "
                    "FROM job_completion_commands command "
                    "JOIN completion_effects effect ON effect.producer_id=command.id "
                    "WHERE command.id=$1 AND effect.effect_name='workspace_teardown'",
                    UUID(accepted.command_id),
                )
            observed["command"] = float(row["command_remaining"])
            observed["effect"] = float(row["effect_remaining"])
            return {"snapshot": "command-keyed"}

        output = await runner.run(
            name="workspace_teardown",
            group="teardown",
            callback=terminal_snapshot,
            effect_timeout_seconds=890,
            command_lease_seconds=900,
        )
        async with pg.acquire() as conn:
            observed["after"] = float(
                await conn.fetchval(
                    "SELECT extract(epoch FROM lease_expires_at-now()) "
                    "FROM job_completion_commands WHERE id=$1",
                    UUID(accepted.command_id),
                )
            )
        return {"status": "handled", "teardown": output}

    result = await CompletionFinalizer(pg, workflow=workflow).finalize_command(
        accepted.command_id
    )

    assert result.disposition == "done"
    assert observed["effect"] > 880
    assert observed["command"] > observed["effect"] + 5
    assert 115 < observed["after"] <= 120


@pytest.mark.asyncio
async def test_expired_owner_is_fenced_from_renew_and_finish_after_takeover(pg):
    accepted, _, _ = await _accepted_pinned(pg)
    async with pg.acquire() as conn:
        await conn.execute(
            "UPDATE job_completion_commands SET state='finalizing', attempts=1, "
            "finalizing_by='old-owner', lease_expires_at=now()-interval '1 second' "
            "WHERE id=$1",
            UUID(accepted.command_id),
        )

    finalizer = CompletionFinalizer(pg, workflow=lambda runner: _outcome("new"))
    assert not await finalizer._renew_command(accepted.command_id, "old-owner")

    result = await finalizer.finalize_command(accepted.command_id)
    assert result.disposition == "done"
    assert result.outcome == {"status": "new"}
    with pytest.raises(CompletionLeaseLost):
        await finalizer._finish(accepted.command_id, "old-owner", {"status": "stale"})


@pytest.mark.asyncio
async def test_cancel_after_accept_supersedes_before_workflow_or_effects(pg):
    accepted, job_id, _ = await _accepted_pinned(pg)
    async with pg.acquire() as conn:
        await conn.execute(
            "UPDATE jobs SET status='cancelled' WHERE id=$1",
            job_id,
        )
    workflow_calls = 0

    async def workflow(_runner):
        nonlocal workflow_calls
        workflow_calls += 1
        return {"status": "handled", "new_status": "completed"}

    result = await CompletionFinalizer(pg, workflow=workflow).finalize_command(
        accepted.command_id
    )

    assert result.disposition == "superseded"
    assert workflow_calls == 0
    async with pg.acquire() as conn:
        command = await conn.fetchrow(
            "SELECT state, attempts, accepted_job_status, outcome, finalized_at, "
            "error_code, finalizing_by, lease_expires_at FROM "
            "job_completion_commands WHERE id=$1",
            UUID(accepted.command_id),
        )
        effect_count = await conn.fetchval(
            "SELECT count(*) FROM completion_effects "
            "WHERE producer_kind='job_completion' AND producer_id=$1",
            UUID(accepted.command_id),
        )
    outcome = json.loads(command["outcome"])
    assert command["state"] == "superseded"
    assert command["attempts"] == 1
    assert command["accepted_job_status"] == "processing"
    assert command["finalized_at"] is not None
    assert command["error_code"] == "entry_status_superseded"
    assert command["finalizing_by"] is None
    assert command["lease_expires_at"] is None
    assert outcome["observed_status"] == "cancelled"
    assert outcome["expected_entry_statuses"] == ["processing"]
    assert outcome["abandoned_effects"] == []
    assert effect_count == 0


@pytest.mark.asyncio
async def test_typed_s17_race_supersedes_exact_term_and_abandons_pending_effect(pg):
    accepted, job_id, _ = await _accepted_pinned(pg)

    async def workflow(runner):
        async def s17_attempt():
            async with pg.acquire() as conn:
                await conn.execute(
                    "UPDATE jobs SET status='cancelled' WHERE id=$1",
                    job_id,
                )
            raise CompletionDispositionSuperseded(
                observed_status="cancelled",
                expected_statuses=("processing",),
            )

        await runner.run(
            name="main_status_write",
            group="job_disposition",
            callback=s17_attempt,
        )
        raise AssertionError("typed status race did not stop the workflow")

    result = await CompletionFinalizer(pg, workflow=workflow).finalize_command(
        accepted.command_id
    )

    assert result.disposition == "superseded"
    async with pg.acquire() as conn:
        command = await conn.fetchrow(
            "SELECT state, attempts, outcome, finalized_at, error_code, "
            "finalizing_by, lease_expires_at FROM job_completion_commands "
            "WHERE id=$1",
            UUID(accepted.command_id),
        )
        effect = await conn.fetchrow(
            "SELECT state, error_code FROM completion_effects "
            "WHERE producer_kind='job_completion' AND producer_id=$1 "
            "AND effect_name='main_status_write'",
            UUID(accepted.command_id),
        )
    outcome = json.loads(command["outcome"])
    assert command["state"] == "superseded"
    assert command["attempts"] == 1
    assert command["finalized_at"] is not None
    assert command["error_code"] == "entry_status_superseded"
    assert command["finalizing_by"] is None
    assert command["lease_expires_at"] is None
    assert outcome["observed_status"] == "cancelled"
    assert outcome["abandoned_effects"] == ["main_status_write"]
    assert effect["state"] == "pending"
    assert effect["error_code"] == "CompletionDispositionSuperseded"


@pytest.mark.asyncio
async def test_post_s17_cancel_supersedes_before_class_c_effect(pg):
    accepted, job_id, _ = await _accepted_pinned(pg)
    class_c_calls = 0

    async def workflow(runner):
        async def write_s17():
            async with pg.acquire() as conn:
                await conn.execute(
                    "UPDATE jobs SET status='completed' WHERE id=$1",
                    job_id,
                )
            return {"new_status": "completed"}

        await runner.run(
            name="main_status_write",
            group="job_disposition",
            callback=write_s17,
        )
        async with pg.acquire() as conn:
            await conn.execute(
                "UPDATE jobs SET status='cancelled' WHERE id=$1",
                job_id,
            )
        await runner.assert_disposition_authority()

        async def must_not_run():
            nonlocal class_c_calls
            class_c_calls += 1
            return {"grafted": True}

        await runner.run(
            name="subjob_output_graft",
            group="subjob_graft",
            callback=must_not_run,
        )
        raise AssertionError("post-S17 cancel did not stop Class C")

    result = await CompletionFinalizer(pg, workflow=workflow).finalize_command(
        accepted.command_id
    )

    assert result.disposition == "superseded"
    assert class_c_calls == 0
    async with pg.acquire() as conn:
        command = await conn.fetchrow(
            "SELECT state, outcome, error_code FROM job_completion_commands "
            "WHERE id=$1",
            UUID(accepted.command_id),
        )
        effects = await conn.fetch(
            "SELECT effect_name, state FROM completion_effects "
            "WHERE producer_kind='job_completion' AND producer_id=$1 "
            "ORDER BY effect_name",
            UUID(accepted.command_id),
        )
    assert command["state"] == "superseded"
    assert command["error_code"] == "entry_status_superseded"
    assert json.loads(command["outcome"])["observed_status"] == "cancelled"
    assert [(row["effect_name"], row["state"]) for row in effects] == [
        ("main_status_write", "done")
    ]


@pytest.mark.asyncio
async def test_completed_s23_pause_is_command_owned_disposition_authority(pg):
    accepted, job_id, _ = await _accepted_pinned(pg)

    async def workflow(runner):
        async def write_s17():
            async with pg.acquire() as conn:
                await conn.execute(
                    "UPDATE jobs SET status='pending_review' WHERE id=$1",
                    job_id,
                )
            return {"new_status": "pending_review"}

        await runner.run(
            name="main_status_write",
            group="job_disposition",
            callback=write_s17,
        )

        async def auto_deny_resume():
            async with pg.acquire() as conn:
                await conn.execute(
                    "UPDATE jobs SET status='paused' WHERE id=$1",
                    job_id,
                )
            return {"auto_denied": True}

        await runner.run(
            name="auto_deny_resume",
            group="auto_deny_resume",
            callback=auto_deny_resume,
        )
        await runner.assert_disposition_authority()
        return {"status": "handled", "new_status": "paused"}

    result = await CompletionFinalizer(pg, workflow=workflow).finalize_command(
        accepted.command_id
    )

    assert result.disposition == "done"
    assert result.outcome == {"status": "handled", "new_status": "paused"}
    async with pg.acquire() as conn:
        assert await conn.fetchval("SELECT status FROM jobs WHERE id=$1", job_id) == (
            "paused"
        )


async def _outcome(status: str) -> dict[str, str]:
    return {"status": status}


@pytest.mark.asyncio
async def test_report_sequence_and_inline_grace_gate_background_claims(pg):
    first, job_id, agent_id = await _accepted_pinned(pg)
    second = await accept_completion_command(
        pg,
        job_id=str(job_id),
        payload={
            "should_stop": True,
            "goal_achieved": False,
            "error": None,
            "freeze_data": None,
        },
        lease_token=None,
        agent_id=str(agent_id),
        client_report_id=str(uuid4()),
        requested_by="real-pg-finalizer-test",
    )

    async def ordered_noop(_runner):
        return {"status": "done", "new_status": "processing"}

    finalizer = CompletionFinalizer(pg, workflow=ordered_noop)

    grace = await finalizer.finalize_command(first.command_id, inline=False)
    assert grace.disposition == "busy"
    assert grace.state == "pending"
    blocked = await finalizer.finalize_command(second.command_id, inline=True)
    assert blocked.disposition == "busy"
    assert blocked.state == "pending"

    assert (await finalizer.finalize_command(first.command_id)).disposition == "done"
    assert (await finalizer.finalize_command(second.command_id)).disposition == "done"


@pytest.mark.asyncio
async def test_successor_adopts_done_predecessor_status_not_stale_accept_snapshot(pg):
    first, job_id, agent_id = await _accepted_pinned(pg)
    second = await accept_completion_command(
        pg,
        job_id=str(job_id),
        payload={
            "should_stop": True,
            "goal_achieved": False,
            "error": "late crash report",
            "freeze_data": None,
        },
        lease_token=None,
        agent_id=str(agent_id),
        client_report_id=str(uuid4()),
        requested_by="real-pg-finalizer-test",
    )

    async def complete_first(_runner):
        async with pg.acquire() as conn:
            await conn.execute("UPDATE jobs SET status='completed' WHERE id=$1", job_id)
        return {"status": "handled", "new_status": "completed"}

    first_result = await CompletionFinalizer(
        pg, workflow=complete_first
    ).finalize_command(first.command_id)
    observed_entry: str | None = None

    async def absorb_late_error(runner):
        nonlocal observed_entry
        observed_entry = str(runner.command["resolved_entry_status"])
        return {"status": "handled", "new_status": "completed"}

    second_result = await CompletionFinalizer(
        pg, workflow=absorb_late_error
    ).finalize_command(second.command_id)

    assert first_result.disposition == "done"
    assert second_result.disposition == "done"
    assert observed_entry == "completed"
    async with pg.acquire() as conn:
        states = await conn.fetch(
            "SELECT report_seq, state, outcome FROM job_completion_commands "
            "WHERE job_id=$1 ORDER BY report_seq",
            job_id,
        )
        status = await conn.fetchval("SELECT status FROM jobs WHERE id=$1", job_id)
    assert [row["state"] for row in states] == ["done", "done"]
    assert json.loads(states[0]["outcome"])["new_status"] == "completed"
    assert json.loads(states[1]["outcome"])["new_status"] == "completed"
    assert status == "completed"


@pytest.mark.asyncio
async def test_destructive_effect_intent_and_higher_sequence_are_durable(pg):
    first, job_id, agent_id = await _accepted_pinned(pg)
    await accept_completion_command(
        pg,
        job_id=str(job_id),
        payload={
            "should_stop": True,
            "goal_achieved": False,
            "error": "later report",
            "freeze_data": None,
        },
        lease_token=None,
        agent_id=str(agent_id),
        client_report_id=str(uuid4()),
        requested_by="real-pg-finalizer-test",
    )
    identity = {
        "kind": "kubernetes",
        "pod_uid": "pod-uid-a",
        "pvc_uid": "pvc-uid-a",
        "service_uid": "service-uid-a",
    }

    async def workflow(runner):
        async def teardown_probe():
            assert await runner.capture_intent("workspace_teardown") is None
            assert (
                await runner.capture_intent("workspace_teardown", identity) == identity
            )
            assert await runner.capture_intent("workspace_teardown") == identity
            return {"captured": True}

        output = await runner.run(
            name="workspace_teardown",
            group="teardown",
            callback=teardown_probe,
        )
        return {"status": "handled", "teardown": output}

    result = await CompletionFinalizer(pg, workflow=workflow).finalize_command(
        first.command_id
    )

    assert result.disposition == "done"
    async with pg.acquire() as conn:
        detail = await conn.fetchval(
            "SELECT detail FROM completion_effects "
            "WHERE producer_kind='job_completion' AND producer_id=$1 "
            "AND effect_name='workspace_teardown'",
            UUID(first.command_id),
        )
    assert json.loads(detail) == {
        "intent": identity,
        "output": {"captured": True},
    }


@pytest.mark.asyncio
async def test_failure_retries_with_bounded_backoff_then_parks_at_cap(pg):
    accepted, _, _ = await _accepted_pinned(pg)
    async with pg.acquire() as conn:
        await conn.execute(
            "UPDATE job_completion_commands SET max_attempts=2 WHERE id=$1",
            UUID(accepted.command_id),
        )

    async def failing(runner):
        async def fail_effect():
            raise RuntimeError("downstream unavailable")

        await runner.run(name="failing_effect", group="proof", callback=fail_effect)
        return {"status": "unreachable"}

    finalizer = CompletionFinalizer(pg, workflow=failing, random_source=lambda: 1.0)
    first = await finalizer.finalize_command(accepted.command_id)
    assert first.disposition == "retry"
    async with pg.acquire() as conn:
        delay = await conn.fetchval(
            "SELECT extract(epoch FROM run_after-now()) "
            "FROM job_completion_commands WHERE id=$1",
            UUID(accepted.command_id),
        )
        await conn.execute(
            "UPDATE job_completion_commands SET run_after=now()-interval '1 second' "
            "WHERE id=$1",
            UUID(accepted.command_id),
        )
        await conn.execute(
            "UPDATE completion_effects SET complete_by=now()-interval '1 second' "
            "WHERE producer_kind='job_completion' AND producer_id=$1",
            UUID(accepted.command_id),
        )
    assert 4.8 <= float(delay) <= 6.1

    second = await finalizer.finalize_command(accepted.command_id, inline=False)
    assert second.disposition == "parked"
    async with pg.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT state, attempts, outcome, finalized_at, error_code "
            "FROM job_completion_commands WHERE id=$1",
            UUID(accepted.command_id),
        )
    assert dict(row) == {
        "state": "parked",
        "attempts": 2,
        "outcome": None,
        "finalized_at": None,
        "error_code": "RuntimeError",
    }


@pytest.mark.asyncio
async def test_code_version_mismatch_parks_and_alerts(pg):
    accepted, _, _ = await _accepted_pinned(pg)
    alerts: list[str] = []
    finalizer = CompletionFinalizer(
        pg,
        workflow=lambda runner: _outcome("never"),
        code_version="job-completion-v2",
        alert=alerts.append,
    )

    result = await finalizer.finalize_command(accepted.command_id)

    assert result.state == "parked"
    assert result.error_code == "code_version_mismatch"
    assert len(alerts) == 1
    assert "code version" in alerts[0]


@pytest.mark.asyncio
async def test_leader_takeover_fences_old_elected_term(pg):
    first = CompletionFinalizer(pg, leader_id="pod-a")
    second = CompletionFinalizer(pg, leader_id="pod-b")
    first_term = await first.acquire_leader()
    assert first_term is not None
    assert await second.acquire_leader() is None

    async with pg.acquire() as conn:
        await conn.execute(
            "UPDATE completion_finalizer_leases "
            "SET elected_at=now()-interval '2 seconds', "
            "expires_at=now()-interval '1 second' "
            "WHERE lease_name='job_completion'"
        )
    second_term = await second.acquire_leader()
    assert second_term is not None
    assert second_term.elected_at != first_term.elected_at
    assert not await first.renew_leader(first_term)
    assert not await first.release_leader(first_term)
    assert await second.renew_leader(second_term)


@pytest.mark.asyncio
async def test_cancelled_inline_attempt_is_resumed_after_lease_expiry(pg):
    accepted, _, _ = await _accepted_pinned(pg)
    entered = asyncio.Event()

    async def stranded(runner):
        entered.set()
        await asyncio.Event().wait()
        return {"status": "unreachable"}

    first = CompletionFinalizer(pg, workflow=stranded)
    task = asyncio.create_task(first.finalize_command(accepted.command_id))
    await asyncio.wait_for(entered.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    async with pg.acquire() as conn:
        stranded_row = await conn.fetchrow(
            "SELECT state, finalizing_by FROM job_completion_commands WHERE id=$1",
            UUID(accepted.command_id),
        )
        assert stranded_row["state"] == "finalizing"
        assert stranded_row["finalizing_by"] is not None
        await conn.execute(
            "UPDATE job_completion_commands "
            "SET lease_expires_at=now()-interval '1 second', "
            "run_after=now()-interval '1 second' WHERE id=$1",
            UUID(accepted.command_id),
        )
        # A callback with no named effect leaves no effect ambiguity window;
        # this assertion makes that precondition explicit.
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM completion_effects WHERE producer_id=$1",
                UUID(accepted.command_id),
            )
            == 0
        )

    resumed = CompletionFinalizer(pg, workflow=lambda runner: _outcome("resumed"))
    result = await resumed.finalize_command(accepted.command_id, inline=False)
    assert result.disposition == "done"
    assert result.outcome == {"status": "resumed"}
