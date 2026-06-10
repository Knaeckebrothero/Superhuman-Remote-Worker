from __future__ import annotations

from pathlib import Path

import pytest

import src.services.cloud_mount as cloud_mount_module
from src.services.cloud_mount import RcloneMountError, RcloneMountManager
from src.services.keycloak_token import BearerToken


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
                "cache": {"vfs_cache_max_size": "10G", "hard_cache_limit": "20G"},
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
    assert "cat srw-thread-1-home:.cloudignore" in script
    assert "MOUNT_ARGS+=(--exclude-from" in script
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


def test_cache_flags_are_gated_by_rclone_mount_help():
    cfg = _cloud_mount_cfg()
    cfg["mounts"][0]["cache"]["vfs_cache_min_free_space"] = "5G"
    backend = FakeRemoteBackend()
    manager = RcloneMountManager(
        thread_id="thread-12345678",
        cloud_cfg=cfg,
        workspace_backend=backend,
        workspace_root=Path("/home/agent-host/workspace"),
    )

    manager._start_all_sync()

    script = next(
        body
        for path, body in backend.files.items()
        if path.endswith("mount_srw-thread-1-home.sh")
    )
    mount_array = script.split("MOUNT_ARGS=(", 1)[1].split(")\n", 1)[0]
    assert "--vfs-cache-min-free-space" not in mount_array
    assert "MOUNT_HELP=" in script
    assert "grep -Fq --" in script
    assert "append_mount_flag --vfs-cache-min-free-space 5G" in script


def test_workspace_link_uses_remote_backend_root_when_available():
    backend = FakeRemoteBackend(root="/home/agent-host/workspace")
    manager = RcloneMountManager(
        thread_id="thread-12345678",
        cloud_cfg=_cloud_mount_cfg(),
        workspace_backend=backend,
        workspace_root=Path("/workspace"),
    )

    manager._start_all_sync()

    link_script = next(
        body
        for path, body in backend.files.items()
        if path.endswith("install_cloud_links.sh")
    )
    assert "workspace=/home/agent-host/workspace" in link_script
    assert "workspace=/workspace" not in link_script


def test_mount_script_applies_default_filters_and_read_only_flag():
    cfg = _cloud_mount_cfg()
    cfg["default_ignores"] = ["Photos/", "*.iso"]
    cfg["mounts"][0]["access"] = "read_only"
    cfg["mounts"][0]["filters"] = {"exclude": ["Videos/"]}
    backend = FakeRemoteBackend()
    manager = RcloneMountManager(
        thread_id="thread-12345678",
        cloud_cfg=cfg,
        workspace_backend=backend,
        workspace_root=Path("/home/agent-host/workspace"),
    )

    manager._start_all_sync()

    script = next(
        body
        for path, body in backend.files.items()
        if path.endswith("mount_srw-thread-1-home.sh")
    )
    assert "compile_cloudignore" in script
    assert "Photos/" in script
    assert "*.iso" in script
    assert "Videos/" in script
    assert "--read-only" in script


def test_cache_limit_message_blocks_when_hard_limit_reached():
    backend = FakeRemoteBackend()
    backend.outputs_by_script["cloud_cache_usage.sh"] = (
        "__SRW_RCLONE_CACHE_USAGE__\tlegacy-session\thome\t/cloud/home\t"
        "/home/agent-host/.cache/srw/rclone/thread-12345678/home/vfs-cache\t"
        "21474836480\t21474836480\t20G\tyes\n"
        "__SRW_RCLONE_MOUNT_OK__\n"
    )
    manager = RcloneMountManager(
        thread_id="thread-12345678",
        cloud_cfg=_cloud_mount_cfg(),
        workspace_backend=backend,
        workspace_root=Path("/home/agent-host/workspace"),
    )
    manager._start_all_sync()

    message = manager.cache_limit_message()

    assert message is not None
    assert "Cloud cache guard" in message
    assert "home at /cloud/home" in message
    assert "20.0 GiB used" in message


def test_status_reports_cache_and_rc_stats_without_credentials():
    backend = FakeRemoteBackend()
    backend.outputs_by_script["cloud_mount_status.sh"] = (
        "__SRW_RCLONE_CACHE_USAGE__\tlegacy-session\thome\t/cloud/home\t"
        "/home/agent-host/.cache/srw/rclone/thread-12345678/home/vfs-cache\t"
        "1048576\t21474836480\t20G\tyes\n"
        '__SRW_RCLONE_RC_CORE__\tlegacy-session\t{"bytes":1048576}\n'
        '__SRW_RCLONE_RC_VFS__\tlegacy-session\t{"cacheSize":1048576}\n'
        "__SRW_RCLONE_MOUNT_OK__\n"
    )
    manager = RcloneMountManager(
        thread_id="thread-12345678",
        cloud_cfg=_cloud_mount_cfg(),
        workspace_backend=backend,
        workspace_root=Path("/home/agent-host/workspace"),
    )
    manager._start_all_sync()

    status = manager.status()

    assert "Cloud mount status" in status
    assert "mounted, cache 1.0 MiB / 20.0 GiB hard limit" in status
    assert 'core/stats: {"bytes":1048576}' in status
    assert manager.mounts[0].rc_pass not in status


# ------------------------------------------------------- Keycloak bearer auth


def _opencloud_mount_cfg() -> dict:
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
                "backend": "opencloud",
                "source": {
                    "type": "webdav",
                    "config": {
                        "url": "https://oc.test/dav/spaces/drive-1/sessions/t1/",
                        "vendor": "infinitescale",
                    },
                },
                "auth": {
                    "type": "keycloak_client_credentials",
                    "issuer": "https://kc.test/realms/srw",
                    "client_id": "opencloud-orchestrator",
                    "client_secret": "kc-secret",
                },
                "cache": {"vfs_cache_max_size": "10G", "hard_cache_limit": "20G"},
                "min_rclone_version": "1.70.0",
            }
        ],
    }


class FakeTokenClient:
    instances: list["FakeTokenClient"] = []

    def __init__(
        self,
        *,
        issuer: str,
        client_id: str,
        client_secret: str,
        target_user_sub: str | None = None,
        **_: object,
    ) -> None:
        self.issuer = issuer
        self.client_id = client_id
        self.client_secret = client_secret
        self.target_user_sub = target_user_sub
        self.minted = 0
        self.closed = False
        self._token_expires_at = 10**9  # far-future monotonic deadline
        FakeTokenClient.instances.append(self)

    async def get_bearer(self, force_refresh: bool = False) -> BearerToken:
        self.minted += 1
        return BearerToken(
            token=f"tok-{self.minted}", expires_at=self._token_expires_at
        )

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture()
def fake_token_client(monkeypatch):
    FakeTokenClient.instances = []
    monkeypatch.setattr(cloud_mount_module, "KeycloakTokenClient", FakeTokenClient)
    yield FakeTokenClient
    FakeTokenClient.instances = []


@pytest.mark.asyncio
async def test_keycloak_mount_uses_bearer_token_command(fake_token_client):
    backend = FakeRemoteBackend()
    manager = RcloneMountManager(
        thread_id="thread-12345678",
        cloud_cfg=_opencloud_mount_cfg(),
        workspace_backend=backend,
        workspace_root=Path("/home/agent-host/workspace"),
    )

    await manager.start_all()
    try:
        script = next(
            body
            for path, body in backend.files.items()
            if path.endswith("mount_srw-thread-1-home.sh")
        )
        state = manager.mounts[0]
        assert state.uses_keycloak_auth is True
        # rclone remote authenticates via the helper, not a static credential.
        assert "bearer_token_command" in script
        assert state.token_helper_path in script
        assert "tok-1" in script  # initial token seeded into the token file
        assert f"chmod 600 {state.token_path}" in script
        assert "pass " not in script.split("config create", 1)[1].split("\n", 1)[0]
        # The Keycloak client secret must never reach the workspace runtime.
        for body in backend.files.values():
            assert "kc-secret" not in body
        for command, _timeout in backend.commands:
            assert "kc-secret" not in command
        # Version preflight gates old runtimes with a clear error.
        assert "required_ver=1.70.0" in script
        assert "sort -V" in script
        # Refresh loop is running for the keycloak mount.
        assert manager._refresh_task is not None
        assert fake_token_client.instances[0].minted == 1
    finally:
        await manager.aclose()

    assert fake_token_client.instances[0].closed is True
    assert manager._refresh_task is None


@pytest.mark.asyncio
async def test_keycloak_auth_missing_fields_raises(fake_token_client):
    cfg = _opencloud_mount_cfg()
    del cfg["mounts"][0]["auth"]["client_secret"]
    manager = RcloneMountManager(
        thread_id="thread-12345678",
        cloud_cfg=cfg,
        workspace_backend=FakeRemoteBackend(),
        workspace_root=Path("/home/agent-host/workspace"),
    )
    with pytest.raises(RcloneMountError, match="issuer/client_id/client_secret"):
        await manager.start_all()


def test_mount_script_requires_prepared_token():
    manager = RcloneMountManager(
        thread_id="thread-12345678",
        cloud_cfg=_opencloud_mount_cfg(),
        workspace_backend=FakeRemoteBackend(),
        workspace_root=Path("/home/agent-host/workspace"),
    )
    # _start_all_sync without the async token prep must refuse keycloak mounts.
    with pytest.raises(RcloneMountError, match="no initial bearer token"):
        manager._start_all_sync()


def test_push_token_writes_tmp_then_moves_atomically():
    backend = FakeRemoteBackend()
    manager = RcloneMountManager(
        thread_id="thread-12345678",
        cloud_cfg=_opencloud_mount_cfg(),
        workspace_backend=backend,
        workspace_root=Path("/home/agent-host/workspace"),
    )
    manager._initial_tokens[0] = "tok-seed"
    manager._start_all_sync()
    state = manager.mounts[0]

    manager._push_token_sync(state, "tok-fresh")

    tmp_rel = f"{state.state_rel}/bearer.token.new"
    assert backend.files[tmp_rel] == "tok-fresh\n"
    last_command, _timeout = backend.commands[-1]
    assert "chmod 600" in last_command
    assert "mv -f" in last_command
    assert state.token_path in last_command


def test_unmount_script_removes_token_files():
    backend = FakeRemoteBackend()
    manager = RcloneMountManager(
        thread_id="thread-12345678",
        cloud_cfg=_opencloud_mount_cfg(),
        workspace_backend=backend,
        workspace_root=Path("/home/agent-host/workspace"),
    )
    manager._initial_tokens[0] = "tok-seed"
    manager._start_all_sync()
    state = manager.mounts[0]

    script = manager._unmount_script(state)

    assert f"rm -f {state.token_path} {state.token_helper_path}" in script
