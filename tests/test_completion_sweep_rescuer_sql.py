"""Flag-vacuity and shared-view coverage for completion-aware rescuers."""

from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
from unittest.mock import AsyncMock

from orchestrator.database.postgres import LeaseRecoveryBatch, PostgresDB


VIEW = "job_completion_sweep_exclusions"


def _db_with_connection(conn):
    db = PostgresDB.__new__(PostgresDB)

    @asynccontextmanager
    async def acquire():
        yield conn

    db.acquire = acquire
    return db


def _assert_view_mode(sql: str, *, enabled: bool, count: int = 1) -> None:
    assert sql.count(VIEW) == (count if enabled else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_orphan_recovery_all_four_arms_share_completion_exclusion(enabled):
    conn = AsyncMock()
    conn.execute = AsyncMock(
        side_effect=["UPDATE 1", "UPDATE 2", "UPDATE 3", "UPDATE 4"]
    )
    db = _db_with_connection(conn)

    assert await db.recover_orphaned_jobs(completion_commands_enabled=enabled) == 10
    statements = [call.args[0] for call in conn.execute.await_args_list]
    sql = "\n".join(statements)
    _assert_view_mode(sql, enabled=enabled, count=4)
    assert "lease_expires_at IS NULL" in statements[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_expired_lease_recovery_uses_completion_exclusion(enabled):
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[])
    db = _db_with_connection(conn)

    assert (
        await db.recover_expired_lease_jobs(completion_commands_enabled=enabled)
        == LeaseRecoveryBatch()
    )
    _assert_view_mode(conn.fetch.await_args.args[0], enabled=enabled)


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_infra_list_and_claim_share_completion_exclusion(enabled):
    job_id = uuid4()
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[{"id": job_id}])
    conn.fetchrow = AsyncMock(return_value={"id": job_id})
    db = _db_with_connection(conn)

    assert await db.list_due_backoff_jobs(
        "infra_transient", completion_commands_enabled=enabled
    ) == [{"id": job_id}]
    assert await db.claim_backoff_redispatch(
        str(job_id),
        "infra_transient",
        completion_commands_enabled=enabled,
    )
    _assert_view_mode(conn.fetch.await_args.args[0], enabled=enabled)
    _assert_view_mode(conn.fetchrow.await_args.args[0], enabled=enabled)


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_llm_list_claim_and_fail_share_completion_exclusion(enabled):
    job_id = uuid4()
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[{"id": job_id}])
    conn.fetchrow = AsyncMock(return_value={"id": job_id})
    db = _db_with_connection(conn)

    assert await db.list_due_llm_outage_jobs(completion_commands_enabled=enabled) == [
        {"id": job_id}
    ]
    assert await db.claim_llm_outage_redispatch(
        str(job_id), completion_commands_enabled=enabled
    )
    assert await db.fail_llm_outage_job(
        str(job_id), "ceiling", completion_commands_enabled=enabled
    )

    _assert_view_mode(conn.fetch.await_args.args[0], enabled=enabled)
    assert len(conn.fetchrow.await_args_list) == 2
    for call in conn.fetchrow.await_args_list:
        _assert_view_mode(call.args[0], enabled=enabled)


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_same_host_registration_pause_uses_completion_exclusion(enabled):
    agent_id = uuid4()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"id": agent_id})
    conn.execute = AsyncMock(side_effect=["UPDATE 1", "UPDATE 1"])
    db = _db_with_connection(conn)

    result = await db.register_agent(
        config_name="defaults",
        pod_ip="10.42.0.3",
        hostname="same-host",
        completion_commands_enabled=enabled,
    )

    assert result["agent_id"] == str(agent_id)
    pause_sql = conn.execute.await_args_list[0].args[0]
    assert "UPDATE jobs" in pause_sql
    assert "lease_expires_at IS NULL" in pause_sql
    _assert_view_mode(pause_sql, enabled=enabled)
