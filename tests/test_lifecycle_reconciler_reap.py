"""Reap-branch decision-flow tests for InstanceLifecycleReconciler.tick()."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.services.lifecycle import (
    Instance,
    InstanceLifecycleReconciler,
    StatefulInstanceManager,
)


def _stateful_mgr(inst, *, healthy=True, reapable=True, dirty=False,
                  reachable=False, exhausted=False, snapshot_ref=None):
    mgr = MagicMock(spec=StatefulInstanceManager)
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
