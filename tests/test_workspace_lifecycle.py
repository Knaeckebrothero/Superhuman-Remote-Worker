import sys
from pathlib import Path

import pytest

project_root = Path(__file__).parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from orchestrator.services.workspace_lifecycle import WorkspaceOwner  # noqa: E402


def test_owner_job_naming_and_labels():
    o = WorkspaceOwner.job("abcdef0123456789")
    assert o.kind == "job"
    assert o.pod_name == "workspace-abcdef012345"  # 12-char id truncation
    assert o.label_key == "srw/job-id"
    assert o.component_label == "workspace"
    assert o.network_tier_kind == "job"


def test_owner_session_naming_and_labels():
    o = WorkspaceOwner.session("abcdef0123456789")
    assert o.pod_name == "ws-thread-abcdef012345"
    assert o.label_key == "srw/thread-id"
    assert o.component_label == "thread-workspace"
    assert o.network_tier_kind == "thread"


def test_owner_is_frozen_hashable():
    o = WorkspaceOwner.session("t1")
    with pytest.raises(Exception):
        o.id = "t2"  # frozen
    assert {o: 1}[WorkspaceOwner.session("t1")] == 1  # hashable by (kind, id)


# =============================================================================
# Tests for ensure_workspace (uses services.* path — conftest puts orchestrator/
# on sys.path, so this resolves without the orchestrator. prefix and avoids the
# dual-class-object problem that breaks assert_called_with equality checks).
# =============================================================================

import asyncio  # noqa: E402 (placed here to keep module-level imports above)
from unittest.mock import AsyncMock  # noqa: E402

from services.workspace_lifecycle import (  # noqa: E402
    EnsureOutcome,
    WorkspaceOwner as _WO,
    ensure_workspace,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,expect_create,outcome",
    [
        (None, True, EnsureOutcome.PENDING),
        ("", True, EnsureOutcome.PENDING),
        ("deleted", True, EnsureOutcome.PENDING),  # no live workspace -> recreate
        ("none", True, EnsureOutcome.PENDING),  # no live workspace -> recreate
        ("failed", True, EnsureOutcome.PENDING),  # SESSION self-heals
        ("created", False, EnsureOutcome.PENDING),  # transient in-progress
        ("creating", False, EnsureOutcome.PENDING),
        ("restoring", False, EnsureOutcome.PENDING),
        ("pending", False, EnsureOutcome.PENDING),
        ("ready", False, EnsureOutcome.READY),
        ("weird-unknown", False, EnsureOutcome.PENDING),  # truly unknown -> wait
    ],
)
async def test_ensure_session_transitions(status, expect_create, outcome):
    prov = AsyncMock()
    prov.create_workspace = AsyncMock(return_value=True)
    susp = AsyncMock()
    susp.restore = AsyncMock(return_value=True)
    res = await ensure_workspace(
        _WO.session("t1"),
        provisioner=prov,
        suspension=susp,
        current_status=status,
    )
    assert res.outcome == outcome
    if expect_create:
        prov.create_workspace.assert_awaited()
    else:
        prov.create_workspace.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_job_failed_surfaces_FAILED_not_recreate():
    """Jobs must NOT auto-retry a failed workspace (preserves dispatcher fail-the-job)."""
    prov = AsyncMock()
    prov.create_workspace = AsyncMock(return_value=True)
    susp = AsyncMock()
    res = await ensure_workspace(
        _WO.job("j1"),
        provisioner=prov,
        suspension=susp,
        current_status="failed",
    )
    assert res.outcome == EnsureOutcome.FAILED
    prov.create_workspace.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_suspended_kicks_off_restore_fire_and_forget():
    prov = AsyncMock()
    susp = AsyncMock()
    susp.restore = AsyncMock(return_value=True)
    owner = _WO.session("t1")
    res = await ensure_workspace(
        owner, provisioner=prov, suspension=susp, current_status="suspended"
    )
    assert res.outcome == EnsureOutcome.PENDING
    await asyncio.sleep(0)  # let the fire-and-forget restore task run
    susp.restore.assert_awaited_once_with(owner)
    prov.create_workspace.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_suspended_job_also_fire_and_forget():
    """The suspended->restore branch is owner-agnostic; jobs restore too."""
    prov = AsyncMock()
    susp = AsyncMock()
    susp.restore = AsyncMock(return_value=True)
    owner = _WO.job("j1")
    res = await ensure_workspace(
        owner, provisioner=prov, suspension=susp, current_status="suspended"
    )
    assert res.outcome == EnsureOutcome.PENDING
    await asyncio.sleep(0)
    susp.restore.assert_awaited_once_with(owner)
    prov.create_workspace.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_create_failure_returns_FAILED():
    prov = AsyncMock()
    prov.create_workspace = AsyncMock(return_value=False)
    susp = AsyncMock()
    res = await ensure_workspace(
        _WO.session("t1"),
        provisioner=prov,
        suspension=susp,
        current_status=None,
    )
    assert res.outcome == EnsureOutcome.FAILED


# =============================================================================
# 'ready' drift check: a workspace marked ready whose pod is actually gone must
# be recreated (design "ready, pod missing → treat as failed → recreate"),
# while a probe that can't tell must NEVER false-recreate a healthy workspace.
# =============================================================================


@pytest.mark.asyncio
async def test_ensure_ready_recreates_when_pod_confirmed_dead():
    """ready + probe says the pod is gone/tombstone (False) → recreate."""
    prov = AsyncMock()
    prov.create_workspace = AsyncMock(return_value=True)
    prov.workspace_pod_live = AsyncMock(return_value=False)
    susp = AsyncMock()
    owner = _WO.session("t1")
    res = await ensure_workspace(
        owner, provisioner=prov, suspension=susp, current_status="ready"
    )
    assert res.outcome == EnsureOutcome.PENDING  # _create → creating/PENDING
    prov.workspace_pod_live.assert_awaited_once_with(owner)
    prov.create_workspace.assert_awaited()


@pytest.mark.asyncio
async def test_ensure_ready_no_recreate_when_pod_live():
    """ready + probe says the pod is live (True) → READY, no recreate."""
    prov = AsyncMock()
    prov.create_workspace = AsyncMock(return_value=True)
    prov.workspace_pod_live = AsyncMock(return_value=True)
    susp = AsyncMock()
    res = await ensure_workspace(
        _WO.session("t1"), provisioner=prov, suspension=susp, current_status="ready"
    )
    assert res.outcome == EnsureOutcome.READY
    prov.create_workspace.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_ready_trusts_db_when_probe_unknown():
    """ready + probe can't tell (None: non-k8s / transient) → READY, no
    recreate — never false-recreate a healthy workspace on a probe blip."""
    prov = AsyncMock()
    prov.create_workspace = AsyncMock(return_value=True)
    prov.workspace_pod_live = AsyncMock(return_value=None)
    susp = AsyncMock()
    res = await ensure_workspace(
        _WO.session("t1"), provisioner=prov, suspension=susp, current_status="ready"
    )
    assert res.outcome == EnsureOutcome.READY
    prov.create_workspace.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_ready_trusts_db_when_provisioner_has_no_probe():
    """A provisioner without workspace_pod_live (e.g. a non-container backend)
    keeps the original behavior: ready → READY, no probe, no recreate."""
    prov = AsyncMock(spec=["create_workspace"])
    prov.create_workspace = AsyncMock(return_value=True)
    susp = AsyncMock()
    res = await ensure_workspace(
        _WO.session("t1"), provisioner=prov, suspension=susp, current_status="ready"
    )
    assert res.outcome == EnsureOutcome.READY
    prov.create_workspace.assert_not_called()
