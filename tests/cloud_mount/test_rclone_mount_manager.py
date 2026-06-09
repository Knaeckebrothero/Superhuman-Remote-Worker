from __future__ import annotations

from pathlib import Path

from src.services.cloud_mount import RcloneMountManager


class FakeRemoteBackend:
    def __init__(self) -> None:
        self.files: dict[str, str] = {}
        self.commands: list[tuple[str, int]] = []

    def resolve_home_path(self, relative_path: str) -> str:
        return f"/home/agent-host/{relative_path}"

    def write_home_file(self, relative_path: str, content: str | bytes) -> None:
        self.files[relative_path] = (
            content.decode("utf-8") if isinstance(content, bytes) else content
        )

    def exec_command(self, command: str, timeout: int = 30) -> str:
        self.commands.append((command, timeout))
        return "__SRW_RCLONE_MOUNT_OK__\n"


def _cloud_mount_cfg() -> dict:
    return {
        "version": 1,
        "driver": "rclone",
        "cloud_root": "/cloud",
        "workspace_entry": "cloud",
        "mounts": [
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
                "cache": {"vfs_cache_max_size": "10G"},
            }
        ],
    }


def test_starts_rclone_mount_and_installs_workspace_symlink():
    backend = FakeRemoteBackend()
    manager = RcloneMountManager(
        thread_id="thread-12345678",
        cloud_cfg=_cloud_mount_cfg(),
        workspace_backend=backend,
        workspace_root=Path("/home/agent-host/workspace"),
    )

    manager._start_all_sync()

    assert manager.active is True
    assert len(manager.mounts) == 1
    mount_scripts = [
        body
        for path, body in backend.files.items()
        if path.endswith("mount_srw-thread-1-home.sh")
    ]
    assert len(mount_scripts) == 1
    script = mount_scripts[0]
    assert "rclone --config" in script
    assert "config create srw-thread-1-home webdav" in script
    assert "--vfs-cache-mode full" in script
    assert "--vfs-cache-max-size 10G" in script
    assert "fusermount3 -u /cloud/home" in script
    assert "/cloud/home" in script
    assert "app-pass" in script

    link_scripts = [
        body
        for path, body in backend.files.items()
        if path.endswith("install_cloud_links.sh")
    ]
    assert len(link_scripts) == 1
    assert "ln -sfn /cloud/home" in link_scripts[0]
    assert "${workspace}/cloud" in link_scripts[0]
