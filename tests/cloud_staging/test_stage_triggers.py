"""Turn-end stage triggers — internal endpoint + teardown hook (Slice C, Task 5).

Covers:
* ``POST /api/agents/threads/{thread_id}/cloud-stage`` — internal-key gate,
  flag-off no-op, fire-and-forget task scheduling + de-dupe registry
  (``main._cloud_stage_tasks``, mirrors ``_protected_engage_tasks``).
* ``WorkspaceSuspensionService.suspend_thread_workspace`` — the teardown
  hook stages the protected thread's upperdir diff BEFORE the S3 VM
  snapshot capture, and swallows staging errors so the snapshot (the
  durable, load-bearing path) always still runs.

Follows the house patterns: ``tests/test_export_to_cloud_endpoint.py``
(ExitStack-patch + ``import main`` directly) and
``tests/test_internal_auth.py`` (bare ``fake_request`` fixture + patched
``access_module._INTERNAL_KEY`` for the 401 case).
"""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

import main
import security.access as access_module
from services.workspace_suspension import WorkspaceSuspensionService


# =============================================================================
# POST /api/agents/threads/{thread_id}/cloud-stage
# =============================================================================

_STAGE_AUTHORITY = {
    "runtime_generation": "11111111-1111-4111-8111-111111111111",
    "workspace_generation": "22222222-2222-4222-8222-222222222222",
    "expected_staged_epoch": 3,
    "runtime_retirement_token": None,
}


@asynccontextmanager
async def _owned_lock(*_args, **_kwargs):
    yield True


class TestCloudStageEndpoint:
    @pytest.mark.asyncio
    async def test_cloud_stage_requires_internal_key(self, fake_request):
        """No/garbage X-Internal-Key -> 401, before the flag or task logic runs."""
        with patch.object(access_module, "_INTERNAL_KEY", "secret"):
            with pytest.raises(HTTPException) as exc:
                await main.agent_trigger_cloud_stage(fake_request, "thread-1")
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    async def test_cloud_stage_flag_off_skips(self, fake_request):
        """Flag off -> {"skipped": "flag_off"}; no task is ever scheduled."""
        fake_request.headers = {"X-Internal-Key": "secret"}
        main._cloud_stage_tasks.clear()
        with (
            patch.object(access_module, "_INTERNAL_KEY", "secret"),
            patch("main._is_protected_cloud_mode_enabled", return_value=False),
        ):
            result = await main.agent_trigger_cloud_stage(fake_request, "thread-1")
        assert result == {"skipped": "flag_off"}
        assert main._cloud_stage_tasks == {}

    @pytest.mark.asyncio
    async def test_cloud_stage_schedules_task(self, fake_request):
        """Flag on -> a background task is registered + {"scheduled": True};
        the task calls stage_thread_cloud_diff and self-evicts from the
        registry when done."""
        fake_request.headers = {"X-Internal-Key": "secret"}
        main._cloud_stage_tasks.clear()
        stage_mock = AsyncMock(return_value={"epoch": 1, "counts": {}})
        with (
            patch.object(access_module, "_INTERNAL_KEY", "secret"),
            patch("main._is_protected_cloud_mode_enabled", return_value=True),
            patch("services.cloud_staging.stage.stage_thread_cloud_diff", stage_mock),
            patch.object(
                main.postgres_db,
                "get_thread",
                AsyncMock(return_value={"id": "thread-1"}),
            ),
            patch.object(
                main.postgres_db,
                "get_ro_mount_by_thread",
                AsyncMock(return_value={"id": "mount-1"}),
            ),
            patch.object(
                main.postgres_db,
                "thread_advisory_lock",
                side_effect=_owned_lock,
            ),
            patch.object(
                main, "_require_pinned_workspace_credential_owner", AsyncMock()
            ),
            patch.object(
                main,
                "_capture_cloud_stage_authority",
                return_value=dict(_STAGE_AUTHORITY),
            ),
        ):
            result = await main.agent_trigger_cloud_stage(fake_request, "thread-1")
            assert result == {"scheduled": True}
            # Task is registered synchronously (create_task schedules but does
            # not run until the event loop gets control back).
            task_key = main._cloud_stage_task_key("thread-1", _STAGE_AUTHORITY)
            assert task_key in main._cloud_stage_tasks
            task = main._cloud_stage_tasks[task_key]
            await task

        stage_mock.assert_awaited_once_with(
            thread_id="thread-1",
            postgres_db=main.postgres_db,
            snapshot_service=main.snapshot_service,
            authority=_STAGE_AUTHORITY,
        )
        # Self-evicts once the task completes.
        assert task_key not in main._cloud_stage_tasks

    @pytest.mark.asyncio
    async def test_cloud_stage_dedupes_inflight_thread(self, fake_request):
        """A second ping for the same thread while one is still in flight
        must not spawn a duplicate task."""
        fake_request.headers = {"X-Internal-Key": "secret"}
        main._cloud_stage_tasks.clear()
        sentinel_task = MagicMock()
        task_key = main._cloud_stage_task_key("thread-1", _STAGE_AUTHORITY)
        main._cloud_stage_tasks[task_key] = sentinel_task
        with (
            patch.object(access_module, "_INTERNAL_KEY", "secret"),
            patch("main._is_protected_cloud_mode_enabled", return_value=True),
            patch.object(
                main.postgres_db,
                "get_thread",
                AsyncMock(return_value={"id": "thread-1"}),
            ),
            patch.object(
                main.postgres_db,
                "get_ro_mount_by_thread",
                AsyncMock(return_value={"id": "mount-1"}),
            ),
            patch.object(
                main, "_require_pinned_workspace_credential_owner", AsyncMock()
            ),
            patch.object(
                main,
                "_capture_cloud_stage_authority",
                return_value=dict(_STAGE_AUTHORITY),
            ),
        ):
            result = await main.agent_trigger_cloud_stage(fake_request, "thread-1")
        assert result == {"scheduled": True}
        # Registry slot untouched — still the sentinel, no new task created.
        assert main._cloud_stage_tasks[task_key] is sentinel_task
        main._cloud_stage_tasks.clear()


# =============================================================================
# WorkspaceSuspensionService.suspend_thread_workspace — teardown hook
# =============================================================================


def _make_protected_thread(**overrides):
    thread = {
        "id": "thread-1",
        "metadata": {
            "protected_cloud": True,
            "workspace_container": {
                "status": "ready",
                "pod_ip": "10.0.0.5",
                "port": 30022,
            },
        },
    }
    thread.update(overrides)
    return thread


def _make_suspension_service(db):
    svc = WorkspaceSuspensionService()
    snapshot_service = MagicMock()
    snapshot_service.is_available = True
    snapshot_service.capture_vm_snapshot = AsyncMock(return_value=True)
    container_provisioner = MagicMock()
    container_provisioner.is_available = True
    container_provisioner.delete_workspace = AsyncMock(return_value=True)
    svc.connect(
        db=db,
        snapshot_service=snapshot_service,
        container_provisioner=container_provisioner,
    )
    return svc, snapshot_service, container_provisioner


def _make_db(thread):
    db = MagicMock()
    db.get_thread = AsyncMock(return_value=thread)
    db.merge_thread_workspace_context = AsyncMock(return_value=True)
    db.merge_thread_vm_context = AsyncMock(return_value=True)
    return db


class TestTeardownStageHook:
    @pytest.mark.asyncio
    async def test_teardown_hook_stages_before_snapshot(self):
        """The teardown hook must call stage_thread_cloud_diff and AWAIT it
        to completion before the S3 VM snapshot capture starts."""
        thread = _make_protected_thread()
        db = _make_db(thread)
        svc, snapshot_service, _ = _make_suspension_service(db)

        call_order: list[str] = []

        async def fake_stage(*, thread_id, postgres_db, snapshot_service):
            call_order.append("stage")
            return {"epoch": 1, "counts": {}}

        async def fake_capture(*args, **kwargs):
            call_order.append("snapshot")
            return True

        snapshot_service.capture_vm_snapshot = AsyncMock(side_effect=fake_capture)

        with patch(
            "services.cloud_staging.stage.stage_thread_cloud_diff",
            AsyncMock(side_effect=fake_stage),
        ):
            result = await svc.suspend_thread_workspace("thread-1")

        assert result is True
        assert call_order == ["stage", "snapshot"]

    @pytest.mark.asyncio
    async def test_teardown_hook_swallows_stage_errors(self):
        """A staging failure must not block or fail the teardown — the
        snapshot (durable path) still runs and suspend still succeeds."""
        thread = _make_protected_thread()
        db = _make_db(thread)
        svc, snapshot_service, _ = _make_suspension_service(db)

        stage_mock = AsyncMock(side_effect=RuntimeError("ssh capture failed"))
        with patch("services.cloud_staging.stage.stage_thread_cloud_diff", stage_mock):
            result = await svc.suspend_thread_workspace("thread-1")

        assert result is True
        stage_mock.assert_awaited_once()
        snapshot_service.capture_vm_snapshot.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_teardown_hook_skipped_for_unprotected_thread(self):
        """Non-protected threads never call the stage path at all."""
        thread = _make_protected_thread(
            metadata={
                "protected_cloud": False,
                "workspace_container": {
                    "status": "ready",
                    "pod_ip": "10.0.0.5",
                    "port": 30022,
                },
            }
        )
        db = _make_db(thread)
        svc, snapshot_service, _ = _make_suspension_service(db)

        stage_mock = AsyncMock(return_value={"epoch": 1})
        with patch("services.cloud_staging.stage.stage_thread_cloud_diff", stage_mock):
            result = await svc.suspend_thread_workspace("thread-1")

        assert result is True
        stage_mock.assert_not_awaited()
        snapshot_service.capture_vm_snapshot.assert_awaited_once()
