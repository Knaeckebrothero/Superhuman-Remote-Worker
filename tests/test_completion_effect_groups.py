"""Real-Postgres contracts for independently retryable completion groups."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio
from testcontainers.postgres import PostgresContainer

from orchestrator.services.completion_finalizer import (
    CompletionFinalizer,
    CompletionLeaseLost,
)
from orchestrator.services.job_completion_commands import accept_completion_command


SCHEMA_FILE = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "orchestrator"
    / "database"
    / "schema_current.sql"
)


@pytest.fixture(scope="module")
def group_pg_dsn():
    with PostgresContainer("postgres:16") as postgres:
        yield postgres.get_connection_url().replace("postgresql+psycopg2", "postgresql")


@pytest_asyncio.fixture(scope="module")
async def _group_schema_applied(group_pg_dsn):
    conn = await asyncpg.connect(group_pg_dsn)
    try:
        await conn.execute(SCHEMA_FILE.read_text())
    finally:
        await conn.close()


@pytest_asyncio.fixture
async def group_pg(group_pg_dsn, _group_schema_applied):
    pool = await asyncpg.create_pool(group_pg_dsn, min_size=1, max_size=4, timeout=10)
    async with pool.acquire() as conn:
        await conn.execute(
            "TRUNCATE completion_effects, completion_finalizer_leases, "
            "job_completion_commands, run_queue, jobs, agents CASCADE"
        )
    try:
        yield pool
    finally:
        await pool.close()


async def _accepted(group_pg):
    async with group_pg.acquire() as conn:
        agent_id = await conn.fetchval(
            "INSERT INTO agents (config_name, hostname, status) "
            "VALUES ('developer', $1, 'working') RETURNING id",
            f"group-{uuid4().hex[:10]}",
        )
        job_id = await conn.fetchval(
            "INSERT INTO jobs "
            "(description, status, execution_lane, assigned_agent_id) "
            "VALUES ('effect group test', 'processing', 'pinned', $1) RETURNING id",
            agent_id,
        )
    accepted = await accept_completion_command(
        group_pg,
        job_id=str(job_id),
        payload={"should_stop": True},
        lease_token=None,
        agent_id=str(agent_id),
        client_report_id=str(uuid4()),
        requested_by="effect-group-test",
    )
    return accepted


@pytest.mark.asyncio
async def test_failed_group_does_not_block_independent_group_and_resumes(group_pg):
    accepted = await _accepted(group_pg)
    calls = {"delivery": 0, "teardown": 0}

    async def workflow(runner):
        async def delivery():
            calls["delivery"] += 1
            if calls["delivery"] == 1:
                raise OSError("point cloud unavailable")
            return {"delivered": True}

        async def teardown():
            calls["teardown"] += 1
            return {"deleted_uid": "pod-uid-a"}

        delivery_result = await runner.run(
            name="cloud_delivery",
            group="delivery",
            callback=delivery,
            retry_on_error=True,
            error_output=lambda exc: {"delivered": False, "error": type(exc).__name__},
        )
        teardown_result = await runner.run(
            name="workspace_teardown",
            group="teardown",
            callback=teardown,
        )
        return {"delivery": delivery_result, "teardown": teardown_result}

    finalizer = CompletionFinalizer(
        group_pg, workflow=workflow, random_source=lambda: 0.0
    )
    first = await finalizer.finalize_command(accepted.command_id)

    assert first.disposition == "effects_pending"
    assert calls == {"delivery": 1, "teardown": 1}
    async with group_pg.acquire() as conn:
        command = await conn.fetchrow(
            "SELECT state, attempts, run_after > now() AS deferred "
            "FROM job_completion_commands WHERE id=$1",
            UUID(accepted.command_id),
        )
        effects = await conn.fetch(
            "SELECT effect_name, state, attempts, run_after > now() AS deferred, "
            "detail FROM completion_effects WHERE producer_id=$1 ORDER BY effect_name",
            UUID(accepted.command_id),
        )
    assert dict(command) == {"state": "pending", "attempts": 0, "deferred": True}
    assert [(row["effect_name"], row["state"]) for row in effects] == [
        ("cloud_delivery", "pending"),
        ("workspace_teardown", "done"),
    ]
    assert effects[0]["deferred"] is True
    assert json.loads(effects[0]["detail"])["output"] == {
        "delivered": False,
        "error": "OSError",
    }

    async with group_pg.acquire() as conn:
        await conn.execute(
            "UPDATE job_completion_commands SET run_after=now()-interval '1 second' "
            "WHERE id=$1",
            UUID(accepted.command_id),
        )
        await conn.execute(
            "UPDATE completion_effects SET run_after=now()-interval '1 second' "
            "WHERE producer_id=$1 AND state='pending'",
            UUID(accepted.command_id),
        )

    second = await finalizer.finalize_command(accepted.command_id, inline=False)
    assert second.disposition == "done"
    assert second.outcome == {
        "delivery": {"delivered": True},
        "teardown": {"deleted_uid": "pod-uid-a"},
    }
    assert calls == {"delivery": 2, "teardown": 1}


@pytest.mark.asyncio
async def test_dependency_group_waits_while_unrelated_group_runs(group_pg):
    accepted = await _accepted(group_pg)
    calls: list[str] = []

    async def workflow(runner):
        async def fail_validation():
            calls.append("validation")
            return {"validated": False, "error": "validation unavailable"}

        async def irreversible():
            calls.append("irreversible")
            return {"merged": True}

        async def cleanup():
            calls.append("cleanup")
            return {"deleted": True}

        await runner.run(
            name="delivery_validation",
            group="delivery",
            callback=fail_validation,
            retry_if=lambda output: bool(output.get("error")),
        )
        blocked = await runner.run(
            name="irreversible_merge",
            group="terminal",
            callback=irreversible,
            retry_on_error=True,
            error_output=lambda exc: {"merged": False, "blocked": True},
            depends_on_groups=("delivery",),
        )
        cleanup_result = await runner.run(
            name="workspace_cleanup",
            group="teardown",
            callback=cleanup,
        )
        return {"merge": blocked, "cleanup": cleanup_result}

    result = await CompletionFinalizer(
        group_pg, workflow=workflow, random_source=lambda: 0.0
    ).finalize_command(accepted.command_id)

    assert result.disposition == "effects_pending"
    assert calls == ["validation", "cleanup"]
    async with group_pg.acquire() as conn:
        names = await conn.fetch(
            "SELECT effect_name, state FROM completion_effects "
            "WHERE producer_id=$1 ORDER BY effect_name",
            UUID(accepted.command_id),
        )
    assert [(row["effect_name"], row["state"]) for row in names] == [
        ("delivery_validation", "pending"),
        ("workspace_cleanup", "done"),
    ]


@pytest.mark.asyncio
async def test_effect_group_budget_parks_only_after_later_group_runs(group_pg):
    accepted = await _accepted(group_pg)
    calls: list[str] = []

    async def workflow(runner):
        async def unavailable():
            calls.append("delivery")
            raise ConnectionError("still unavailable")

        async def cleanup():
            calls.append("cleanup")
            return {"deleted": True}

        await runner.run(
            name="cloud_delivery",
            group="delivery",
            callback=unavailable,
            retry_on_error=True,
            error_output=lambda exc: {"delivered": False},
        )
        await runner.run(name="workspace_cleanup", group="teardown", callback=cleanup)
        return {"status": "legacy-shaped"}

    async with group_pg.acquire() as conn:
        # The first intent inherits this default after it is inserted; make the
        # test cap explicit by pre-seeding the stable row under a temporary term.
        await conn.execute(
            "UPDATE job_completion_commands SET state='finalizing', attempts=1, "
            "finalizing_by='seed', lease_expires_at=now()+interval '2 minutes' "
            "WHERE id=$1",
            UUID(accepted.command_id),
        )
        await conn.execute(
            "INSERT INTO completion_effects "
            "(producer_kind, producer_id, scope_id, effect_name, effect_group, "
            "state, attempts, max_attempts, run_after, intent_at, complete_by) "
            "SELECT 'job_completion', id, job_id, 'cloud_delivery', 'delivery', "
            "'pending', 0, 1, now()-interval '1 second', now()-interval '2 minutes', "
            "now()-interval '1 second' FROM job_completion_commands WHERE id=$1",
            UUID(accepted.command_id),
        )
        await conn.execute(
            "UPDATE job_completion_commands SET state='pending', attempts=0, "
            "finalizing_by=NULL, lease_expires_at=NULL WHERE id=$1",
            UUID(accepted.command_id),
        )

    result = await CompletionFinalizer(
        group_pg, workflow=workflow, random_source=lambda: 0.0
    ).finalize_command(accepted.command_id)

    assert result.disposition == "parked"
    assert result.error_code == "effect_group_attempts_exhausted"
    assert calls == ["delivery", "cleanup"]


@pytest.mark.asyncio
async def test_finish_cas_refuses_any_nonterminal_effect_row(group_pg):
    accepted = await _accepted(group_pg)

    async def inject_pending(runner):
        async with group_pg.acquire() as conn:
            await conn.execute(
                "INSERT INTO completion_effects "
                "(producer_kind, producer_id, scope_id, effect_name, effect_group) "
                "VALUES ('job_completion', $1, $2, 'stranded', 'delivery')",
                UUID(accepted.command_id),
                UUID(str(runner.command["job_id"])),
            )
        return {"status": "must-not-finish"}

    with pytest.raises(CompletionLeaseLost):
        await CompletionFinalizer(group_pg, workflow=inject_pending).finalize_command(
            accepted.command_id
        )

    # The runner did not explicitly mark a local group failure, so the final
    # command CAS is the last line of defence and must refuse done.
    async with group_pg.acquire() as conn:
        state = await conn.fetchval(
            "SELECT state FROM job_completion_commands WHERE id=$1",
            UUID(accepted.command_id),
        )
    assert state == "finalizing"
