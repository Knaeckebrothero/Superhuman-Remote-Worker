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


def test_reset_upper_script_order_and_content():
    backend = FakeRemoteBackend()
    mgr = _manager(backend)
    mgr.mount()
    starting_commands = len(backend.commands)

    mgr.reset_upper(refresh_lower=lambda: None)

    commands = [cmd for cmd, _timeout in backend.commands[starting_commands:]]
    assert len(commands) == 3, (
        "expected exactly 3 remote scripts: reset-unmount, wipe-upper, remount"
    )
    assert "overlay_reset_unmount.sh" in commands[0]
    assert "overlay_wipe_upper.sh" in commands[1]
    assert "overlay_remount.sh" in commands[2]

    wipe_script = next(
        b for p, b in backend.files.items() if p.endswith("overlay_wipe_upper.sh")
    )
    # rm -rf of BOTH upper and work
    assert "rm -rf /home/agent-host/.overlay/upper /home/agent-host/.overlay/work" in wipe_script
    # mkdir -p of both, recreated fresh
    assert "mkdir -p /home/agent-host/.overlay/upper /home/agent-host/.overlay/work" in wipe_script
    # NEVER touch the merged mountpoint or the lower
    assert "/cloud/merged" not in wipe_script
    assert "/cloud/lower" not in wipe_script

    remount_script = next(
        b for p, b in backend.files.items() if p.endswith("overlay_remount.sh")
    )
    assert "fuse-overlayfs -o lowerdir=/cloud/lower" in remount_script


def test_reset_upper_unmount_plain_u_with_uz_fallback():
    backend = FakeRemoteBackend()
    mgr = _manager(backend)
    mgr.mount()

    mgr.reset_upper(refresh_lower=lambda: None)

    unmount_script = next(
        b for p, b in backend.files.items() if p.endswith("overlay_reset_unmount.sh")
    )
    assert "fusermount3 -u /cloud/merged" in unmount_script
    assert "fusermount3 -uz /cloud/merged" in unmount_script
    # the plain attempt must be tried before the lazy fallback
    plain_idx = unmount_script.index("fusermount3 -u /cloud/merged")
    lazy_idx = unmount_script.index("fusermount3 -uz /cloud/merged")
    assert plain_idx < lazy_idx
    assert "||" in unmount_script


def test_reset_upper_refresh_lower_called_between_wipe_and_remount():
    backend = FakeRemoteBackend()
    mgr = _manager(backend)
    mgr.mount()
    starting_commands = len(backend.commands)
    calls: list[int] = []

    def _refresh_lower() -> None:
        # Must fire after exactly 2 scripts (reset-unmount + wipe-upper),
        # before the remount (3rd script).
        calls.append(len(backend.commands) - starting_commands)

    mgr.reset_upper(refresh_lower=_refresh_lower)
    assert calls == [2]


def test_reset_upper_raises_on_remount_failure():
    backend = FakeRemoteBackend()
    backend.outputs_by_script["overlay_remount.sh"] = "__SRW_OVERLAY_FAILED__ rc=3\n"
    mgr = _manager(backend)
    mgr.mount()

    with pytest.raises(OverlayMountError):
        mgr.reset_upper(refresh_lower=lambda: None)
