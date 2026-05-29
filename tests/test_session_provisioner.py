import sys
from pathlib import Path

import pytest

project_root = Path(__file__).parent.parent
for p in (str(project_root), str(project_root / "orchestrator")):
    if p not in sys.path:
        sys.path.insert(0, p)

from unittest.mock import AsyncMock  # noqa: E402

from services.session_provisioner import (  # noqa: E402
    ensure_session_workspace,
    reconcile_session_workspaces,
)
from services.workspace_lifecycle import EnsureOutcome  # noqa: E402


@pytest.mark.asyncio
async def test_failed_session_workspace_is_recreated():
    """Regression for the stuck RAG session: a 'failed' workspace on an active
    thread must be recreated, not left stuck."""
    db = AsyncMock()
    db.get_thread = AsyncMock(
        return_value={
            "id": "t1",
            "status": "active",
            "metadata": {"workspace_container": {"status": "failed"}},
        }
    )
    prov = AsyncMock()
    prov.create_workspace = AsyncMock(return_value=True)
    susp = AsyncMock()
    res = await ensure_session_workspace("t1", db=db, provisioner=prov, suspension=susp)
    prov.create_workspace.assert_awaited()  # recreated — not stuck
    assert res.outcome in (EnsureOutcome.PENDING, EnsureOutcome.READY)


@pytest.mark.asyncio
async def test_ensure_skips_ended_thread():
    db = AsyncMock()
    db.get_thread = AsyncMock(
        return_value={"id": "t1", "status": "ended", "metadata": {}}
    )
    prov = AsyncMock()
    susp = AsyncMock()
    res = await ensure_session_workspace("t1", db=db, provisioner=prov, suspension=susp)
    assert res is None
    prov.create_workspace.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_skips_missing_thread():
    db = AsyncMock()
    db.get_thread = AsyncMock(return_value=None)
    prov = AsyncMock()
    res = await ensure_session_workspace(
        "gone", db=db, provisioner=prov, suspension=AsyncMock()
    )
    assert res is None
    prov.create_workspace.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_handles_str_metadata_ready():
    """metadata stored as a JSON string is parsed; a ready workspace is a no-op."""
    db = AsyncMock()
    db.get_thread = AsyncMock(
        return_value={
            "id": "t1",
            "status": "active",
            "metadata": '{"workspace_container": {"status": "ready"}}',
        }
    )
    prov = AsyncMock()
    res = await ensure_session_workspace(
        "t1", db=db, provisioner=prov, suspension=AsyncMock()
    )
    assert res.outcome == EnsureOutcome.READY
    prov.create_workspace.assert_not_called()


@pytest.mark.asyncio
async def test_reconcile_iterates_and_counts():
    db = AsyncMock()
    db.list_threads_needing_workspace = AsyncMock(
        return_value=[{"id": "t1"}, {"id": "t2"}]
    )

    async def _get_thread(tid):
        return {
            "id": tid,
            "status": "active",
            "metadata": {"workspace_container": {"status": "failed"}},
        }

    db.get_thread = AsyncMock(side_effect=_get_thread)
    prov = AsyncMock()
    prov.create_workspace = AsyncMock(return_value=True)
    n = await reconcile_session_workspaces(
        db=db, provisioner=prov, suspension=AsyncMock()
    )
    assert n == 2
    assert prov.create_workspace.await_count == 2


@pytest.mark.asyncio
async def test_reconcile_empty_is_noop():
    db = AsyncMock()
    db.list_threads_needing_workspace = AsyncMock(return_value=[])
    n = await reconcile_session_workspaces(
        db=db, provisioner=AsyncMock(), suspension=AsyncMock()
    )
    assert n == 0


@pytest.mark.asyncio
async def test_reconcile_survives_one_thread_failing():
    """One bad thread must not abort the whole sweep."""
    db = AsyncMock()
    db.list_threads_needing_workspace = AsyncMock(
        return_value=[{"id": "bad"}, {"id": "good"}]
    )

    async def _get_thread(tid):
        if tid == "bad":
            raise RuntimeError("boom")
        return {
            "id": tid,
            "status": "active",
            "metadata": {"workspace_container": {"status": "failed"}},
        }

    db.get_thread = AsyncMock(side_effect=_get_thread)
    prov = AsyncMock()
    prov.create_workspace = AsyncMock(return_value=True)
    n = await reconcile_session_workspaces(
        db=db, provisioner=prov, suspension=AsyncMock()
    )
    assert n == 1  # only "good" succeeded; "bad" was caught


@pytest.mark.asyncio
async def test_suspended_workspace_status_fires_restore():
    """An active thread whose workspace is 'suspended' is restored (fire-and-forget),
    not recreated."""
    import asyncio

    db = AsyncMock()
    db.get_thread = AsyncMock(
        return_value={
            "id": "t1",
            "status": "active",
            "metadata": {"workspace_container": {"status": "suspended"}},
        }
    )
    prov = AsyncMock()
    prov.create_workspace = AsyncMock(return_value=True)
    susp = AsyncMock()
    susp.restore = AsyncMock(return_value=True)
    res = await ensure_session_workspace("t1", db=db, provisioner=prov, suspension=susp)
    await asyncio.sleep(0)  # let the fire-and-forget restore task run
    susp.restore.assert_awaited_once()
    prov.create_workspace.assert_not_called()
    assert res.outcome == EnsureOutcome.PENDING
