"""Tests for the WorkspaceSuspensionService.

Tests cover:
1. suspend_workspace: snapshot capture → pod deletion → status transitions
2. restore_workspace: pod creation → snapshot extraction → status transitions
3. check_idle_all: idle detection query, timeout calculation, sweep behavior
4. Edge cases: S3 unavailable, capture failure, restore failure, double entry
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

project_root = Path(__file__).parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from orchestrator.services.workspace_suspension import WorkspaceSuspensionService  # noqa: E402


# =============================================================================
# Fixtures
# =============================================================================


class _MockAsyncCtx:
    """Async context manager that wraps a mock connection."""

    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *args):
        return False


def make_mock_db():
    """Create a mock PostgresDB with get_job, acquire, and merge methods."""
    db = AsyncMock()
    # acquire() must be a plain MagicMock (not AsyncMock) that returns
    # an async context manager — otherwise calling it produces a coroutine
    # that doesn't support `async with`.
    mock_conn = AsyncMock()
    db.acquire = MagicMock(return_value=_MockAsyncCtx(mock_conn))
    db._conn = mock_conn
    return db


def make_service(*, s3_available=True, k8s_available=True):
    """Create a fully wired WorkspaceSuspensionService with mocks."""
    svc = WorkspaceSuspensionService()

    mock_db = make_mock_db()
    mock_snapshot = MagicMock()
    type(mock_snapshot).is_available = PropertyMock(return_value=s3_available)
    mock_snapshot.capture_vm_snapshot = AsyncMock(return_value=True)
    mock_snapshot.download_snapshot = AsyncMock(return_value=True)

    mock_container = MagicMock()
    type(mock_container).is_available = PropertyMock(return_value=k8s_available)
    mock_container.create_workspace = AsyncMock(return_value=True)
    mock_container.delete_workspace = AsyncMock(return_value=True)

    svc.connect(
        db=mock_db,
        snapshot_service=mock_snapshot,
        container_provisioner=mock_container,
    )
    return svc


def make_job(
    *,
    job_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
    status="paused",
    ws_status="ready",
    pod_ip="10.0.0.42",
    last_activity=None,
):
    """Create a minimal job dict."""
    ws_ctx = {
        "status": ws_status,
        "pod_ip": pod_ip,
        "pod_name": f"workspace-{job_id[:12]}",
    }
    if last_activity:
        ws_ctx["last_activity"] = last_activity
    return {
        "id": job_id,
        "status": status,
        "context": {"workspace_container": ws_ctx},
    }


# =============================================================================
# Test: is_enabled property
# =============================================================================


class TestIsEnabled:
    def test_enabled_when_both_available(self):
        svc = make_service(s3_available=True, k8s_available=True)
        assert svc.is_enabled is True

    def test_disabled_without_s3(self):
        svc = make_service(s3_available=False, k8s_available=True)
        assert svc.is_enabled is False

    def test_disabled_without_k8s(self):
        svc = make_service(s3_available=True, k8s_available=False)
        assert svc.is_enabled is False

    def test_disabled_without_both(self):
        svc = make_service(s3_available=False, k8s_available=False)
        assert svc.is_enabled is False

    def test_disabled_before_connect(self):
        svc = WorkspaceSuspensionService()
        assert svc.is_enabled is False


# =============================================================================
# Test: suspend_workspace
# =============================================================================


class TestSuspendWorkspace:
    @pytest.mark.asyncio
    async def test_suspend_success(self):
        """Full suspend flow: capture → delete → status suspended."""
        svc = make_service()
        job = make_job()
        svc._db.get_job.return_value = job

        result = await svc.suspend_workspace(job["id"])

        assert result is True
        # Snapshot captured
        svc._snapshot_service.capture_vm_snapshot.assert_awaited_once_with(
            job_id=job["id"],
            ssh_host="10.0.0.42",
            ssh_port=22,
            source_type="pod",
        )
        # Pod deleted
        svc._container_provisioner.delete_workspace.assert_awaited_once_with(job["id"])
        # Status transitions: suspending → suspended
        calls = svc._db.merge_workspace_container_context.call_args_list
        assert calls[0][0] == (job["id"], {"status": "suspending"})
        last_call = calls[-1][0]
        assert last_call[0] == job["id"]
        assert last_call[1]["status"] == "suspended"
        assert "suspended_at" in last_call[1]

    @pytest.mark.asyncio
    async def test_suspend_capture_fails_reverts(self):
        """If snapshot capture fails, status reverts to ready and pod stays."""
        svc = make_service()
        job = make_job()
        svc._db.get_job.return_value = job
        svc._snapshot_service.capture_vm_snapshot.return_value = False

        result = await svc.suspend_workspace(job["id"])

        assert result is False
        # Pod NOT deleted
        svc._container_provisioner.delete_workspace.assert_not_awaited()
        # Status reverted to ready
        calls = svc._db.merge_workspace_container_context.call_args_list
        assert calls[-1][0] == (job["id"], {"status": "ready"})

    @pytest.mark.asyncio
    async def test_suspend_skips_non_ready(self):
        """Suspend skips containers not in 'ready' status."""
        svc = make_service()
        job = make_job(ws_status="suspended")
        svc._db.get_job.return_value = job

        result = await svc.suspend_workspace(job["id"])

        assert result is False
        svc._snapshot_service.capture_vm_snapshot.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_suspend_skips_no_pod_ip(self):
        """Suspend skips if pod_ip is missing."""
        svc = make_service()
        job = make_job(pod_ip=None)
        svc._db.get_job.return_value = job

        result = await svc.suspend_workspace(job["id"])

        assert result is False

    @pytest.mark.asyncio
    async def test_suspend_skips_missing_job(self):
        """Suspend returns False for non-existent jobs."""
        svc = make_service()
        svc._db.get_job.return_value = None

        result = await svc.suspend_workspace("nonexistent")

        assert result is False

    @pytest.mark.asyncio
    async def test_suspend_disabled_returns_false(self):
        """Suspend returns False when service is disabled."""
        svc = make_service(s3_available=False)

        result = await svc.suspend_workspace("some-id")

        assert result is False

    @pytest.mark.asyncio
    async def test_suspend_exception_reverts(self):
        """If an exception occurs during capture, status reverts to ready."""
        svc = make_service()
        job = make_job()
        svc._db.get_job.return_value = job
        svc._snapshot_service.capture_vm_snapshot.side_effect = RuntimeError(
            "ssh failed"
        )

        result = await svc.suspend_workspace(job["id"])

        assert result is False
        calls = svc._db.merge_workspace_container_context.call_args_list
        assert calls[-1][0] == (job["id"], {"status": "ready"})


# =============================================================================
# Test: restore_workspace
# =============================================================================


class TestRestoreWorkspace:
    @pytest.mark.asyncio
    async def test_restore_success(self):
        """Full restore flow: create pod → extract snapshot → status ready."""
        svc = make_service()
        # After create_workspace, get_job returns the new pod IP
        svc._db.get_job.return_value = make_job(
            ws_status="restoring", pod_ip="10.0.0.99"
        )

        with patch.object(
            svc, "_extract_snapshot", new_callable=AsyncMock
        ) as mock_extract:
            result = await svc.restore_workspace("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")

        assert result is True
        svc._container_provisioner.create_workspace.assert_awaited_once()
        mock_extract.assert_awaited_once_with(
            "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "10.0.0.99"
        )
        # Status transitions: restoring → ready
        calls = svc._db.merge_workspace_container_context.call_args_list
        assert calls[0][0] == (
            "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            {"status": "restoring"},
        )
        last_call = calls[-1][0]
        assert last_call[1]["status"] == "ready"
        assert "restored_at" in last_call[1]

    @pytest.mark.asyncio
    async def test_restore_pod_creation_fails(self):
        """If pod creation fails, status is set to failed."""
        svc = make_service()
        svc._container_provisioner.create_workspace.return_value = False

        result = await svc.restore_workspace("some-job-id")

        assert result is False
        calls = svc._db.merge_workspace_container_context.call_args_list
        assert calls[-1][0][1]["status"] == "failed"

    @pytest.mark.asyncio
    async def test_restore_no_pod_ip(self):
        """If pod has no IP after creation, status is set to failed."""
        svc = make_service()
        svc._db.get_job.return_value = make_job(ws_status="restoring", pod_ip=None)

        result = await svc.restore_workspace("some-job-id")

        assert result is False
        calls = svc._db.merge_workspace_container_context.call_args_list
        assert calls[-1][0][1]["status"] == "failed"

    @pytest.mark.asyncio
    async def test_restore_disabled_returns_false(self):
        svc = make_service(s3_available=False)

        result = await svc.restore_workspace("some-id")

        assert result is False


# =============================================================================
# Test: check_idle_all
# =============================================================================


class TestCheckIdleAll:
    @pytest.mark.asyncio
    async def test_sweep_suspends_idle_containers(self):
        """Sweep suspends containers that have been idle beyond timeout."""
        svc = make_service()
        old_time = (datetime.now(timezone.utc) - timedelta(minutes=60)).isoformat()

        # Mock the query result
        svc._db._conn.fetch.return_value = [
            {
                "id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                "context": {
                    "workspace_container": {
                        "status": "ready",
                        "pod_ip": "10.0.0.42",
                        "last_activity": old_time,
                    }
                },
                "updated_at": datetime.now(timezone.utc) - timedelta(minutes=60),
            }
        ]

        # Mock the suspend
        svc._db.get_job.return_value = make_job(last_activity=old_time)

        count = await svc.check_idle_all()

        assert count == 1
        svc._snapshot_service.capture_vm_snapshot.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_sweep_skips_recent_activity(self):
        """Sweep does not suspend containers with recent activity."""
        svc = make_service()
        recent_time = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()

        svc._db._conn.fetch.return_value = [
            {
                "id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                "context": {
                    "workspace_container": {
                        "status": "ready",
                        "pod_ip": "10.0.0.42",
                        "last_activity": recent_time,
                    }
                },
                "updated_at": datetime.now(timezone.utc) - timedelta(minutes=5),
            }
        ]

        count = await svc.check_idle_all()

        assert count == 0
        svc._snapshot_service.capture_vm_snapshot.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_sweep_uses_updated_at_fallback(self):
        """When last_activity is missing, uses updated_at as fallback."""
        svc = make_service()
        old_updated_at = datetime.now(timezone.utc) - timedelta(minutes=60)

        svc._db._conn.fetch.return_value = [
            {
                "id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                "context": {
                    "workspace_container": {
                        "status": "ready",
                        "pod_ip": "10.0.0.42",
                        # No last_activity key
                    }
                },
                "updated_at": old_updated_at,
            }
        ]

        svc._db.get_job.return_value = make_job()

        count = await svc.check_idle_all()

        assert count == 1

    @pytest.mark.asyncio
    async def test_sweep_empty_results(self):
        """Sweep returns 0 when no idle containers found."""
        svc = make_service()
        svc._db._conn.fetch.return_value = []

        count = await svc.check_idle_all()

        assert count == 0

    @pytest.mark.asyncio
    async def test_sweep_disabled_returns_zero(self):
        svc = make_service(s3_available=False)

        count = await svc.check_idle_all()

        assert count == 0

    @pytest.mark.asyncio
    async def test_sweep_respects_custom_timeout(self):
        """Sweep uses WORKSPACE_IDLE_TIMEOUT env var."""
        svc = make_service()
        # Set a very long timeout — 120 minutes
        with patch.dict("os.environ", {"WORKSPACE_IDLE_TIMEOUT": "120"}):
            assert svc.idle_timeout_minutes == 120

            # Activity 60 minutes ago should NOT be idle at 120min timeout
            old_time = (datetime.now(timezone.utc) - timedelta(minutes=60)).isoformat()
            svc._db._conn.fetch.return_value = [
                {
                    "id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                    "context": {
                        "workspace_container": {
                            "status": "ready",
                            "pod_ip": "10.0.0.42",
                            "last_activity": old_time,
                        }
                    },
                    "updated_at": datetime.now(timezone.utc) - timedelta(minutes=60),
                }
            ]

            count = await svc.check_idle_all()
            assert count == 0


# =============================================================================
# Test: idle_timeout_minutes property
# =============================================================================


class TestIdleTimeout:
    def test_default_timeout(self):
        svc = make_service()
        with patch.dict("os.environ", {}, clear=False):
            # Remove WORKSPACE_IDLE_TIMEOUT if it exists
            import os

            os.environ.pop("WORKSPACE_IDLE_TIMEOUT", None)
            assert svc.idle_timeout_minutes == 30

    def test_custom_timeout(self):
        svc = make_service()
        with patch.dict("os.environ", {"WORKSPACE_IDLE_TIMEOUT": "60"}):
            assert svc.idle_timeout_minutes == 60


# =============================================================================
# Test: status transition correctness
# =============================================================================


class TestStatusTransitions:
    @pytest.mark.asyncio
    async def test_suspend_transitions(self):
        """Verify the exact status transition sequence during suspend."""
        svc = make_service()
        svc._db.get_job.return_value = make_job()

        await svc.suspend_workspace("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")

        calls = svc._db.merge_workspace_container_context.call_args_list
        statuses = [c[0][1]["status"] for c in calls]
        assert statuses == ["suspending", "suspended"]

    @pytest.mark.asyncio
    async def test_restore_transitions(self):
        """Verify the exact status transition sequence during restore."""
        svc = make_service()
        svc._db.get_job.return_value = make_job(
            ws_status="restoring", pod_ip="10.0.0.99"
        )

        with patch.object(svc, "_extract_snapshot", new_callable=AsyncMock):
            await svc.restore_workspace("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")

        calls = svc._db.merge_workspace_container_context.call_args_list
        statuses = [c[0][1]["status"] for c in calls]
        assert statuses == ["restoring", "ready"]

    @pytest.mark.asyncio
    async def test_suspend_failure_transitions(self):
        """Verify status reverts to ready on capture failure."""
        svc = make_service()
        svc._db.get_job.return_value = make_job()
        svc._snapshot_service.capture_vm_snapshot.return_value = False

        await svc.suspend_workspace("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")

        calls = svc._db.merge_workspace_container_context.call_args_list
        statuses = [c[0][1]["status"] for c in calls]
        assert statuses == ["suspending", "ready"]

    @pytest.mark.asyncio
    async def test_restore_failure_transitions(self):
        """Verify status set to failed on restore failure."""
        svc = make_service()
        svc._container_provisioner.create_workspace.return_value = False

        await svc.restore_workspace("some-job-id")

        calls = svc._db.merge_workspace_container_context.call_args_list
        statuses = [c[0][1]["status"] for c in calls]
        assert statuses == ["restoring", "failed"]


# =============================================================================
# Test: suspend clears pod metadata
# =============================================================================


class TestSuspendClearsMetadata:
    @pytest.mark.asyncio
    async def test_suspend_clears_pod_ip_and_name(self):
        """After suspend, pod_ip and pod_name are set to None."""
        svc = make_service()
        svc._db.get_job.return_value = make_job()

        await svc.suspend_workspace("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")

        # Find the "suspended" call
        calls = svc._db.merge_workspace_container_context.call_args_list
        suspended_call = [c for c in calls if c[0][1].get("status") == "suspended"][0]
        updates = suspended_call[0][1]
        assert updates["pod_ip"] is None
        assert updates["pod_name"] is None
        assert updates["suspended_at"] is not None


# =============================================================================
# Test: restore with extraction failure
# =============================================================================


class TestRestoreExtractionFailure:
    @pytest.mark.asyncio
    async def test_restore_snapshot_download_fails(self):
        """Restore still marks ready even if snapshot download fails.

        The pod is created successfully — extraction failure is logged as a
        warning but the pod is still usable (agent can reinitialize workspace
        from Gitea).
        """
        svc = make_service()
        svc._db.get_job.return_value = make_job(
            ws_status="restoring", pod_ip="10.0.0.99"
        )
        svc._snapshot_service.download_snapshot.return_value = False

        result = await svc.restore_workspace("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")

        # Restore succeeds even if extraction had issues — pod is created
        assert result is True
        calls = svc._db.merge_workspace_container_context.call_args_list
        assert calls[-1][0][1]["status"] == "ready"

    @pytest.mark.asyncio
    async def test_restore_exception_during_extract(self):
        """If extraction throws, restore fails and status is set to failed."""
        svc = make_service()
        svc._db.get_job.return_value = make_job(
            ws_status="restoring", pod_ip="10.0.0.99"
        )

        with patch.object(
            svc,
            "_extract_snapshot",
            new_callable=AsyncMock,
            side_effect=RuntimeError("SSH connection refused"),
        ):
            result = await svc.restore_workspace("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")

        assert result is False
        calls = svc._db.merge_workspace_container_context.call_args_list
        assert calls[-1][0][1]["status"] == "failed"


# =============================================================================
# Test: multiple containers in one sweep
# =============================================================================


class TestMultipleContainerSweep:
    @pytest.mark.asyncio
    async def test_sweep_multiple_idle_containers(self):
        """Sweep handles multiple idle containers in one cycle."""
        svc = make_service()
        old_time = (datetime.now(timezone.utc) - timedelta(minutes=60)).isoformat()

        svc._db._conn.fetch.return_value = [
            {
                "id": "aaaaaaaa-1111-1111-1111-111111111111",
                "context": {
                    "workspace_container": {
                        "status": "ready",
                        "pod_ip": "10.0.0.1",
                        "last_activity": old_time,
                    }
                },
                "updated_at": datetime.now(timezone.utc) - timedelta(minutes=60),
            },
            {
                "id": "bbbbbbbb-2222-2222-2222-222222222222",
                "context": {
                    "workspace_container": {
                        "status": "ready",
                        "pod_ip": "10.0.0.2",
                        "last_activity": old_time,
                    }
                },
                "updated_at": datetime.now(timezone.utc) - timedelta(minutes=60),
            },
        ]

        # Mock get_job for each job
        svc._db.get_job.side_effect = [
            make_job(
                job_id="aaaaaaaa-1111-1111-1111-111111111111",
                pod_ip="10.0.0.1",
                last_activity=old_time,
            ),
            make_job(
                job_id="bbbbbbbb-2222-2222-2222-222222222222",
                pod_ip="10.0.0.2",
                last_activity=old_time,
            ),
        ]

        count = await svc.check_idle_all()

        assert count == 2
        assert svc._snapshot_service.capture_vm_snapshot.await_count == 2
        assert svc._container_provisioner.delete_workspace.await_count == 2

    @pytest.mark.asyncio
    async def test_sweep_partial_failure(self):
        """If one suspend fails, others still proceed."""
        svc = make_service()
        old_time = (datetime.now(timezone.utc) - timedelta(minutes=60)).isoformat()

        svc._db._conn.fetch.return_value = [
            {
                "id": "aaaaaaaa-1111-1111-1111-111111111111",
                "context": {
                    "workspace_container": {
                        "status": "ready",
                        "pod_ip": "10.0.0.1",
                        "last_activity": old_time,
                    }
                },
                "updated_at": datetime.now(timezone.utc) - timedelta(minutes=60),
            },
            {
                "id": "bbbbbbbb-2222-2222-2222-222222222222",
                "context": {
                    "workspace_container": {
                        "status": "ready",
                        "pod_ip": "10.0.0.2",
                        "last_activity": old_time,
                    }
                },
                "updated_at": datetime.now(timezone.utc) - timedelta(minutes=60),
            },
        ]

        # First job: capture fails. Second job: succeeds.
        svc._snapshot_service.capture_vm_snapshot.side_effect = [False, True]

        svc._db.get_job.side_effect = [
            make_job(
                job_id="aaaaaaaa-1111-1111-1111-111111111111",
                pod_ip="10.0.0.1",
                last_activity=old_time,
            ),
            make_job(
                job_id="bbbbbbbb-2222-2222-2222-222222222222",
                pod_ip="10.0.0.2",
                last_activity=old_time,
            ),
        ]

        count = await svc.check_idle_all()

        # Only 1 succeeded
        assert count == 1
        # Pod only deleted for the second (successful) one
        svc._container_provisioner.delete_workspace.assert_awaited_once_with(
            "bbbbbbbb-2222-2222-2222-222222222222"
        )


# =============================================================================
# Test: end-to-end suspend → restore cycle
# =============================================================================


class TestSuspendRestoreRoundTrip:
    @pytest.mark.asyncio
    async def test_full_round_trip(self):
        """Suspend a workspace, then restore it — verify the full cycle."""
        svc = make_service()
        job_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

        # Phase 1: Suspend
        svc._db.get_job.return_value = make_job(
            job_id=job_id, ws_status="ready", pod_ip="10.0.0.42"
        )
        ok = await svc.suspend_workspace(job_id)
        assert ok is True

        # Verify snapshot was captured with the original pod IP
        svc._snapshot_service.capture_vm_snapshot.assert_awaited_once_with(
            job_id=job_id, ssh_host="10.0.0.42", ssh_port=22, source_type="pod"
        )

        # Verify pod was deleted
        svc._container_provisioner.delete_workspace.assert_awaited_once_with(job_id)

        # Reset mocks for restore phase
        svc._snapshot_service.capture_vm_snapshot.reset_mock()
        svc._container_provisioner.delete_workspace.reset_mock()
        svc._container_provisioner.create_workspace.reset_mock()
        svc._db.merge_workspace_container_context.reset_mock()

        # Phase 2: Restore
        svc._db.get_job.return_value = make_job(
            job_id=job_id,
            ws_status="restoring",
            pod_ip="10.0.0.99",  # new IP
        )

        with patch.object(
            svc, "_extract_snapshot", new_callable=AsyncMock
        ) as mock_extract:
            ok = await svc.restore_workspace(job_id)

        assert ok is True

        # Pod created
        svc._container_provisioner.create_workspace.assert_awaited_once_with(job_id)

        # Snapshot extracted to new pod IP
        mock_extract.assert_awaited_once_with(job_id, "10.0.0.99")

        # Final status is ready
        calls = svc._db.merge_workspace_container_context.call_args_list
        assert calls[-1][0][1]["status"] == "ready"
        assert "restored_at" in calls[-1][0][1]


# =============================================================================
# Test: dispatch loop status handling (replicated logic)
# =============================================================================


class TestDispatchStatusHandling:
    """Test that the dispatch loop correctly routes suspended/restoring statuses.

    Replicates the logic from orchestrator/main.py dispatch loop rather than
    importing it (same pattern as test_container_provisioner.py).
    """

    @staticmethod
    def _should_dispatch(container_ctx: dict) -> str:
        """Replicate the dispatch loop's container status handling.

        Returns: "provision", "restore", "wait", "skip", or "dispatch".
        """
        status = container_ctx.get("status")
        if not status:
            return "provision"
        elif status == "suspended":
            return "restore"
        elif status in ("restoring", "suspending", "creating"):
            return "wait"
        elif status != "ready":
            return "skip"
        else:
            return "dispatch"

    def test_no_status_provisions(self):
        assert self._should_dispatch({}) == "provision"

    def test_ready_dispatches(self):
        assert self._should_dispatch({"status": "ready"}) == "dispatch"

    def test_suspended_restores(self):
        assert self._should_dispatch({"status": "suspended"}) == "restore"

    def test_restoring_waits(self):
        assert self._should_dispatch({"status": "restoring"}) == "wait"

    def test_suspending_waits(self):
        assert self._should_dispatch({"status": "suspending"}) == "wait"

    def test_creating_waits(self):
        assert self._should_dispatch({"status": "creating"}) == "wait"

    def test_failed_skips(self):
        assert self._should_dispatch({"status": "failed"}) == "skip"

    def test_deleted_skips(self):
        assert self._should_dispatch({"status": "deleted"}) == "skip"


# =============================================================================
# Test: heartbeat activity tracking (replicated logic)
# =============================================================================


class TestHeartbeatActivityTracking:
    """Test the heartbeat last_activity update logic.

    Replicates the conditional from orchestrator/main.py agent_heartbeat.
    """

    @staticmethod
    def _should_update_activity(status: str, current_job_id: str | None) -> bool:
        """Replicate the heartbeat condition for activity tracking."""
        return bool(current_job_id and status == "working")

    def test_working_with_job_updates(self):
        assert self._should_update_activity("working", "some-job-id") is True

    def test_ready_does_not_update(self):
        assert self._should_update_activity("ready", "some-job-id") is False

    def test_working_without_job_does_not_update(self):
        assert self._should_update_activity("working", None) is False

    def test_idle_does_not_update(self):
        assert self._should_update_activity("idle", "some-job-id") is False
