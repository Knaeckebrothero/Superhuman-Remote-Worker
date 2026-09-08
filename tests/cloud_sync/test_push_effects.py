"""Commit-then-effects on the cloud-sync side (stateless_turn_resilience.md 4a).

The turn-end push is split into a workspace STAGE (reads + temp files) and a
cloud TRANSMIT (conditional WebDAV writes under the writer fence, with durable
per-file progress), so the run_queue unit can complete while the transmit
continues off-slot and a successor resumes from ``push_progress`` instead of
re-uploading. The local-fs transport applies RFC 4918 preconditions exactly
as a WebDAV server would.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

import pytest

from agent.services.cloud_sync.base import CloudSyncMarker
from agent.services.cloud_sync.coordinator import (
    CloudSyncError,
    MountSync,
    WorkspaceSyncCoordinator,
)
from shared.cloud_sync_generations import (
    CloudSyncRequirement,
    encode_cloud_sync_baseline,
)
from tests._fs_backend import FilesystemTestBackend
from tests.cloud_sync._local_fs import LocalFsWorkspaceSync

THREAD = "11111111-1111-4111-8111-111111111111"
WORKSPACE = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
SCOPE = "a" * 64


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def _ok() -> None:
    return None


def _sync(tmp_path: Path) -> tuple[LocalFsWorkspaceSync, Path, Path]:
    ws = tmp_path / "ws"
    ws.mkdir()
    remote = tmp_path / "remote"
    sync = LocalFsWorkspaceSync(
        tmp_path / "unused-local",
        remote_root=remote,
        workspace_backend=FilesystemTestBackend(ws),
    )
    return sync, ws, remote


def _requirement(baseline: dict, *, generation: int = 7) -> CloudSyncRequirement:
    manifest, _encoded, sha = encode_cloud_sync_baseline(baseline)
    return CloudSyncRequirement(
        mount_id="mount-a",
        required_generation=generation,
        acknowledged_generation=0,
        required_lease_token=generation,
        workspace_generation=WORKSPACE,
        sync_scope_sha256=SCOPE,
        baseline_manifest=manifest,
        baseline_sha256=sha,
    )


def _coordinator(sync: LocalFsWorkspaceSync) -> WorkspaceSyncCoordinator:
    coordinator = WorkspaceSyncCoordinator(
        thread_id=THREAD, workspace_generation=WORKSPACE
    )
    coordinator.add(
        MountSync(
            mount_id="mount-a",
            target_path="/",
            sync=sync,
            sync_scope_sha256=SCOPE,
            generation_key="mount-a",
        )
    )
    return coordinator


# ---------------------------------------------------------------- stage / transmit


@pytest.mark.asyncio
async def test_stage_then_transmit_uses_preconditions_and_cleans_temp_files(
    tmp_path: Path,
):
    sync, ws, remote = _sync(tmp_path)
    (ws / "a.txt").write_bytes(b"alpha")
    (ws / "sub").mkdir()
    (ws / "sub" / "b.txt").write_bytes(b"bravo")

    staged = await sync.stage_generation_delta({})
    assert [u.path for u in staged.uploads] == ["a.txt", "sub/b.txt"]
    assert all(u.if_none_match and u.if_match is None for u in staged.uploads)
    assert staged.planned == 2 and staged.skipped_by_progress == 0
    tmp_paths = [u.tmp_path for u in staged.uploads]
    assert all(os.path.exists(p) for p in tmp_paths)

    landed: list[tuple[str, dict[str, Any]]] = []

    async def progress(path: str, entry: dict[str, Any]) -> None:
        landed.append((path, entry))

    commit = await sync.transmit_generation_delta(
        staged, before_write=_ok, progress_cb=progress
    )
    assert (remote / "a.txt").read_bytes() == b"alpha"
    assert (remote / "sub" / "b.txt").read_bytes() == b"bravo"
    assert not any(os.path.exists(p) for p in tmp_paths)
    assert sync.conditional_writes == [("a.txt", None, True), ("sub/b.txt", None, True)]
    # the manifest now carries the server's etag for every landed path
    assert commit.manifest["a.txt"] == {
        "sha256": _sha(b"alpha"),
        "remote_etag": _sha(b"alpha"),
    }
    assert [p for p, _ in landed] == ["a.txt", "sub/b.txt"]
    assert landed[0][1] == {
        "sha256": _sha(b"alpha"),
        "size": 5,
        "remote_etag": _sha(b"alpha"),
        "state": "uploaded",
    }


@pytest.mark.asyncio
async def test_changed_baseline_file_is_written_with_if_match(tmp_path: Path):
    sync, ws, remote = _sync(tmp_path)
    (remote / "a.txt").write_bytes(b"old")
    (ws / "a.txt").write_bytes(b"new")
    baseline = {"a.txt": {"sha256": _sha(b"old"), "remote_etag": _sha(b"old")}}
    staged = await sync.stage_generation_delta(baseline)
    assert [(u.path, u.if_match, u.if_none_match) for u in staged.uploads] == [
        ("a.txt", _sha(b"old"), False)
    ]
    await sync.transmit_generation_delta(staged, before_write=_ok)
    assert (remote / "a.txt").read_bytes() == b"new"


@pytest.mark.asyncio
async def test_412_with_a_live_fence_retries_once_with_the_fresh_etag(tmp_path: Path):
    sync, ws, remote = _sync(tmp_path)
    (remote / "a.txt").write_bytes(b"user-edit")  # moved under us
    (ws / "a.txt").write_bytes(b"agent")
    baseline = {"a.txt": {"sha256": _sha(b"old"), "remote_etag": "stale-etag"}}
    staged = await sync.stage_generation_delta(baseline)
    fence_checks = 0

    async def fence() -> None:
        nonlocal fence_checks
        fence_checks += 1

    await sync.transmit_generation_delta(staged, before_write=fence)
    assert (remote / "a.txt").read_bytes() == b"agent"
    # first attempt carried the stale precondition, the retry the fresh one
    assert [w[1] for w in sync.conditional_writes] == [_sha(b"user-edit")]
    assert fence_checks >= 3  # transmit start, per-write, the post-412 recheck


@pytest.mark.asyncio
async def test_412_with_a_lost_fence_stops_and_cleans_up(tmp_path: Path):
    sync, ws, remote = _sync(tmp_path)
    (remote / "a.txt").write_bytes(b"successor-bytes")
    (ws / "a.txt").write_bytes(b"stale-owner-bytes")
    baseline = {"a.txt": {"sha256": _sha(b"old"), "remote_etag": "stale-etag"}}
    staged = await sync.stage_generation_delta(baseline)
    tmp_paths = [u.tmp_path for u in staged.uploads]
    checks = 0

    class Lost(RuntimeError):
        pass

    async def fence() -> None:
        # The DB says we were adopted away the moment the write bounced.
        nonlocal checks
        checks += 1
        if checks >= 3:
            raise Lost("push-owner fence")

    with pytest.raises(Lost):
        await sync.transmit_generation_delta(staged, before_write=fence)
    assert (remote / "a.txt").read_bytes() == b"successor-bytes"  # never clobbered
    assert not any(os.path.exists(p) for p in tmp_paths)


@pytest.mark.asyncio
async def test_progress_resumes_uploads_and_skips_recorded_deletes(tmp_path: Path):
    sync, ws, remote = _sync(tmp_path)
    (ws / "a.txt").write_bytes(b"alpha")
    (ws / "b.txt").write_bytes(b"bravo")
    (remote / "a.txt").write_bytes(b"alpha")  # the predecessor landed a.txt
    baseline = {"gone.txt": {"sha256": _sha(b"x"), "remote_etag": "g"}}
    progress = {
        "planned": 3,
        "files": {
            "a.txt": {
                "sha256": _sha(b"alpha"),
                "size": 5,
                "remote_etag": _sha(b"alpha"),
                "state": "uploaded",
            },
            "gone.txt": {"state": "deleted"},
        },
    }
    staged = await sync.stage_generation_delta(baseline, progress=progress)
    assert [u.path for u in staged.uploads] == ["b.txt"]
    assert staged.deletes == []
    assert staged.skipped_by_progress == 2 and staged.planned == 3
    commit = await sync.transmit_generation_delta(staged, before_write=_ok)
    assert commit.paths == ["b.txt"]
    assert commit.manifest["a.txt"]["remote_etag"] == _sha(b"alpha")
    assert "gone.txt" not in commit.manifest


@pytest.mark.asyncio
async def test_deletes_carry_if_match_and_report_progress(tmp_path: Path):
    sync, ws, remote = _sync(tmp_path)
    (remote / "old.txt").write_bytes(b"old")
    baseline = {"old.txt": {"sha256": _sha(b"old"), "remote_etag": _sha(b"old")}}
    staged = await sync.stage_generation_delta(baseline)
    assert [(d.path, d.if_match) for d in staged.deletes] == [("old.txt", _sha(b"old"))]
    landed: list[tuple[str, dict[str, Any]]] = []

    async def progress(path: str, entry: dict[str, Any]) -> None:
        landed.append((path, entry))

    commit = await sync.transmit_generation_delta(
        staged, before_write=_ok, progress_cb=progress
    )
    assert not (remote / "old.txt").exists()
    assert landed == [
        ("old.txt", {"sha256": "", "size": 0, "remote_etag": "", "state": "deleted"})
    ]
    assert commit.manifest == {}


# ---------------------------------------------------------------- coordinator


@pytest.mark.asyncio
async def test_coordinator_stage_transmit_writes_marker_acks_and_reports(
    tmp_path: Path,
):
    sync, ws, remote = _sync(tmp_path)
    (ws / "a.txt").write_bytes(b"alpha")
    coordinator = _coordinator(sync)
    requirement = _requirement({})
    acked: list[str] = []
    planned: list[tuple[str, int]] = []
    landed: list[tuple[str, str]] = []

    async def acknowledge(mount_id: str, req: Any) -> None:
        acked.append(mount_id)

    async def on_planned(mount_id: str, n: int) -> None:
        planned.append((mount_id, n))

    async def on_progress(mount_id: str, path: str, entry: dict[str, Any]) -> None:
        landed.append((mount_id, path))

    staged = await coordinator.stage_generation({"mount-a": requirement})
    assert staged[0].staged is not None and staged[0].staged.planned == 1
    out = await coordinator.transmit_generation(
        staged,
        before_write=_ok,
        acknowledge=acknowledge,
        progress=on_progress,
        planned=on_planned,
    )
    assert out == {"mount-a": ["a.txt"]}
    assert acked == ["mount-a"] and planned == [("mount-a", 1)]
    assert landed == [("mount-a", "a.txt")]
    marker = await sync.read_sync_generation_marker(
        thread_id=THREAD, sync_scope_sha256=SCOPE
    )
    assert marker is not None and marker.generation == 7
    assert marker.committed_manifest["a.txt"]["remote_etag"] == _sha(b"alpha")
    # push_generation is the same two halves in one call
    (ws / "b.txt").write_bytes(b"bravo")
    again = await coordinator.push_generation(
        {"mount-a": _requirement(marker.committed_manifest, generation=8)},
        before_write=_ok,
        acknowledge=acknowledge,
    )
    assert again == {"mount-a": ["b.txt"]}


@pytest.mark.asyncio
async def test_reconcile_adopts_once_and_resumes_from_the_predecessor(tmp_path: Path):
    sync, ws, remote = _sync(tmp_path)
    (ws / "a.txt").write_bytes(b"alpha")
    (ws / "b.txt").write_bytes(b"bravo")
    (remote / "a.txt").write_bytes(b"alpha")  # predecessor landed this, then died
    coordinator = _coordinator(sync)
    requirement = _requirement({})  # pending: acknowledged 0 < required 7
    adopt_calls = 0
    acked: list[str] = []

    async def adopt() -> dict[str, Any]:
        nonlocal adopt_calls
        adopt_calls += 1
        return {
            "mount-a": {
                "planned": 2,
                "files": {
                    "a.txt": {
                        "sha256": _sha(b"alpha"),
                        "size": 5,
                        "remote_etag": _sha(b"alpha"),
                        "state": "uploaded",
                    }
                },
            }
        }

    async def acknowledge(mount_id: str, req: Any) -> None:
        acked.append(mount_id)

    out = await coordinator.reconcile_before_pull(
        {"mount-a": requirement}, before_write=_ok, acknowledge=acknowledge, adopt=adopt
    )
    assert adopt_calls == 1
    assert out == {"mount-a": ["b.txt"]}  # a.txt resumed, not re-uploaded
    assert [w[0] for w in sync.conditional_writes if not w[0].startswith(".srw/")] == [
        "b.txt"
    ]
    assert acked == ["mount-a"]
    marker = await sync.read_sync_generation_marker(
        thread_id=THREAD, sync_scope_sha256=SCOPE
    )
    assert marker is not None and set(marker.committed_manifest) == {"a.txt", "b.txt"}


@pytest.mark.asyncio
async def test_reconcile_does_not_adopt_when_nothing_is_pending(tmp_path: Path):
    sync, ws, remote = _sync(tmp_path)
    coordinator = _coordinator(sync)
    manifest, _e, sha = encode_cloud_sync_baseline({})
    done = CloudSyncRequirement(
        mount_id="mount-a",
        required_generation=7,
        acknowledged_generation=7,
        required_lease_token=7,
        workspace_generation=WORKSPACE,
        sync_scope_sha256=SCOPE,
        baseline_manifest=manifest,
        baseline_sha256=sha,
    )
    await sync.write_sync_generation_marker(
        CloudSyncMarker(
            thread_id=THREAD,
            mount_id="mount-a",
            generation=7,
            lease_token=7,
            workspace_generation=WORKSPACE,
            sync_scope_sha256=SCOPE,
            baseline_sha256=sha,
        )
    )
    adopt_calls = 0

    async def adopt() -> dict[str, Any]:
        nonlocal adopt_calls
        adopt_calls += 1
        return {}

    async def acknowledge(mount_id: str, req: Any) -> None:
        raise AssertionError("nothing to acknowledge")

    await coordinator.reconcile_before_pull(
        {"mount-a": done}, before_write=_ok, acknowledge=acknowledge, adopt=adopt
    )
    assert adopt_calls == 0


@pytest.mark.asyncio
async def test_stage_failure_cleans_every_mount(tmp_path: Path):
    sync, ws, remote = _sync(tmp_path)
    (ws / "a.txt").write_bytes(b"alpha")
    coordinator = _coordinator(sync)
    broken = _requirement({})
    original = sync.stage_generation_delta

    async def boom(*args: Any, **kwargs: Any):
        staged = await original(*args, **kwargs)
        staged.cleanup()
        raise RuntimeError("workspace vanished")

    sync.stage_generation_delta = boom  # type: ignore[method-assign]
    with pytest.raises(CloudSyncError):
        await coordinator.stage_generation({"mount-a": broken})
