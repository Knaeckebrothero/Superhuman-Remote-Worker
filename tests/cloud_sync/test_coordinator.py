"""Tests for ``WorkspaceSyncCoordinator`` — Phase 1 cloud-mirror foundation.

Drives the coordinator end-to-end through ``LocalFsWorkspaceSync`` so we
exercise the real ``WorkspaceSyncBase`` algorithm (push/pull, etag dedup,
mount_subdir retargeting) without WebDAV. Failure paths use a deliberately-
broken sync to validate the raise-and-block aggregation policy.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.services.cloud_sync import (
    CloudSyncError,
    MountSync,
    WorkspaceSyncCoordinator,
)

from ._local_fs import FailingLocalFsWorkspaceSync, LocalFsWorkspaceSync


def _make_mount(
    workspace_path: Path,
    *,
    remote_root: Path,
    mount_id: str = "m",
    target_path: str = "",
    mount_subdir: str = "",
) -> MountSync:
    sync = LocalFsWorkspaceSync(
        workspace_path,
        remote_root=remote_root,
        mount_subdir=mount_subdir,
    )
    return MountSync(mount_id=mount_id, target_path=target_path, sync=sync)


@pytest.mark.asyncio
async def test_push_all_uploads_each_mount(tmp_path: Path):
    ws_a = tmp_path / "ws_a"
    ws_a.mkdir()
    (ws_a / "a.txt").write_text("alpha")

    ws_b = tmp_path / "ws_b"
    ws_b.mkdir()
    (ws_b / "b.txt").write_text("bravo")

    remote_a = tmp_path / "remote_a"
    remote_b = tmp_path / "remote_b"
    mount_a = _make_mount(ws_a, remote_root=remote_a, mount_id="A", target_path="")
    mount_b = _make_mount(
        ws_b, remote_root=remote_b, mount_id="B", target_path="projects/b"
    )

    coordinator = WorkspaceSyncCoordinator([mount_a, mount_b])
    result = await coordinator.push_all()

    assert (remote_a / "a.txt").read_text() == "alpha"
    assert (remote_b / "b.txt").read_text() == "bravo"
    assert set(result.keys()) == {"A", "B"}


@pytest.mark.asyncio
async def test_pull_all_downloads_each_mount(tmp_path: Path):
    ws_a = tmp_path / "ws_a"
    ws_a.mkdir()
    ws_b = tmp_path / "ws_b"
    ws_b.mkdir()

    remote_a = tmp_path / "remote_a"
    remote_a.mkdir()
    (remote_a / "x.md").write_text("cloud-A")

    remote_b = tmp_path / "remote_b"
    remote_b.mkdir()
    (remote_b / "y.md").write_text("cloud-B")

    mount_a = _make_mount(ws_a, remote_root=remote_a, mount_id="A")
    mount_b = _make_mount(ws_b, remote_root=remote_b, mount_id="B")

    coordinator = WorkspaceSyncCoordinator([mount_a, mount_b])
    await coordinator.pull_all()

    assert (ws_a / "x.md").read_text() == "cloud-A"
    assert (ws_b / "y.md").read_text() == "cloud-B"


@pytest.mark.asyncio
async def test_failure_aggregates_into_raise(tmp_path: Path):
    ws_ok = tmp_path / "ws_ok"
    ws_ok.mkdir()
    (ws_ok / "ok.txt").write_text("fine")

    ws_bad = tmp_path / "ws_bad"
    ws_bad.mkdir()
    (ws_bad / "broken.txt").write_text("...")

    remote_ok = tmp_path / "remote_ok"
    remote_bad = tmp_path / "remote_bad"

    ok = _make_mount(ws_ok, remote_root=remote_ok, mount_id="OK")

    bad_sync = FailingLocalFsWorkspaceSync(
        ws_bad,
        remote_root=remote_bad,
        fail_on="push",
        message="quota exceeded",
    )
    bad = MountSync(mount_id="BAD", target_path="projects/bad", sync=bad_sync)

    coordinator = WorkspaceSyncCoordinator([ok, bad])

    with pytest.raises(CloudSyncError) as exc_info:
        await coordinator.push_all()

    err = exc_info.value
    assert err.op == "push"
    # Aggregation includes every failure, by mount_id.
    assert any(mid == "BAD" for mid, _path, _exc in err.failures)
    # OK mount must still have completed before the aggregate raise.
    assert (remote_ok / "ok.txt").read_text() == "fine"


@pytest.mark.asyncio
async def test_pull_failure_aggregates_into_raise(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    remote_ok = tmp_path / "remote_ok"
    remote_ok.mkdir()
    (remote_ok / "x.md").write_text("ok")
    remote_bad = tmp_path / "remote_bad"

    ok_sync = LocalFsWorkspaceSync(ws, remote_root=remote_ok, mount_subdir="ok")
    ok = MountSync(mount_id="OK", target_path="ok", sync=ok_sync)

    bad_sync = FailingLocalFsWorkspaceSync(
        ws,
        remote_root=remote_bad,
        fail_on="pull",
        mount_subdir="bad",
    )
    bad = MountSync(mount_id="BAD", target_path="bad", sync=bad_sync)

    coordinator = WorkspaceSyncCoordinator([ok, bad])
    with pytest.raises(CloudSyncError) as exc_info:
        await coordinator.pull_all()
    assert exc_info.value.op == "pull"


@pytest.mark.asyncio
async def test_mount_subdir_round_trip_local(tmp_path: Path):
    """A mount with ``mount_subdir`` confines its sync to that subtree."""
    ws = tmp_path / "ws"
    (ws / "projects" / "alpha").mkdir(parents=True)
    (ws / "projects" / "alpha" / "a.txt").write_text("alpha-content")
    (ws / "session.md").write_text("at-root")

    remote = tmp_path / "remote_alpha"

    # The mount targets the projects/alpha subtree by setting
    # workspace_path itself to that subdir for the local-FS code path.
    project_sync = LocalFsWorkspaceSync(
        ws / "projects" / "alpha",
        remote_root=remote,
        mount_subdir="",  # local-FS mode uses workspace_path directly
    )
    coordinator = WorkspaceSyncCoordinator(
        [MountSync(mount_id="alpha", target_path="projects/alpha", sync=project_sync)]
    )
    await coordinator.push_all()

    # Only the file inside the mount lands in the cloud — not the one at root.
    assert (remote / "a.txt").read_text() == "alpha-content"
    assert not (remote / "session.md").exists()


@pytest.mark.asyncio
async def test_empty_coordinator_is_no_op(tmp_path: Path):
    coordinator = WorkspaceSyncCoordinator()
    assert await coordinator.push_all() == {}
    assert await coordinator.pull_all() == {}
