"""Reap-branch decision-flow tests for InstanceLifecycleReconciler.tick()."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.services.lifecycle import (
    Instance,
    InstanceLifecycleReconciler,
    ReapableInstanceManager,
)


class _StatefulNonReapable:
    """Shaped like VMInstanceManager: a StatefulInstanceManager (has
    snapshot/restore) but WITHOUT the reap predicates. The deployed
    regression ran _reap on exactly this shape and AttributeError'd every
    tick. A spec'd MagicMock can't catch this — it auto-stubs the missing
    methods — so this regression test uses a real object."""

    kind = "vm"

    def __init__(self, instances):
        self._instances = instances
        self.delete_calls: list[str] = []

    async def list_instances(self):
        return self._instances

    async def expected_versions(self):
        return set()

    async def is_healthy(self, inst):
        return True

    async def is_idle(self, inst):
        return False

    async def signal_drain_pending(self, inst):
        ...

    async def drain(self, inst, grace_s):
        ...

    async def delete(self, inst, grace_s):
        self.delete_calls.append(inst.id)

    async def snapshot(self, inst):
        return None

    async def restore(self, inst, snapshot_ref):
        ...


def _stateful_mgr(
    inst,
    *,
    healthy=True,
    reapable=True,
    dirty=False,
    reachable=False,
    exhausted=False,
    snapshot_ref=None,
):
    mgr = MagicMock(spec=ReapableInstanceManager)
    mgr.kind = "workspace"
    mgr.expected_versions = AsyncMock(return_value=set())
    mgr.list_instances = AsyncMock(return_value=[inst])
    mgr.is_healthy = AsyncMock(return_value=healthy)
    mgr.is_idle = AsyncMock(return_value=False)
    mgr.signal_drain_pending = AsyncMock()
    mgr.is_reapable = AsyncMock(return_value=reapable)
    mgr.is_dirty = AsyncMock(return_value=dirty)
    mgr.is_reachable = AsyncMock(return_value=reachable)
    mgr.attempts_exhausted = AsyncMock(return_value=exhausted)
    mgr.snapshot = AsyncMock(return_value=snapshot_ref)
    mgr.delete = AsyncMock()
    mgr.give_up = AsyncMock()
    mgr.record_attempt = AsyncMock()
    return mgr


def _inst():
    return Instance(kind="workspace", id="ws-1", bound_to="j1")


@pytest.mark.asyncio
async def test_clean_reapable_deletes_without_probe():
    mgr = _stateful_mgr(_inst(), dirty=False)
    rec = InstanceLifecycleReconciler([mgr])
    await rec.tick()
    mgr.delete.assert_awaited_once()
    mgr.is_reachable.assert_not_called()
    mgr.snapshot.assert_not_called()


@pytest.mark.asyncio
async def test_dirty_reachable_snapshots_then_deletes():
    mgr = _stateful_mgr(_inst(), dirty=True, reachable=True, snapshot_ref="j1")
    rec = InstanceLifecycleReconciler([mgr])
    await rec.tick()
    mgr.snapshot.assert_awaited_once()
    mgr.delete.assert_awaited_once()
    mgr.give_up.assert_not_called()


@pytest.mark.asyncio
async def test_dirty_reachable_snapshot_fails_records_attempt():
    mgr = _stateful_mgr(_inst(), dirty=True, reachable=True, snapshot_ref=None)
    rec = InstanceLifecycleReconciler([mgr])
    await rec.tick()
    mgr.snapshot.assert_awaited_once()
    mgr.record_attempt.assert_awaited_once()
    mgr.delete.assert_not_called()


@pytest.mark.asyncio
async def test_dirty_unreachable_not_exhausted_records_attempt():
    mgr = _stateful_mgr(_inst(), dirty=True, reachable=False, exhausted=False)
    rec = InstanceLifecycleReconciler([mgr])
    await rec.tick()
    mgr.record_attempt.assert_awaited_once()
    mgr.give_up.assert_not_called()
    mgr.delete.assert_not_called()


@pytest.mark.asyncio
async def test_dirty_unreachable_exhausted_gives_up():
    mgr = _stateful_mgr(_inst(), dirty=True, reachable=False, exhausted=True)
    rec = InstanceLifecycleReconciler([mgr])
    await rec.tick()
    mgr.give_up.assert_awaited_once()
    mgr.delete.assert_not_called()  # give_up owns the deletion


@pytest.mark.asyncio
async def test_not_reapable_is_untouched():
    mgr = _stateful_mgr(_inst(), reapable=False)
    rec = InstanceLifecycleReconciler([mgr])
    await rec.tick()
    mgr.delete.assert_not_called()
    mgr.give_up.assert_not_called()
    mgr.record_attempt.assert_not_called()


@pytest.mark.asyncio
async def test_unhealthy_still_crash_deletes_before_reap():
    mgr = _stateful_mgr(_inst(), healthy=False)
    rec = InstanceLifecycleReconciler([mgr])
    await rec.tick()
    mgr.delete.assert_awaited_once()  # crash path
    mgr.is_reapable.assert_not_called()


@pytest.mark.asyncio
async def test_stateful_non_reapable_manager_is_skipped(caplog):
    """Regression: a StatefulInstanceManager that is NOT reapable (VM) must
    be skipped by the reap branch, not crash it.

    Pre-fix, _reap gated on isinstance(StatefulInstanceManager), so it called
    is_reapable() on the VM manager and AttributeError'd every tick. The gate
    is now ReapableInstanceManager, which the VM-shaped manager does not
    satisfy."""
    mgr = _StatefulNonReapable([Instance(kind="vm", id="vm-job-1", bound_to="j1")])
    rec = InstanceLifecycleReconciler([mgr])
    with caplog.at_level(logging.ERROR):
        report = await rec.tick()
    # No crash surfaced as a logged "Reap failed" error.
    assert "Reap failed" not in caplog.text
    assert "AttributeError" not in caplog.text
    # The VM manager was left entirely alone by the reap path.
    assert mgr.delete_calls == []
    assert report["vm"]["reaped"] == 0
