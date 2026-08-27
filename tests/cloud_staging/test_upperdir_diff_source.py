"""``UpperdirDiffSource`` — protected cloud mode Slice C diff review (Task 7).

Summary comes from the S3 manifest (Task 4 shape); new-side bytes from the
staged ``upper.tar``; old-side bytes from the live cloud backend via the
same ``get_project_folder_file_bytes`` method ``export_job_to_shared_folder``
uses. Fakes: ``snapshot_service`` is a ``MagicMock`` with an ``AsyncMock``
``get_blob`` dispatched by key (mirrors ``tests/cloud_staging/test_stage.py``);
old-side reads go through ``FakeMainCloudBackend`` (tests/cloud/fake.py),
seeded via ``seed_project_file``. Tar-building helper mirrors
``tests/cloud_staging/test_manifest.py::_build_tar``.

Content binding (design §5 addendum 2026-07-12): every read that touches the
tar must first verify ``sha256(tar_bytes) == manifest["tar_sha256"]`` — on
mismatch or an absent hash, the whole staging is treated as missing
(``summary()``/``file()`` both ``None``). This is a multi-replica torn-pair
defense: two non-atomic S3 PUTs (manifest, tar) can interleave under two
orchestrator replicas racing the same thread's staging.
"""

import hashlib
import io
import json
import tarfile
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.services.cloud.handles import ProjectFolderHandle
from orchestrator.services.cloud_staging.stage import (
    staging_manifest_key,
    staging_tar_key,
)
from orchestrator.services.cloud_staging.source_identity import (
    ProtectedMountSourceIdentity,
)
from orchestrator.services.diff_source import DiffEntrySummary, UpperdirDiffSource

from tests.cloud.fake import FakeMainCloudBackend

THREAD_ID = "thread-upper-1"
_SOURCE = ProtectedMountSourceIdentity(
    backend_instance_id="11111111-1111-4111-8111-111111111111",
    source_ref="22222222-2222-4222-8222-222222222222",
    target_path="projects/example",
    native_id="1",
    mountpoint="Example",
)


def _build_tar(members: list[tuple[str, bytes]]) -> bytes:
    """Same helper pattern as test_manifest.py::_build_tar, in-memory.

    members: list of (name, data) tuples — regular files only (no whiteouts
    needed here; the manifest classification is Task 3's concern).
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tf:
        for name, data in members:
            ti = tarfile.TarInfo(name=name)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


def _manifest(
    entries: list[dict], *, tar_bytes: bytes, epoch=5, staged_at="ts-1", tar_sha256=None
):
    counts = {"added": 0, "modified": 0, "deleted": 0}
    for e in entries:
        counts[e["status"]] += 1
    return {
        "epoch": epoch,
        "staged_at": staged_at,
        "counts": counts,
        "entries": entries,
        "skipped": [],
        "tar_sha256": (
            hashlib.sha256(tar_bytes).hexdigest() if tar_sha256 is None else tar_sha256
        ),
        "source_binding": _SOURCE.binding,
        "source_binding_sha256": _SOURCE.sha256,
    }


def _snapshot_service(blobs: dict[str, bytes]):
    svc = MagicMock()

    async def _get_blob(key):
        return blobs.get(key)

    svc.get_blob = AsyncMock(side_effect=_get_blob)
    return svc


def _mount_row(staged: bool = True):
    if not staged:
        return {
            "id": "mount-1",
            "staged_summary": None,
            "source_binding": _SOURCE.binding,
            "source_binding_sha256": _SOURCE.sha256,
        }
    return {
        "id": "mount-1",
        "source_binding": _SOURCE.binding,
        "source_binding_sha256": _SOURCE.sha256,
        "staged_summary": {
            "counts": {},
            "signature": "sig",
            "tar_sha256": "irrelevant",
            "source_binding": _SOURCE.binding,
            "source_binding_sha256": _SOURCE.sha256,
        },
    }


def _make_source(
    *,
    tar_bytes: bytes | None,
    manifest: dict | None,
    thread_id: str = THREAD_ID,
    backend=None,
    handle=None,
    mount_row=None,
):
    blobs: dict[str, bytes] = {}
    if tar_bytes is not None:
        blobs[staging_tar_key(thread_id)] = tar_bytes
    if manifest is not None:
        blobs[staging_manifest_key(thread_id)] = json.dumps(manifest).encode()
    backend = backend if backend is not None else FakeMainCloudBackend()
    handle = (
        handle
        if handle is not None
        else ProjectFolderHandle(backend=backend.backend_id, native_id="proj-1")
    )
    row = mount_row if mount_row is not None else _mount_row(staged=True)
    return UpperdirDiffSource(
        thread_id=thread_id,
        mount_row=row,
        backend=backend,
        handle=handle,
        snapshot_service=_snapshot_service(blobs),
    )


# --------------------------------------------------------------------------- #
# summary()
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_summary_maps_manifest_entries_and_meta():
    tar_bytes = _build_tar([("upper/new.txt", b"hello"), ("upper/mod.txt", b"world")])
    entries = [
        {"path": "new.txt", "status": "added", "size": 5, "binary": False},
        {"path": "mod.txt", "status": "modified", "size": 5, "binary": False},
        {"path": "del.txt", "status": "deleted", "size": 0, "binary": False},
    ]
    manifest = _manifest(
        entries, tar_bytes=tar_bytes, epoch=9, staged_at="2026-07-12T00:00:00Z"
    )
    source = _make_source(tar_bytes=tar_bytes, manifest=manifest)

    summary = await source.summary()

    assert summary is not None
    assert summary.files == [
        DiffEntrySummary(path="new.txt", status="added", binary=False),
        DiffEntrySummary(path="mod.txt", status="modified", binary=False),
        DiffEntrySummary(path="del.txt", status="deleted", binary=False),
    ]
    assert summary.meta == {
        "epoch": 9,
        "staged_at": "2026-07-12T00:00:00Z",
        "counts": {"added": 1, "modified": 1, "deleted": 1},
        "source_binding": _SOURCE.binding,
        "source_binding_sha256": _SOURCE.sha256,
    }


@pytest.mark.asyncio
async def test_summary_none_when_no_manifest_blob():
    tar_bytes = _build_tar([("upper/new.txt", b"hello")])
    source = _make_source(
        tar_bytes=tar_bytes, manifest=None, mount_row=_mount_row(staged=True)
    )

    assert await source.summary() is None


@pytest.mark.asyncio
async def test_summary_none_when_mount_row_staged_summary_is_none():
    tar_bytes = _build_tar([("upper/new.txt", b"hello")])
    manifest = _manifest(
        [{"path": "new.txt", "status": "added", "size": 5, "binary": False}],
        tar_bytes=tar_bytes,
    )
    source = _make_source(
        tar_bytes=tar_bytes, manifest=manifest, mount_row=_mount_row(staged=False)
    )

    assert await source.summary() is None


# --------------------------------------------------------------------------- #
# file()
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_file_modified_old_from_cloud_new_from_tar():
    tar_bytes = _build_tar([("upper/mod.txt", b"newv")])
    entries = [{"path": "mod.txt", "status": "modified", "size": 4, "binary": False}]
    manifest = _manifest(entries, tar_bytes=tar_bytes)
    backend = FakeMainCloudBackend()
    handle = ProjectFolderHandle(backend=backend.backend_id, native_id="proj-1")
    backend.seed_project_file(handle.native_id, "mod.txt", b"oldv")
    source = _make_source(
        tar_bytes=tar_bytes, manifest=manifest, backend=backend, handle=handle
    )

    content = await source.file("mod.txt")

    assert content is not None
    assert content.status == "modified"
    assert content.old_content == "oldv"
    assert content.new_content == "newv"
    assert content.old_binary is False
    assert content.new_binary is False


@pytest.mark.asyncio
async def test_file_added_old_is_none():
    tar_bytes = _build_tar([("upper/new.txt", b"abc")])
    entries = [{"path": "new.txt", "status": "added", "size": 3, "binary": False}]
    manifest = _manifest(entries, tar_bytes=tar_bytes)
    source = _make_source(tar_bytes=tar_bytes, manifest=manifest)

    content = await source.file("new.txt")

    assert content is not None
    assert content.old_content is None
    assert content.old_binary is False
    assert content.new_content == "abc"


@pytest.mark.asyncio
async def test_file_deleted_new_is_none_old_from_cloud():
    tar_bytes = _build_tar([])  # deleted file has no upperdir tar member
    entries = [{"path": "del.txt", "status": "deleted", "size": 0, "binary": False}]
    manifest = _manifest(entries, tar_bytes=tar_bytes)
    backend = FakeMainCloudBackend()
    handle = ProjectFolderHandle(backend=backend.backend_id, native_id="proj-1")
    backend.seed_project_file(handle.native_id, "del.txt", b"gone")
    source = _make_source(
        tar_bytes=tar_bytes, manifest=manifest, backend=backend, handle=handle
    )

    content = await source.file("del.txt")

    assert content is not None
    assert content.new_content is None
    assert content.new_binary is False
    assert content.old_content == "gone"


@pytest.mark.asyncio
async def test_file_binary_member_sets_flag_and_none_content():
    raw = b"\x00\x01\x02\x03"  # tar member with b"\0" — not valid text content
    tar_bytes = _build_tar([("upper/bin.dat", raw)])
    entries = [{"path": "bin.dat", "status": "added", "size": len(raw), "binary": True}]
    manifest = _manifest(entries, tar_bytes=tar_bytes)
    source = _make_source(tar_bytes=tar_bytes, manifest=manifest)

    content = await source.file("bin.dat")

    assert content is not None
    assert content.new_content is None
    assert content.new_binary is True


@pytest.mark.asyncio
async def test_file_old_side_binary_cloud_bytes_flagged():
    tar_bytes = _build_tar([("upper/mod.bin", b"newtext")])
    entries = [{"path": "mod.bin", "status": "modified", "size": 7, "binary": False}]
    manifest = _manifest(entries, tar_bytes=tar_bytes)
    backend = FakeMainCloudBackend()
    handle = ProjectFolderHandle(backend=backend.backend_id, native_id="proj-1")
    backend.seed_project_file(handle.native_id, "mod.bin", b"\xff\xfe\x00\x01")
    source = _make_source(
        tar_bytes=tar_bytes, manifest=manifest, backend=backend, handle=handle
    )

    content = await source.file("mod.bin")

    assert content is not None
    assert content.old_content is None
    assert content.old_binary is True
    # new side is unaffected by the old side's decode failure
    assert content.new_content == "newtext"
    assert content.new_binary is False


# --------------------------------------------------------------------------- #
# raw_new_bytes()
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_raw_new_bytes_returns_exact_member_bytes():
    raw = b"\x89PNG\x00exact-bytes"
    tar_bytes = _build_tar([("upper/img.png", raw)])
    entries = [{"path": "img.png", "status": "added", "size": len(raw), "binary": True}]
    manifest = _manifest(entries, tar_bytes=tar_bytes)
    source = _make_source(tar_bytes=tar_bytes, manifest=manifest)

    out = await source.raw_new_bytes("img.png")

    assert out == raw


# --------------------------------------------------------------------------- #
# Content binding (mandatory — design §5 addendum 2026-07-12)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_content_binding_mismatch_treats_staging_as_missing():
    """Post-review hardening (2026-07-12): binding is no longer checked by
    summary() — a valid manifest always yields a summary now, even when the
    tar behind it is torn. The protection moves to the tar layer: any read
    that actually needs new-side bytes (``file()`` on an added/modified
    entry) still returns ``None`` on a binding mismatch.
    """
    tar_bytes = _build_tar([("upper/new.txt", b"abc")])
    other_bytes = _build_tar([("upper/new.txt", b"different-content-entirely")])
    entries = [{"path": "new.txt", "status": "added", "size": 3, "binary": False}]
    # manifest describes `other_bytes`, but the tar blob at the deterministic
    # key is `tar_bytes` — a torn pair.
    manifest = _manifest(entries, tar_bytes=other_bytes)
    source = _make_source(tar_bytes=tar_bytes, manifest=manifest)

    summary = await source.summary()
    assert summary is not None
    assert summary.files == [
        DiffEntrySummary(path="new.txt", status="added", binary=False)
    ]
    assert await source.file("new.txt") is None
    assert await source.raw_new_bytes("new.txt") is None
    assert await source.ensure_tar_bound() is False


@pytest.mark.asyncio
async def test_content_binding_absent_key_treats_staging_as_missing():
    """Same contract shift as above, absent-hash variant."""
    tar_bytes = _build_tar([("upper/new.txt", b"abc")])
    entries = [{"path": "new.txt", "status": "added", "size": 3, "binary": False}]
    manifest = _manifest(entries, tar_bytes=tar_bytes)
    del manifest["tar_sha256"]
    source = _make_source(tar_bytes=tar_bytes, manifest=manifest)

    summary = await source.summary()
    assert summary is not None
    assert summary.files == [
        DiffEntrySummary(path="new.txt", status="added", binary=False)
    ]
    assert await source.file("new.txt") is None
    assert await source.raw_new_bytes("new.txt") is None
    assert await source.ensure_tar_bound() is False


# --------------------------------------------------------------------------- #
# Manifest-only summaries (post-review hardening, 2026-07-12) — summary()
# must never download/hash the tar; only actual tar touches do.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_summary_does_not_download_tar():
    tar_bytes = _build_tar([("upper/new.txt", b"hello")])
    entries = [{"path": "new.txt", "status": "added", "size": 5, "binary": False}]
    manifest = _manifest(entries, tar_bytes=tar_bytes)
    source = _make_source(tar_bytes=tar_bytes, manifest=manifest)

    summary = await source.summary()

    assert summary is not None
    get_blob = source._snapshot_service.get_blob
    keys_fetched = [c.args[0] for c in get_blob.await_args_list]
    assert keys_fetched == [
        staging_manifest_key(THREAD_ID)
    ]  # manifest ONLY, never the tar


@pytest.mark.asyncio
async def test_summary_does_not_download_tar_even_when_called_repeatedly():
    """summary() is memoized, but even the first call alone must not reach
    the tar blob — repeated calls must not either (cache, not a fluke)."""
    tar_bytes = _build_tar([("upper/new.txt", b"hello")])
    entries = [{"path": "new.txt", "status": "added", "size": 5, "binary": False}]
    manifest = _manifest(entries, tar_bytes=tar_bytes)
    source = _make_source(tar_bytes=tar_bytes, manifest=manifest)

    await source.summary()
    await source.summary()
    await source.summary()

    get_blob = source._snapshot_service.get_blob
    assert get_blob.await_count == 1  # manifest fetched once, tar never


@pytest.mark.asyncio
async def test_ensure_tar_bound_true_for_valid_pair():
    tar_bytes = _build_tar([("upper/new.txt", b"hello")])
    entries = [{"path": "new.txt", "status": "added", "size": 5, "binary": False}]
    manifest = _manifest(entries, tar_bytes=tar_bytes)
    source = _make_source(tar_bytes=tar_bytes, manifest=manifest)

    assert await source.ensure_tar_bound() is True
    # Cached: a subsequent tar-touching read doesn't re-fetch either blob.
    get_blob = source._snapshot_service.get_blob
    call_count_after_bind = get_blob.await_count
    assert await source.raw_new_bytes("new.txt") == b"hello"
    assert get_blob.await_count == call_count_after_bind


@pytest.mark.asyncio
async def test_ensure_tar_bound_false_when_tar_blob_missing():
    entries = [{"path": "new.txt", "status": "added", "size": 5, "binary": False}]
    manifest = _manifest(
        entries, tar_bytes=b"whatever-this-hash-is-never-checked-against"
    )
    # No tar blob uploaded at all.
    source = _make_source(tar_bytes=None, manifest=manifest)

    assert await source.ensure_tar_bound() is False
