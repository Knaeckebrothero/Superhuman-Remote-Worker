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


def test_v2_project_default_at_workspace_root(tmp_path: Path, build_sync_coordinator):
    """Phase 2: a ``project_default`` mount at ``target_path=''`` is routed
    as a root-level workspace mirror — the user's cloud home becomes the
    workspace. No legacy session folder is needed in this shape.
    """
    cfg = {
        "version": 2,
        "session_folder": None,
        "mounts": [
            {
                "mount_id": "m_home",
                "mount_kind": "project_default",
                "target_path": "",
                **_nc_cfg("home-of-alice"),
            }
        ],
    }
    coord = build_sync_coordinator(
        workspace_path=tmp_path, workspace_backend=None, cloud_cfg=cfg
    )
    assert coord is not None
    assert len(coord) == 1
    [m] = coord.mounts
    assert m.target_path == ""
    assert m.mount_id == "m_home"
    # Generation rows use a separate logical destination identity, not the
    # replace-on-edit thread_mounts UUID supplied in the payload. Keeping the
    # transport id preserves pinned-lane behavior and diagnostics.
    assert m.generation_id.startswith("mount:")


def test_generation_identity_survives_replace_on_edit_mount_uuid(
    tmp_path: Path, build_sync_coordinator
):
    """Replacing thread_mounts deletes/reinserts rows with fresh UUIDs.

    The durable generation identity must therefore be derived from the
    non-secret logical cloud destination, not ``mount_id`` from either row.
    """
    base_mount = {
        "mount_kind": "project",
        "target_path": "projects/alpha",
        **_nc_cfg("project-alpha"),
    }
    first = build_sync_coordinator(
        workspace_path=tmp_path,
        workspace_backend=None,
        cloud_cfg={
            "version": 2,
            "session_folder": None,
            "mounts": [{"mount_id": "row-uuid-before", **base_mount}],
        },
        thread_id="11111111-1111-4111-8111-111111111111",
        workspace_generation="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    )
    replaced = build_sync_coordinator(
        workspace_path=tmp_path,
        workspace_backend=None,
        cloud_cfg={
            "version": 2,
            "session_folder": None,
            "mounts": [{"mount_id": "row-uuid-after", **base_mount}],
        },
        thread_id="11111111-1111-4111-8111-111111111111",
        workspace_generation="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    )

    assert first is not None and replaced is not None
    assert first.mounts[0].mount_id != replaced.mounts[0].mount_id
    assert first.mounts[0].generation_id == replaced.mounts[0].generation_id
    assert first.mounts[0].sync_scope_sha256 == replaced.mounts[0].sync_scope_sha256
