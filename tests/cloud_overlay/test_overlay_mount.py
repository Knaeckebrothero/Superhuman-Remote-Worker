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


def test_unmount_is_plain_and_leaves_upperdir():
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


def test_mount_script_quotes_paths_with_spaces():
    """Verify that paths with spaces are properly quoted in fuse-overlayfs -o option."""
    backend = FakeRemoteBackend()
    # Config with a space in the lower path
    cfg = _cfg()
    cfg["lower"] = "/cloud/low er"
    mgr = OverlayMountManager(
        thread_id="thread-12345678",
        overlay_cfg=cfg,
        workspace_backend=backend,
        workspace_root=Path("/home/agent-host/workspace"),
    )
    mgr.mount()
    scripts = [b for p, b in backend.files.items() if p.endswith("overlay_mount.sh")]
    assert len(scripts) == 1
    s = scripts[0]
    # The -o value should be quoted as ONE shell word, preserving the space
    assert "'lowerdir=/cloud/low er,upperdir=/home/agent-host/.overlay/upper,workdir=/home/agent-host/.overlay/work'" in s
    # The unquoted form must NOT appear
    assert "-o lowerdir=/cloud/low er," not in s


def test_refresh_unmounts_overlay_refreshes_lower_then_remounts():
    backend = FakeRemoteBackend()
    mgr = _manager(backend)
    mgr.mount()
    starting_commands = len(backend.commands)
    calls: list[int] = []

    def _refresh_lower() -> None:
        # Record how many remote scripts have run since mount(): must be
        # exactly 1 (the plain unmount) when this callback fires, proving
        # order: plain-unmount -> callback -> remount.
        calls.append(len(backend.commands) - starting_commands)

    mgr.refresh(_refresh_lower)
    assert calls == [1]

    commands = [cmd for cmd, _timeout in backend.commands[starting_commands:]]
    assert len(commands) == 2, "expected exactly 2 remote scripts: pre-refresh unmount + remount"
    assert "overlay_pre_refresh_unmount.sh" in commands[0]
    assert "overlay_remount.sh" in commands[1]

    unmount_script = next(
        b for p, b in backend.files.items() if p.endswith("overlay_pre_refresh_unmount.sh")
    )
    assert "fusermount3 -u /cloud/merged" in unmount_script  # PLAIN unmount (not -uz)
    assert "-uz" not in unmount_script

    remount_script = next(
        b for p, b in backend.files.items() if p.endswith("overlay_remount.sh")
    )
    assert "fuse-overlayfs -o lowerdir=/cloud/lower" in remount_script
