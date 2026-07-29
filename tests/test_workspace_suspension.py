"""Tests for the WorkspaceSuspensionService.

Tests cover:
1. suspend_workspace: snapshot capture → pod deletion → status transitions
2. restore_workspace: pod creation → snapshot extraction → status transitions
3. check_idle_all: idle detection query, timeout calculation, sweep behavior
4. Edge cases: S3 unavailable, capture failure, restore failure, double entry
"""

import sys
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

project_root = Path(__file__).parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))
_orchestrator_dir = str(project_root / "orchestrator")
if _orchestrator_dir not in sys.path:
    sys.path.insert(0, _orchestrator_dir)

from orchestrator.services.workspace_suspension import WorkspaceSuspensionService  # noqa: E402
from services.workspace_lifecycle import WorkspaceOwner  # noqa: E402


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
    # Default get_job return — restore_workspace reads the job to detect
    # provisioner type before dispatching.
    mock_db.get_job.return_value = make_job()

    mock_snapshot = MagicMock()
    type(mock_snapshot).is_available = PropertyMock(return_value=s3_available)
    mock_snapshot.capture_vm_snapshot = AsyncMock(return_value=True)
    mock_snapshot.download_snapshot = AsyncMock(return_value=True)

    mock_container = MagicMock()
    type(mock_container).is_available = PropertyMock(return_value=k8s_available)
    mock_container.create_workspace = AsyncMock(return_value=True)
    mock_container.delete_workspace = AsyncMock(return_value=True)
    mock_container.delete_workspace_pvc = AsyncMock(return_value=True)

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
            ssh_port=30022,
            source_type="pod",
        )
        # Pod deleted
        svc._container_provisioner.delete_workspace.assert_awaited_once_with(
            WorkspaceOwner.job(job["id"])
        )
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
    async def test_stale_docker_suspend_cannot_touch_reassigned_static_host(self):
        """A sweep queued on an old lease must remain a no-op after reassignment."""

        svc = make_service()
        read_started = asyncio.Event()
        host_reassigned = asyncio.Event()
        stale_lease = {
            "status": "ready",
            "host": "workspace-1",
            "port": 30022,
            "provisioner": "docker",
            "_docker_workspace_lease_id": "old-lease",
        }

        async def delayed_stale_job(job_id):
            read_started.set()
            await host_reassigned.wait()
            return {
                "id": job_id,
                "context": {"workspace_container": stale_lease},
            }

        svc._db.get_job.side_effect = delayed_stale_job
        docker = MagicMock()
        docker._reset_workspace_via_ssh = AsyncMock(return_value=True)
        svc._docker_provisioner = docker

        pending = asyncio.create_task(svc.suspend_workspace("old-job"))
        await read_started.wait()
        # The authoritative lease can now belong to a different owner; the
        # delayed read represents the stale snapshot already held by a sweep.
        host_reassigned.set()

        assert await pending is False
        svc._snapshot_service.capture_vm_snapshot.assert_not_awaited()
        svc._container_provisioner.delete_workspace.assert_not_awaited()
        svc._db.merge_workspace_container_context.assert_not_awaited()
        docker._reset_workspace_via_ssh.assert_not_awaited()

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
            "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "10.0.0.99", ssh_port=30022
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
            WorkspaceOwner.job("bbbbbbbb-2222-2222-2222-222222222222")
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
            job_id=job_id, ssh_host="10.0.0.42", ssh_port=30022, source_type="pod"
        )

        # Verify pod was deleted
        svc._container_provisioner.delete_workspace.assert_awaited_once_with(
            WorkspaceOwner.job(job_id)
        )

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
        svc._container_provisioner.create_workspace.assert_awaited_once_with(
            WorkspaceOwner.job(job_id)
        )

        # Snapshot extracted to new pod IP
        mock_extract.assert_awaited_once_with(job_id, "10.0.0.99", ssh_port=30022)

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
# Test: restore(owner) — owner-keyed dispatch
# =============================================================================


class TestRestoreOwnerDispatch:
    """Tests for WorkspaceSuspensionService.restore(owner).

    Verifies that the owner-keyed router dispatches to the correct underlying
    method.  Uses direct AsyncMock patching rather than a fully wired service
    to keep these tests fast and isolated from the heavy restore logic.
    """

    @pytest.mark.asyncio
    async def test_restore_job_calls_restore_workspace(self):
        """restore(job owner) must delegate to restore_workspace(job_id)."""
        from services.workspace_lifecycle import WorkspaceOwner as WO

        svc = make_service()
        svc.restore_workspace = AsyncMock(return_value=True)
        svc.restore_thread_workspace = AsyncMock(return_value=True)

        owner = WO.job("j1")
        result = await svc.restore(owner)

        assert result is True
        svc.restore_workspace.assert_awaited_once_with("j1")
        svc.restore_thread_workspace.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_restore_session_calls_restore_thread_workspace(self):
        """restore(session owner) must delegate to restore_thread_workspace(thread_id)."""
        from services.workspace_lifecycle import WorkspaceOwner as WO

        svc = make_service()
        svc.restore_workspace = AsyncMock(return_value=True)
        svc.restore_thread_workspace = AsyncMock(return_value=True)

        owner = WO.session("t1")
        result = await svc.restore(owner)

        assert result is True
        svc.restore_thread_workspace.assert_awaited_once_with("t1")
        svc.restore_workspace.assert_not_awaited()


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


# =============================================================================
# Test: thread tier is read explicitly, not inferred from metadata presence
# docs/issues/workspace_suspension_infers_tier_from_metadata_presence.md
# =============================================================================


def make_vm_thread(thread_id="tid-vm", ssh_host="100.64.0.235"):
    """A healthy vm-tier session.

    The workspace_container key is present but holds ONLY git coordinates —
    _setup_gitea writes those for EVERY thread including vm-tier ones. It has no
    'status'/'pod_ip', because no container was ever provisioned for it.
    """
    return {
        "id": thread_id,
        "status": "active",
        "metadata": {
            "config_override": {"workspace": {"backend": "vm"}},
            "workspace_container": {
                "git_remote_url": "http://gitea/srw/thread-tid-vm.git",
                "repo_name": "thread-tid-vm",
            },
            "vm": {"status": "ready", "ssh_host": ssh_host, "ssh_port": 22},
        },
    }


def make_container_thread(thread_id="tid-pod"):
    return {
        "id": thread_id,
        "status": "active",
        "metadata": {
            "config_override": {"workspace": {"backend": "sandbox"}},
            "workspace_container": {
                "status": "ready",
                "pod_ip": "10.42.2.32",
                "git_remote_url": "http://gitea/srw/thread-tid-pod.git",
            },
        },
    }


def make_vm_service():
    svc = make_service()
    vm_prov = MagicMock()
    type(vm_prov).is_available = PropertyMock(return_value=True)
    vm_prov.delete_thread_vm = AsyncMock(return_value=True)
    vm_prov.create_thread_vm = AsyncMock(return_value=True)
    svc._vm_provisioner = vm_prov
    svc._agent_provisioner = None
    return svc, vm_prov


class TestThreadTierIsExplicit:
    """A vm-tier thread must suspend via the VM branch. Previously the tier was
    inferred from whether metadata.workspace_container existed at all — and
    _setup_gitea makes it exist for every thread — so a VM session read as
    pod-tier and suspend bailed out entirely, leaving its VM running forever."""

    @pytest.mark.asyncio
    async def test_vm_tier_thread_actually_suspends(self):
        svc, vm_prov = make_vm_service()
        svc._db.get_thread = AsyncMock(return_value=make_vm_thread())

        ok = await svc.suspend_thread_workspace("tid-vm")

        assert ok is True, "vm-tier suspend must not bail on the container status"
        # purge_disk=True here: this suite does not enable VM_PERSISTENT_ROOTDISK,
        # so there is no disk to keep. See TestVmSuspendRidesThePersistentRootdisk.
        vm_prov.delete_thread_vm.assert_awaited_once_with("tid-vm", purge_disk=True)

    @pytest.mark.asyncio
    async def test_vm_tier_snapshot_is_labelled_vm(self):
        """source_type is persisted into the snapshot manifest and read back on
        restore, so mislabelling a VM snapshot 'pod' outlives the suspend."""
        svc, _ = make_vm_service()
        svc._db.get_thread = AsyncMock(return_value=make_vm_thread())

        await svc.suspend_thread_workspace("tid-vm")

        kwargs = svc._snapshot_service.capture_vm_snapshot.await_args.kwargs
        assert kwargs["source_type"] == "vm"
        assert kwargs["ssh_host"] == "100.64.0.235"
        # _resolve_ssh_port also branched on ws_ctx presence, so a vm-tier thread
        # (whose ws_ctx holds git coordinates) was handed the POD port 30022.
        assert kwargs["ssh_port"] == 22

    @pytest.mark.asyncio
    async def test_vm_tier_status_markers_go_to_vm_context(self):
        """Progress markers must land on metadata.vm, not on the git-only
        workspace_container key."""
        svc, _ = make_vm_service()
        svc._db.get_thread = AsyncMock(return_value=make_vm_thread())

        await svc.suspend_thread_workspace("tid-vm")

        assert svc._db.merge_thread_vm_context.await_count >= 1
        svc._db.merge_thread_workspace_context.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_container_tier_thread_still_uses_pod_branch(self):
        """Regression guard: sandbox-tier threads are unaffected."""
        svc, vm_prov = make_vm_service()
        svc._db.get_thread = AsyncMock(return_value=make_container_thread())

        ok = await svc.suspend_thread_workspace("tid-pod")

        assert ok is True
        vm_prov.delete_thread_vm.assert_not_awaited()
        svc._container_provisioner.delete_workspace.assert_awaited()
        kwargs = svc._snapshot_service.capture_vm_snapshot.await_args.kwargs
        assert kwargs["source_type"] == "pod"

    @pytest.mark.asyncio
    async def test_idle_sweep_actually_suspends_a_vm_thread(self):
        """End-to-end for the leak: the sweeper already SELECTed vm-tier threads
        (metadata->'vm'->>'status' = 'ready'), but suspend_thread_workspace bailed
        on every one of them, so an idle VM session's VM ran until the session
        ended. This asserts the whole chain now completes."""
        svc, vm_prov = make_vm_service()
        thread = make_vm_thread()
        svc._db._conn.fetch = AsyncMock(
            return_value=[
                {
                    "id": "tid-vm",
                    "metadata": thread["metadata"],
                    "last_activity": datetime.now(timezone.utc) - timedelta(hours=3),
                }
            ]
        )
        svc._db.get_thread = AsyncMock(return_value=thread)

        n = await svc.check_idle_threads()

        assert n == 1, "idle vm-tier thread must actually be suspended"
        vm_prov.delete_thread_vm.assert_awaited_once_with("tid-vm", purge_disk=True)


class TestVmSuspendRidesThePersistentRootdisk:
    """VM session suspend used to be fail-closed on a snapshot that can never
    succeed: capture_vm_snapshot SSHes from the orchestrator, a VM workspace is
    only reachable over the tailnet, so every VM suspend refused and left the VM
    running. With a persistent rootdisk the snapshot stops being load-bearing —
    the disk itself carries the files across the teardown.

    docs/features/vm_workspace_persistence_reconciliation.md,
    docs/issues/vm_workspace_snapshot_unreachable_from_orchestrator.md.
    """

    @pytest.mark.asyncio
    async def test_suspend_keeps_the_rootdisk(self, monkeypatch):
        monkeypatch.setenv("VM_PERSISTENT_ROOTDISK", "true")
        svc, vm_prov = make_vm_service()
        svc._db.get_thread = AsyncMock(return_value=make_vm_thread())

        ok = await svc.suspend_thread_workspace("tid-vm")

        assert ok is True
        vm_prov.delete_thread_vm.assert_awaited_once_with("tid-vm", purge_disk=False)

    @pytest.mark.asyncio
    async def test_unreachable_snapshot_no_longer_blocks_suspend(self, monkeypatch):
        """The exact live failure: capture returns False (not raises) because
        the orchestrator has no tailnet route. The VM must still suspend."""
        monkeypatch.setenv("VM_PERSISTENT_ROOTDISK", "true")
        svc, vm_prov = make_vm_service()
        svc._db.get_thread = AsyncMock(return_value=make_vm_thread())
        svc._snapshot_service.capture_vm_snapshot = AsyncMock(return_value=False)

        ok = await svc.suspend_thread_workspace("tid-vm")

        assert ok is True, "a kept disk makes the snapshot non-load-bearing"
        vm_prov.delete_thread_vm.assert_awaited_once_with("tid-vm", purge_disk=False)

    @pytest.mark.asyncio
    async def test_suspend_records_the_kept_disk(self, monkeypatch):
        """Restore reads this to decide whether to extract a snapshot over the
        reattached disk (it must not)."""
        monkeypatch.setenv("VM_PERSISTENT_ROOTDISK", "true")
        svc, _ = make_vm_service()
        svc._db.get_thread = AsyncMock(return_value=make_vm_thread())

        await svc.suspend_thread_workspace("tid-vm")

        merged = {}
        for call in svc._db.merge_thread_vm_context.await_args_list:
            merged.update(call.args[1])
        assert merged.get("rootdisk") == "kept"

    @pytest.mark.asyncio
    async def test_flag_off_stays_fail_closed(self, monkeypatch):
        """Without the controller-side flag the disk cascade-deletes, so a
        failed snapshot must still keep the workspace alive — suspending would
        destroy the session."""
        monkeypatch.setenv("VM_PERSISTENT_ROOTDISK", "false")
        svc, vm_prov = make_vm_service()
        svc._db.get_thread = AsyncMock(return_value=make_vm_thread())
        svc._snapshot_service.capture_vm_snapshot = AsyncMock(return_value=False)

        ok = await svc.suspend_thread_workspace("tid-vm")

        assert ok is False
        vm_prov.delete_thread_vm.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_container_tier_stays_fail_closed(self, monkeypatch):
        """Pods have no rootdisk to keep — the snapshot is still the only copy,
        flag or no flag."""
        monkeypatch.setenv("VM_PERSISTENT_ROOTDISK", "true")
        svc, _ = make_vm_service()
        svc._db.get_thread = AsyncMock(return_value=make_container_thread())
        svc._snapshot_service.capture_vm_snapshot = AsyncMock(return_value=False)

        ok = await svc.suspend_thread_workspace("tid-pod")

        assert ok is False
        svc._container_provisioner.delete_workspace.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_restore_does_not_extract_over_a_reattached_disk(self, monkeypatch):
        """The disk is the live state at teardown; any snapshot is at best the
        same moment and at worst older. Extracting it would overwrite newer
        files with stale ones."""
        monkeypatch.setenv("VM_PERSISTENT_ROOTDISK", "true")
        svc, _ = make_vm_service()
        thread = make_vm_thread()
        thread["metadata"]["vm"]["rootdisk"] = "kept"
        svc._db.get_thread = AsyncMock(return_value=thread)
        svc._extract_snapshot = AsyncMock()

        ok = await svc.restore_thread_workspace("tid-vm")

        assert ok is True
        svc._extract_snapshot.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_restore_still_extracts_when_the_disk_was_purged(self, monkeypatch):
        """A thread suspended before the flag was on has no disk waiting — its
        S3 snapshot is still the only copy."""
        monkeypatch.setenv("VM_PERSISTENT_ROOTDISK", "true")
        svc, _ = make_vm_service()
        thread = make_vm_thread()
        thread["metadata"]["vm"]["rootdisk"] = "purged"
        svc._db.get_thread = AsyncMock(return_value=thread)
        svc._extract_snapshot = AsyncMock()

        await svc.restore_thread_workspace("tid-vm")

        svc._extract_snapshot.assert_awaited_once()
