"""Thread cloud-diff read endpoints — summary, per-file, restage (Task 8).

Owner-facing review surface for protected cloud mode (Slice C): ``GET
.../cloud-diff`` (summary), ``GET .../cloud-diff/{path}`` (per-file content),
``POST .../cloud-diff/restage``. All three share one gate — 404 unless the
thread is protected (``metadata.protected_cloud``) AND
``PROTECTED_CLOUD_MODE_ENABLED`` — and one resolver
(``main._thread_cloud_diff_source``) that builds a real ``UpperdirDiffSource``
(Task 7) off a fake S3 manifest + tar via a patched ``snapshot_service``, so
these tests exercise the actual diff-source logic rather than re-mocking it.

Spec §11: reads must work for ENDED threads too — a mount row can be
``status='revoked'`` (grant already reconciled away) while
``staged_summary`` is still present, and review must still work. Only
restage needs a live workspace.

Follows the house pattern in tests/test_job_diff_endpoints.py and
tests/cloud_staging/test_stage_triggers.py: ``import main`` (conftest puts
orchestrator/ on sys.path), ExitStack-patch its module globals
(``require_thread_owner`` / ``postgres_db`` / ``snapshot_service`` /
``main_cloud_router``), and call the endpoint coroutines directly.
"""

import hashlib
import io
import json
import tarfile
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

import main
from tests.cloud.fake import FakeMainCloudBackend

THREAD_ID = "thread-cd-1"


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #


def _make_user() -> dict:
    return {
        "id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "email": "user@example.test",
    }


def _make_thread(
    *, protected: bool = True, workspace: bool = True, **meta_over
) -> dict:
    metadata = {"protected_cloud": protected}
    if workspace:
        metadata["workspace_container"] = {"pod_ip": "10.0.0.5", "port": 30022}
    metadata.update(meta_over)
    return {"id": THREAD_ID, "user_id": _make_user()["id"], "metadata": metadata}


def _build_tar(members: list[tuple[str, bytes]]) -> bytes:
    """Same helper pattern as test_upperdir_diff_source.py::_build_tar."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tf:
        for name, data in members:
            ti = tarfile.TarInfo(name=name)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


def _manifest(
    entries: list[dict], *, tar_bytes: bytes, epoch=3, staged_at="2026-07-12T00:00:00Z"
) -> dict:
    counts = {"added": 0, "modified": 0, "deleted": 0}
    for e in entries:
        counts[e["status"]] += 1
    return {
        "epoch": epoch,
        "staged_at": staged_at,
        "counts": counts,
        "entries": entries,
        "skipped": [],
        "tar_sha256": hashlib.sha256(tar_bytes).hexdigest(),
    }


def _mount_row(*, staged_summary=None, status="active", backend="fake") -> dict:
    return {
        "id": "mount-1",
        "status": status,
        "backend": backend,
        "staged_summary": staged_summary,
    }


def _thread_mounts_row(*, mountpoint="MyProject", cloud_handle="proj-1") -> dict:
    # Real ``thread_mounts`` rows use backend_id/cloud_handle (verified against
    # postgres.list_thread_mounts' SELECT) — select_protected_mount matches on
    # those two keys. ``mountpoint`` is NOT a real column (real rows carry
    # ``target_path`` instead); it's included here to drive the display-name
    # resolution the same way tests/cloud_staging/test_manifest.py does.
    return {
        "backend_id": "nextcloud",
        "cloud_handle": cloud_handle,
        "mountpoint": mountpoint,
    }


def _snapshot_service(blobs: dict[str, bytes] | None = None):
    svc = MagicMock()

    async def _get_blob(key):
        return (blobs or {}).get(key)

    svc.get_blob = AsyncMock(side_effect=_get_blob)
    return svc


def _cloud_router(backend):
    router = MagicMock()
    router.for_backend = MagicMock(return_value=backend)
    return router


def _patch_endpoint(
    *,
    user: dict,
    thread: dict,
    ro_mount_row: dict | None = None,
    thread_mounts: list[dict] | None = None,
    snapshot_service=None,
    backend=None,
    require_thread_owner_result=None,
) -> tuple[ExitStack, MagicMock]:
    """Patch every global the three endpoints touch. Returns (stack, db)."""
    stack = ExitStack()
    if require_thread_owner_result is not None:
        stack.enter_context(
            patch("main.require_thread_owner", require_thread_owner_result)
        )
    else:
        stack.enter_context(
            patch("main.require_thread_owner", AsyncMock(return_value=(user, thread)))
        )
    db = MagicMock()
    db.get_ro_mount_by_thread = AsyncMock(return_value=ro_mount_row)
    db.list_thread_mounts = AsyncMock(return_value=thread_mounts or [])
    stack.enter_context(patch("main.postgres_db", db))
    stack.enter_context(
        patch("main.snapshot_service", snapshot_service or _snapshot_service())
    )
    stack.enter_context(
        patch(
            "main.main_cloud_router",
            _cloud_router(backend if backend is not None else FakeMainCloudBackend()),
        )
    )
    stack.enter_context(patch("main._is_protected_cloud_mode_enabled", lambda: True))
    return stack, db


# --------------------------------------------------------------------------- #
# GET /api/agents/threads/{thread_id}/cloud-diff  (summary)
# --------------------------------------------------------------------------- #


class TestCloudDiffSummary:
    @pytest.mark.asyncio
    async def test_summary_returns_counts_epoch_and_files(self, fake_request):
        user = _make_user()
        thread = _make_thread()
        tar_bytes = _build_tar(
            [("upper/new.txt", b"hello"), ("upper/mod.txt", b"world")]
        )
        entries = [
            {"path": "new.txt", "status": "added", "size": 5, "binary": False},
            {"path": "mod.txt", "status": "modified", "size": 5, "binary": False},
        ]
        manifest = _manifest(entries, tar_bytes=tar_bytes, epoch=7, staged_at="ts-7")
        row = _mount_row(
            staged_summary={"signature": "sig", "tar_sha256": "irrelevant"}
        )
        svc = _snapshot_service(
            {
                f"cloud-staging/{THREAD_ID}/manifest.json": json.dumps(
                    manifest
                ).encode(),
                f"cloud-staging/{THREAD_ID}/upper.tar": tar_bytes,
            }
        )
        stack, _db = _patch_endpoint(
            user=user,
            thread=thread,
            ro_mount_row=row,
            thread_mounts=[_thread_mounts_row()],
            snapshot_service=svc,
        )
        with stack:
            result = await main.get_thread_cloud_diff_summary(THREAD_ID, fake_request)

        assert result == {
            "thread_id": THREAD_ID,
            "epoch": 7,
            "staged_at": "ts-7",
            "counts": {"added": 1, "modified": 1, "deleted": 0},
            "protected_mount": "MyProject",
            "files": [
                {"path": "new.txt", "status": "added", "binary": False},
                {"path": "mod.txt", "status": "modified", "binary": False},
            ],
        }

    @pytest.mark.asyncio
    async def test_summary_empty_when_nothing_staged(self, fake_request):
        user = _make_user()
        thread = _make_thread()
        row = _mount_row(staged_summary=None)
        stack, _db = _patch_endpoint(
            user=user,
            thread=thread,
            ro_mount_row=row,
            thread_mounts=[_thread_mounts_row()],
        )
        with stack:
            result = await main.get_thread_cloud_diff_summary(THREAD_ID, fake_request)

        assert result == {
            "thread_id": THREAD_ID,
            "epoch": 0,
            "staged_at": None,
            "counts": {"added": 0, "modified": 0, "deleted": 0},
            "protected_mount": "MyProject",
            "files": [],
        }

    @pytest.mark.asyncio
    async def test_summary_404_when_thread_not_protected(self, fake_request):
        user = _make_user()
        thread = _make_thread(protected=False)
        stack, _db = _patch_endpoint(user=user, thread=thread)
        with stack, pytest.raises(HTTPException) as ei:
            await main.get_thread_cloud_diff_summary(THREAD_ID, fake_request)
        assert ei.value.status_code == 404

    @pytest.mark.asyncio
    async def test_summary_works_on_revoked_mount_row(self, fake_request):
        """Ended thread: mount row status='revoked' but staged_summary set —
        review must still work (spec §11); the resolver never checks status."""
        user = _make_user()
        thread = _make_thread(workspace=False)  # ended thread has no live pod
        tar_bytes = _build_tar([("upper/del.txt", b"")])
        entries = [
            {"path": "gone.txt", "status": "deleted", "size": 0, "binary": False}
        ]
        manifest = _manifest(
            entries, tar_bytes=tar_bytes, epoch=4, staged_at="ts-ended"
        )
        row = _mount_row(
            status="revoked",
            staged_summary={"signature": "sig", "tar_sha256": "irrelevant"},
        )
        svc = _snapshot_service(
            {
                f"cloud-staging/{THREAD_ID}/manifest.json": json.dumps(
                    manifest
                ).encode(),
                f"cloud-staging/{THREAD_ID}/upper.tar": tar_bytes,
            }
        )
        stack, _db = _patch_endpoint(
            user=user,
            thread=thread,
            ro_mount_row=row,
            thread_mounts=[_thread_mounts_row()],
            snapshot_service=svc,
        )
        with stack:
            result = await main.get_thread_cloud_diff_summary(THREAD_ID, fake_request)

        assert result["epoch"] == 4
        assert result["counts"] == {"added": 0, "modified": 0, "deleted": 1}
        assert result["files"] == [
            {"path": "gone.txt", "status": "deleted", "binary": False}
        ]


# --------------------------------------------------------------------------- #
# GET /api/agents/threads/{thread_id}/cloud-diff/{file_path:path}
# --------------------------------------------------------------------------- #


class TestCloudDiffFile:
    @pytest.mark.asyncio
    async def test_file_returns_old_new_content(self, fake_request):
        user = _make_user()
        thread = _make_thread()
        tar_bytes = _build_tar([("upper/mod.txt", b"newv")])
        entries = [
            {"path": "mod.txt", "status": "modified", "size": 4, "binary": False}
        ]
        manifest = _manifest(entries, tar_bytes=tar_bytes)
        row = _mount_row(
            staged_summary={"signature": "sig", "tar_sha256": "irrelevant"}
        )
        svc = _snapshot_service(
            {
                f"cloud-staging/{THREAD_ID}/manifest.json": json.dumps(
                    manifest
                ).encode(),
                f"cloud-staging/{THREAD_ID}/upper.tar": tar_bytes,
            }
        )
        backend = FakeMainCloudBackend()
        backend.seed_project_file("proj-1", "mod.txt", b"oldv")
        stack, _db = _patch_endpoint(
            user=user,
            thread=thread,
            ro_mount_row=row,
            thread_mounts=[_thread_mounts_row(cloud_handle="proj-1")],
            snapshot_service=svc,
            backend=backend,
        )
        with stack:
            result = await main.get_thread_cloud_diff_file(
                THREAD_ID, "mod.txt", fake_request
            )

        assert result == {
            "thread_id": THREAD_ID,
            "path": "mod.txt",
            "status": "modified",
            "old_content": "oldv",
            "new_content": "newv",
            "old_binary": False,
            "new_binary": False,
        }

    @pytest.mark.asyncio
    async def test_file_404_for_unknown_path(self, fake_request):
        user = _make_user()
        thread = _make_thread()
        tar_bytes = _build_tar([("upper/mod.txt", b"newv")])
        entries = [
            {"path": "mod.txt", "status": "modified", "size": 4, "binary": False}
        ]
        manifest = _manifest(entries, tar_bytes=tar_bytes)
        row = _mount_row(
            staged_summary={"signature": "sig", "tar_sha256": "irrelevant"}
        )
        svc = _snapshot_service(
            {
                f"cloud-staging/{THREAD_ID}/manifest.json": json.dumps(
                    manifest
                ).encode(),
                f"cloud-staging/{THREAD_ID}/upper.tar": tar_bytes,
            }
        )
        stack, _db = _patch_endpoint(
            user=user,
            thread=thread,
            ro_mount_row=row,
            thread_mounts=[_thread_mounts_row()],
            snapshot_service=svc,
        )
        with stack, pytest.raises(HTTPException) as ei:
            await main.get_thread_cloud_diff_file(
                THREAD_ID, "does-not-exist.txt", fake_request
            )
        assert ei.value.status_code == 404
        # The code is what lets the review UI say which of the three
        # situations this is instead of guessing "the session re-staged".
        assert ei.value.detail["code"] == "not_in_staged_diff"

    @pytest.mark.asyncio
    async def test_file_404_distinguishes_unreadable_staged_content(self, fake_request):
        """A path the manifest lists but whose tar cannot be trusted.

        ``_get_tar()`` refuses a tar whose sha256 does not match the
        manifest's ``tar_sha256`` (the torn manifest/tar pair defence), so
        ``file()`` returns None for an entry that IS staged. Reported as
        re-staging, this reads as "your session moved on"; it is actually
        "the staged copy is unusable", and only a restage fixes it.
        """
        user = _make_user()
        thread = _make_thread()
        tar_bytes = _build_tar([("upper/mod.txt", b"newv")])
        entries = [
            {"path": "mod.txt", "status": "modified", "size": 4, "binary": False}
        ]
        manifest = _manifest(entries, tar_bytes=tar_bytes)
        manifest["tar_sha256"] = "0" * 64  # content binding will fail
        row = _mount_row(
            staged_summary={"signature": "sig", "tar_sha256": "irrelevant"}
        )
        svc = _snapshot_service(
            {
                f"cloud-staging/{THREAD_ID}/manifest.json": json.dumps(
                    manifest
                ).encode(),
                f"cloud-staging/{THREAD_ID}/upper.tar": tar_bytes,
            }
        )
        stack, _db = _patch_endpoint(
            user=user,
            thread=thread,
            ro_mount_row=row,
            thread_mounts=[_thread_mounts_row()],
            snapshot_service=svc,
        )
        with stack, pytest.raises(HTTPException) as ei:
            await main.get_thread_cloud_diff_file(THREAD_ID, "mod.txt", fake_request)
        assert ei.value.status_code == 404
        assert ei.value.detail["code"] == "staged_content_unreadable"


# --------------------------------------------------------------------------- #
# POST /api/agents/threads/{thread_id}/cloud-diff/restage
# --------------------------------------------------------------------------- #


class TestCloudDiffRestage:
    @pytest.mark.asyncio
    async def test_restage_schedules_stage(self, fake_request):
        user = _make_user()
        thread = _make_thread(workspace=True)
        stack, _db = _patch_endpoint(user=user, thread=thread)
        stage_mock = AsyncMock(return_value={"epoch": 1, "counts": {}})
        main._cloud_stage_tasks.clear()
        with (
            stack,
            patch("services.cloud_staging.stage.stage_thread_cloud_diff", stage_mock),
        ):
            result = await main.restage_thread_cloud_diff(THREAD_ID, fake_request)
            assert result == {"scheduled": True}
            assert THREAD_ID in main._cloud_stage_tasks
            task = main._cloud_stage_tasks[THREAD_ID]
            await task

            # Assert inside the patch context: main.postgres_db/snapshot_service
            # are only the patched fakes while ``stack`` is still active.
            stage_mock.assert_awaited_once_with(
                thread_id=THREAD_ID,
                postgres_db=main.postgres_db,
                snapshot_service=main.snapshot_service,
            )
        assert THREAD_ID not in main._cloud_stage_tasks

    @pytest.mark.asyncio
    async def test_restage_409_without_workspace(self, fake_request):
        user = _make_user()
        thread = _make_thread(workspace=False)
        stack, _db = _patch_endpoint(user=user, thread=thread)
        main._cloud_stage_tasks.clear()
        with stack, pytest.raises(HTTPException) as ei:
            await main.restage_thread_cloud_diff(THREAD_ID, fake_request)
        assert ei.value.status_code == 409
        assert ei.value.detail == {"code": "no_workspace"}
        assert THREAD_ID not in main._cloud_stage_tasks


# --------------------------------------------------------------------------- #
# Auth propagation (shared by all three endpoints)
# --------------------------------------------------------------------------- #


class TestOwnerAuthPropagation:
    @pytest.mark.asyncio
    async def test_owner_auth_denied_for_other_user(self, fake_request):
        denied = AsyncMock(
            side_effect=HTTPException(status_code=403, detail="Not your thread")
        )
        stack, _db = _patch_endpoint(
            user=_make_user(),
            thread=_make_thread(),
            require_thread_owner_result=denied,
        )
        with stack, pytest.raises(HTTPException) as ei:
            await main.get_thread_cloud_diff_summary(THREAD_ID, fake_request)
        assert ei.value.status_code == 403
