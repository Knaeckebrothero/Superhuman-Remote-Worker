"""Exact parent-worker authority model and shared transactional lock gate."""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from pydantic import ValidationError

from src.shared.subagent_parent_authority import (
    ParentExecutionAuthority,
    ParentExecutionAuthorityRefused,
    require_parent_execution_authority,
)

JOB = UUID("aaaaaaaa-1111-4222-8333-444444444444")
OTHER_JOB = UUID("bbbbbbbb-1111-4222-8333-444444444444")
AGENT = UUID("cccccccc-1111-4222-8333-444444444444")


def _stateless(token: int = 7) -> ParentExecutionAuthority:
    return ParentExecutionAuthority(
        execution_lane="stateless",
        parent_job_id=JOB,
        worker_lease_token=token,
    )


def _pinned() -> ParentExecutionAuthority:
    return ParentExecutionAuthority(
        execution_lane="pinned",
        parent_job_id=JOB,
        agent_id=AGENT,
        pod_uid="pod-1",
        dispatch_process_generation="process-1",
    )


def _compact(sql: str) -> str:
    return " ".join(sql.split())


def test_lane_shapes_are_closed_and_wire_safe():
    assert _stateless().to_wire() == {
        "version": 1,
        "execution_lane": "stateless",
        "parent_job_id": str(JOB),
        "worker_lease_token": 7,
        "agent_id": None,
        "pod_uid": None,
        "dispatch_process_generation": None,
    }
    with pytest.raises(ValidationError):
        ParentExecutionAuthority(
            execution_lane="stateless",
            parent_job_id=JOB,
            worker_lease_token=0,
        )
    with pytest.raises(ValidationError):
        ParentExecutionAuthority(
            execution_lane="pinned",
            parent_job_id=JOB,
            agent_id=AGENT,
            dispatch_process_generation="process-1",
        )
    with pytest.raises(ValidationError):
        ParentExecutionAuthority.model_validate(
            {**_pinned().to_wire(), "unexpected": "field"}
        )


@pytest.mark.asyncio
async def test_stateless_gate_locks_queue_then_current_job():
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(
        side_effect=[
            {"one": 1},
            {
                "id": JOB,
                "status": "processing",
                "execution_lane": "stateless",
                "assigned_agent_id": None,
            },
        ]
    )

    row = await require_parent_execution_authority(
        conn, _stateless(), parent_job_id=JOB, mutation=True
    )

    assert row["id"] == JOB
    calls = conn.fetchrow.await_args_list
    assert "FROM run_queue" in _compact(calls[0].args[0])
    assert "lease_token = $2::bigint" in _compact(calls[0].args[0])
    assert calls[0].args[1:] == (JOB, 7)
    assert _compact(calls[1].args[0]).endswith("FOR UPDATE")
    assert calls[1].args[1:] == (JOB,)


@pytest.mark.asyncio
async def test_stateless_stolen_lease_refuses_before_job_or_child():
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)

    with pytest.raises(ParentExecutionAuthorityRefused) as excinfo:
        await require_parent_execution_authority(
            conn, _stateless(), parent_job_id=JOB, mutation=True
        )

    assert excinfo.value.reason == "stale_worker_lease"
    assert conn.fetchrow.await_count == 1
    assert "FROM run_queue" in _compact(conn.fetchrow.await_args.args[0])


@pytest.mark.asyncio
async def test_pinned_gate_locks_job_then_exact_reciprocal_process():
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(
        side_effect=[
            {
                "id": JOB,
                "status": "processing",
                "execution_lane": "pinned",
                "assigned_agent_id": AGENT,
            },
            {"id": AGENT},
        ]
    )

    await require_parent_execution_authority(
        conn, _pinned(), parent_job_id=JOB, mutation=False
    )

    job_call, agent_call = conn.fetchrow.await_args_list
    assert _compact(job_call.args[0]).endswith("FOR SHARE")
    agent_sql = _compact(agent_call.args[0])
    assert "FROM agents" in agent_sql and agent_sql.endswith("FOR SHARE")
    assert "current_job_id = $2::uuid" in agent_sql
    assert "dispatch_process_generation" in agent_sql
    assert agent_call.args[1:] == (AGENT, JOB, "pod-1", "process-1")


@pytest.mark.asyncio
async def test_pinned_replacement_refuses_before_child_access():
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(
        side_effect=[
            {
                "id": JOB,
                "status": "processing",
                "execution_lane": "pinned",
                "assigned_agent_id": AGENT,
            },
            None,
        ]
    )

    with pytest.raises(ParentExecutionAuthorityRefused) as excinfo:
        await require_parent_execution_authority(
            conn, _pinned(), parent_job_id=JOB, mutation=True
        )

    assert excinfo.value.reason == "pinned_process_not_current"
    assert conn.fetchrow.await_count == 2


@pytest.mark.asyncio
async def test_cross_parent_reuse_is_a_zero_sql_refusal():
    conn = AsyncMock()
    with pytest.raises(ParentExecutionAuthorityRefused) as excinfo:
        await require_parent_execution_authority(
            conn, _stateless(), parent_job_id=OTHER_JOB, mutation=True
        )
    assert excinfo.value.reason == "parent_mismatch"
    conn.fetchrow.assert_not_awaited()
