from __future__ import annotations

from pathlib import Path

import pytest

from src.services.cloud_mount import RcloneMountManager
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
    assert (
        "'lowerdir=/cloud/low er,upperdir=/home/agent-host/.overlay/upper,workdir=/home/agent-host/.overlay/work'"
        in s
    )
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
    assert len(commands) == 2, (
        "expected exactly 2 remote scripts: pre-refresh unmount + remount"
    )
    assert "overlay_pre_refresh_unmount.sh" in commands[0]
    assert "overlay_remount.sh" in commands[1]

    unmount_script = next(
        b
        for p, b in backend.files.items()
        if p.endswith("overlay_pre_refresh_unmount.sh")
    )
    assert "fusermount3 -u /cloud/merged" in unmount_script  # PLAIN unmount (not -uz)
    assert "-uz" not in unmount_script

    remount_script = next(
        b for p, b in backend.files.items() if p.endswith("overlay_remount.sh")
    )
    assert "fuse-overlayfs -o lowerdir=/cloud/lower" in remount_script


def test_health_check_true_on_ok_false_on_enotconn():
    ok_backend = FakeRemoteBackend()
    assert _manager(ok_backend).health_check() is True

    dead = FakeRemoteBackend()
    dead.outputs_by_script["overlay_probe.sh"] = "__SRW_OVERLAY_DEAD__ ENOTCONN\n"
    assert _manager(dead).health_check() is False


def test_health_check_false_when_no_sentinel_in_output():
    backend = FakeRemoteBackend()
    backend.outputs_by_script["overlay_probe.sh"] = "garbled output with no sentinel\n"
    assert _manager(backend).health_check() is False


def test_heal_lazy_unmounts_overlay_first_then_remounts_lower_then_overlay():
    backend = FakeRemoteBackend()
    mgr = _manager(backend)
    mgr.mount()
    starting_commands = len(backend.commands)
    calls: list[int] = []

    def _remount_lower() -> None:
        # Record how many remote scripts have run since mount(): must be
        # exactly 1 (the lazy heal-unmount) when this callback fires, proving
        # order: lazy-unmount -> callback -> remount.
        calls.append(len(backend.commands) - starting_commands)

    mgr.heal(_remount_lower)
    assert calls == [1], "callback must fire after exactly 1 script (heal-unmount)"

    commands = [cmd for cmd, _timeout in backend.commands[starting_commands:]]
    assert len(commands) == 2, (
        "expected exactly 2 remote scripts: heal-unmount + remount"
    )
    assert "overlay_heal_unmount.sh" in commands[0]
    assert "overlay_remount.sh" in commands[1]

    unmount = next(
        b for p, b in backend.files.items() if p.endswith("overlay_heal_unmount.sh")
    )
    assert "fusermount3 -uz /cloud/merged" in unmount  # LAZY is correct on heal

    remount = next(
        b for p, b in backend.files.items() if p.endswith("overlay_remount.sh")
    )
    assert "fuse-overlayfs -o lowerdir=/cloud/lower" in remount
    assert "fusermount3 -u" not in remount  # no unmount folded in


def test_mount_body_script_freshens_workdir_not_upper():
    """Task 11: heal/refresh remounts must wipe+recreate the workdir (a
    fuse-overlayfs workdir must never be reused across mounts) but MUST NOT
    touch the upperdir — staged data has to survive a heal (design §11.2/§11.6)."""
    backend = FakeRemoteBackend()
    mgr = _manager(backend)
    mgr.mount()
    starting_commands = len(backend.commands)

    calls: list[int] = []

    def _remount_lower() -> None:
        calls.append(len(backend.commands) - starting_commands)

    mgr.heal(_remount_lower)
    assert calls == [1]

    remount_script = next(
        b for p, b in backend.files.items() if p.endswith("overlay_remount.sh")
    )
    assert "rm -rf /home/agent-host/.overlay/work" in remount_script
    assert "mkdir -p /home/agent-host/.overlay/work" in remount_script
    # the rm must land BEFORE the fuse-overlayfs mount line
    rm_idx = remount_script.index("rm -rf /home/agent-host/.overlay/work")
    mount_idx = remount_script.index("fuse-overlayfs -o")
    assert rm_idx < mount_idx
    # upperdir must NEVER be wiped by this script
    assert "rm -rf /home/agent-host/.overlay/upper" not in remount_script


def test_upperdir_usage_parses_du_and_flags_over_quota():
    backend = FakeRemoteBackend()
    backend.outputs_by_script["overlay_usage.sh"] = (
        "9663676416\t/home/agent-host/.overlay/upper\n__SRW_OVERLAY_OK__\n"
    )
    mgr = _manager(backend)  # quota_bytes = 8 GiB
    assert mgr.upperdir_usage_bytes() == 9663676416
    assert mgr.over_quota() is True
    assert mgr.quota_guard_message() is not None


def test_under_quota_has_no_guard_message():
    backend = FakeRemoteBackend()
    backend.outputs_by_script["overlay_usage.sh"] = (
        "1024\t/home/agent-host/.overlay/upper\n__SRW_OVERLAY_OK__\n"
    )
    mgr = _manager(backend)
    assert mgr.over_quota() is False
    assert mgr.quota_guard_message() is None


def test_upperdir_usage_skips_sentinel_and_junk_lines_falls_back_to_zero():
    backend = FakeRemoteBackend()
    backend.outputs_by_script["overlay_usage.sh"] = (
        "du: cannot access: No such file\n__SRW_OVERLAY_OK__\n"
    )
    mgr = _manager(backend)
    assert mgr.upperdir_usage_bytes() == 0
    assert mgr.over_quota() is False


def test_over_quota_false_when_quota_bytes_absent():
    backend = FakeRemoteBackend()
    backend.outputs_by_script["overlay_usage.sh"] = (
        "999999999999\t/home/agent-host/.overlay/upper\n__SRW_OVERLAY_OK__\n"
    )
    cfg = _cfg()
    del cfg["quota_bytes"]
    mgr = OverlayMountManager(
        thread_id="thread-12345678",
        overlay_cfg=cfg,
        workspace_backend=backend,
        workspace_root=Path("/home/agent-host/workspace"),
    )
    assert mgr.over_quota() is False
    assert mgr.quota_guard_message() is None


def test_over_quota_true_at_exact_quota_boundary():
    """used == quota_bytes must block AT the cap, not only strictly above it."""
    backend = FakeRemoteBackend()
    quota = 8 * 1024**3  # matches _cfg()'s quota_bytes
    backend.outputs_by_script["overlay_usage.sh"] = (
        f"{quota}\t/home/agent-host/.overlay/upper\n__SRW_OVERLAY_OK__\n"
    )
    mgr = _manager(backend)
    assert mgr.over_quota() is True
    assert mgr.quota_guard_message() is not None


def test_over_quota_false_when_quota_bytes_zero():
    backend = FakeRemoteBackend()
    backend.outputs_by_script["overlay_usage.sh"] = (
        "999999999999\t/home/agent-host/.overlay/upper\n__SRW_OVERLAY_OK__\n"
    )
    cfg = _cfg()
    cfg["quota_bytes"] = 0
    mgr = OverlayMountManager(
        thread_id="thread-12345678",
        overlay_cfg=cfg,
        workspace_backend=backend,
        workspace_root=Path("/home/agent-host/workspace"),
    )
    assert mgr.over_quota() is False
    assert mgr.quota_guard_message() is None


def test_protected_payload_mounts_lower_then_overlay():
    """B9 seam test: a protected _build_protected_cloud_mount-shaped payload
    drives RcloneMountManager (RO lower, skip_workspace_links) and then
    OverlayMountManager (capture overlay + workspace/cloud -> merged symlink)
    against ONE FakeRemoteBackend — mirrors what persistent_session's
    _setup_cloud_mount does when it stacks the two managers.
    """
    backend = FakeRemoteBackend()
    # This fixture's default exec_command output satisfies OverlayMountManager's
    # sentinel (__SRW_OVERLAY_OK__); RcloneMountManager checks a different one
    # (__SRW_RCLONE_MOUNT_OK__), so pin the rclone mount script's output too.
    backend.outputs_by_script["mount_srw-thread-t-lower.sh"] = (
        "__SRW_RCLONE_MOUNT_OK__\n"
    )
    cfg = {  # shape of _build_protected_cloud_mount output
        "driver": "rclone",
        "protected": True,
        "skip_workspace_links": True,
        "overlay": {
            "lower": "/cloud/lower",
            "upper": "/home/agent-host/.overlay/upper",
            "work": "/home/agent-host/.overlay/work",
            "merged": "/cloud/merged",
            "quota_bytes": 8 * 1024**3,
        },
        "mounts": [
            {
                "mount_id": "protected-t",
                "mount_kind": "protected_lower",
                "target_path": "/cloud/lower",
                "workspace_name": "lower",
                "access": "read_only",
                "backend": "nextcloud",
                "source": {
                    "type": "webdav",
                    "config": {
                        "url": "https://nc/x/",
                        "vendor": "nextcloud",
                        "user": "srw-reader-u",
                    },
                },
                "auth": {"type": "basic", "password": "p"},
            }
        ],
    }

    rclone_mgr = RcloneMountManager(
        thread_id="thread-t",
        cloud_cfg=cfg,
        workspace_backend=backend,
        workspace_root=Path("/home/agent-host/workspace"),
    )
    rclone_mgr._start_all_sync()
    assert rclone_mgr.active is True
    # overlay owns the symlink in protected mode -> rclone must not install one
    assert not any(p.endswith("install_cloud_links.sh") for p in backend.files)

    overlay_mgr = OverlayMountManager(
        thread_id="thread-t",
        overlay_cfg=cfg["overlay"],
        workspace_backend=backend,
        workspace_root=Path("/home/agent-host/workspace"),
    )
    overlay_mgr.mount()
    assert overlay_mgr.active is True
    assert any(p.endswith("overlay_mount.sh") for p in backend.files)
