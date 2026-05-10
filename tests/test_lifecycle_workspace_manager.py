"""Tests for WorkspaceInstanceManager (Phase 2a).

Covers list_instances (job + thread bound), drift, idle predicates,
snapshot/restore delegation, drain → delete, and dispatch between
job and thread workspace deletion.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.services.lifecycle import (
    Instance,
    StatefulInstanceManager,
    WorkspaceInstanceManager,
    expected_workspace_shas,
)


# =============================================================================
# Helpers
# =============================================================================


def _make_pod(name: str, labels: dict | None = None, phase: str = "Running"):
    pod = MagicMock()
    pod.metadata.name = name
    pod.metadata.labels = labels or {}
    pod.status.phase = phase
    return pod


def _make_manager(
    pods: list | None = None,
    job_rows: dict | None = None,
    thread_rows: dict | None = None,
    k8s_available: bool = True,
    snapshot_available: bool = True,
    suspension_enabled: bool = True,
):
    """Build a WorkspaceInstanceManager wrapping mocked dependencies."""
    container = MagicMock()
    container._k8s_available = k8s_available
    container._namespace = "test-ns"
    container._core_api = MagicMock()
    pod_list = MagicMock()
    pod_list.items = pods or []
    container._core_api.list_namespaced_pod.return_value = pod_list
    container.delete_workspace = AsyncMock(return_value=True)
    container.delete_thread_workspace = AsyncMock(return_value=True)

    suspension = MagicMock()
    suspension.is_enabled = suspension_enabled
    suspension.restore_workspace = AsyncMock(return_value=True)
    suspension.restore_thread_workspace = AsyncMock(return_value=True)

    snapshot = MagicMock()
    snapshot.is_available = snapshot_available
    snapshot.capture_vm_snapshot = AsyncMock(return_value=True)

    db = AsyncMock()
    db.get_job = AsyncMock(side_effect=lambda jid: (job_rows or {}).get(jid))
    db.acquire = MagicMock()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(side_effect=lambda sql, tid: (thread_rows or {}).get(tid))
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    db.acquire.return_value = ctx

    mgr = WorkspaceInstanceManager(
        container_provisioner=container,
        suspension_service=suspension,
        snapshot_service=snapshot,
        db=db,
    )
    return mgr, container, suspension, snapshot, db


async def _fake_to_thread(fn, *args, **kwargs):
    return fn(*args, **kwargs)


# =============================================================================
# expected_workspace_shas
# =============================================================================


class TestExpectedWorkspaceShas:
    def test_returns_empty_when_no_env(self, monkeypatch):
        monkeypatch.delenv("WORKSPACE_IMAGE", raising=False)
        assert expected_workspace_shas() == set()

    def test_returns_empty_when_tag_lacks_sha_prefix(self, monkeypatch):
        monkeypatch.setenv("WORKSPACE_IMAGE", "registry/workspace:latest")
        assert expected_workspace_shas() == set()

    def test_extracts_sha(self, monkeypatch):
        monkeypatch.setenv("WORKSPACE_IMAGE", "registry/workspace:sha-abc1234")
        assert expected_workspace_shas() == {"abc1234"}


# =============================================================================
# Protocol satisfaction
# =============================================================================


class TestProtocolSatisfaction:
    def test_implements_stateful_manager(self):
        mgr, *_ = _make_manager()
        assert isinstance(mgr, StatefulInstanceManager)


# =============================================================================
# list_instances
# =============================================================================


class TestListInstances:
    @pytest.mark.asyncio
    async def test_empty_when_k8s_unavailable(self):
        mgr, *_ = _make_manager(k8s_available=False)
        assert await mgr.list_instances() == []

    @pytest.mark.asyncio
    async def test_job_workspace_join(self):
        pod = _make_pod(
            "workspace-abc",
            labels={
                "srw/job-id": "job-uuid-1",
                "srw.io/component": "agent-workspace",
                "srw/build-sha": "sha1",
            },
        )
        job = {
            "id": "job-uuid-1",
            "status": "paused",
            "context": {
                "workspace_container": {
                    "status": "ready",
                    "pod_ip": "10.0.0.7",
                }
            },
        }
        mgr, *_ = _make_manager(pods=[pod], job_rows={"job-uuid-1": job})
        with patch(
            "orchestrator.services.lifecycle.workspace_manager.asyncio.to_thread",
            side_effect=_fake_to_thread,
        ):
            instances = await mgr.list_instances()
        assert len(instances) == 1
        inst = instances[0]
        assert inst.kind == "workspace"
        assert inst.id == "workspace-abc"
        assert inst.version == "sha1"
        assert inst.bound_to == "job-uuid-1"
        assert inst.metadata["job_status"] == "paused"
        assert inst.metadata["workspace_status"] == "ready"
        assert inst.metadata["pod_ip"] == "10.0.0.7"

    @pytest.mark.asyncio
    async def test_thread_workspace_uses_thread_table(self):
        # Thread workspaces carry both job-id (slot reuse) and thread-id;
        # the manager must hit the threads table, not the jobs table.
        pod = _make_pod(
            "ws-thread-xyz",
            labels={
                "srw/job-id": "thread-uuid-1",
                "srw/thread-id": "thread-uuid-1",
                "srw/component": "thread-workspace",
                "srw.io/component": "agent-workspace",
                "srw/build-sha": "sha2",
            },
        )
        thread_row = {"id": "thread-uuid-1", "status": "ended"}
        mgr, _, _, _, db = _make_manager(
            pods=[pod], thread_rows={"thread-uuid-1": thread_row}
        )
        with patch(
            "orchestrator.services.lifecycle.workspace_manager.asyncio.to_thread",
            side_effect=_fake_to_thread,
        ):
            instances = await mgr.list_instances()
        assert len(instances) == 1
        inst = instances[0]
        assert inst.bound_to == "thread-uuid-1"
        assert inst.metadata["thread_status"] == "ended"
        # Job table must NOT have been consulted for thread workspaces.
        db.get_job.assert_not_called()

    @pytest.mark.asyncio
    async def test_pod_without_db_row_still_listed(self):
        # A new workspace pod whose context hasn't been written yet still
        # appears in the listing — the manager just won't claim it idle.
        pod = _make_pod(
            "workspace-fresh",
            labels={
                "srw/job-id": "job-fresh",
                "srw.io/component": "agent-workspace",
            },
        )
        mgr, *_ = _make_manager(pods=[pod])
        with patch(
            "orchestrator.services.lifecycle.workspace_manager.asyncio.to_thread",
            side_effect=_fake_to_thread,
        ):
            instances = await mgr.list_instances()
        assert len(instances) == 1
        assert "job_status" not in instances[0].metadata


# =============================================================================
# is_idle / is_healthy
# =============================================================================


class TestIsIdle:
    @pytest.mark.asyncio
    async def test_paused_job_is_idle(self):
        mgr, *_ = _make_manager()
        inst = Instance(kind="workspace", id="x", metadata={"job_status": "paused"})
        assert await mgr.is_idle(inst) is True

    @pytest.mark.asyncio
    async def test_pending_review_is_idle(self):
        mgr, *_ = _make_manager()
        inst = Instance(
            kind="workspace", id="x", metadata={"job_status": "pending_review"}
        )
        assert await mgr.is_idle(inst) is True

    @pytest.mark.asyncio
    async def test_processing_is_not_idle(self):
        mgr, *_ = _make_manager()
        inst = Instance(kind="workspace", id="x", metadata={"job_status": "processing"})
        assert await mgr.is_idle(inst) is False

    @pytest.mark.asyncio
    async def test_ended_thread_is_idle(self):
        mgr, *_ = _make_manager()
        inst = Instance(kind="workspace", id="x", metadata={"thread_status": "ended"})
        assert await mgr.is_idle(inst) is True

    @pytest.mark.asyncio
    async def test_active_thread_is_not_idle(self):
        mgr, *_ = _make_manager()
        inst = Instance(kind="workspace", id="x", metadata={"thread_status": "active"})
        assert await mgr.is_idle(inst) is False

    @pytest.mark.asyncio
    async def test_no_status_is_not_idle(self):
        mgr, *_ = _make_manager()
        inst = Instance(kind="workspace", id="x", metadata={})
        assert await mgr.is_idle(inst) is False


class TestIsHealthy:
    @pytest.mark.asyncio
    async def test_running_pod_is_healthy(self):
        mgr, *_ = _make_manager()
        inst = Instance(kind="workspace", id="x", metadata={"pod_phase": "Running"})
        assert await mgr.is_healthy(inst) is True

    @pytest.mark.asyncio
    async def test_failed_pod_is_unhealthy(self):
        # Phase 2b will use this signal to trigger the crash detector.
        mgr, *_ = _make_manager()
        inst = Instance(kind="workspace", id="x", metadata={"pod_phase": "Failed"})
        assert await mgr.is_healthy(inst) is False


# =============================================================================
# snapshot / restore
# =============================================================================


class TestSnapshot:
    @pytest.mark.asyncio
    async def test_captures_via_snapshot_service(self):
        mgr, _, _, snapshot, _ = _make_manager()
        inst = Instance(
            kind="workspace",
            id="workspace-abc",
            bound_to="job-uuid-1",
            metadata={"pod_ip": "10.0.0.5"},
        )
        ref = await mgr.snapshot(inst)
        assert ref == "job-uuid-1"
        snapshot.capture_vm_snapshot.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_returns_none_without_pod_ip(self):
        mgr, _, _, snapshot, _ = _make_manager()
        inst = Instance(kind="workspace", id="x", bound_to="job1", metadata={})
        assert await mgr.snapshot(inst) is None
        snapshot.capture_vm_snapshot.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_none_when_snapshot_unavailable(self):
        mgr, *_ = _make_manager(snapshot_available=False)
        inst = Instance(
            kind="workspace",
            id="x",
            bound_to="job1",
            metadata={"pod_ip": "10.0.0.5"},
        )
        assert await mgr.snapshot(inst) is None


class TestRestore:
    @pytest.mark.asyncio
    async def test_restore_job_workspace(self):
        mgr, _, suspension, _, _ = _make_manager()
        inst = Instance(
            kind="workspace",
            id="workspace-abc",
            metadata={"labels": {"srw/job-id": "job1"}},
        )
        await mgr.restore(inst, "job1")
        suspension.restore_workspace.assert_awaited_once_with("job1")
        suspension.restore_thread_workspace.assert_not_called()

    @pytest.mark.asyncio
    async def test_restore_thread_workspace(self):
        mgr, _, suspension, _, _ = _make_manager()
        inst = Instance(
            kind="workspace",
            id="ws-thread-xyz",
            metadata={"labels": {"srw/thread-id": "t1"}},
        )
        await mgr.restore(inst, "t1")
        suspension.restore_thread_workspace.assert_awaited_once_with("t1")
        suspension.restore_workspace.assert_not_called()


# =============================================================================
# drain / delete
# =============================================================================


class TestSignalDrainPending:
    @pytest.mark.asyncio
    async def test_is_noop(self):
        # Workspaces have no in-pod drain hook. The soft signal is a
        # no-op; drift drain happens once the bound work goes idle.
        mgr, container, suspension, snapshot, db = _make_manager()
        inst = Instance(kind="workspace", id="x", bound_to="job1", metadata={})
        await mgr.signal_drain_pending(inst)
        container.delete_workspace.assert_not_called()
        suspension.restore_workspace.assert_not_called()
        snapshot.capture_vm_snapshot.assert_not_called()
        db.acquire.assert_not_called()


class TestDrain:
    @pytest.mark.asyncio
    async def test_job_drain_calls_delete_workspace(self):
        mgr, container, *_ = _make_manager()
        inst = Instance(
            kind="workspace",
            id="workspace-abc",
            bound_to="job1",
            metadata={"labels": {"srw/job-id": "job1"}},
        )
        await mgr.drain(inst, grace_s=10)
        container.delete_workspace.assert_awaited_once_with("job1")
        container.delete_thread_workspace.assert_not_called()

    @pytest.mark.asyncio
    async def test_thread_drain_calls_thread_delete(self):
        mgr, container, *_ = _make_manager()
        inst = Instance(
            kind="workspace",
            id="ws-thread-xyz",
            bound_to="t1",
            metadata={"labels": {"srw/thread-id": "t1"}},
        )
        await mgr.drain(inst, grace_s=10)
        container.delete_thread_workspace.assert_awaited_once_with("t1")
        container.delete_workspace.assert_not_called()

    @pytest.mark.asyncio
    async def test_drain_skipped_without_bound(self):
        mgr, container, *_ = _make_manager()
        inst = Instance(kind="workspace", id="x", bound_to=None, metadata={})
        await mgr.drain(inst, grace_s=10)
        container.delete_workspace.assert_not_called()
        container.delete_thread_workspace.assert_not_called()


class TestDeleteNoopWhenK8sUnavailable:
    @pytest.mark.asyncio
    async def test_noop(self):
        mgr, container, *_ = _make_manager(k8s_available=False)
        inst = Instance(
            kind="workspace",
            id="x",
            bound_to="job1",
            metadata={"labels": {"srw/job-id": "job1"}},
        )
        await mgr.delete(inst, grace_s=10)
        container.delete_workspace.assert_not_called()
