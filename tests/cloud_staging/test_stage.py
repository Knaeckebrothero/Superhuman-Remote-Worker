"""stage_thread_cloud_diff — turn-end staging pipeline (spec §5, Task 4).

Fake db/snapshot_service are MagicMock with AsyncMock methods; the two
subprocess seams (_run_ssh_capture / _stream_tar_to_file) are monkeypatched
per-test so nothing here actually shells out to ssh.
"""

import hashlib
import io
import json
import shutil
import tarfile
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.services.cloud_staging import stage


def _build_tar(tmp_path, members):
    """Same helper pattern as tests/cloud_staging/test_manifest.py::_build_tar.

    members: list of (name, kind, data, pax) tuples; kind in file|chr|dir.
    """
    p = tmp_path / "upper.tar"
    with tarfile.open(p, "w", format=tarfile.PAX_FORMAT) as tf:
        for name, kind, data, pax in members:
            ti = tarfile.TarInfo(name=name)
            if pax:
                ti.pax_headers = pax
            if kind == "chr":
                ti.type = tarfile.CHRTYPE
                ti.devmajor = 0
                ti.devminor = 0
                tf.addfile(ti)
            elif kind == "dir":
                ti.type = tarfile.DIRTYPE
                tf.addfile(ti)
            else:
                ti.size = len(data)
                tf.addfile(ti, io.BytesIO(data))
    return str(p)


_PROTECTED_METADATA = {
    "protected_cloud": True,
    "workspace_container": {"pod_ip": "10.0.0.5", "port": 30022},
}

_ACTIVE_ROW = {
    "id": "mount-1",
    "status": "active",
    "staged_epoch": 3,
    "staged_summary": None,
    "etag_baseline": {},
}


def _make_db(*, thread_metadata=None, mount_row=None):
    db = MagicMock()
    thread = {"id": "thread-1", "metadata": thread_metadata if thread_metadata is not None else {}}
    db.get_thread = AsyncMock(return_value=thread)
    db.get_ro_mount_by_thread = AsyncMock(return_value=mount_row)
    db.update_ro_mount_staging = AsyncMock(return_value=True)
    return db


def _make_snapshot_service():
    svc = MagicMock()
    svc.save_blob = AsyncMock(return_value="some-key")
    svc.upload_blob_file = AsyncMock(return_value=True)
    svc.delete_blob = AsyncMock(return_value=True)
    return svc


@pytest.fixture(autouse=True)
def _clear_inflight():
    stage._inflight.clear()
    yield
    stage._inflight.clear()


# =============================================================================
# Pure command builders
# =============================================================================


def test_command_strings_pinned():
    assert stage.stage_signature_cmd() == (
        "find /home/agent-host/.overlay/upper -mindepth 1 "
        "-printf '%P|%y|%s|%T@\\n' 2>/dev/null | sort | sha256sum | cut -d' ' -f1"
    )
    tar_cmd = stage.stage_tar_cmd()
    assert tar_cmd == (
        "tar --xattrs --xattrs-include='*' --acls -C /home/agent-host/.overlay "
        "-cf - upper"
    )
    assert "--xattrs" in tar_cmd
    assert "-C /home/agent-host/.overlay" in tar_cmd


def test_key_builders():
    assert stage.staging_tar_key("t1") == "cloud-staging/t1/upper.tar"
    assert stage.staging_manifest_key("t1") == "cloud-staging/t1/manifest.json"


# =============================================================================
# Early skips
# =============================================================================


@pytest.mark.asyncio
async def test_skips_when_not_protected(monkeypatch):
    db = _make_db(thread_metadata={"protected_cloud": False})
    svc = _make_snapshot_service()
    run_mock = AsyncMock()
    monkeypatch.setattr(stage, "_run_ssh_capture", run_mock)

    result = await stage.stage_thread_cloud_diff(
        thread_id="thread-1", postgres_db=db, snapshot_service=svc
    )

    assert result == {"skipped": "not_protected"}
    run_mock.assert_not_called()
    db.get_ro_mount_by_thread.assert_not_called()


@pytest.mark.asyncio
async def test_skips_when_no_active_mount(monkeypatch):
    db = _make_db(
        thread_metadata=_PROTECTED_METADATA,
        mount_row={**_ACTIVE_ROW, "status": "revoked"},
    )
    svc = _make_snapshot_service()
    run_mock = AsyncMock()
    monkeypatch.setattr(stage, "_run_ssh_capture", run_mock)

    result = await stage.stage_thread_cloud_diff(
        thread_id="thread-1", postgres_db=db, snapshot_service=svc
    )

    assert result == {"skipped": "no_active_mount"}
    run_mock.assert_not_called()


@pytest.mark.asyncio
async def test_skips_when_no_active_mount_missing_row(monkeypatch):
    db = _make_db(thread_metadata=_PROTECTED_METADATA, mount_row=None)
    svc = _make_snapshot_service()
    result = await stage.stage_thread_cloud_diff(
        thread_id="thread-1", postgres_db=db, snapshot_service=svc
    )
    assert result == {"skipped": "no_active_mount"}


@pytest.mark.asyncio
async def test_skips_when_no_workspace_host(monkeypatch):
    db = _make_db(
        thread_metadata={"protected_cloud": True},
        mount_row=dict(_ACTIVE_ROW),
    )
    svc = _make_snapshot_service()
    run_mock = AsyncMock(return_value=b"deadbeef\n")
    monkeypatch.setattr(stage, "_run_ssh_capture", run_mock)

    result = await stage.stage_thread_cloud_diff(
        thread_id="thread-1", postgres_db=db, snapshot_service=svc
    )

    assert result == {"skipped": "no_workspace"}
    run_mock.assert_not_called()


# =============================================================================
# Signature comparison
# =============================================================================


@pytest.mark.asyncio
async def test_unchanged_signature_skips_upload(monkeypatch):
    row = {
        **_ACTIVE_ROW,
        "staged_summary": {
            "counts": {"added": 1, "modified": 0, "deleted": 0},
            "signature": "deadbeef",
        },
    }
    db = _make_db(thread_metadata=_PROTECTED_METADATA, mount_row=row)
    svc = _make_snapshot_service()
    monkeypatch.setattr(stage, "_run_ssh_capture", AsyncMock(return_value=b"deadbeef\n"))
    stream_mock = AsyncMock()
    monkeypatch.setattr(stage, "_stream_tar_to_file", stream_mock)

    result = await stage.stage_thread_cloud_diff(
        thread_id="thread-1", postgres_db=db, snapshot_service=svc
    )

    assert result == {"skipped": "unchanged"}
    stream_mock.assert_not_called()
    svc.upload_blob_file.assert_not_called()
    db.update_ro_mount_staging.assert_not_called()


# =============================================================================
# Empty upperdir
# =============================================================================


@pytest.mark.asyncio
async def test_empty_upperdir_clears_staging(monkeypatch):
    row = {
        **_ACTIVE_ROW,
        "staged_epoch": 3,
        "staged_summary": {"counts": {"added": 1, "modified": 0, "deleted": 0}, "signature": "deadbeef"},
    }
    db = _make_db(thread_metadata=_PROTECTED_METADATA, mount_row=row)
    svc = _make_snapshot_service()
    monkeypatch.setattr(
        stage, "_run_ssh_capture", AsyncMock(return_value=(stage._EMPTY_SIGNATURE + "\n").encode())
    )

    result = await stage.stage_thread_cloud_diff(
        thread_id="thread-1", postgres_db=db, snapshot_service=svc
    )

    assert result == {"skipped": "empty"}
    assert svc.delete_blob.await_count == 2
    deleted_keys = {c.args[0] for c in svc.delete_blob.await_args_list}
    assert deleted_keys == {
        stage.staging_tar_key("thread-1"),
        stage.staging_manifest_key("thread-1"),
    }
    db.update_ro_mount_staging.assert_awaited_once_with(
        "mount-1", staged_epoch=4, staged_summary=None
    )


@pytest.mark.asyncio
async def test_empty_upperdir_when_nothing_staged_is_pure_noop(monkeypatch):
    row = {**_ACTIVE_ROW, "staged_summary": None}
    db = _make_db(thread_metadata=_PROTECTED_METADATA, mount_row=row)
    svc = _make_snapshot_service()
    monkeypatch.setattr(
        stage, "_run_ssh_capture", AsyncMock(return_value=(stage._EMPTY_SIGNATURE + "\n").encode())
    )

    result = await stage.stage_thread_cloud_diff(
        thread_id="thread-1", postgres_db=db, snapshot_service=svc
    )

    assert result == {"skipped": "empty"}
    svc.delete_blob.assert_not_called()
    db.update_ro_mount_staging.assert_not_called()


# =============================================================================
# Push path
# =============================================================================


@pytest.mark.asyncio
async def test_push_derives_manifest_uploads_and_bumps_epoch(tmp_path, monkeypatch):
    real_tar = _build_tar(tmp_path, [
        ("upper/new.txt", "file", b"hello", None),
    ])
    row = {**_ACTIVE_ROW, "staged_epoch": 5, "staged_summary": None, "etag_baseline": {}}
    db = _make_db(thread_metadata=_PROTECTED_METADATA, mount_row=row)
    svc = _make_snapshot_service()

    signature = "freshsig123"
    monkeypatch.setattr(
        stage, "_run_ssh_capture", AsyncMock(return_value=(signature + "\n").encode())
    )

    seen_cmds = []

    async def _fake_stream(cmd, dest_path):
        seen_cmds.append(cmd)
        shutil.copyfile(real_tar, dest_path)
        return True

    monkeypatch.setattr(stage, "_stream_tar_to_file", _fake_stream)

    uploaded = {}

    async def _fake_upload(key, local_path):
        with open(local_path, "rb") as f:
            uploaded[key] = f.read()
        return True

    svc.upload_blob_file = AsyncMock(side_effect=_fake_upload)

    result = await stage.stage_thread_cloud_diff(
        thread_id="thread-1", postgres_db=db, snapshot_service=svc
    )

    assert result == {"epoch": 6, "counts": {"added": 1, "modified": 0, "deleted": 0}}

    tar_key = stage.staging_tar_key("thread-1")
    manifest_key = stage.staging_manifest_key("thread-1")
    assert tar_key in uploaded
    assert manifest_key in uploaded
    assert uploaded[tar_key]  # non-empty tar bytes made it through

    manifest = json.loads(uploaded[manifest_key])
    assert manifest["epoch"] == 6
    assert manifest["signature"] == signature
    assert manifest["counts"] == {"added": 1, "modified": 0, "deleted": 0}
    assert manifest["entries"] == [
        {"path": "new.txt", "status": "added", "size": 5, "binary": False}
    ]
    assert manifest["skipped"] == []

    # Content binding (multi-replica torn-pair defense): the manifest must
    # carry the sha256 of the EXACT tar bytes that were uploaded, so readers
    # can detect a manifest/tar pair interleaved by two concurrent stagings.
    expected_sha = hashlib.sha256(uploaded[tar_key]).hexdigest()
    assert manifest["tar_sha256"] == expected_sha

    svc.save_blob.assert_not_called()  # manifest goes through upload_blob_file, not save_blob

    db.update_ro_mount_staging.assert_awaited_once_with(
        "mount-1",
        staged_epoch=6,
        staged_summary={
            "counts": {"added": 1, "modified": 0, "deleted": 0},
            "signature": signature,
            "tar_sha256": expected_sha,
        },
    )
    # staged_summary carries counts+signature+tar_sha256, never entries
    _, kwargs = db.update_ro_mount_staging.await_args
    assert "entries" not in kwargs["staged_summary"]

    # ssh host/port from thread metadata reached the tar command
    assert seen_cmds[0][-2] == "agent-host@10.0.0.5"


# =============================================================================
# Debounce
# =============================================================================


@pytest.mark.asyncio
async def test_inflight_debounce():
    db = _make_db(thread_metadata=_PROTECTED_METADATA, mount_row=dict(_ACTIVE_ROW))
    svc = _make_snapshot_service()
    stage._inflight.add("thread-1")

    result = await stage.stage_thread_cloud_diff(
        thread_id="thread-1", postgres_db=db, snapshot_service=svc
    )

    assert result == {"skipped": "in_flight"}
    db.get_thread.assert_not_called()


# =============================================================================
# SnapshotService.upload_blob_file / delete_blob (direct boto3-idiom coverage —
# same pattern as tests/test_citation_snapshot_blob.py::_make_service, since
# test_stage.py's other cases only exercise these two through an AsyncMock)
# =============================================================================


def _make_snapshot_service_instance():
    from orchestrator.services.snapshot_service import SnapshotService

    svc = SnapshotService()
    svc._s3 = MagicMock()
    svc._bucket = "srw-snapshots"
    svc._available = True
    return svc


@pytest.mark.asyncio
async def test_upload_blob_file_calls_boto3_upload_file(tmp_path):
    svc = _make_snapshot_service_instance()
    local = tmp_path / "upper.tar"
    local.write_bytes(b"tar bytes")

    ok = await svc.upload_blob_file("cloud-staging/t1/upper.tar", str(local))

    assert ok is True
    svc._s3.upload_file.assert_called_once_with(
        str(local), "srw-snapshots", "cloud-staging/t1/upper.tar"
    )


@pytest.mark.asyncio
async def test_upload_blob_file_unavailable_returns_false(tmp_path):
    svc = _make_snapshot_service_instance()
    svc._available = False
    local = tmp_path / "upper.tar"
    local.write_bytes(b"x")

    assert await svc.upload_blob_file("k", str(local)) is False
    svc._s3.upload_file.assert_not_called()


@pytest.mark.asyncio
async def test_upload_blob_file_exception_returns_false(tmp_path):
    svc = _make_snapshot_service_instance()
    svc._s3.upload_file.side_effect = Exception("boom")
    local = tmp_path / "upper.tar"
    local.write_bytes(b"x")

    assert await svc.upload_blob_file("k", str(local)) is False


@pytest.mark.asyncio
async def test_delete_blob_calls_boto3_delete_object():
    svc = _make_snapshot_service_instance()

    ok = await svc.delete_blob("cloud-staging/t1/manifest.json")

    assert ok is True
    svc._s3.delete_object.assert_called_once_with(
        Bucket="srw-snapshots", Key="cloud-staging/t1/manifest.json"
    )


@pytest.mark.asyncio
async def test_delete_blob_unavailable_returns_false():
    svc = _make_snapshot_service_instance()
    svc._available = False

    assert await svc.delete_blob("k") is False
    svc._s3.delete_object.assert_not_called()


@pytest.mark.asyncio
async def test_delete_blob_exception_returns_false():
    svc = _make_snapshot_service_instance()
    svc._s3.delete_object.side_effect = Exception("boom")

    assert await svc.delete_blob("k") is False
