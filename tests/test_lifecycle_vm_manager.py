"""Tests for VMInstanceManager (Phase 3).

VMs are tracked in jobs.context.vm and threads.metadata.vm rather than
as a fleet K8s object, so list_instances reads from the DB. The manager
delegates dispatch to the multi-backend VMProvisioner — tests don't need
to know which backend is active.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.services.lifecycle import (
    Instance,
    StatefulInstanceManager,
    VMInstanceManager,
    expected_vm_shas,
)


# =============================================================================
# Helpers
# =============================================================================


def _make_manager(
    job_rows: list[dict] | None = None,
    thread_rows: list[dict] | None = None,
    is_available: bool = True,
    snapshot_available: bool = True,
    suspension_enabled: bool = True,
):
    provisioner = MagicMock()
    provisioner.is_available = is_available
    provisioner.delete_vm = AsyncMock(return_value=True)
    provisioner.delete_thread_vm = AsyncMock(return_value=True)

    suspension = MagicMock()
    suspension.is_enabled = suspension_enabled
    suspension.restore_workspace = AsyncMock(return_value=True)
    suspension.restore_thread_workspace = AsyncMock(return_value=True)

    snapshot = MagicMock()
    snapshot.is_available = snapshot_available
    snapshot.capture_vm_snapshot = AsyncMock(return_value=True)

    db = AsyncMock()
    db.acquire = MagicMock()
    conn = AsyncMock()

    # Two-call pattern: first fetch is jobs, second is threads.
    conn.fetch = AsyncMock(side_effect=[job_rows or [], thread_rows or []])
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=False)
    db.acquire.return_value = ctx

    mgr = VMInstanceManager(
        vm_provisioner=provisioner,
        suspension_service=suspension,
        snapshot_service=snapshot,
        db=db,
    )
    return mgr, provisioner, suspension, snapshot, db


# =============================================================================
# expected_vm_shas
# =============================================================================


class TestExpectedVMShas:
    def test_empty_when_no_env(self, monkeypatch):
        monkeypatch.delenv("DEFAULT_VM_IMAGE", raising=False)
        assert expected_vm_shas() == set()

    def test_extracts_sha(self, monkeypatch):
        monkeypatch.setenv("DEFAULT_VM_IMAGE", "registry/vm:sha-abc123")
        assert expected_vm_shas() == {"abc123"}

    def test_skips_latest(self, monkeypatch):
        monkeypatch.setenv("DEFAULT_VM_IMAGE", "registry/vm:latest")
        assert expected_vm_shas() == set()


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
    async def test_empty_when_provisioner_unavailable(self):
        mgr, *_ = _make_manager(is_available=False)
        assert await mgr.list_instances() == []

    @pytest.mark.asyncio
    async def test_returns_job_vm(self):
        job = {
            "id": "job-uuid-1",
            "status": "paused",
            "context": {
                "vm": {
                    "status": "ready",
                    "ssh_host": "10.0.0.42",
                    "ssh_port": 22,
                    "vm_image": "registry/vm:sha-abc123",
                    "provisioner": "kubevirt",
                }
            },
        }
        mgr, *_ = _make_manager(job_rows=[job])
        instances = await mgr.list_instances()
        assert len(instances) == 1
        inst = instances[0]
        assert inst.kind == "vm"
        assert inst.bound_to == "job-uuid-1"
        assert inst.version == "abc123"
        assert inst.metadata["scope"] == "job"
        assert inst.metadata["job_status"] == "paused"
        assert inst.metadata["vm_status"] == "ready"
        assert inst.metadata["ssh_host"] == "10.0.0.42"
        assert inst.metadata["provisioner"] == "kubevirt"

    @pytest.mark.asyncio
    async def test_returns_thread_vm(self):
        thread = {
            "id": "thread-uuid-1",
            "status": "ended",
            "metadata": {
                "vm": {
                    "status": "ready",
                    "ssh_host": "10.0.0.7",
                    "vm_image": "registry/vm:sha-xyz789",
                }
            },
        }
        mgr, *_ = _make_manager(thread_rows=[thread])
        instances = await mgr.list_instances()
        assert len(instances) == 1
        inst = instances[0]
        assert inst.metadata["scope"] == "thread"
        assert inst.metadata["thread_status"] == "ended"
        assert inst.version == "xyz789"

    @pytest.mark.asyncio
    async def test_handles_jsonb_returned_as_string(self):
        # asyncpg sometimes surfaces JSONB as a string.
        import json

        job = {
            "id": "job-2",
            "status": "processing",
            "context": json.dumps(
                {"vm": {"status": "ready", "vm_image": "registry/vm:sha-aa"}}
            ),
        }
        mgr, *_ = _make_manager(job_rows=[job])
        instances = await mgr.list_instances()
        assert len(instances) == 1
        assert instances[0].version == "aa"

    @pytest.mark.asyncio
    async def test_synthesizes_id_when_no_native_name(self):
        job = {
            "id": "job-uuid-noname",
            "status": "paused",
            "context": {"vm": {"status": "ready"}},
        }
        mgr, *_ = _make_manager(job_rows=[job])
        instances = await mgr.list_instances()
        assert instances[0].id.startswith("vm-job-")

    @pytest.mark.asyncio
    async def test_skips_empty_vm_context(self):
        # Defensive: a row with vm = {} shouldn't produce an instance.
        # (The SQL filter excludes this, but the parser should be safe
        # against it too in case of stale rows.)
        job = {"id": "j", "status": "paused", "context": {"vm": {}}}
        mgr, *_ = _make_manager(job_rows=[job])
        instances = await mgr.list_instances()
        assert instances == []


# =============================================================================
# is_healthy / is_idle
# =============================================================================


class TestIsHealthy:
    @pytest.mark.asyncio
    async def test_failed_is_unhealthy(self):
        mgr, *_ = _make_manager()
        inst = Instance(kind="vm", id="x", metadata={"vm_status": "failed"})
        assert await mgr.is_healthy(inst) is False

    @pytest.mark.asyncio
    async def test_ready_is_healthy(self):
        mgr, *_ = _make_manager()
        inst = Instance(kind="vm", id="x", metadata={"vm_status": "ready"})
        assert await mgr.is_healthy(inst) is True

    @pytest.mark.asyncio
    async def test_creating_is_healthy(self):
        # Creating/restoring/etc. — anything not 'failed' is healthy.
        # Crash recovery only kicks in on the explicit failure signal.
        mgr, *_ = _make_manager()
        for status in ("creating", "restoring", "suspended", None):
            inst = Instance(kind="vm", id="x", metadata={"vm_status": status})
            assert await mgr.is_healthy(inst) is True


class TestIsIdle:
    @pytest.mark.asyncio
    async def test_paused_job_is_idle(self):
        mgr, *_ = _make_manager()
        inst = Instance(kind="vm", id="x", metadata={"job_status": "paused"})
        assert await mgr.is_idle(inst) is True

    @pytest.mark.asyncio
    async def test_processing_job_is_not_idle(self):
        mgr, *_ = _make_manager()
        inst = Instance(kind="vm", id="x", metadata={"job_status": "processing"})
        assert await mgr.is_idle(inst) is False

    @pytest.mark.asyncio
    async def test_ended_thread_is_idle(self):
        mgr, *_ = _make_manager()
        inst = Instance(kind="vm", id="x", metadata={"thread_status": "ended"})
        assert await mgr.is_idle(inst) is True


# =============================================================================
# snapshot / restore / drain / delete
# =============================================================================


class TestSnapshot:
    @pytest.mark.asyncio
    async def test_captures_via_snapshot_service(self):
        mgr, _, _, snapshot, _ = _make_manager()
        inst = Instance(
            kind="vm",
            id="vm-foo",
            bound_to="job-1",
            metadata={"ssh_host": "10.0.0.5", "ssh_port": 2222},
        )
        ref = await mgr.snapshot(inst)
        assert ref == "job-1"
        snapshot.capture_vm_snapshot.assert_awaited_once()
        # source_type=vm distinguishes from container snapshots.
        call_kwargs = snapshot.capture_vm_snapshot.call_args.kwargs
        assert call_kwargs["source_type"] == "vm"
        assert call_kwargs["ssh_port"] == 2222

    @pytest.mark.asyncio
    async def test_returns_none_without_ssh_host(self):
        mgr, _, _, snapshot, _ = _make_manager()
        inst = Instance(kind="vm", id="x", bound_to="job-1", metadata={})
        assert await mgr.snapshot(inst) is None
        snapshot.capture_vm_snapshot.assert_not_called()


class TestRestore:
    @pytest.mark.asyncio
    async def test_job_scope_restores_via_workspace(self):
        mgr, _, suspension, _, _ = _make_manager()
        inst = Instance(kind="vm", id="x", metadata={"scope": "job"})
        await mgr.restore(inst, "job-1")
        suspension.restore_workspace.assert_awaited_once_with("job-1")

    @pytest.mark.asyncio
    async def test_thread_scope_restores_via_thread(self):
        mgr, _, suspension, _, _ = _make_manager()
        inst = Instance(kind="vm", id="x", metadata={"scope": "thread"})
        await mgr.restore(inst, "thread-1")
        suspension.restore_thread_workspace.assert_awaited_once_with("thread-1")


class TestSignalDrainPending:
    @pytest.mark.asyncio
    async def test_is_noop(self):
        # No in-pod drain hook for VMs (the management daemon doesn't
        # poll intents). Drift drain fires only when bound work pauses.
        mgr, provisioner, suspension, snapshot, db = _make_manager()
        inst = Instance(kind="vm", id="x", bound_to="job-1", metadata={"scope": "job"})
        await mgr.signal_drain_pending(inst)
        provisioner.delete_vm.assert_not_called()
        provisioner.delete_thread_vm.assert_not_called()
        snapshot.capture_vm_snapshot.assert_not_called()


class TestDelete:
    @pytest.mark.asyncio
    async def test_job_vm_calls_delete_vm(self):
        mgr, provisioner, *_ = _make_manager()
        inst = Instance(kind="vm", id="x", bound_to="job-1", metadata={"scope": "job"})
        await mgr.delete(inst, grace_s=0)
        provisioner.delete_vm.assert_awaited_once_with("job-1")
        provisioner.delete_thread_vm.assert_not_called()

    @pytest.mark.asyncio
    async def test_thread_vm_calls_thread_delete(self):
        mgr, provisioner, *_ = _make_manager()
        inst = Instance(
            kind="vm",
            id="x",
            bound_to="thread-1",
            metadata={"scope": "thread"},
        )
        await mgr.delete(inst, grace_s=0)
        provisioner.delete_thread_vm.assert_awaited_once_with("thread-1")
        provisioner.delete_vm.assert_not_called()

    @pytest.mark.asyncio
    async def test_drain_dispatches_through_delete(self):
        mgr, provisioner, *_ = _make_manager()
        inst = Instance(kind="vm", id="x", bound_to="job-1", metadata={"scope": "job"})
        await mgr.drain(inst, grace_s=0)
        provisioner.delete_vm.assert_awaited_once_with("job-1")

    @pytest.mark.asyncio
    async def test_noop_when_provisioner_unavailable(self):
        mgr, provisioner, *_ = _make_manager(is_available=False)
        inst = Instance(kind="vm", id="x", bound_to="job-1", metadata={"scope": "job"})
        await mgr.delete(inst, grace_s=0)
        provisioner.delete_vm.assert_not_called()

    @pytest.mark.asyncio
    async def test_skipped_without_bound(self):
        mgr, provisioner, *_ = _make_manager()
        inst = Instance(kind="vm", id="x", bound_to=None, metadata={})
        await mgr.delete(inst, grace_s=0)
        provisioner.delete_vm.assert_not_called()
        provisioner.delete_thread_vm.assert_not_called()
