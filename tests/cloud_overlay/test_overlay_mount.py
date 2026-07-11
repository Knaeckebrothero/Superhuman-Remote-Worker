from __future__ import annotations

from pathlib import Path

import pytest

from src.services.cloud_overlay.overlay_mount import (
    OverlayMountError,
    OverlayMountManager,
)


class FakeRemoteBackend:
    def __init__(self, *, root: str | None = None) -> None:
        self.files: dict[str, str] = {}
        self.commands: list[tuple[str, int]] = []
        self.outputs_by_script: dict[str, str] = {}
        if root is not None:
            self.root = root

    def resolve_home_path(self, relative_path: str) -> str:
        return f"/home/agent-host/{relative_path}"

    def write_home_file(self, relative_path: str, content) -> None:
        self.files[relative_path] = (
            content.decode("utf-8") if isinstance(content, bytes) else content
        )

    def exec_command(self, command: str, timeout: int = 30) -> str:
        self.commands.append((command, timeout))
        for name, out in self.outputs_by_script.items():
            if name in command:
                return out
        return "__SRW_OVERLAY_OK__\n"


def _cfg() -> dict:
    return {
        "lower": "/cloud/lower",
        "upper": "/home/agent-host/.overlay/upper",
        "work": "/home/agent-host/.overlay/work",
        "merged": "/cloud/merged",
        "quota_bytes": 8 * 1024**3,
    }


def _manager(backend) -> OverlayMountManager:
    return OverlayMountManager(
        thread_id="thread-12345678",
        overlay_cfg=_cfg(),
        workspace_backend=backend,
        workspace_root=Path("/home/agent-host/workspace"),
    )


def test_mount_script_builds_fuse_overlayfs_over_ro_lower_and_repoints_symlink():
    backend = FakeRemoteBackend()
    _manager(backend).mount()
    scripts = [b for p, b in backend.files.items() if p.endswith("overlay_mount.sh")]
    assert len(scripts) == 1
    s = scripts[0]
    # exact spike option string, no extra overlay opts (Global Constraints)
    assert (
        "fuse-overlayfs -o "
        "lowerdir=/cloud/lower,upperdir=/home/agent-host/.overlay/upper,"
        "workdir=/home/agent-host/.overlay/work /cloud/merged" in s
    )
    assert "mkdir -p /home/agent-host/.overlay/upper" in s
    assert "mountpoint -q /cloud/lower" in s  # refuses if the lower isn't up
    # symlink workspace/cloud -> merged (NOT the raw lower)
    assert "ln -sfn /cloud/merged" in s
    assert "${workspace}/cloud" in s


def test_mount_refuses_when_lower_not_mounted():
    backend = FakeRemoteBackend()
    backend.outputs_by_script["overlay_mount.sh"] = "__SRW_OVERLAY_FAILED__ rc=1\n"
    with pytest.raises(OverlayMountError):
        _manager(backend).mount()


def test_unmount_is_plain_and_leaves_upperdir(monkeypatch):
    backend = FakeRemoteBackend()
    mgr = _manager(backend)
    mgr.mount()
    mgr.unmount()
    scripts = [b for p, b in backend.files.items() if p.endswith("overlay_unmount.sh")]
    assert len(scripts) == 1
    u = scripts[0]
    assert "fusermount3 -u /cloud/merged" in u
    # unmount must NOT rm the upperdir/workdir
    assert "rm -rf /home/agent-host/.overlay/upper" not in u
    assert "rm -rf /home/agent-host/.overlay/work" not in u
