"""stage_thread_cloud_diff — turn-end staging pipeline (spec §5, Task 4).

Fake db/snapshot_service are MagicMock with AsyncMock methods; the two
subprocess seams (_run_ssh_capture / _stream_tar_to_file) are monkeypatched
per-test so nothing here actually shells out to ssh.
"""

import asyncio
import hashlib
import io
import json
import shutil
import tarfile
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.services.cloud_staging import stage
from orchestrator.services.cloud_staging.source_identity import (
    ProtectedMountSourceIdentity,
)


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

_SOURCE = ProtectedMountSourceIdentity(
    backend_instance_id="11111111-1111-4111-8111-111111111111",
    source_ref="22222222-2222-4222-8222-222222222222",
    target_path="projects/example",
    native_id="17",
    mountpoint="Example",
)

_ACTIVE_ROW = {
    "id": "mount-1",
    "status": "active",
    "staged_epoch": 3,
    "staged_summary": None,
    "etag_baseline": {},
    "engage_attempt": "33333333-3333-4333-8333-333333333333",
    "source_binding": _SOURCE.binding,
    "source_binding_sha256": _SOURCE.sha256,
}


def _make_db(*, thread_metadata=None, mount_row=None):
    db = MagicMock()
    thread = {
        "id": "thread-1",
        "metadata": thread_metadata if thread_metadata is not None else {},
    }
    db.get_thread = AsyncMock(return_value=thread)
    if mount_row is not None:
        mount_row = dict(mount_row)
        mount_row.setdefault("engage_attempt", _ACTIVE_ROW["engage_attempt"])
        mount_row.setdefault("source_binding", _SOURCE.binding)
        mount_row.setdefault("source_binding_sha256", _SOURCE.sha256)
        summary = mount_row.get("staged_summary")
        if isinstance(summary, dict):
            summary = dict(summary)
            summary.setdefault("source_binding", _SOURCE.binding)
            summary.setdefault("source_binding_sha256", _SOURCE.sha256)
            mount_row["staged_summary"] = summary
    db.get_ro_mount_by_thread = AsyncMock(return_value=mount_row)
    db.update_ro_mount_staging = AsyncMock(return_value=True)
    return db


def _make_snapshot_service():
    svc = MagicMock()
    svc.save_blob = AsyncMock(return_value="some-key")
    svc.upload_blob_file = AsyncMock(return_value=True)
    svc.delete_blob = AsyncMock(return_value=True)
    # Default: the manifest blob exists (used by the "unchanged" skip's
    # existence check, I5) — individual tests override to simulate a
    # missing blob.
    svc.get_blob = AsyncMock(return_value=b"{}")
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


class _LiveSshProcess:
    def __init__(self):
        self.returncode = None
        self.stdout = MagicMock()
        self.stdout.read = AsyncMock(return_value=b"")
        self.stderr = MagicMock()
        self.stderr.read = AsyncMock(return_value=b"")
        self.terminated = asyncio.Event()
        self.terminate = MagicMock(side_effect=self._terminate)
        self.kill = MagicMock(side_effect=self._kill)
        self.wait = AsyncMock(side_effect=self._wait)

    def _terminate(self):
        self.returncode = -15
        self.terminated.set()

    def _kill(self):
        self.returncode = -9
        self.terminated.set()

    async def _wait(self):
        if self.returncode is None:
            await self.terminated.wait()
        return self.returncode


@pytest.mark.asyncio
async def test_capture_cancellation_racing_spawn_still_owns_and_reaps_ssh(
    monkeypatch,
):
    process = _LiveSshProcess()
    spawn_started = asyncio.Event()
    release_spawn = asyncio.Event()

    async def delayed_spawn(*_args, **_kwargs):
        # Model create_subprocess_exec having already forked the child while
        # the coroutine has not yet returned ownership to the lease task.
        spawn_started.set()
        await release_spawn.wait()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_spawn)

    task = asyncio.create_task(stage._run_ssh_capture(["ssh"], timeout=60))
    await spawn_started.wait()
    task.cancel()
    release_spawn.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    process.terminate.assert_called_once()
    assert process.returncode is not None
    assert process.wait.await_count >= 1


@pytest.mark.asyncio
async def test_capture_lease_loss_cancellation_terminates_and_reaps_ssh(monkeypatch):
    process = _LiveSshProcess()
    started = asyncio.Event()

    reads = 0

    async def read(_size):
        nonlocal reads
        reads += 1
        if reads == 1:
            started.set()
            await process.terminated.wait()
            return b"late bytes"
        return b""

    process.stdout.read = AsyncMock(side_effect=read)
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        AsyncMock(return_value=process),
    )

    task = asyncio.create_task(stage._run_ssh_capture(["ssh"], timeout=60))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    process.terminate.assert_called_once()
    assert process.returncode is not None
    assert process.wait.await_count >= 1
    reads_after_return = reads
    await asyncio.sleep(0)
    assert reads == reads_after_return


@pytest.mark.asyncio
async def test_tar_lease_loss_cancellation_reaps_ssh_and_removes_partial_file(
    monkeypatch, tmp_path
):
    process = _LiveSshProcess()
    process.terminate = MagicMock()
    blocked = asyncio.Event()
    reads = 0

    async def read(_size):
        nonlocal reads
        reads += 1
        if reads == 1:
            return b"partial"
        blocked.set()
        await process.terminated.wait()
        return b"late bytes"

    process.stdout = MagicMock()
    process.stdout.read = AsyncMock(side_effect=read)
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        AsyncMock(return_value=process),
    )
    destination = tmp_path / "partial.tar"

    task = asyncio.create_task(stage._stream_tar_to_file(["ssh"], str(destination)))
    await blocked.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    process.terminate.assert_called_once()
    process.kill.assert_called_once()
    assert process.returncode is not None
    assert process.wait.await_count >= 1
    assert not destination.exists()
    reads_after_return = reads
    await asyncio.sleep(0)
    assert reads == reads_after_return


@pytest.mark.asyncio
async def test_nonzero_tar_with_partial_stdout_is_refused_and_removed(
    monkeypatch, tmp_path
):
    process = _LiveSshProcess()
    process.returncode = 1
    process.stdout = MagicMock()
    process.stdout.read = AsyncMock(side_effect=[b"truncated", b""])
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        AsyncMock(return_value=process),
    )
    destination = tmp_path / "truncated.tar"

    assert not await stage._stream_tar_to_file(["ssh"], str(destination))
    assert not destination.exists()


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
    monkeypatch.setattr(
        stage, "_run_ssh_capture", AsyncMock(return_value=b"deadbeef\n")
    )
    stream_mock = AsyncMock()
    monkeypatch.setattr(stage, "_stream_tar_to_file", stream_mock)

    result = await stage.stage_thread_cloud_diff(
        thread_id="thread-1", postgres_db=db, snapshot_service=svc
    )

    assert result == {"skipped": "unchanged"}
    stream_mock.assert_not_called()
    svc.upload_blob_file.assert_not_called()
    db.update_ro_mount_staging.assert_not_called()
    # The skip is conditional on the manifest blob actually existing (I5) —
    # the default fixture returns a truthy blob, so the existence check
    # passed and the skip fired.
    svc.get_blob.assert_awaited_once_with(stage.staging_manifest_key("thread-1"))


@pytest.mark.asyncio
async def test_stage_unchanged_skip_restages_when_blobs_missing(tmp_path, monkeypatch):
    """Same unchanged signature, but the manifest blob is gone (e.g. an
    out-of-band deletion) — the skip must not be trusted; stage falls
    through to a full re-stage instead of leaving the review/apply path
    reading "staged" against nothing.
    """
    real_tar = _build_tar(
        tmp_path,
        [
            ("upper/new.txt", "file", b"hello", None),
        ],
    )
    row = {
        **_ACTIVE_ROW,
        "staged_epoch": 5,
        "staged_summary": {
            "counts": {"added": 1, "modified": 0, "deleted": 0},
            "signature": "deadbeef",
        },
        "etag_baseline": {},
    }
    db = _make_db(thread_metadata=_PROTECTED_METADATA, mount_row=row)
    svc = _make_snapshot_service()
    svc.get_blob = AsyncMock(return_value=None)  # manifest blob missing
    monkeypatch.setattr(
        stage, "_run_ssh_capture", AsyncMock(return_value=b"deadbeef\n")
    )

    async def _fake_stream(cmd, dest_path):
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

    # Did NOT skip — a full re-stage ran instead.
    assert result == {"epoch": 6, "counts": {"added": 1, "modified": 0, "deleted": 0}}
    assert stage.staging_tar_key("thread-1") in uploaded
    assert stage.staging_manifest_key("thread-1") in uploaded
    db.update_ro_mount_staging.assert_awaited_once_with(
        "mount-1",
        staged_epoch=6,
        staged_summary={
            "counts": {"added": 1, "modified": 0, "deleted": 0},
            "signature": "deadbeef",
            "tar_sha256": hashlib.sha256(
                uploaded[stage.staging_tar_key("thread-1")]
            ).hexdigest(),
            "source_binding": _SOURCE.binding,
            "source_binding_sha256": _SOURCE.sha256,
        },
        expected_engage_attempt=_ACTIVE_ROW["engage_attempt"],
        expected_source_binding_sha256=_SOURCE.sha256,
    )


# =============================================================================
# Empty upperdir
# =============================================================================


@pytest.mark.asyncio
async def test_empty_upperdir_clears_staging(monkeypatch):
    row = {
        **_ACTIVE_ROW,
        "staged_epoch": 3,
        "staged_summary": {
            "counts": {"added": 1, "modified": 0, "deleted": 0},
            "signature": "deadbeef",
        },
    }
    db = _make_db(thread_metadata=_PROTECTED_METADATA, mount_row=row)
    svc = _make_snapshot_service()
    monkeypatch.setattr(
        stage,
        "_run_ssh_capture",
        AsyncMock(return_value=(stage._EMPTY_SIGNATURE + "\n").encode()),
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
        "mount-1",
        staged_epoch=4,
        staged_summary=None,
        expected_engage_attempt=_ACTIVE_ROW["engage_attempt"],
        expected_source_binding_sha256=_SOURCE.sha256,
    )


@pytest.mark.asyncio
async def test_empty_upperdir_when_nothing_staged_is_pure_noop(monkeypatch):
    row = {**_ACTIVE_ROW, "staged_summary": None}
    db = _make_db(thread_metadata=_PROTECTED_METADATA, mount_row=row)
    svc = _make_snapshot_service()
    monkeypatch.setattr(
        stage,
        "_run_ssh_capture",
        AsyncMock(return_value=(stage._EMPTY_SIGNATURE + "\n").encode()),
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
    real_tar = _build_tar(
        tmp_path,
        [
            ("upper/new.txt", "file", b"hello", None),
        ],
    )
    row = {
        **_ACTIVE_ROW,
        "staged_epoch": 5,
        "staged_summary": None,
        "etag_baseline": {},
    }
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
            "source_binding": _SOURCE.binding,
            "source_binding_sha256": _SOURCE.sha256,
        },
        expected_engage_attempt=_ACTIVE_ROW["engage_attempt"],
        expected_source_binding_sha256=_SOURCE.sha256,
    )
    # staged_summary carries counts+signature+tar_sha256, never entries
    _, kwargs = db.update_ro_mount_staging.await_args
    assert "entries" not in kwargs["staged_summary"]

    # ssh host/port from thread metadata reached the tar command
    assert seen_cmds[0][-2] == "agent-host@10.0.0.5"


@pytest.mark.asyncio
async def test_source_replacement_after_tar_upload_never_publishes_manifest_or_event(
    tmp_path, monkeypatch
):
    """A stale A stage may leave an unreachable A blob, never expose it as B."""

    runtime_generation = "44444444-4444-4444-8444-444444444444"
    attach_token = "55555555-5555-4555-8555-555555555555"
    agent_id = "66666666-6666-4666-8666-666666666666"
    workspace_generation = "77777777-7777-4777-8777-777777777777"
    workspace_runtime = "88888888-8888-4888-8888-888888888888"
    workspace = {
        "status": "ready",
        "pod_ip": "10.0.0.5",
        "port": 30022,
        "_runtime_incarnation": workspace_runtime,
        "_canvas_workspace_generation": workspace_generation,
    }
    binding = {
        "kind": "remote",
        "generation": workspace_generation,
        "ssh_host_key_fingerprint": "SHA256:exact-host-key",
    }
    row_a = {
        **_ACTIVE_ROW,
        "runtime_generation": runtime_generation,
        "staged_epoch": 5,
        "staged_summary": None,
    }
    source_b = ProtectedMountSourceIdentity(
        backend_instance_id="99999999-9999-4999-8999-999999999999",
        source_ref="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        target_path="projects/replacement",
        native_id="18",
        mountpoint="Replacement",
    )
    row_b = {
        **row_a,
        "engage_attempt": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        "source_binding": source_b.binding,
        "source_binding_sha256": source_b.sha256,
    }
    thread = {
        "id": "thread-1",
        "execution_lane": "pinned",
        "runtime_generation": runtime_generation,
        "runtime_retirement_token": None,
        "agent_id": agent_id,
        "runtime_attach_token": attach_token,
        "metadata": {
            "protected_cloud": True,
            "workspace_container": workspace,
            "_workspace_binding": binding,
        },
    }
    authority = {
        "runtime_generation": runtime_generation,
        "runtime_retirement_token": None,
        "agent_id": agent_id,
        "runtime_attach_token": attach_token,
        "workspace": workspace,
        "workspace_binding": binding,
        "workspace_generation": workspace_generation,
        "workspace_runtime_incarnation": workspace_runtime,
        "workspace_ssh_host_key_fingerprint": "SHA256:exact-host-key",
        "mount_row_id": "mount-1",
        "engage_attempt": row_a["engage_attempt"],
        "source_binding_sha256": _SOURCE.sha256,
        "expected_staged_epoch": 5,
    }
    db = MagicMock()
    db.get_thread = AsyncMock(return_value=thread)
    db.get_ro_mount_by_thread = AsyncMock(return_value=row_a)
    db.publish_ro_mount_staging_exact = AsyncMock(return_value={"published": True})
    svc = _make_snapshot_service()
    real_tar = _build_tar(
        tmp_path,
        [("upper/new.txt", "file", b"from-a", None)],
    )
    monkeypatch.setattr(
        stage,
        "_run_authorized_ssh_capture",
        AsyncMock(return_value=b"source-a-signature\n"),
    )

    async def _stream(_authority, **kwargs):
        shutil.copyfile(real_tar, kwargs["dest_path"])
        return True

    monkeypatch.setattr(stage, "_stream_authorized_tar", _stream)
    uploaded: list[str] = []

    async def _upload(key, _path):
        uploaded.append(key)
        if len(uploaded) == 1:
            # Selection A was replaced after A's immutable tar PUT but before
            # the manifest/publication visibility boundary.
            db.get_ro_mount_by_thread.return_value = row_b
        return True

    svc.upload_blob_file = AsyncMock(side_effect=_upload)

    result = await stage.stage_thread_cloud_diff(
        thread_id="thread-1",
        postgres_db=db,
        snapshot_service=svc,
        authority=authority,
    )

    assert result == {"skipped": "authority_changed"}
    assert len(uploaded) == 1
    assert f"/{_SOURCE.sha256}/" in uploaded[0]
    db.publish_ro_mount_staging_exact.assert_not_called()


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
