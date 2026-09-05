"""Exact pinned/stateless authority for session-owned subagents."""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from pydantic import ValidationError

import shared.persistent_input_delivery as input_delivery
from shared.persistent_input_delivery import InputDeliveryAuthorityLost
from shared.session_subagent_authority import (
    SessionParentAuthority,
    SessionParentAuthorityRefused,
    require_session_parent_authority,
    session_subagent_delivery_id,
)

THREAD = UUID("aaaaaaaa-1111-4222-8333-444444444444")
OTHER_THREAD = UUID("bbbbbbbb-1111-4222-8333-444444444444")
AGENT = UUID("cccccccc-1111-4222-8333-444444444444")
GENERATION = UUID("dddddddd-1111-4222-8333-444444444444")
ATTACH = UUID("eeeeeeee-1111-4222-8333-444444444444")
CHILD = UUID("ffffffff-1111-4222-8333-444444444444")


def _pinned() -> SessionParentAuthority:
    return SessionParentAuthority(
        execution_lane="pinned",
        parent_thread_id=THREAD,
        agent_id=AGENT,
        pod_uid="pod-uid",
        session_runtime_generation=GENERATION,
        runtime_attach_token=ATTACH,
    )


def _stateless(token: int = 9) -> SessionParentAuthority:
    return SessionParentAuthority(
        execution_lane="stateless",
        parent_thread_id=THREAD,
        lease_token=token,
        executor_id="worker-1",
        executor_pod_uid="pod-uid-1",
    )


def test_lane_shapes_are_frozen_disjoint_and_wire_safe():
    assert _pinned().to_wire() == {
        "version": 1,
        "execution_lane": "pinned",
        "parent_thread_id": str(THREAD),
        "agent_id": str(AGENT),
        "pod_uid": "pod-uid",
        "session_runtime_generation": str(GENERATION),
        "runtime_attach_token": str(ATTACH),
        "lease_token": None,
        "executor_id": None,
        "executor_pod_uid": None,
    }
    assert _stateless().to_wire()["executor_id"] == "worker-1"
    assert _pinned().for_thread(str(THREAD))
    assert not _pinned().for_thread("not-a-uuid")

    with pytest.raises(ValidationError):
        SessionParentAuthority.model_validate({**_pinned().to_wire(), "lease_token": 2})
    with pytest.raises(ValidationError):
        SessionParentAuthority.model_validate(
            {**_stateless().to_wire(), "executor_pod_uid": None}
        )
    with pytest.raises(ValidationError):
        SessionParentAuthority.model_validate(
            {**_pinned().to_wire(), "unexpected": "field"}
        )
    for field, value in (
        ("version", True),
        ("version", 1.0),
        ("lease_token", True),
        ("lease_token", 9.0),
        ("executor_id", 123),
        ("executor_id", " worker-1"),
        ("executor_pod_uid", ""),
    ):
        with pytest.raises(ValidationError):
            SessionParentAuthority.model_validate(
                {**_stateless().to_wire(), field: value}
            )
    with pytest.raises(ValidationError):
        SessionParentAuthority.model_validate(
            {**_pinned().to_wire(), "pod_uid": object()}
        )
    with pytest.raises(ValidationError):
        _pinned().pod_uid = "replacement"


@pytest.mark.asyncio
async def test_cross_parent_and_stateless_background_refuse_before_sql():
    conn = AsyncMock()
    with pytest.raises(SessionParentAuthorityRefused) as mismatch:
        await require_session_parent_authority(
            conn, _pinned(), parent_thread_id=OTHER_THREAD
        )
    assert mismatch.value.reason == "parent_mismatch"
    conn.fetchrow.assert_not_awaited()

    with pytest.raises(SessionParentAuthorityRefused) as background:
        await require_session_parent_authority(
            conn,
            _stateless(),
            parent_thread_id=THREAD,
            run_in_background=True,
        )
    assert background.value.reason == "stateless_background_unsupported"
    conn.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
async def test_pinned_gate_uses_exact_runtime_then_checks_session_shape(monkeypatch):
    lock = AsyncMock(return_value={"id": THREAD})
    monkeypatch.setattr(input_delivery, "lock_runtime_authority", lock)
    conn = AsyncMock()
    conn.fetchrow.return_value = {
        "id": THREAD,
        "kind": "session",
        "user_id": UUID(int=1),
        "project_id": None,
        "status": "active",
        "execution_lane": "pinned",
        "total_turns": 4,
    }

    row = await require_session_parent_authority(
        conn, _pinned(), parent_thread_id=THREAD
    )

    assert row["kind"] == "session"
    lock.assert_awaited_once_with(
        conn,
        thread_id=THREAD,
        agent_id=AGENT,
        pod_uid="pod-uid",
        session_runtime_generation=GENERATION,
        runtime_attach_token=ATTACH,
    )


@pytest.mark.asyncio
async def test_stateless_gate_uses_fresh_exact_turn_lease(monkeypatch):
    lock = AsyncMock(return_value=({"id": THREAD}, {"lease_token": 9}))
    monkeypatch.setattr(input_delivery, "_lock_stateless_runtime_authority", lock)
    conn = AsyncMock()
    conn.fetchrow.return_value = {
        "id": THREAD,
        "kind": "session",
        "user_id": UUID(int=1),
        "project_id": None,
        "status": "active",
        "execution_lane": "stateless",
        "total_turns": 4,
    }

    await require_session_parent_authority(conn, _stateless(), parent_thread_id=THREAD)

    lock.assert_awaited_once_with(
        conn,
        thread_id=THREAD,
        lease_token=9,
        executor_id="worker-1",
        pod_uid="pod-uid-1",
        for_update=False,
    )


@pytest.mark.asyncio
async def test_stale_runtime_is_a_typed_refusal(monkeypatch):
    lock = AsyncMock(side_effect=InputDeliveryAuthorityLost("gone"))
    monkeypatch.setattr(input_delivery, "lock_runtime_authority", lock)
    conn = AsyncMock()

    with pytest.raises(SessionParentAuthorityRefused) as excinfo:
        await require_session_parent_authority(conn, _pinned(), parent_thread_id=THREAD)
    assert excinfo.value.detail() == {
        "code": "session_parent_authority_refused",
        "reason": "pinned_parent_not_current",
    }
    conn.fetchrow.assert_not_awaited()


def test_delivery_identity_is_generation_stable():
    assert str(session_subagent_delivery_id(CHILD, GENERATION)) == str(
        session_subagent_delivery_id(str(CHILD), str(GENERATION))
    )
    assert session_subagent_delivery_id(CHILD, GENERATION) != (
        session_subagent_delivery_id(CHILD, ATTACH)
    )
