"""capture_vm_snapshot stamps the work-marker into the workspace context."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.services.snapshot_service import SnapshotService


@pytest.mark.asyncio
async def test_marker_written_to_thread_context_on_success():
    svc = SnapshotService()
    svc._available = True
    svc._db = AsyncMock()
    svc._db.merge_thread_workspace_context = AsyncMock(return_value=True)
    svc.upload_snapshot = AsyncMock(return_value=True)

    # Stub the SSH-tar pipeline so capture "succeeds" without a real pod.
    with (
        patch.object(
            SnapshotService, "_collect_environment_info", AsyncMock(return_value={})
        ),
        patch(
            "orchestrator.services.snapshot_service.asyncio.create_subprocess_exec"
        ) as cse,
    ):
        proc = MagicMock()
        proc.stdout.read = AsyncMock(side_effect=[b"data", b""])
        proc.stderr.read = AsyncMock(return_value=b"")
        proc.wait = AsyncMock(return_value=0)
        proc.returncode = 0
        cse.return_value = proc
        ok = await svc.capture_vm_snapshot(
            job_id="t1",
            ssh_host="10.0.0.5",
            ssh_port=30022,
            source_type="pod",
            entity_type="threads",
            work_marker=7,
        )
    assert ok is True
    svc._db.merge_thread_workspace_context.assert_any_await(
        "t1", {"last_snapshot_turns": 7}
    )


@pytest.mark.asyncio
async def test_no_marker_write_when_work_marker_none():
    svc = SnapshotService()
    svc._available = True
    svc._db = AsyncMock()
    svc._db.merge_workspace_container_context = AsyncMock(return_value=True)
    svc.upload_snapshot = AsyncMock(return_value=True)

    with (
        patch.object(
            SnapshotService, "_collect_environment_info", AsyncMock(return_value={})
        ),
        patch(
            "orchestrator.services.snapshot_service.asyncio.create_subprocess_exec"
        ) as cse,
    ):
        proc = MagicMock()
        proc.stdout.read = AsyncMock(side_effect=[b"data", b""])
        proc.stderr.read = AsyncMock(return_value=b"")
        proc.wait = AsyncMock(return_value=0)
        proc.returncode = 0
        cse.return_value = proc
        ok = await svc.capture_vm_snapshot(
            job_id="j1",
            ssh_host="10.0.0.5",
            ssh_port=30022,
            source_type="pod",
            entity_type="jobs",
        )
    assert ok is True
    svc._db.merge_workspace_container_context.assert_not_called()
