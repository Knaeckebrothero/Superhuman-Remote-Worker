"""Focused lifecycle discovery tests for dedicated persistent pods."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.services.lifecycle.persistent_agent_manager import (
    PersistentAgentInstanceManager,
)


def _pod(
    thread_id: str,
    *,
    sha: str = "new",
    uid: str = "pod-1",
    phase: str = "Running",
    ready: bool = True,
    age_minutes: int = 0,
):
    status = SimpleNamespace(
        phase=phase,
        container_statuses=[SimpleNamespace(ready=ready)],
    )
    metadata = SimpleNamespace(
        name=f"persistent-{thread_id[:12]}",
        uid=uid,
        deletion_timestamp=None,
        creation_timestamp=datetime.now(timezone.utc) - timedelta(minutes=age_minutes),
        labels={
            "srw/component": "persistent-agent",
            "srw/thread-id": thread_id,
            "srw/build-sha": sha,
        },
    )
    return SimpleNamespace(metadata=metadata, status=status)


def _manager(*, rows, pods, expected="new", automatic_enabled=True):
    provisioner = MagicMock()
    provisioner.is_available = True
    provisioner.expected_build_sha = expected
    provisioner._namespace = "test"
    provisioner._core_api.list_namespaced_pod.return_value = SimpleNamespace(items=pods)
    conn = AsyncMock()
    conn.fetch.return_value = rows
    acquire = AsyncMock()
    acquire.__aenter__.return_value = conn
    acquire.__aexit__.return_value = False
    db = MagicMock()
    db.acquire.return_value = acquire
    recycler = AsyncMock()
    return (
        PersistentAgentInstanceManager(
            provisioner,
            db,
            recycler,
            automatic_enabled=automatic_enabled,
        ),
        provisioner,
        recycler,
    )


def _row(thread_id: str, *, uid: str = "pod-1", recycle=None):
    metadata = {"agent_pod": {"pod_name": f"persistent-{thread_id[:12]}"}}
    if recycle is not None:
        metadata["agent_pod"]["recycle"] = recycle
    return {
        "id": thread_id,
        "project_id": "project-1",
        "metadata": metadata,
        "agent_row_id": "agent-1",
        "agent_hostname": f"persistent-{thread_id[:12]}",
        "agent_thread_id": thread_id,
        "agent_pod_uid": uid,
        "agent_status": "session",
        "agent_last_heartbeat": datetime.now(timezone.utc),
    }


@pytest.mark.asyncio
async def test_selector_and_expected_version_are_persistent_only():
    manager, provisioner, _ = _manager(rows=[], pods=[], expected="persistent-sha")
    assert await manager.expected_versions() == {"persistent-sha"}
    await manager.list_instances()
    call = provisioner._core_api.list_namespaced_pod.call_args
    assert call.kwargs["label_selector"] == "srw/component=persistent-agent"
    query = (
        manager._db.acquire.return_value.__aenter__.return_value.fetch.await_args.args[
            0
        ]
    )
    assert "JOIN project_officers" in query


@pytest.mark.asyncio
async def test_label_version_and_reciprocal_uid_are_authoritative():
    thread_id = "11111111-1111-1111-1111-111111111111"
    manager, _, _ = _manager(
        rows=[_row(thread_id)], pods=[_pod(thread_id, sha="stale")]
    )
    [instance] = await manager.list_instances()
    assert instance.version == "stale"
    assert instance.metadata["reciprocal"] is True
    assert instance.metadata["observation"].pod_uid == "pod-1"


@pytest.mark.asyncio
async def test_missing_pod_does_not_trust_lingering_agent_row():
    thread_id = "22222222-2222-2222-2222-222222222222"
    manager, _, recycler = _manager(rows=[_row(thread_id)], pods=[])
    [instance] = await manager.list_instances()
    assert instance.version == "__persistent_pod_missing__"
    assert instance.metadata["agent_present"] is True
    assert instance.metadata["reciprocal"] is False

    await manager.signal_drain_pending(instance)
    recycler.request_and_reconcile.assert_awaited_once()
    assert recycler.request_and_reconcile.await_args.kwargs["reason"] == "missing_pod"


@pytest.mark.asyncio
async def test_disabled_rollout_fence_keeps_observation_but_blocks_mutation():
    thread_id = "28282828-2828-2828-2828-282828282828"
    manager, _, recycler = _manager(
        rows=[_row(thread_id)],
        pods=[],
        automatic_enabled=False,
    )
    [instance] = await manager.list_instances()
    assert instance.version == "__persistent_pod_missing__"
    assert manager.automatic_enabled is False

    await manager.signal_drain_pending(instance)
    await manager.drain(instance, 0)
    await manager.delete(instance, 0)

    recycler.request_and_reconcile.assert_not_awaited()


@pytest.mark.asyncio
async def test_disabled_rollout_fence_finishes_manual_generation():
    thread_id = "29292929-2929-2929-2929-292929292929"
    manager, _, recycler = _manager(
        rows=[_row(thread_id, recycle={"phase": "awaiting_replacement"})],
        pods=[_pod(thread_id)],
        automatic_enabled=False,
    )
    [instance] = await manager.list_instances()
    assert instance.version == "__persistent_recycle_active__"

    await manager.signal_drain_pending(instance)

    recycler.request_and_reconcile.assert_awaited_once_with(
        thread_id=thread_id,
        reason="image_drift",
        expected_build_sha="new",
        observation=instance.metadata["observation"],
        expected_project_id="project-1",
    )


@pytest.mark.asyncio
async def test_active_recycle_remains_reconcilable_on_current_image():
    thread_id = "33333333-3333-3333-3333-333333333333"
    manager, _, _ = _manager(
        rows=[_row(thread_id, recycle={"phase": "awaiting_replacement"})],
        pods=[_pod(thread_id, sha="new")],
    )
    [instance] = await manager.list_instances()
    assert instance.version == "__persistent_recycle_active__"
    assert await manager.is_idle(instance) is False


@pytest.mark.asyncio
async def test_pod_uid_mismatch_forces_authority_reconciliation():
    thread_id = "44444444-4444-4444-4444-444444444444"
    manager, _, _ = _manager(
        rows=[_row(thread_id, uid="old")], pods=[_pod(thread_id, uid="new")]
    )
    [instance] = await manager.list_instances()
    assert instance.version == "__persistent_authority_mismatch__"
    assert instance.metadata["reason"] == "authority_mismatch"


@pytest.mark.asyncio
async def test_current_build_with_offline_agent_is_not_treated_as_current():
    thread_id = "55555555-5555-5555-5555-555555555555"
    row = _row(thread_id)
    row["agent_status"] = "offline"
    manager, _, _ = _manager(rows=[row], pods=[_pod(thread_id)])
    [instance] = await manager.list_instances()
    assert instance.version == "__persistent_authority_mismatch__"
    assert instance.metadata["agent_live"] is False


@pytest.mark.asyncio
async def test_ready_pod_without_agent_gets_boot_grace_then_recovers():
    thread_id = "66666666-6666-6666-6666-666666666666"
    row = _row(thread_id)
    for key in (
        "agent_row_id",
        "agent_hostname",
        "agent_thread_id",
        "agent_pod_uid",
        "agent_status",
        "agent_last_heartbeat",
    ):
        row[key] = None

    manager, _, _ = _manager(rows=[row], pods=[_pod(thread_id, age_minutes=1)])
    [booting] = await manager.list_instances()
    assert booting.version == "new"

    manager, _, _ = _manager(rows=[row], pods=[_pod(thread_id, age_minutes=4)])
    [wedged] = await manager.list_instances()
    assert wedged.version == "__persistent_authority_mismatch__"


@pytest.mark.asyncio
async def test_terminal_current_build_is_recovered():
    thread_id = "77777777-7777-7777-7777-777777777777"
    manager, _, _ = _manager(
        rows=[_row(thread_id)], pods=[_pod(thread_id, phase="Failed", ready=False)]
    )
    [instance] = await manager.list_instances()
    assert instance.version == "__persistent_authority_mismatch__"
