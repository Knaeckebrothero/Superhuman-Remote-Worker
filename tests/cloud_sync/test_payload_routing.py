"""Tests for the agent-side payload router that picks v1 vs v2.

The function under test is ``src.api.persistent_app._build_sync_coordinator``.
Phase 1 of cloud_collaboration_model.md §9: both shapes are accepted so a
cluster on an older orchestrator (v1 flat) still mounts its session folder,
and a cluster on a new orchestrator (v2) mounts session folder + projects.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def build_sync_coordinator():
    from src.api.persistent_app import _build_sync_coordinator

    return _build_sync_coordinator


def _nc_cfg(folder: str) -> dict:
    return {
        "backend": "nextcloud",
        "webdav_url": f"http://nc.local/remote.php/dav/files/u/{folder}/",
        "auth": {"type": "basic", "username": "u", "password": "p"},
    }


def test_v1_flat_returns_single_legacy_mount(tmp_path: Path, build_sync_coordinator):
    coord = build_sync_coordinator(
        workspace_path=tmp_path,
        workspace_backend=None,
        cloud_cfg=_nc_cfg("session-abc"),
    )
    assert coord is not None
    assert len(coord) == 1
    [m] = coord.mounts
    assert m.mount_id == "legacy-session"
    assert m.target_path == ""


def test_v2_session_folder_plus_one_project(tmp_path: Path, build_sync_coordinator):
    cfg = {
        "version": 2,
        "session_folder": _nc_cfg("session-abc"),
        "mounts": [
            {
                "mount_id": "m1",
                "mount_kind": "project",
                "target_path": "projects/alpha",
                **_nc_cfg("project-alpha"),
            }
        ],
    }
    coord = build_sync_coordinator(
        workspace_path=tmp_path, workspace_backend=None, cloud_cfg=cfg
    )
    assert coord is not None
    assert len(coord) == 2
    paths = sorted(m.target_path for m in coord.mounts)
    assert paths == ["", "projects/alpha"]


def test_v2_with_no_session_folder(tmp_path: Path, build_sync_coordinator):
    """A v2 payload without legacy session folder still produces a coordinator."""
    cfg = {
        "version": 2,
        "session_folder": None,
        "mounts": [
            {
                "mount_id": "m1",
                "mount_kind": "project",
                "target_path": "projects/alpha",
                **_nc_cfg("project-alpha"),
            }
        ],
    }
    coord = build_sync_coordinator(
        workspace_path=tmp_path, workspace_backend=None, cloud_cfg=cfg
    )
    assert coord is not None
    assert len(coord) == 1
    assert coord.mounts[0].target_path == "projects/alpha"


def test_empty_cfg_returns_none(tmp_path: Path, build_sync_coordinator):
    assert (
        build_sync_coordinator(
            workspace_path=tmp_path, workspace_backend=None, cloud_cfg=None
        )
        is None
    )
    assert (
        build_sync_coordinator(
            workspace_path=tmp_path, workspace_backend=None, cloud_cfg={}
        )
        is None
    )


def test_v2_with_no_resolvable_mounts_returns_none(
    tmp_path: Path, build_sync_coordinator
):
    """All mounts unresolvable → coordinator collapses to None."""
    cfg = {
        "version": 2,
        "session_folder": None,
        "mounts": [
            {
                "mount_id": "m1",
                "mount_kind": "project",
                "target_path": "projects/alpha",
                # No backend / webdav_url / auth — build_workspace_sync drops it.
            }
        ],
    }
    coord = build_sync_coordinator(
        workspace_path=tmp_path, workspace_backend=None, cloud_cfg=cfg
    )
    assert coord is None
