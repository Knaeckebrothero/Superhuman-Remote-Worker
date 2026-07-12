"""Tests for RcloneMountManager.restart_mount (Task 11 — ENOTCONN heal path).

Copies the FakeRemoteBackend fixture used across tests/cloud_mount (see
test_rclone_mount_manager.py) rather than importing it, matching this
package's existing convention of one fake per test module.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.services.cloud_mount import RcloneMountError, RcloneMountManager


class FakeRemoteBackend:
    def __init__(self, *, root: str | None = None) -> None:
        self.files: dict[str, str] = {}
        self.commands: list[tuple[str, int]] = []
        self.outputs_by_script: dict[str, str] = {}
        if root is not None:
            self.root = root

    def resolve_home_path(self, relative_path: str) -> str:
        return f"/home/agent-host/{relative_path}"

    def write_home_file(self, relative_path: str, content: str | bytes) -> None:
        self.files[relative_path] = (
            content.decode("utf-8") if isinstance(content, bytes) else content
        )

    def exec_command(self, command: str, timeout: int = 30) -> str:
        self.commands.append((command, timeout))
        for script_name, output in self.outputs_by_script.items():
            if script_name in command:
                return output
        return "__SRW_RCLONE_MOUNT_OK__\n"


def _cloud_mount_cfg() -> dict:
    """Two mounts so 'that mount only' isolation is actually exercised."""
    return {
        "version": 1,
        "driver": "rclone",
        "cloud_root": "/cloud",
        "workspace_entry": "cloud",
        "mounts": [
            {
                "mount_id": "protected-thread-1",
                "mount_kind": "protected_lower",
                "target_path": "/cloud/lower",
                "workspace_name": "lower",
                "access": "read_only",
                "backend": "nextcloud",
                "source": {
                    "type": "webdav",
                    "config": {
                        "url": "https://nc.test/remote.php/dav/files/reader/",
                        "vendor": "nextcloud",
                        "user": "srw-reader-u",
                    },
                },
                "auth": {"type": "basic", "password": "reader-pass"},
            },
            {
                "mount_id": "legacy-session",
                "mount_kind": "session_folder",
                "target_path": "/cloud/home",
                "workspace_name": "home",
                "backend": "nextcloud",
                "source": {
                    "type": "webdav",
                    "config": {
                        "url": "https://nc.test/remote.php/dav/files/agent/session/",
                        "vendor": "nextcloud",
                        "user": "agent-service",
                    },
                },
                "auth": {"type": "basic", "password": "app-pass"},
            },
        ],
    }


def _manager(backend) -> RcloneMountManager:
    return RcloneMountManager(
        thread_id="thread-12345678",
        cloud_cfg=_cloud_mount_cfg(),
        workspace_backend=backend,
        workspace_root=Path("/home/agent-host/workspace"),
    )


def test_restart_mount_runs_unmount_then_mount_scripts_for_that_mount_only():
    backend = FakeRemoteBackend()
    manager = _manager(backend)
    manager._start_all_sync()
    starting_commands = len(backend.commands)

    manager.restart_mount("protected-thread-1")

    commands = [cmd for cmd, _timeout in backend.commands[starting_commands:]]
    assert len(commands) == 2, "expected exactly 2 remote scripts: unmount then mount"
    assert "unmount_srw-thread-1-lower.sh" in commands[0]
    assert "mount_srw-thread-1-lower.sh" in commands[1]
    # the other mount must be completely untouched by the restart
    assert not any("lower" not in c and "home" in c for c in commands)
    assert not any("srw-thread-1-home" in c for c in commands)

    unmount_script = next(
        b
        for p, b in backend.files.items()
        if p.endswith("unmount_srw-thread-1-lower.sh")
    )
    assert "fusermount3 -u /cloud/lower" in unmount_script

    mount_script = next(
        b for p, b in backend.files.items() if p.endswith("mount_srw-thread-1-lower.sh")
    )
    assert "config create srw-thread-1-lower webdav" in mount_script
    assert "reader-pass" in mount_script


def test_restart_mount_unknown_mount_id_raises():
    backend = FakeRemoteBackend()
    manager = _manager(backend)
    manager._start_all_sync()

    with pytest.raises(RcloneMountError):
        manager.restart_mount("does-not-exist")


def test_restart_mount_raises_when_remount_script_fails():
    backend = FakeRemoteBackend()
    manager = _manager(backend)
    manager._start_all_sync()
    backend.outputs_by_script["mount_srw-thread-1-lower.sh"] = (
        "__SRW_RCLONE_MOUNT_FAILED__ rc=1\n"
    )

    with pytest.raises(RcloneMountError):
        manager.restart_mount("protected-thread-1")


def test_restart_mount_before_any_mount_started_raises():
    backend = FakeRemoteBackend()
    manager = _manager(backend)
    # never called _start_all_sync()

    with pytest.raises(RcloneMountError):
        manager.restart_mount("protected-thread-1")
