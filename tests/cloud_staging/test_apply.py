"""Apply / reject engine — protected cloud mode Slice C (Task 10).

The security-critical write path: whole-diff, epoch-pinned, conflict-gated
writes to the real cloud folder. Fakes: ``FakeMainCloudBackend`` (real
folder registration + file storage, tests/cloud/fake.py), a fake
``snapshot_service`` (MagicMock with dict-backed AsyncMock ``get_blob`` /
``delete_blob``, mirrors tests/cloud_staging/test_upperdir_diff_source.py),
and a ``MagicMock`` ``postgres_db`` with AsyncMock methods. Tar/manifest
fixtures follow the same builder pattern as
tests/cloud_staging/test_upperdir_diff_source.py::_build_tar.

Note on etags: ``FakeMainCloudBackend.list_project_folder`` always reports
``etag=""`` for every file (tests/cloud/fake.py never sets it). So a clean
(non-diverged) baseline entry is ``path: ""``, and any other string in
``etag_baseline`` deterministically mismatches — this is what the conflict
tests below use to force an ``etag_mismatch``.
"""

import hashlib
import io
import json
import tarfile
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.services.cloud.errors import CloudBackendError, CloudBackendErrorKind
from orchestrator.services.cloud.handles import ProjectFolderHandle
from orchestrator.services.cloud_staging.apply import (
    StagedApplyError,
    apply_staged_diff,
    reject_staged_diff,
)
from orchestrator.services.cloud_staging.stage import (
    staging_manifest_key,
    staging_tar_key,
)
from orchestrator.services.cloud_staging.source_identity import (
    ProtectedMountSourceIdentity,
)
from orchestrator.services.job_cloud_baseline import (
    detect_external_mods_against_baseline,
)

from tests.cloud.fake import FakeMainCloudBackend

THREAD_ID = "thread-apply-1"
_ENGAGE_ATTEMPT = "11111111-1111-4111-8111-111111111111"
_SOURCE = ProtectedMountSourceIdentity(
    backend_instance_id="22222222-2222-4222-8222-222222222222",
    source_ref="33333333-3333-4333-8333-333333333333",
    target_path="projects/proj",
    native_id="1",
    mountpoint="proj",
)


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #


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
    entries: list[dict], *, tar_bytes: bytes, epoch: int, staged_at: str = "ts-1"
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
        "source_binding": _SOURCE.binding,
        "source_binding_sha256": _SOURCE.sha256,
    }


def _mount_row(
    *,
    epoch: int = 5,
    staged_summary=...,
    etag_baseline: dict | None = None,
    row_id: str = "mount-1",
    backend: str = "nextcloud",
) -> dict:
    if staged_summary is ...:
        staged_summary = {
            "counts": {},
            "signature": "sig",
            "tar_sha256": "irrelevant",
            "source_binding": _SOURCE.binding,
            "source_binding_sha256": _SOURCE.sha256,
        }
    elif isinstance(staged_summary, dict):
        staged_summary = dict(staged_summary)
        staged_summary.setdefault("source_binding", _SOURCE.binding)
        staged_summary.setdefault("source_binding_sha256", _SOURCE.sha256)
    return {
        "id": row_id,
        "status": "active",
        "backend": backend,
        "staged_epoch": epoch,
        "staged_summary": staged_summary,
        "etag_baseline": etag_baseline if etag_baseline is not None else {},
        "engage_attempt": _ENGAGE_ATTEMPT,
        "source_binding": _SOURCE.binding,
        "source_binding_sha256": _SOURCE.sha256,
    }


def _thread_mounts_row(*, cloud_handle: str) -> dict:
    # Real thread_mounts rows carry backend_id/cloud_handle (Task 8 note) —
    # select_protected_mount matches on those two keys.
    return {
        "backend_id": "nextcloud",
        "cloud_handle": cloud_handle,
        "target_path": "/x",
    }


def _snapshot_service(blobs: dict[str, bytes] | None = None):
    svc = MagicMock()
    store = dict(blobs or {})

    async def _get_blob(key):
        return store.get(key)

    async def _delete_blob(key):
        store.pop(key, None)
        return True

    svc.get_blob = AsyncMock(side_effect=_get_blob)
    svc.delete_blob = AsyncMock(side_effect=_delete_blob)
    return svc


def _db(*, mount_row, thread_mounts) -> MagicMock:
    db = MagicMock()
    db.get_ro_mount_by_thread = AsyncMock(return_value=mount_row)
    db.list_thread_mounts = AsyncMock(return_value=thread_mounts)
    db.update_ro_mount_baseline = AsyncMock(return_value=True)
    db.update_ro_mount_staging = AsyncMock(return_value=True)
    return db


def _cloud_router(backend) -> MagicMock:
    router = MagicMock()
    router.for_backend_instance = MagicMock(return_value=backend)
    return router


async def _seeded_backend(
    files: dict[str, bytes] | None = None,
    *,
    backend: FakeMainCloudBackend | None = None,
) -> tuple[FakeMainCloudBackend, str]:
    """Ensure a project folder (so list/put work, not just seed_project_file's
    direct-poke path get/put use) and seed it with ``files``. Returns
    ``(backend, native_id)`` — use ``native_id`` as ``cloud_handle`` on the
    thread_mounts row.
    """
    backend = backend if backend is not None else FakeMainCloudBackend()
    handle = await backend.ensure_project_folder(project_name="proj", group_id="grp-1")
    for path, data in (files or {}).items():
        backend.seed_project_file(handle.native_id, path, data)
    return backend, handle.native_id


def _write_ops(backend: FakeMainCloudBackend) -> list[tuple[str, tuple, dict]]:
    """``backend.calls`` filtered to actual cloud mutations — excludes fixture
    setup noise like ``ensure_project_folder``."""
    return [
        c
        for c in backend.calls
        if c[0] in ("delete_project_folder_file", "put_project_folder_file_bytes")
    ]


class _FaultyPutBackend(FakeMainCloudBackend):
    """FakeMainCloudBackend that raises for one specific put path — models a
    single-file write failure mid-apply (fault injection the base fake
    doesn't support since its ``fail_mode`` is all-or-nothing)."""

    def __init__(self, *, fail_path: str) -> None:
        super().__init__()
        self._fail_path = fail_path

    async def put_project_folder_file_bytes(
        self, handle, *, path, content, content_type=None
    ):
        if path == self._fail_path:
            raise CloudBackendError(
                CloudBackendErrorKind.UNAVAILABLE, "boom", backend=self.backend_id
            )
        return await super().put_project_folder_file_bytes(
            handle, path=path, content=content, content_type=content_type
        )


# --------------------------------------------------------------------------- #
# Gates (409/410) — must short-circuit before any read/write
# --------------------------------------------------------------------------- #


class TestApplyGates:
    @pytest.mark.asyncio
    async def test_legacy_staging_without_source_fails_apply_closed(self):
        row = _mount_row(epoch=5)
        row.pop("source_binding")
        row.pop("source_binding_sha256")
        row["staged_summary"].pop("source_binding")
        row["staged_summary"].pop("source_binding_sha256")
        db = _db(mount_row=row, thread_mounts=[])
        router = _cloud_router(FakeMainCloudBackend())

        with pytest.raises(StagedApplyError) as exc:
            await apply_staged_diff(
                thread_id=THREAD_ID,
                epoch=5,
                postgres_db=db,
                main_cloud_router=router,
                snapshot_service=_snapshot_service(),
                reset_agent_overlay=AsyncMock(return_value=True),
            )

        assert exc.value.detail == {"code": "staged_source_invalid"}
        router.for_backend_instance.assert_not_called()

    @pytest.mark.asyncio
    async def test_apply_epoch_stale_409(self):
        row = _mount_row(epoch=5)
        db = _db(
            mount_row=row, thread_mounts=[_thread_mounts_row(cloud_handle="proj-1")]
        )

        with pytest.raises(StagedApplyError) as ei:
            await apply_staged_diff(
                thread_id=THREAD_ID,
                epoch=4,
                postgres_db=db,
                main_cloud_router=_cloud_router(FakeMainCloudBackend()),
                snapshot_service=_snapshot_service(),
                reset_agent_overlay=AsyncMock(return_value=True),
            )

        assert ei.value.status_code == 409
        assert ei.value.detail == {"code": "epoch_stale", "staged_epoch": 5}
        # Epoch pin fires before any further read/write.
        db.list_thread_mounts.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_apply_nothing_staged_409(self):
        row = _mount_row(staged_summary=None)
        db = _db(
            mount_row=row, thread_mounts=[_thread_mounts_row(cloud_handle="proj-1")]
        )

        with pytest.raises(StagedApplyError) as ei:
            await apply_staged_diff(
                thread_id=THREAD_ID,
                epoch=0,
                postgres_db=db,
                main_cloud_router=_cloud_router(FakeMainCloudBackend()),
                snapshot_service=_snapshot_service(),
                reset_agent_overlay=AsyncMock(return_value=True),
            )

        assert ei.value.status_code == 409
        assert ei.value.detail == {"code": "nothing_staged"}
        db.list_thread_mounts.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_apply_staging_missing_410(self):
        """Manifest content-binding mismatch (torn multi-replica pair) is
        caught by ``ensure_tar_bound()`` (post-review hardening: summary()
        is manifest-only now, so it no longer fails this way — see
        ``TestApplyTornTar`` below for the ordering-sensitive version of
        this). Apply must write NOTHING to the cloud in this case.
        """
        tar_bytes = _build_tar([("upper/new.txt", b"hello")])
        other_bytes = _build_tar([("upper/new.txt", b"different-content-entirely")])
        entries = [{"path": "new.txt", "status": "added", "size": 5, "binary": False}]
        # Manifest describes `other_bytes`, but the tar blob at the
        # deterministic key is `tar_bytes` — a torn pair.
        manifest = _manifest(entries, tar_bytes=other_bytes, epoch=5)
        row = _mount_row(epoch=5)
        backend, native_id = await _seeded_backend()
        db = _db(
            mount_row=row, thread_mounts=[_thread_mounts_row(cloud_handle=native_id)]
        )
        svc = _snapshot_service(
            {
                staging_tar_key(THREAD_ID): tar_bytes,
                staging_manifest_key(THREAD_ID): json.dumps(manifest).encode(),
            }
        )

        with pytest.raises(StagedApplyError) as ei:
            await apply_staged_diff(
                thread_id=THREAD_ID,
                epoch=5,
                postgres_db=db,
                main_cloud_router=_cloud_router(backend),
                snapshot_service=svc,
                reset_agent_overlay=AsyncMock(return_value=True),
            )

        assert ei.value.status_code == 410
        assert ei.value.detail == {"code": "staging_missing"}
        assert _write_ops(backend) == []
        db.update_ro_mount_staging.assert_not_awaited()
        db.update_ro_mount_baseline.assert_not_awaited()
        svc.delete_blob.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_apply_manifest_epoch_mismatch_409(self):
        """The manifest's own recorded ``epoch`` must match the DB row's
        ``staged_epoch`` even after the DB-row epoch pin already passed —
        this is the TOCTOU close: a concurrent turn-end stage can overwrite
        the S3 manifest/tar pair (and bump the row) in the gap between the
        DB read and the S3 read. Manifest epoch N+1 vs row epoch N -> 409,
        NOTHING written.
        """
        tar_bytes = _build_tar([("upper/new.txt", b"hello")])
        entries = [{"path": "new.txt", "status": "added", "size": 5, "binary": False}]
        # Manifest claims epoch 6, but the row (as apply read it) is still
        # pinned at 5 — as if a concurrent restage landed between apply's DB
        # read and its S3 read.
        manifest = _manifest(entries, tar_bytes=tar_bytes, epoch=6)
        row = _mount_row(epoch=5)
        backend, native_id = await _seeded_backend()
        db = _db(
            mount_row=row, thread_mounts=[_thread_mounts_row(cloud_handle=native_id)]
        )
        svc = _snapshot_service(
            {
                staging_tar_key(THREAD_ID): tar_bytes,
                staging_manifest_key(THREAD_ID): json.dumps(manifest).encode(),
            }
        )

        with pytest.raises(StagedApplyError) as ei:
            await apply_staged_diff(
                thread_id=THREAD_ID,
                epoch=5,
                postgres_db=db,
                main_cloud_router=_cloud_router(backend),
                snapshot_service=svc,
                reset_agent_overlay=AsyncMock(return_value=True),
            )

        assert ei.value.status_code == 409
        assert ei.value.detail == {"code": "epoch_stale", "staged_epoch": 5}
        assert _write_ops(backend) == []
        db.update_ro_mount_staging.assert_not_awaited()
        db.update_ro_mount_baseline.assert_not_awaited()
        svc.delete_blob.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_current_mount_selection_cannot_retarget_staged_source(self):
        """Mutable mount rows are ignored; the immutable A staging is used."""
        row = _mount_row(epoch=5)
        # Neither row satisfies select_protected_mount: wrong backend_id, or
        # a nextcloud row with no cloud_handle.
        db = _db(
            mount_row=row,
            thread_mounts=[
                {"backend_id": "gitea", "cloud_handle": "x", "target_path": "/a"},
                {"backend_id": "nextcloud", "cloud_handle": None, "target_path": "/b"},
            ],
        )
        svc = _snapshot_service()
        router = _cloud_router(FakeMainCloudBackend())

        with pytest.raises(StagedApplyError) as ei:
            await apply_staged_diff(
                thread_id=THREAD_ID,
                epoch=5,
                postgres_db=db,
                main_cloud_router=router,
                snapshot_service=svc,
                reset_agent_overlay=AsyncMock(return_value=True),
            )

        assert ei.value.status_code == 410
        assert ei.value.detail == {"code": "staging_missing"}
        router.for_backend_instance.assert_called_once_with(
            _SOURCE.backend_instance_id,
            expected_backend_id="nextcloud",
        )
        svc.get_blob.assert_awaited_once()
        db.list_thread_mounts.assert_not_awaited()
        db.update_ro_mount_staging.assert_not_awaited()


# --------------------------------------------------------------------------- #
# Conflict gate — hard 409, no force flag, scoped to touched paths
# --------------------------------------------------------------------------- #


class TestApplyConflictGate:
    @pytest.mark.asyncio
    async def test_apply_conflict_blocks_hard(self):
        tar_bytes = _build_tar([("upper/mod.txt", b"newv")])
        entries = [
            {"path": "mod.txt", "status": "modified", "size": 4, "binary": False}
        ]
        manifest = _manifest(entries, tar_bytes=tar_bytes, epoch=5)
        backend, native_id = await _seeded_backend({"mod.txt": b"oldv"})
        # Fake backend etags are always "" — any other baseline string
        # deterministically mismatches.
        row = _mount_row(epoch=5, etag_baseline={"mod.txt": "stale-etag"})
        db = _db(
            mount_row=row, thread_mounts=[_thread_mounts_row(cloud_handle=native_id)]
        )
        svc = _snapshot_service(
            {
                staging_tar_key(THREAD_ID): tar_bytes,
                staging_manifest_key(THREAD_ID): json.dumps(manifest).encode(),
            }
        )

        with pytest.raises(StagedApplyError) as ei:
            await apply_staged_diff(
                thread_id=THREAD_ID,
                epoch=5,
                postgres_db=db,
                main_cloud_router=_cloud_router(backend),
                snapshot_service=svc,
                reset_agent_overlay=AsyncMock(return_value=True),
            )

        assert ei.value.status_code == 409
        assert ei.value.detail["code"] == "external_modifications_detected"
        assert ei.value.detail["diverged"] == [
            {"path": "mod.txt", "kind": "etag_mismatch"}
        ]
        assert "force" not in ei.value.detail
        assert _write_ops(backend) == []  # no writes happened
        db.update_ro_mount_staging.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_apply_scope_paths_limits_conflict_check(self):
        """An external change on a path NOT in the staged diff must not
        block the apply — the conflict gate only checks touched paths."""
        tar_bytes = _build_tar([("upper/touched.txt", b"newv")])
        entries = [
            {"path": "touched.txt", "status": "modified", "size": 4, "binary": False}
        ]
        manifest = _manifest(entries, tar_bytes=tar_bytes, epoch=5)
        backend, native_id = await _seeded_backend(
            {"touched.txt": b"oldv", "untouched.txt": b"whatever"}
        )
        row = _mount_row(
            epoch=5,
            etag_baseline={
                "touched.txt": "",  # matches fake live etag -> no divergence
                "untouched.txt": "stale-etag",  # would mismatch, but out of scope
            },
        )
        db = _db(
            mount_row=row, thread_mounts=[_thread_mounts_row(cloud_handle=native_id)]
        )
        svc = _snapshot_service(
            {
                staging_tar_key(THREAD_ID): tar_bytes,
                staging_manifest_key(THREAD_ID): json.dumps(manifest).encode(),
            }
        )

        result = await apply_staged_diff(
            thread_id=THREAD_ID,
            epoch=5,
            postgres_db=db,
            main_cloud_router=_cloud_router(backend),
            snapshot_service=svc,
            reset_agent_overlay=AsyncMock(return_value=True),
        )

        assert result["errors"] == []
        assert result["applied"] == 1


# --------------------------------------------------------------------------- #
# Torn tar (content-binding failure) — must be caught BEFORE any write
# --------------------------------------------------------------------------- #


class TestApplyTornTar:
    @pytest.mark.asyncio
    async def test_torn_tar_apply_writes_nothing(self):
        """A torn multi-replica manifest/tar pair must be caught by
        ``ensure_tar_bound()`` before the delete loop even starts — deletes
        run first (whiteout-before-create), so if the torn-tar probe ran
        late (e.g. only inside the create loop, discovered per-file via
        ``raw_new_bytes``), a torn staging could still delete real cloud
        files before failing. This manifest mixes a delete AND a create so
        BOTH loops are proven to never have started: zero backend ops.
        """
        tar_bytes = _build_tar([("upper/new.txt", b"hello")])
        other_bytes = _build_tar([("upper/new.txt", b"different-content-entirely")])
        entries = [
            {"path": "old.txt", "status": "deleted", "size": 0, "binary": False},
            {"path": "new.txt", "status": "added", "size": 5, "binary": False},
        ]
        # Manifest describes `other_bytes`, but the tar blob at the
        # deterministic key is `tar_bytes` — a torn pair.
        manifest = _manifest(entries, tar_bytes=other_bytes, epoch=5)
        # A live file matching the baseline so the conflict gate passes
        # cleanly and execution actually reaches the ensure_tar_bound() gate.
        row = _mount_row(epoch=5, etag_baseline={"old.txt": ""})
        backend, native_id = await _seeded_backend({"old.txt": b"still here"})
        db = _db(
            mount_row=row, thread_mounts=[_thread_mounts_row(cloud_handle=native_id)]
        )
        svc = _snapshot_service(
            {
                staging_tar_key(THREAD_ID): tar_bytes,
                staging_manifest_key(THREAD_ID): json.dumps(manifest).encode(),
            }
        )

        with pytest.raises(StagedApplyError) as ei:
            await apply_staged_diff(
                thread_id=THREAD_ID,
                epoch=5,
                postgres_db=db,
                main_cloud_router=_cloud_router(backend),
                snapshot_service=svc,
                reset_agent_overlay=AsyncMock(return_value=True),
            )

        assert ei.value.status_code == 410
        assert ei.value.detail == {"code": "staging_missing"}
        # Zero backend ops: neither the delete nor the create ran.
        assert _write_ops(backend) == []
        db.update_ro_mount_staging.assert_not_awaited()
        db.update_ro_mount_baseline.assert_not_awaited()
        svc.delete_blob.assert_not_awaited()


# --------------------------------------------------------------------------- #
# Write ordering + fail-soft partial failure
# --------------------------------------------------------------------------- #


class TestApplyWrites:
    @pytest.mark.asyncio
    async def test_staged_a_never_writes_current_replacement_b(self):
        """A staged source is immutable even after thread_mounts selects B."""

        tar_bytes = _build_tar([("upper/new.txt", b"from-a")])
        entries = [{"path": "new.txt", "status": "added", "size": 6, "binary": False}]
        manifest = _manifest(entries, tar_bytes=tar_bytes, epoch=5)
        backend_a, native_a = await _seeded_backend()
        backend_b, native_b = await _seeded_backend()
        assert native_a == native_b == _SOURCE.native_id
        row = _mount_row(epoch=5)
        db = _db(
            mount_row=row,
            thread_mounts=[_thread_mounts_row(cloud_handle=native_b)],
        )
        svc = _snapshot_service(
            {
                staging_tar_key(THREAD_ID): tar_bytes,
                staging_manifest_key(THREAD_ID): json.dumps(manifest).encode(),
            }
        )
        router = _cloud_router(backend_a)
        router.for_backend = MagicMock(return_value=backend_b)

        result = await apply_staged_diff(
            thread_id=THREAD_ID,
            epoch=5,
            postgres_db=db,
            main_cloud_router=router,
            snapshot_service=svc,
            reset_agent_overlay=AsyncMock(return_value=True),
        )

        assert result["errors"] == []
        assert any(
            call[0] == "put_project_folder_file_bytes" for call in backend_a.calls
        )
        assert _write_ops(backend_b) == []
        router.for_backend.assert_not_called()
        db.list_thread_mounts.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_apply_deletes_before_creates(self):
        tar_bytes = _build_tar([("upper/new.txt", b"created")])
        entries = [
            {"path": "a.txt", "status": "deleted", "size": 0, "binary": False},
            {"path": "z.txt", "status": "deleted", "size": 0, "binary": False},
            {"path": "new.txt", "status": "added", "size": 7, "binary": False},
        ]
        manifest = _manifest(entries, tar_bytes=tar_bytes, epoch=5)
        backend, native_id = await _seeded_backend({"a.txt": b"1", "z.txt": b"2"})
        # A "deleted" manifest entry only ever exists for a path that WAS in
        # the baseline (manifest.py's whiteout-expansion rule) — a live file
        # with an etag matching baseline (fake etags are always "") so the
        # conflict gate sees it as clean, not "unexpected_at_cloud".
        row = _mount_row(epoch=5, etag_baseline={"a.txt": "", "z.txt": ""})
        db = _db(
            mount_row=row, thread_mounts=[_thread_mounts_row(cloud_handle=native_id)]
        )
        svc = _snapshot_service(
            {
                staging_tar_key(THREAD_ID): tar_bytes,
                staging_manifest_key(THREAD_ID): json.dumps(manifest).encode(),
            }
        )

        result = await apply_staged_diff(
            thread_id=THREAD_ID,
            epoch=5,
            postgres_db=db,
            main_cloud_router=_cloud_router(backend),
            snapshot_service=svc,
            reset_agent_overlay=AsyncMock(return_value=True),
        )

        assert result["errors"] == []
        assert result["deleted"] == 2
        assert result["applied"] == 1

        ops = [(op, args) for op, args, _kwargs in _write_ops(backend)]
        delete_positions = [
            i for i, (op, _a) in enumerate(ops) if op == "delete_project_folder_file"
        ]
        put_positions = [
            i for i, (op, _a) in enumerate(ops) if op == "put_project_folder_file_bytes"
        ]
        assert delete_positions and put_positions
        assert max(delete_positions) < min(put_positions)
        # Descending path order among the deletes (children before parents).
        delete_paths = [
            args[1] for op, args in ops if op == "delete_project_folder_file"
        ]
        assert delete_paths == sorted(delete_paths, reverse=True)

    @pytest.mark.asyncio
    async def test_apply_partial_failure_keeps_staging(self):
        tar_bytes = _build_tar([("upper/ok.txt", b"good"), ("upper/bad.txt", b"boom")])
        entries = [
            {"path": "ok.txt", "status": "added", "size": 4, "binary": False},
            {"path": "bad.txt", "status": "added", "size": 4, "binary": False},
        ]
        manifest = _manifest(entries, tar_bytes=tar_bytes, epoch=5)
        backend, native_id = await _seeded_backend(
            backend=_FaultyPutBackend(fail_path="bad.txt")
        )
        row = _mount_row(epoch=5, etag_baseline={})
        db = _db(
            mount_row=row, thread_mounts=[_thread_mounts_row(cloud_handle=native_id)]
        )
        svc = _snapshot_service(
            {
                staging_tar_key(THREAD_ID): tar_bytes,
                staging_manifest_key(THREAD_ID): json.dumps(manifest).encode(),
            }
        )
        reset_mock = AsyncMock(return_value=True)

        result = await apply_staged_diff(
            thread_id=THREAD_ID,
            epoch=5,
            postgres_db=db,
            main_cloud_router=_cloud_router(backend),
            snapshot_service=svc,
            reset_agent_overlay=reset_mock,
        )

        assert result["applied"] == 1
        assert result["deleted"] == 0
        assert len(result["errors"]) == 1
        assert "bad.txt" in result["errors"][0]
        assert "epoch" not in result
        assert "overlay_reset" not in result
        # Staging state must be completely untouched — retry is safe.
        db.update_ro_mount_staging.assert_not_awaited()
        db.update_ro_mount_baseline.assert_not_awaited()
        svc.delete_blob.assert_not_awaited()
        reset_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_apply_modified_entry_overwrites_cloud_bytes(self):
        """A ``modified`` entry PUTs the staged tar bytes over the existing
        cloud file — WebDAV overwrite semantics, byte-true."""
        new_bytes = b"\x89PNG\x00new-binary-content"  # byte-true, not text
        tar_bytes = _build_tar([("upper/mod.bin", new_bytes)])
        entries = [
            {
                "path": "mod.bin",
                "status": "modified",
                "size": len(new_bytes),
                "binary": True,
            }
        ]
        manifest = _manifest(entries, tar_bytes=tar_bytes, epoch=5)
        backend, native_id = await _seeded_backend({"mod.bin": b"old-content"})
        row = _mount_row(epoch=5, etag_baseline={"mod.bin": ""})
        db = _db(
            mount_row=row, thread_mounts=[_thread_mounts_row(cloud_handle=native_id)]
        )
        svc = _snapshot_service(
            {
                staging_tar_key(THREAD_ID): tar_bytes,
                staging_manifest_key(THREAD_ID): json.dumps(manifest).encode(),
            }
        )

        result = await apply_staged_diff(
            thread_id=THREAD_ID,
            epoch=5,
            postgres_db=db,
            main_cloud_router=_cloud_router(backend),
            snapshot_service=svc,
            reset_agent_overlay=AsyncMock(return_value=True),
        )

        assert result["errors"] == []
        assert result["applied"] == 1
        assert result["deleted"] == 0
        # The write went through put_project_folder_file_bytes...
        put_ops = [
            c for c in _write_ops(backend) if c[0] == "put_project_folder_file_bytes"
        ]
        assert [args[1] for _op, args, _kw in put_ops] == ["mod.bin"]
        # ...and the stored bytes are the exact staged bytes (old overwritten).
        stored = await backend.get_project_folder_file_bytes(
            ProjectFolderHandle(backend=backend.backend_id, native_id=native_id),
            path="mod.bin",
        )
        assert stored == new_bytes

    @pytest.mark.asyncio
    async def test_apply_missing_tar_member_collected_walk_continues(self):
        """A manifest entry whose path has NO tar member (binding still valid:
        the hash covers the actual tar) -> per-file error collected, the walk
        continues to other entries, and the partial-failure contract holds
        (staging untouched)."""
        tar_bytes = _build_tar([("upper/present.txt", b"here")])
        entries = [
            # Not in the tar — raw_new_bytes() returns None for it.
            {"path": "absent.txt", "status": "added", "size": 4, "binary": False},
            {"path": "present.txt", "status": "added", "size": 4, "binary": False},
        ]
        manifest = _manifest(entries, tar_bytes=tar_bytes, epoch=5)
        backend, native_id = await _seeded_backend()
        row = _mount_row(epoch=5, etag_baseline={})
        db = _db(
            mount_row=row, thread_mounts=[_thread_mounts_row(cloud_handle=native_id)]
        )
        svc = _snapshot_service(
            {
                staging_tar_key(THREAD_ID): tar_bytes,
                staging_manifest_key(THREAD_ID): json.dumps(manifest).encode(),
            }
        )
        reset_mock = AsyncMock(return_value=True)

        result = await apply_staged_diff(
            thread_id=THREAD_ID,
            epoch=5,
            postgres_db=db,
            main_cloud_router=_cloud_router(backend),
            snapshot_service=svc,
            reset_agent_overlay=reset_mock,
        )

        # Walk continued past the missing member: the present file landed.
        assert result["applied"] == 1
        assert result["errors"] == [
            "absent.txt: staged content missing from upperdir tar"
        ]
        put_ops = [
            c for c in _write_ops(backend) if c[0] == "put_project_folder_file_bytes"
        ]
        assert [args[1] for _op, args, _kw in put_ops] == ["present.txt"]
        # Partial contract: staging state untouched, no finalization steps ran.
        assert "epoch" not in result
        db.update_ro_mount_staging.assert_not_awaited()
        db.update_ro_mount_baseline.assert_not_awaited()
        svc.delete_blob.assert_not_awaited()
        reset_mock.assert_not_awaited()


# --------------------------------------------------------------------------- #
# Full success sequencing
# --------------------------------------------------------------------------- #


class TestApplySuccess:
    @pytest.mark.asyncio
    async def test_apply_success_full_sequence(self):
        tar_bytes = _build_tar([("upper/new.txt", b"hello")])
        entries = [{"path": "new.txt", "status": "added", "size": 5, "binary": False}]
        manifest = _manifest(entries, tar_bytes=tar_bytes, epoch=5)
        backend, native_id = await _seeded_backend()
        row = _mount_row(epoch=5, etag_baseline={})
        db = _db(
            mount_row=row, thread_mounts=[_thread_mounts_row(cloud_handle=native_id)]
        )
        svc = _snapshot_service(
            {
                staging_tar_key(THREAD_ID): tar_bytes,
                staging_manifest_key(THREAD_ID): json.dumps(manifest).encode(),
            }
        )

        call_order: list[str] = []

        async def _reset():
            call_order.append("reset")
            return True

        real_capture = backend.capture_etag_baseline

        async def _capture(handle):
            call_order.append("capture_etag_baseline")
            return await real_capture(handle)

        backend.capture_etag_baseline = _capture

        async def _update_baseline(
            row_id, baseline, *, require_active=True, **authority
        ):
            call_order.append("update_baseline")
            assert require_active is False  # bookkeeping must tolerate a revoked row
            assert authority == {
                "expected_engage_attempt": _ENGAGE_ATTEMPT,
                "expected_source_binding_sha256": _SOURCE.sha256,
                "expected_staged_epoch": 5,
            }
            return True

        db.update_ro_mount_baseline = AsyncMock(side_effect=_update_baseline)

        async def _delete_blob(key):
            call_order.append(f"delete_blob:{key}")
            return True

        svc.delete_blob = AsyncMock(side_effect=_delete_blob)

        async def _update_staging(
            row_id,
            *,
            staged_epoch,
            staged_summary,
            require_active=True,
            **authority,
        ):
            call_order.append("update_staging")
            assert require_active is False
            assert authority == {
                "expected_engage_attempt": _ENGAGE_ATTEMPT,
                "expected_source_binding_sha256": _SOURCE.sha256,
            }
            return True

        db.update_ro_mount_staging = AsyncMock(side_effect=_update_staging)

        result = await apply_staged_diff(
            thread_id=THREAD_ID,
            epoch=5,
            postgres_db=db,
            main_cloud_router=_cloud_router(backend),
            snapshot_service=svc,
            reset_agent_overlay=_reset,
        )

        assert call_order == [
            "reset",
            "capture_etag_baseline",
            "update_baseline",
            f"delete_blob:{staging_tar_key(THREAD_ID)}",
            f"delete_blob:{staging_manifest_key(THREAD_ID)}",
            "update_staging",
        ]
        assert result == {
            "applied": 1,
            "deleted": 0,
            "errors": [],
            "epoch": 6,
            "overlay_reset": True,
        }

    @pytest.mark.asyncio
    async def test_apply_success_with_dead_pod(self):
        tar_bytes = _build_tar([("upper/new.txt", b"hello")])
        entries = [{"path": "new.txt", "status": "added", "size": 5, "binary": False}]
        manifest = _manifest(entries, tar_bytes=tar_bytes, epoch=5)
        backend, native_id = await _seeded_backend()
        row = _mount_row(epoch=5, etag_baseline={})
        db = _db(
            mount_row=row, thread_mounts=[_thread_mounts_row(cloud_handle=native_id)]
        )
        svc = _snapshot_service(
            {
                staging_tar_key(THREAD_ID): tar_bytes,
                staging_manifest_key(THREAD_ID): json.dumps(manifest).encode(),
            }
        )

        result = await apply_staged_diff(
            thread_id=THREAD_ID,
            epoch=5,
            postgres_db=db,
            main_cloud_router=_cloud_router(backend),
            snapshot_service=svc,
            reset_agent_overlay=AsyncMock(return_value=False),
        )

        assert result["errors"] == []
        assert result["applied"] == 1
        assert result["epoch"] == 6
        assert result["overlay_reset"] is False
        # Staging still clears even though the overlay reset failed.
        db.update_ro_mount_staging.assert_awaited_once_with(
            row["id"],
            staged_epoch=6,
            staged_summary=None,
            require_active=False,
            expected_engage_attempt=_ENGAGE_ATTEMPT,
            expected_source_binding_sha256=_SOURCE.sha256,
        )


# --------------------------------------------------------------------------- #
# Reject
# --------------------------------------------------------------------------- #


class TestReject:
    @pytest.mark.asyncio
    async def test_legacy_staging_without_source_remains_rejectable(self):
        row = _mount_row(epoch=5)
        row.pop("source_binding")
        row.pop("source_binding_sha256")
        row["staged_summary"].pop("source_binding")
        row["staged_summary"].pop("source_binding_sha256")
        db = _db(mount_row=row, thread_mounts=[])

        result = await reject_staged_diff(
            thread_id=THREAD_ID,
            epoch=5,
            postgres_db=db,
            snapshot_service=_snapshot_service(),
            reset_agent_overlay=AsyncMock(return_value=True),
        )

        assert result["rejected"] is True
        assert db.update_ro_mount_staging.await_args.kwargs == {
            "staged_epoch": 6,
            "staged_summary": None,
            "require_active": False,
        }

    @pytest.mark.asyncio
    async def test_reject_clears_without_writes(self):
        row = _mount_row(epoch=5)
        db = _db(mount_row=row, thread_mounts=[])
        svc = _snapshot_service(
            {
                staging_tar_key(THREAD_ID): b"tar-bytes",
                staging_manifest_key(THREAD_ID): b"{}",
            }
        )

        result = await reject_staged_diff(
            thread_id=THREAD_ID,
            epoch=5,
            postgres_db=db,
            snapshot_service=svc,
            reset_agent_overlay=AsyncMock(return_value=True),
        )

        assert result == {"rejected": True, "epoch": 6, "overlay_reset": True}
        svc.delete_blob.assert_any_await(staging_tar_key(THREAD_ID))
        svc.delete_blob.assert_any_await(staging_manifest_key(THREAD_ID))
        db.update_ro_mount_staging.assert_awaited_once_with(
            row["id"],
            staged_epoch=6,
            staged_summary=None,
            require_active=False,
            expected_engage_attempt=_ENGAGE_ATTEMPT,
            expected_source_binding_sha256=_SOURCE.sha256,
        )
        db.update_ro_mount_baseline.assert_not_awaited()
        # Reject never resolves a mount/backend at all.
        db.list_thread_mounts.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reject_epoch_stale_409(self):
        row = _mount_row(epoch=5)
        db = _db(mount_row=row, thread_mounts=[])
        svc = _snapshot_service()

        with pytest.raises(StagedApplyError) as ei:
            await reject_staged_diff(
                thread_id=THREAD_ID,
                epoch=1,
                postgres_db=db,
                snapshot_service=svc,
                reset_agent_overlay=AsyncMock(return_value=True),
            )

        assert ei.value.status_code == 409
        assert ei.value.detail == {"code": "epoch_stale", "staged_epoch": 5}
        svc.delete_blob.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reject_nothing_staged_409(self):
        row = _mount_row(staged_summary=None)
        db = _db(mount_row=row, thread_mounts=[])
        svc = _snapshot_service()

        with pytest.raises(StagedApplyError) as ei:
            await reject_staged_diff(
                thread_id=THREAD_ID,
                epoch=0,
                postgres_db=db,
                snapshot_service=svc,
                reset_agent_overlay=AsyncMock(return_value=True),
            )

        assert ei.value.status_code == 409
        assert ei.value.detail == {"code": "nothing_staged"}


# --------------------------------------------------------------------------- #
# Bookkeeping on a revoked row (idle-drain reconciler can revoke a
# cloud_ro_mounts row within ~15min regardless of a pending review; reads
# already tolerate a revoked row — Task 8 — writes must too)
# --------------------------------------------------------------------------- #


class _RevokedAwareDB:
    """Minimal fake modeling the real SQL's ``WHERE id=$1 [AND
    status='active']`` gate — a plain ``AsyncMock(return_value=True)``
    couldn't distinguish "require_active reached the query and mattered"
    from "require_active was silently ignored". Mutates an in-memory row so
    the test can assert the UPDATE actually took effect.
    """

    def __init__(self, *, mount_row: dict, thread_mounts: list[dict]):
        self._row = dict(mount_row)
        self._thread_mounts = thread_mounts
        self.baseline_calls: list[tuple[str, dict, bool]] = []
        self.staging_calls: list[dict[str, Any]] = []

    async def get_ro_mount_by_thread(self, thread_id):
        return dict(self._row)

    async def list_thread_mounts(self, thread_id):
        return self._thread_mounts

    async def update_ro_mount_baseline(
        self, row_id, baseline, *, require_active=True, **_authority
    ):
        self.baseline_calls.append((row_id, baseline, require_active))
        if require_active and self._row.get("status") != "active":
            return False
        self._row["etag_baseline"] = baseline
        return True

    async def update_ro_mount_staging(
        self,
        row_id,
        *,
        staged_epoch,
        staged_summary,
        require_active=True,
        **_authority,
    ):
        self.staging_calls.append(
            {
                "staged_epoch": staged_epoch,
                "staged_summary": staged_summary,
                "require_active": require_active,
            }
        )
        if require_active and self._row.get("status") != "active":
            return False
        self._row["staged_epoch"] = staged_epoch
        self._row["staged_summary"] = staged_summary
        return True


class TestRevokedRowBookkeeping:
    @pytest.mark.asyncio
    async def test_apply_and_reject_bookkeeping_on_revoked_row(self):
        # --- apply on a revoked row ---
        tar_bytes = _build_tar([("upper/new.txt", b"hello")])
        entries = [{"path": "new.txt", "status": "added", "size": 5, "binary": False}]
        manifest = _manifest(entries, tar_bytes=tar_bytes, epoch=5)
        backend, native_id = await _seeded_backend()
        row = _mount_row(epoch=5, etag_baseline={})
        row["status"] = "revoked"
        db = _RevokedAwareDB(
            mount_row=row, thread_mounts=[_thread_mounts_row(cloud_handle=native_id)]
        )
        svc = _snapshot_service(
            {
                staging_tar_key(THREAD_ID): tar_bytes,
                staging_manifest_key(THREAD_ID): json.dumps(manifest).encode(),
            }
        )

        result = await apply_staged_diff(
            thread_id=THREAD_ID,
            epoch=5,
            postgres_db=db,
            main_cloud_router=_cloud_router(backend),
            snapshot_service=svc,
            reset_agent_overlay=AsyncMock(return_value=True),
        )

        assert result["errors"] == []
        assert result["epoch"] == 6
        # The write itself succeeded (revoked row never blocked reads/writes
        # to the cloud folder — only the bookkeeping gate is at issue here).
        assert result["applied"] == 1
        # Bookkeeping calls were made with require_active=False...
        assert db.baseline_calls[-1][2] is False
        assert db.staging_calls[-1]["require_active"] is False
        # ...and actually took effect on the revoked row (the fake's
        # require_active=True branch would have returned False and left
        # these untouched).
        assert db._row["etag_baseline"] == {"new.txt": ""}
        assert db._row["staged_summary"] is None
        assert db._row["staged_epoch"] == 6

        # --- reject on a (separate) revoked row ---
        row2 = _mount_row(epoch=9)
        row2["status"] = "revoked"
        db2 = _RevokedAwareDB(mount_row=row2, thread_mounts=[])
        svc2 = _snapshot_service(
            {
                staging_tar_key(THREAD_ID): b"tar-bytes",
                staging_manifest_key(THREAD_ID): b"{}",
            }
        )

        reject_result = await reject_staged_diff(
            thread_id=THREAD_ID,
            epoch=9,
            postgres_db=db2,
            snapshot_service=svc2,
            reset_agent_overlay=AsyncMock(return_value=True),
        )

        assert reject_result == {"rejected": True, "epoch": 10, "overlay_reset": True}
        assert db2.staging_calls[-1]["require_active"] is False
        assert db2._row["staged_summary"] is None
        assert db2._row["staged_epoch"] == 10


# --------------------------------------------------------------------------- #
# detect_external_mods_against_baseline — direct unit
# --------------------------------------------------------------------------- #


class TestDetectExternalModsAgainstBaseline:
    @pytest.mark.asyncio
    async def test_detect_against_baseline_scope_and_kinds(self):
        backend, native_id = await _seeded_backend(
            {"same.txt": b"x", "changed.txt": b"y", "new_on_cloud.txt": b"z"}
        )
        handle = ProjectFolderHandle(backend=backend.backend_id, native_id=native_id)
        baseline = {
            "same.txt": "",  # matches fake live etag "" -> no divergence
            "changed.txt": "stale",  # mismatches -> etag_mismatch
            "gone.txt": "was-here",  # not present live -> missing_at_cloud
        }

        diverged = await detect_external_mods_against_baseline(
            baseline_entries=baseline, backend=backend, handle=handle
        )

        assert sorted(diverged, key=lambda d: d["path"]) == sorted(
            [
                {"path": "changed.txt", "kind": "etag_mismatch"},
                {"path": "gone.txt", "kind": "missing_at_cloud"},
                {"path": "new_on_cloud.txt", "kind": "unexpected_at_cloud"},
            ],
            key=lambda d: d["path"],
        )

        # Scoped to a single touched path in both directions: the
        # out-of-scope missing/unexpected paths must not appear.
        scoped = await detect_external_mods_against_baseline(
            baseline_entries=baseline,
            backend=backend,
            handle=handle,
            scope_paths={"changed.txt"},
        )
        assert scoped == [{"path": "changed.txt", "kind": "etag_mismatch"}]
