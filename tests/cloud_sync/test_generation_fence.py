"""Focused contracts for the stateless cloud-sync generation fence."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent.services.cloud_sync import base as cloud_sync_base
from agent.services.cloud_sync.base import (
    CloudSyncMarker,
    CloudSyncMarkerError,
)
from agent.services.cloud_sync.coordinator import (
    CloudSyncError,
    CloudSyncGenerationError,
    MountSync,
    WorkspaceSyncCoordinator,
)
from shared.cloud_sync_generations import (
    CloudSyncRequirement,
    encode_cloud_sync_baseline,
)
from tests._fs_backend import FilesystemTestBackend

from tests.cloud_sync._local_fs import LocalFsWorkspaceSync


THREAD_A = "11111111-1111-4111-8111-111111111111"
THREAD_B = "22222222-2222-4222-8222-222222222222"
WORKSPACE_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
WORKSPACE_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
SCOPE_A = "a" * 64
SCOPE_B = "b" * 64


def _requirement(
    *,
    mount_id: str = "mount-a",
    generation: int = 7,
    acknowledged: int = 0,
    workspace_generation: str = WORKSPACE_A,
    scope: str = SCOPE_A,
    baseline: dict[str, dict[str, str]] | None = None,
) -> CloudSyncRequirement:
    baseline, _encoded, baseline_sha256 = encode_cloud_sync_baseline(baseline or {})
    return CloudSyncRequirement(
        mount_id=mount_id,
        required_generation=generation,
        acknowledged_generation=acknowledged,
        required_lease_token=generation,
        workspace_generation=workspace_generation,
        sync_scope_sha256=scope,
        baseline_manifest=baseline,
        baseline_sha256=baseline_sha256,
    )


def _coordinator(
    sync: LocalFsWorkspaceSync,
    *,
    thread_id: str = THREAD_A,
    mount_id: str = "mount-a",
    workspace_generation: str = WORKSPACE_A,
    scope: str = SCOPE_A,
) -> WorkspaceSyncCoordinator:
    return WorkspaceSyncCoordinator(
        [
            MountSync(
                mount_id=mount_id,
                target_path="",
                sync=sync,
                sync_scope_sha256=scope,
            )
        ],
        thread_id=thread_id,
        workspace_generation=workspace_generation,
    )


async def _lease_current() -> None:
    return None


@pytest.mark.asyncio
async def test_resource_markers_are_namespaced_for_threads_sharing_root(
    tmp_path: Path,
):
    remote = tmp_path / "shared-remote"
    ws_a = tmp_path / "ws-a"
    ws_b = tmp_path / "ws-b"
    ws_a.mkdir()
    ws_b.mkdir()
    sync_a = LocalFsWorkspaceSync(ws_a, remote_root=remote)
    sync_b = LocalFsWorkspaceSync(ws_b, remote_root=remote)
    marker_a = CloudSyncMarker(
        thread_id=THREAD_A,
        mount_id="shared",
        generation=3,
        lease_token=3,
        workspace_generation=WORKSPACE_A,
        sync_scope_sha256=SCOPE_A,
    )
    marker_b = CloudSyncMarker(
        thread_id=THREAD_B,
        mount_id="shared",
        generation=9,
        lease_token=9,
        workspace_generation=WORKSPACE_B,
        # Deliberately the same scope digest: thread identity must still
        # provide a separate marker namespace on a shared cloud root.
        sync_scope_sha256=SCOPE_A,
    )

    await sync_a.write_sync_generation_marker(marker_a)
    await sync_b.write_sync_generation_marker(marker_b)

    marker_files = list((remote / ".srw" / "sync-generations").glob("*.json"))
    assert len(marker_files) == 2
    assert (
        await sync_a.read_sync_generation_marker(
            thread_id=THREAD_A, sync_scope_sha256=SCOPE_A
        )
        == marker_a
    )
    assert (
        await sync_b.read_sync_generation_marker(
            thread_id=THREAD_B, sync_scope_sha256=SCOPE_A
        )
        == marker_b
    )


@pytest.mark.asyncio
async def test_marker_rejects_boolean_generation(tmp_path: Path):
    remote = tmp_path / "remote"
    ws = tmp_path / "ws"
    ws.mkdir()
    sync = LocalFsWorkspaceSync(ws, remote_root=remote)
    marker = CloudSyncMarker(
        thread_id=THREAD_A,
        mount_id="mount-a",
        generation=7,
        lease_token=7,
        workspace_generation=WORKSPACE_A,
        sync_scope_sha256=SCOPE_A,
    )
    await sync.write_sync_generation_marker(marker)
    marker_file = next((remote / ".srw" / "sync-generations").glob("*.json"))
    marker_file.write_text(
        marker_file.read_text().replace('"generation":7', '"generation":true')
    )

    with pytest.raises(CloudSyncMarkerError, match="positive integer"):
        await sync.read_sync_generation_marker(
            thread_id=THREAD_A, sync_scope_sha256=SCOPE_A
        )


@pytest.mark.asyncio
async def test_user_writable_marker_cannot_authorize_ignored_path_deletion(
    tmp_path: Path,
):
    remote = tmp_path / "remote"
    ws = tmp_path / "ws"
    (ws / ".git").mkdir(parents=True)
    protected = ws / ".git" / "config"
    protected.write_text("must-survive")
    sync = LocalFsWorkspaceSync(
        tmp_path / "unused-local-path",
        remote_root=remote,
        workspace_backend=FilesystemTestBackend(ws),
    )
    await sync.write_sync_generation_marker(
        CloudSyncMarker(
            thread_id=THREAD_A,
            mount_id="mount-a",
            generation=7,
            lease_token=7,
            workspace_generation=WORKSPACE_A,
            sync_scope_sha256=SCOPE_A,
        )
    )
    marker_file = next((remote / ".srw" / "sync-generations").glob("*.json"))
    raw = json.loads(marker_file.read_text())
    forged, _encoded, digest = encode_cloud_sync_baseline(
        {".git/config": {"sha256": "1" * 64, "remote_etag": ""}}
    )
    raw["committed_manifest"] = forged
    raw["committed_manifest_sha256"] = digest
    marker_file.write_text(json.dumps(raw))

    with pytest.raises(CloudSyncError):
        await _coordinator(sync).reconcile_before_pull(
            {"mount-a": _requirement()},
            before_write=_lease_current,
            acknowledge=AsyncMock(),
        )

    assert protected.read_text() == "must-survive"


@pytest.mark.asyncio
async def test_committed_manifest_digest_tamper_fails_closed(tmp_path: Path):
    remote = tmp_path / "remote"
    ws = tmp_path / "ws"
    ws.mkdir()
    sync = LocalFsWorkspaceSync(ws, remote_root=remote)
    manifest, _encoded, digest = encode_cloud_sync_baseline(
        {"result.txt": {"sha256": "2" * 64, "remote_etag": "etag"}}
    )
    await sync.write_sync_generation_marker(
        CloudSyncMarker(
            thread_id=THREAD_A,
            mount_id="mount-a",
            generation=7,
            lease_token=7,
            workspace_generation=WORKSPACE_A,
            sync_scope_sha256=SCOPE_A,
            committed_manifest=manifest,
            committed_manifest_sha256=digest,
        )
    )
    marker_file = next((remote / ".srw" / "sync-generations").glob("*.json"))
    raw = json.loads(marker_file.read_text())
    raw["committed_manifest"]["result.txt"]["sha256"] = "3" * 64
    marker_file.write_text(json.dumps(raw))

    with pytest.raises(CloudSyncMarkerError, match="digest mismatch"):
        await sync.read_sync_generation_marker(
            thread_id=THREAD_A, sync_scope_sha256=SCOPE_A
        )


@pytest.mark.asyncio
async def test_failed_remote_listing_cannot_apply_marker_membership_delete(
    tmp_path: Path,
):
    remote = tmp_path / "remote"
    ws = tmp_path / "ws"
    ws.mkdir()
    protected = ws / "remote-known.txt"
    protected.write_text("keep")
    sync = LocalFsWorkspaceSync(
        tmp_path / "unused-local-path",
        remote_root=remote,
        workspace_backend=FilesystemTestBackend(ws),
    )
    manifest, _encoded, _digest = encode_cloud_sync_baseline(
        {
            "remote-known.txt": {
                "sha256": hashlib.sha256(b"keep").hexdigest(),
                "remote_etag": "etag",
            }
        }
    )
    sync.install_generation_baseline(manifest)

    async def fail_listing():
        raise RuntimeError("listing failed")

    sync._list_remote_tree = fail_listing  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="listing failed"):
        await sync.pull(strict=True, force_unknown=True)

    assert protected.read_text() == "keep"


@pytest.mark.asyncio
async def test_reconcile_rejects_marker_with_wrong_exact_lease_token(tmp_path: Path):
    remote = tmp_path / "remote"
    ws = tmp_path / "ws"
    ws.mkdir()
    sync = LocalFsWorkspaceSync(ws, remote_root=remote)
    coordinator = _coordinator(sync)
    requirement = _requirement(acknowledged=7)
    await sync.write_sync_generation_marker(
        CloudSyncMarker(
            thread_id=THREAD_A,
            mount_id="mount-a",
            generation=7,
            lease_token=6,
            workspace_generation=WORKSPACE_A,
            sync_scope_sha256=SCOPE_A,
        )
    )
    acknowledgements: list[str] = []

    async def acknowledge(mount_id, _requirement):
        acknowledgements.append(mount_id)

    with pytest.raises(CloudSyncError) as exc_info:
        await coordinator.reconcile_before_pull(
            {"mount-a": requirement},
            before_write=_lease_current,
            acknowledge=acknowledge,
        )

    assert any(
        isinstance(error, CloudSyncMarkerError)
        for _mount_id, _path, error in exc_info.value.failures
    )
    assert acknowledgements == []


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["push", "recovery"])
async def test_generation_delta_uploads_same_length_local_edit(
    tmp_path: Path, operation: str
):
    remote = tmp_path / "remote"
    remote.mkdir()
    (remote / "same.txt").write_text("AAAA")
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "same.txt").write_text("AAAA")
    backend = FilesystemTestBackend(ws)
    sync = LocalFsWorkspaceSync(
        tmp_path / "unused-local-path",
        remote_root=remote,
        workspace_backend=backend,
    )
    # Capture the durable post-pull baseline exactly as turn start does. The
    # content hash, not size or process-local mtime, must detect this edit.
    await sync.pull(strict=True, force_unknown=True)
    baseline, _baseline_sha256 = await sync.capture_generation_baseline()
    (ws / "same.txt").write_text("BBBB")
    # Recovery deliberately uses a fresh coordinator/sync instance to prove
    # that no process-local dedup cache is part of the generation contract.
    if operation == "recovery":
        sync = LocalFsWorkspaceSync(
            tmp_path / "second-unused-local-path",
            remote_root=remote,
            workspace_backend=backend,
        )
    coordinator = _coordinator(sync)
    acknowledgements: list[str] = []

    async def acknowledge(mount_id, _requirement):
        acknowledgements.append(mount_id)

    requirements = {"mount-a": _requirement(baseline=baseline)}
    if operation == "push":
        await coordinator.push_generation(
            requirements,
            before_write=_lease_current,
            acknowledge=acknowledge,
        )
    else:
        await coordinator.reconcile_before_pull(
            requirements,
            before_write=_lease_current,
            acknowledge=acknowledge,
        )

    assert (remote / "same.txt").read_text() == "BBBB"
    assert acknowledgements == ["mount-a"]


@pytest.mark.asyncio
async def test_generation_staging_retries_short_writes_before_marker_and_ack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    remote = tmp_path / "remote"
    ws = tmp_path / "ws"
    ws.mkdir()
    payload = (b"generation-bytes-" * 257) + b"complete"
    (ws / "result.bin").write_bytes(payload)
    sync = LocalFsWorkspaceSync(
        tmp_path / "unused-local-path",
        remote_root=remote,
        workspace_backend=FilesystemTestBackend(ws),
    )
    coordinator = _coordinator(sync)
    acknowledgements: list[str] = []
    real_write = cloud_sync_base.os.write
    short_write_calls = 0

    def short_write(fd: int, content: bytes | memoryview) -> int:
        nonlocal short_write_calls
        short_write_calls += 1
        # Exercise both the file-content and JSON-marker staging loops. POSIX
        # permits a successful write to consume fewer bytes than requested.
        chunk_size = max(1, len(content) // 3)
        return real_write(fd, content[:chunk_size])

    monkeypatch.setattr(cloud_sync_base.os, "write", short_write)

    async def acknowledge(mount_id, _requirement):
        acknowledgements.append(mount_id)

    await coordinator.push_generation(
        {"mount-a": _requirement()},
        before_write=_lease_current,
        acknowledge=acknowledge,
    )

    assert short_write_calls > 2
    assert (remote / "result.bin").read_bytes() == payload
    marker = await sync.read_sync_generation_marker(
        thread_id=THREAD_A, sync_scope_sha256=SCOPE_A
    )
    assert marker is not None and marker.generation == 7
    assert acknowledgements == ["mount-a"]


@pytest.mark.parametrize("operation", ["push", "recovery"])
@pytest.mark.asyncio
async def test_generation_delta_preserves_concurrent_remote_edit_to_untouched_file(
    tmp_path: Path, operation: str
):
    remote = tmp_path / "remote"
    remote.mkdir()
    (remote / "agent-edit.txt").write_text("AAAA")
    (remote / "cloud-edit.txt").write_text("original-cloud")
    ws = tmp_path / "ws"
    ws.mkdir()
    backend = FilesystemTestBackend(ws)
    sync = LocalFsWorkspaceSync(
        tmp_path / "unused-local-path",
        remote_root=remote,
        workspace_backend=backend,
    )
    await sync.pull(strict=True, force_unknown=True)
    baseline, _baseline_sha256 = await sync.capture_generation_baseline()

    # The agent changes A while a user changes unrelated B directly in cloud.
    # Only A is in the durable delta; B must never be replayed from the stale
    # workspace copy, including after a crash and fresh-process recovery.
    (ws / "agent-edit.txt").write_text("BBBB")
    (remote / "cloud-edit.txt").write_text("concurrent-cloud")
    if operation == "recovery":
        sync = LocalFsWorkspaceSync(
            tmp_path / "second-unused-local-path",
            remote_root=remote,
            workspace_backend=backend,
        )
    coordinator = _coordinator(sync)
    acknowledgements: list[str] = []

    async def acknowledge(mount_id, _requirement):
        acknowledgements.append(mount_id)

    requirements = {"mount-a": _requirement(baseline=baseline)}
    if operation == "push":
        await coordinator.push_generation(
            requirements,
            before_write=_lease_current,
            acknowledge=acknowledge,
        )
    else:
        await coordinator.reconcile_before_pull(
            requirements,
            before_write=_lease_current,
            acknowledge=acknowledge,
        )

    assert (remote / "agent-edit.txt").read_text() == "BBBB"
    assert (remote / "cloud-edit.txt").read_text() == "concurrent-cloud"
    assert acknowledgements == ["mount-a"]
    marker = await sync.read_sync_generation_marker(
        thread_id=THREAD_A, sync_scope_sha256=SCOPE_A
    )
    assert marker is not None and marker.generation == 7


@pytest.mark.asyncio
async def test_empty_remote_etag_still_makes_unchanged_path_clean(tmp_path: Path):
    remote = tmp_path / "remote"
    remote.mkdir()
    (remote / "cloud-edit.txt").write_text("original-cloud")
    ws = tmp_path / "ws"
    ws.mkdir()
    sync = LocalFsWorkspaceSync(
        tmp_path / "unused-local-path",
        remote_root=remote,
        workspace_backend=FilesystemTestBackend(ws),
    )
    original_list = sync._list_remote_files

    async def list_without_etags(rel_dir: str = "") -> list[dict]:
        return [{**item, "etag": ""} for item in await original_list(rel_dir)]

    sync._list_remote_files = list_without_etags  # type: ignore[method-assign]
    await sync.pull(strict=True, force_unknown=True)
    baseline, _digest = await sync.capture_generation_baseline()
    assert baseline["cloud-edit.txt"]["remote_etag"] == ""

    # The agent did not touch the file. A WebDAV server omitting ETags must not
    # make the path look local-only and let the generation push overwrite this
    # unrelated concurrent cloud edit.
    (remote / "cloud-edit.txt").write_text("concurrent-cloud")
    await _coordinator(sync).push_generation(
        {"mount-a": _requirement(baseline=baseline)},
        before_write=_lease_current,
        acknowledge=AsyncMock(),
    )

    assert (remote / "cloud-edit.txt").read_text() == "concurrent-cloud"


@pytest.mark.asyncio
async def test_force_unknown_pull_fetches_same_size_remote_edit_cross_instance(
    tmp_path: Path,
):
    remote = tmp_path / "remote"
    remote.mkdir()
    (remote / "same.txt").write_text("AAAA")
    ws = tmp_path / "ws"
    ws.mkdir()
    backend = FilesystemTestBackend(ws)

    first = LocalFsWorkspaceSync(
        tmp_path / "first-unused-local-path",
        remote_root=remote,
        workspace_backend=backend,
    )
    await first.pull(strict=True, force_unknown=True)
    assert (ws / "same.txt").read_text() == "AAAA"

    # A fresh claimant has no ETag cache. Equal byte length must not be treated
    # as proof that the durable workspace already holds the new cloud bytes.
    (remote / "same.txt").write_text("BBBB")
    second = LocalFsWorkspaceSync(
        tmp_path / "second-unused-local-path",
        remote_root=remote,
        workspace_backend=backend,
    )
    pulled = await second.pull(strict=True, force_unknown=True)

    assert pulled == ["same.txt"]
    assert (ws / "same.txt").read_text() == "BBBB"


@pytest.mark.asyncio
async def test_staged_pull_noop_move_fails_before_sync_state_advances(tmp_path: Path):
    remote = tmp_path / "remote"
    remote.mkdir()
    (remote / "result.txt").write_text("remote-bytes")
    ws = tmp_path / "ws"
    ws.mkdir()
    backend = FilesystemTestBackend(ws)
    backend.move = lambda _src, _dst: None  # type: ignore[method-assign]
    sync = LocalFsWorkspaceSync(
        tmp_path / "unused-local-path",
        remote_root=remote,
        workspace_backend=backend,
    )

    with pytest.raises(FileNotFoundError):
        await sync.pull(strict=True, force_unknown=True)

    assert not (ws / "result.txt").exists()
    assert "result.txt" not in sync._remote_state


@pytest.mark.asyncio
async def test_staged_pull_rejects_existing_directory_target_without_residue(
    tmp_path: Path,
):
    remote = tmp_path / "remote"
    remote.mkdir()
    (remote / "collision").write_text("remote-file")
    ws = tmp_path / "ws"
    (ws / "collision").mkdir(parents=True)
    sync = LocalFsWorkspaceSync(
        tmp_path / "unused-local-path",
        remote_root=remote,
        workspace_backend=FilesystemTestBackend(ws),
    )

    with pytest.raises(CloudSyncMarkerError, match="target is a directory"):
        await sync.pull(strict=True, force_unknown=True)

    assert (ws / "collision").is_dir()
    assert list((ws / "collision").iterdir()) == []
    assert "collision" not in sync._remote_state


@pytest.mark.asyncio
async def test_exact_marker_membership_applies_remote_delete_without_resurrection(
    tmp_path: Path,
):
    remote = tmp_path / "remote"
    remote.mkdir()
    (remote / "deleted-in-cloud.txt").write_text("committed")
    ws = tmp_path / "ws"
    ws.mkdir()
    backend = FilesystemTestBackend(ws)
    first = LocalFsWorkspaceSync(
        tmp_path / "first-unused-local-path",
        remote_root=remote,
        workspace_backend=backend,
    )
    await first.pull(strict=True, force_unknown=True)
    baseline, _baseline_sha256 = await first.capture_generation_baseline()
    requirement = _requirement(baseline=baseline)
    await _coordinator(first).push_generation(
        {"mount-a": requirement},
        before_write=_lease_current,
        acknowledge=AsyncMock(),
    )
    marker = await first.read_sync_generation_marker(
        thread_id=THREAD_A, sync_scope_sha256=SCOPE_A
    )
    assert marker is not None
    assert set(marker.committed_manifest) == {"deleted-in-cloud.txt"}

    # The cloud is canonical at the next turn boundary. A fresh claimant gets
    # the predecessor's exact post-commit membership from the marker, so a
    # missing remote file is an attested tombstone rather than a local-only
    # file that the next generation would accidentally resurrect.
    (remote / "deleted-in-cloud.txt").unlink()
    second = LocalFsWorkspaceSync(
        tmp_path / "second-unused-local-path",
        remote_root=remote,
        workspace_backend=backend,
    )
    coordinator = _coordinator(second)
    await coordinator.reconcile_before_pull(
        {"mount-a": requirement},
        before_write=_lease_current,
        acknowledge=AsyncMock(),
    )
    changed = await second.pull(
        strict=True,
        before_write=_lease_current,
        force_unknown=True,
    )
    assert changed == ["deleted-in-cloud.txt"]
    assert not (ws / "deleted-in-cloud.txt").exists()

    next_baseline, _next_digest = await second.capture_generation_baseline()
    assert next_baseline == {}
    await coordinator.push_generation(
        {"mount-a": _requirement(generation=8, baseline=next_baseline)},
        before_write=_lease_current,
        acknowledge=AsyncMock(),
    )
    assert not (remote / "deleted-in-cloud.txt").exists()


@pytest.mark.parametrize("remote_preexists", [True, False])
@pytest.mark.asyncio
async def test_post_marker_workspace_write_becomes_next_delta_not_false_ack(
    tmp_path: Path,
    remote_preexists: bool,
):
    remote = tmp_path / "remote"
    remote.mkdir()
    ws = tmp_path / "ws"
    ws.mkdir()
    backend = FilesystemTestBackend(ws)
    first = LocalFsWorkspaceSync(
        tmp_path / "first-unused-local-path",
        remote_root=remote,
        workspace_backend=backend,
    )
    if remote_preexists:
        (remote / "idle-writer.txt").write_text("AAAA")
        await first.pull(strict=True, force_unknown=True)
        baseline, _digest = await first.capture_generation_baseline()
    else:
        (ws / "idle-writer.txt").write_text("AAAA")
        baseline = {}
    requirement = _requirement(baseline=baseline)
    await _coordinator(first).push_generation(
        {"mount-a": requirement},
        before_write=_lease_current,
        acknowledge=AsyncMock(),
    )

    # A resident/background workspace process writes after the turn marker.
    # The successor must not call those bytes clean merely because the remote
    # ETag still matches the marker. It preserves the local divergence while
    # keeping the marker's remote hash as the next armed baseline.
    (ws / "idle-writer.txt").write_text("BBBB")
    second = LocalFsWorkspaceSync(
        tmp_path / "second-unused-local-path",
        remote_root=remote,
        workspace_backend=backend,
    )
    coordinator = _coordinator(second)
    await coordinator.reconcile_before_pull(
        {"mount-a": requirement},
        before_write=_lease_current,
        acknowledge=AsyncMock(),
    )
    pulled = await second.pull(
        strict=True,
        before_write=_lease_current,
        force_unknown=True,
    )
    assert pulled == []
    assert (ws / "idle-writer.txt").read_text() == "BBBB"

    next_baseline, _next_digest = await second.capture_generation_baseline()
    assert (
        next_baseline["idle-writer.txt"]["sha256"]
        == hashlib.sha256(b"AAAA").hexdigest()
    )
    next_requirement = _requirement(generation=8, baseline=next_baseline)
    await coordinator.push_generation(
        {"mount-a": next_requirement},
        before_write=_lease_current,
        acknowledge=AsyncMock(),
    )

    assert (remote / "idle-writer.txt").read_text() == "BBBB"
    marker = await second.read_sync_generation_marker(
        thread_id=THREAD_A, sync_scope_sha256=SCOPE_A
    )
    assert marker is not None
    assert (
        marker.committed_manifest["idle-writer.txt"]["sha256"]
        == hashlib.sha256(b"BBBB").hexdigest()
    )


@pytest.mark.asyncio
async def test_post_marker_workspace_delete_becomes_fenced_remote_delete(
    tmp_path: Path,
):
    remote = tmp_path / "remote"
    remote.mkdir()
    (remote / "idle-delete.txt").write_text("AAAA")
    ws = tmp_path / "ws"
    ws.mkdir()
    backend = FilesystemTestBackend(ws)
    first = LocalFsWorkspaceSync(
        tmp_path / "first-unused-local-path",
        remote_root=remote,
        workspace_backend=backend,
    )
    await first.pull(strict=True, force_unknown=True)
    baseline, _digest = await first.capture_generation_baseline()
    requirement = _requirement(baseline=baseline)
    await _coordinator(first).push_generation(
        {"mount-a": requirement},
        before_write=_lease_current,
        acknowledge=AsyncMock(),
    )

    (ws / "idle-delete.txt").unlink()
    second = LocalFsWorkspaceSync(
        tmp_path / "second-unused-local-path",
        remote_root=remote,
        workspace_backend=backend,
    )
    coordinator = _coordinator(second)
    await coordinator.reconcile_before_pull(
        {"mount-a": requirement},
        before_write=_lease_current,
        acknowledge=AsyncMock(),
    )
    assert (
        await second.pull(
            strict=True,
            before_write=_lease_current,
            force_unknown=True,
        )
        == []
    )
    assert not (ws / "idle-delete.txt").exists()

    # Capture retains the marker-proven remote path even though the local file
    # is absent, allowing the next generation delta to issue a real DELETE.
    next_baseline, _next_digest = await second.capture_generation_baseline()
    assert set(next_baseline) == {"idle-delete.txt"}
    await coordinator.push_generation(
        {"mount-a": _requirement(generation=8, baseline=next_baseline)},
        before_write=_lease_current,
        acknowledge=AsyncMock(),
    )
    assert not (remote / "idle-delete.txt").exists()
    marker = await second.read_sync_generation_marker(
        thread_id=THREAD_A, sync_scope_sha256=SCOPE_A
    )
    assert marker is not None and marker.committed_manifest == {}


@pytest.mark.asyncio
async def test_forced_pull_without_marker_membership_never_deletes_local_only_file(
    tmp_path: Path,
):
    remote = tmp_path / "remote"
    ws = tmp_path / "ws"
    ws.mkdir()
    local_only = ws / "local-only.txt"
    local_only.write_text("keep")
    sync = LocalFsWorkspaceSync(
        tmp_path / "unused-local-path",
        remote_root=remote,
        workspace_backend=FilesystemTestBackend(ws),
    )

    assert await sync.pull(strict=True, force_unknown=True) == []
    assert local_only.read_text() == "keep"


@pytest.mark.asyncio
async def test_exact_marker_with_lagging_db_ack_is_ack_only(tmp_path: Path):
    remote = tmp_path / "remote"
    remote.mkdir()
    (remote / "data.txt").write_text("baseline")
    ws = tmp_path / "ws"
    ws.mkdir()
    backend = FilesystemTestBackend(ws)
    sync = LocalFsWorkspaceSync(
        tmp_path / "unused-local-path",
        remote_root=remote,
        workspace_backend=backend,
    )
    await sync.pull(strict=True, force_unknown=True)
    baseline, baseline_sha256 = await sync.capture_generation_baseline()
    requirement = _requirement(baseline=baseline)
    assert requirement.baseline_sha256 == baseline_sha256
    await sync.write_sync_generation_marker(
        CloudSyncMarker(
            thread_id=THREAD_A,
            mount_id="mount-a",
            generation=7,
            lease_token=7,
            workspace_generation=WORKSPACE_A,
            sync_scope_sha256=SCOPE_A,
            baseline_sha256=baseline_sha256,
        )
    )

    # If the marker committed but the DB ACK response was lost, retrying the
    # delta could overwrite cloud edits that arrived after the marker.
    (ws / "data.txt").write_text("stale-local-after-commit")
    (remote / "data.txt").write_text("new-cloud-after-commit")
    sync.push_generation_delta = AsyncMock(  # type: ignore[method-assign]
        side_effect=AssertionError("exact marker must not replay generation bytes")
    )
    before_write = AsyncMock()
    acknowledgements: list[str] = []

    async def acknowledge(mount_id, _requirement):
        acknowledgements.append(mount_id)

    result = await _coordinator(sync).reconcile_before_pull(
        {"mount-a": requirement},
        before_write=before_write,
        acknowledge=acknowledge,
    )

    assert result == {"mount-a": []}
    assert acknowledgements == ["mount-a"]
    before_write.assert_not_awaited()
    sync.push_generation_delta.assert_not_awaited()
    assert (remote / "data.txt").read_text() == "new-cloud-after-commit"


@pytest.mark.asyncio
async def test_partial_multi_mount_retry_skips_already_marked_mount(tmp_path: Path):
    workspace_a = tmp_path / "workspace-a"
    workspace_b = tmp_path / "workspace-b"
    workspace_a.mkdir()
    workspace_b.mkdir()
    remote_a = tmp_path / "remote-a"
    remote_b = tmp_path / "remote-b"
    remote_a.mkdir()
    remote_b.mkdir()
    (remote_a / "a.txt").write_text("a-old")
    (remote_b / "b.txt").write_text("b-old")
    sync_a = LocalFsWorkspaceSync(
        tmp_path / "unused-a",
        remote_root=remote_a,
        workspace_backend=FilesystemTestBackend(workspace_a),
    )
    sync_b = LocalFsWorkspaceSync(
        tmp_path / "unused-b",
        remote_root=remote_b,
        workspace_backend=FilesystemTestBackend(workspace_b),
    )
    await sync_a.pull(strict=True, force_unknown=True)
    await sync_b.pull(strict=True, force_unknown=True)
    baseline_a, digest_a = await sync_a.capture_generation_baseline()
    baseline_b, _digest_b = await sync_b.capture_generation_baseline()
    requirement_a = _requirement(
        mount_id="logical-a", scope=SCOPE_A, baseline=baseline_a
    )
    requirement_b = _requirement(
        mount_id="logical-b", scope=SCOPE_B, baseline=baseline_b
    )

    # Mount A reached bytes+marker before a mount-B failure killed the aggregate
    # operation. Its DB ACK was lost. Recovery must ACK A without touching its
    # bytes, while still repairing B.
    (remote_a / "a.txt").write_text("a-committed")
    await sync_a.write_sync_generation_marker(
        CloudSyncMarker(
            thread_id=THREAD_A,
            mount_id="logical-a",
            generation=7,
            lease_token=7,
            workspace_generation=WORKSPACE_A,
            sync_scope_sha256=SCOPE_A,
            baseline_sha256=digest_a,
        )
    )
    (workspace_a / "a.txt").write_text("a-stale-retry")
    (workspace_b / "b.txt").write_text("b-new")
    sync_a.push_generation_delta = AsyncMock(  # type: ignore[method-assign]
        side_effect=AssertionError("already-marked mount must be ack-only")
    )
    coordinator = WorkspaceSyncCoordinator(
        [
            MountSync(
                mount_id="row-a",
                target_path="a",
                sync=sync_a,
                sync_scope_sha256=SCOPE_A,
                generation_key="logical-a",
            ),
            MountSync(
                mount_id="row-b",
                target_path="b",
                sync=sync_b,
                sync_scope_sha256=SCOPE_B,
                generation_key="logical-b",
            ),
        ],
        thread_id=THREAD_A,
        workspace_generation=WORKSPACE_A,
    )
    acknowledgements: list[str] = []

    async def acknowledge(mount_id, _requirement):
        acknowledgements.append(mount_id)

    result = await coordinator.reconcile_before_pull(
        {"logical-a": requirement_a, "logical-b": requirement_b},
        before_write=_lease_current,
        acknowledge=acknowledge,
    )

    assert result == {"row-a": [], "row-b": ["b.txt"]}
    assert sorted(acknowledgements) == ["logical-a", "logical-b"]
    sync_a.push_generation_delta.assert_not_awaited()
    assert (remote_a / "a.txt").read_text() == "a-committed"
    assert (remote_b / "b.txt").read_text() == "b-new"


@pytest.mark.asyncio
async def test_duplicate_logical_generation_keys_fail_before_resource_access():
    def inert_sync():
        return SimpleNamespace(
            read_sync_generation_marker=AsyncMock(),
            install_generation_baseline=AsyncMock(),
            push_generation_delta=AsyncMock(),
            write_sync_generation_marker=AsyncMock(),
        )

    sync_a = inert_sync()
    sync_b = inert_sync()
    coordinator = WorkspaceSyncCoordinator(
        [
            MountSync("row-a", "a", sync_a, SCOPE_A, generation_key="same"),
            MountSync("row-b", "b", sync_b, SCOPE_B, generation_key="same"),
        ],
        thread_id=THREAD_A,
        workspace_generation=WORKSPACE_A,
    )
    before_write = AsyncMock()
    acknowledge = AsyncMock()

    with pytest.raises(CloudSyncGenerationError, match="duplicate"):
        await coordinator.reconcile_before_pull(
            {"same": _requirement(mount_id="same")},
            before_write=before_write,
            acknowledge=acknowledge,
        )

    before_write.assert_not_awaited()
    acknowledge.assert_not_awaited()
    for sync in (sync_a, sync_b):
        sync.read_sync_generation_marker.assert_not_awaited()
        sync.push_generation_delta.assert_not_awaited()
        sync.write_sync_generation_marker.assert_not_awaited()


@pytest.mark.parametrize("fail_at", ["root", "nested"])
@pytest.mark.asyncio
async def test_strict_generation_walk_failure_writes_no_marker_or_ack(
    tmp_path: Path,
    fail_at: str,
):
    remote = tmp_path / "remote"
    ws = tmp_path / "ws"
    (ws / "nested").mkdir(parents=True)
    (ws / "nested" / "data.txt").write_text("payload")
    backend = FilesystemTestBackend(ws)
    original_list_dir = backend.list_dir

    def failing_list_dir(path: str = "", pattern: str = "*") -> list[str]:
        if (fail_at == "root" and path == "") or (
            fail_at == "nested" and path == "nested"
        ):
            raise RuntimeError(f"forced {fail_at} list failure")
        return original_list_dir(path, pattern)

    backend.list_dir = failing_list_dir  # type: ignore[method-assign]
    sync = LocalFsWorkspaceSync(
        tmp_path / "unused-local-path",
        remote_root=remote,
        workspace_backend=backend,
    )
    coordinator = _coordinator(sync)
    acknowledgements: list[str] = []

    async def acknowledge(mount_id, _requirement):
        acknowledgements.append(mount_id)

    with pytest.raises(CloudSyncError):
        await coordinator.push_generation(
            {"mount-a": _requirement()},
            before_write=_lease_current,
            acknowledge=acknowledge,
        )

    assert acknowledgements == []
    assert not list(remote.rglob("*.json"))
    assert not (remote / "nested" / "data.txt").exists()


@pytest.mark.asyncio
async def test_pending_scope_change_fails_closed_but_clean_change_is_fresh(
    tmp_path: Path,
):
    remote = tmp_path / "remote"
    ws = tmp_path / "ws"
    ws.mkdir()
    sync = LocalFsWorkspaceSync(ws, remote_root=remote)
    coordinator = _coordinator(
        sync,
        workspace_generation=WORKSPACE_B,
        scope=SCOPE_B,
    )
    acknowledgements: list[str] = []

    async def acknowledge(mount_id, _requirement):
        acknowledgements.append(mount_id)

    with pytest.raises(CloudSyncError) as exc_info:
        await coordinator.reconcile_before_pull(
            {"mount-a": _requirement(acknowledged=6)},
            before_write=_lease_current,
            acknowledge=acknowledge,
        )
    assert any(
        isinstance(error, CloudSyncGenerationError)
        for _mount_id, _path, error in exc_info.value.failures
    )

    result = await coordinator.reconcile_before_pull(
        {"mount-a": _requirement(acknowledged=7)},
        before_write=_lease_current,
        acknowledge=acknowledge,
    )
    assert result == {"mount-a": []}
    assert acknowledgements == []


@pytest.mark.asyncio
async def test_absent_pending_mount_row_fails_before_any_current_mount_recovery(
    tmp_path: Path,
):
    remote = tmp_path / "remote"
    ws = tmp_path / "ws"
    ws.mkdir()
    coordinator = _coordinator(
        LocalFsWorkspaceSync(ws, remote_root=remote),
        mount_id="current",
    )

    with pytest.raises(CloudSyncGenerationError, match="absent mount"):
        await coordinator.reconcile_before_pull(
            {"removed": _requirement(mount_id="removed")},
            before_write=_lease_current,
            acknowledge=lambda *_args: _lease_current(),
        )
    assert list(remote.rglob("*")) == []


def test_generation_scope_validation_rejects_duplicate_and_malformed_mounts(
    tmp_path: Path,
):
    remote = tmp_path / "remote"
    ws = tmp_path / "ws"
    ws.mkdir()
    sync = LocalFsWorkspaceSync(ws, remote_root=remote)
    malformed = WorkspaceSyncCoordinator(
        [MountSync("m", "", sync, "short")],
        thread_id=THREAD_A,
        workspace_generation=WORKSPACE_A,
    )
    with pytest.raises(CloudSyncGenerationError, match="invalid or duplicate"):
        malformed.generation_scopes()

    duplicate = WorkspaceSyncCoordinator(
        [
            MountSync("m", "", sync, SCOPE_A),
            MountSync("m", "other", sync, SCOPE_B),
        ],
        thread_id=THREAD_A,
        workspace_generation=WORKSPACE_A,
    )
    with pytest.raises(CloudSyncGenerationError, match="invalid or duplicate"):
        duplicate.generation_scopes()


def test_validate_requirements_sees_all_rows_but_allows_clean_history(tmp_path: Path):
    remote = tmp_path / "remote"
    ws = tmp_path / "ws"
    ws.mkdir()
    coordinator = _coordinator(LocalFsWorkspaceSync(ws, remote_root=remote))

    coordinator.validate_requirements(
        {
            "mount-a": _requirement(),
            "removed-clean": _requirement(
                mount_id="removed-clean", acknowledged=7, scope=SCOPE_B
            ),
        }
    )
    with pytest.raises(CloudSyncGenerationError, match="absent mount"):
        coordinator.validate_requirements(
            {
                "mount-a": _requirement(),
                "removed-pending": _requirement(
                    mount_id="removed-pending", acknowledged=6, scope=SCOPE_B
                ),
            }
        )
