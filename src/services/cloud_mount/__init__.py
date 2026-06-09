"""Agent-side lazy cloud mounts backed by rclone.

The orchestrator owns provider decisions and sends a generic ``cloud_mount``
payload. This module runs inside the agent process but performs setup on the
remote workspace runtime, so shell commands and workspace tools see the same
mounted filesystem.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_OK = "__SRW_RCLONE_MOUNT_OK__"
_FAILED = "__SRW_RCLONE_MOUNT_FAILED__"

_CACHE_FLAG_MAP = {
    "vfs_cache_mode": "--vfs-cache-mode",
    "vfs_cache_max_size": "--vfs-cache-max-size",
    "vfs_cache_max_age": "--vfs-cache-max-age",
    "vfs_cache_min_free_space": "--vfs-cache-min-free-space",
    "dir_cache_time": "--dir-cache-time",
    "poll_interval": "--poll-interval",
    "vfs_read_chunk_size": "--vfs-read-chunk-size",
    "vfs_read_chunk_size_limit": "--vfs-read-chunk-size-limit",
}

_DEFAULT_CACHE = {
    "vfs_cache_mode": "full",
    "vfs_cache_max_size": "10G",
    "vfs_cache_max_age": "24h",
    "vfs_cache_min_free_space": "5G",
    "dir_cache_time": "5m",
    "poll_interval": "1m",
    "vfs_read_chunk_size": "16M",
    "vfs_read_chunk_size_limit": "128M",
}


class RcloneMountError(RuntimeError):
    """Raised when a lazy cloud mount cannot be started."""


@dataclass(frozen=True)
class RcloneMountState:
    mount_id: str
    mount_kind: str
    target_path: str
    workspace_name: str
    remote_name: str
    state_dir: str
    cache_dir: str
    config_path: str
    pid_file: str
    rc_addr: str
    rc_user: str
    rc_pass: str


class RcloneMountManager:
    """Start and stop rclone mounts in a remote workspace runtime."""

    def __init__(
        self,
        *,
        thread_id: str,
        cloud_cfg: dict[str, Any],
        workspace_backend: Any,
        workspace_root: Path,
    ) -> None:
        self.thread_id = thread_id
        self.cloud_cfg = cloud_cfg or {}
        self.workspace_backend = workspace_backend
        self.workspace_root = str(workspace_root)
        self._states: list[RcloneMountState] = []

    @property
    def active(self) -> bool:
        return bool(self._states)

    @property
    def mounts(self) -> list[RcloneMountState]:
        return list(self._states)

    async def start_all(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._start_all_sync)

    async def aclose(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._close_sync)

    def _start_all_sync(self) -> None:
        if self.cloud_cfg.get("driver") != "rclone":
            raise RcloneMountError("cloud_mount driver must be 'rclone'")
        if not self.cloud_cfg.get("mounts"):
            return
        if not hasattr(self.workspace_backend, "exec_command"):
            raise RcloneMountError(
                "rclone cloud_mount requires a shell-capable workspace"
            )
        if not hasattr(self.workspace_backend, "write_home_file"):
            raise RcloneMountError("rclone cloud_mount requires remote home writes")

        states: list[RcloneMountState] = []
        for index, mount in enumerate(self.cloud_cfg.get("mounts") or []):
            state = self._state_for_mount(mount, index)
            script = self._mount_script(mount, state)
            self._run_remote_script(f"mount_{state.remote_name}.sh", script, timeout=90)
            states.append(state)
            logger.info(
                "rclone cloud mount started: thread=%s mount_id=%s target=%s",
                self.thread_id,
                state.mount_id,
                state.target_path,
            )

        self._install_workspace_links(states)
        self._states = states

    def _state_for_mount(self, mount: dict[str, Any], index: int) -> RcloneMountState:
        mount_id = str(mount.get("mount_id") or f"mount-{index}")
        workspace_name = str(mount.get("workspace_name") or f"mount-{index}")
        safe_name = "".join(
            ch.lower() if ch.isalnum() else "-" for ch in workspace_name
        ).strip("-")
        safe_name = safe_name or f"mount-{index}"
        remote_name = f"srw-{self.thread_id[:8]}-{safe_name}"
        state_rel = f".cache/srw/rclone/{self.thread_id}/{safe_name}"
        state_dir = self.workspace_backend.resolve_home_path(state_rel)
        cache_dir = f"{state_dir}/vfs-cache"
        config_path = f"{state_dir}/rclone.conf"
        pid_file = f"{state_dir}/rclone.pid"
        port = 43000 + (
            int(
                hashlib.sha256(f"{self.thread_id}:{index}".encode()).hexdigest()[:8],
                16,
            )
            % 10000
        )
        return RcloneMountState(
            mount_id=mount_id,
            mount_kind=str(mount.get("mount_kind") or "cloud"),
            target_path=str(mount.get("target_path") or f"/cloud/{safe_name}"),
            workspace_name=workspace_name,
            remote_name=remote_name,
            state_dir=state_dir,
            cache_dir=cache_dir,
            config_path=config_path,
            pid_file=pid_file,
            rc_addr=f"127.0.0.1:{port}",
            rc_user=f"srw-{self.thread_id[:8]}",
            rc_pass=secrets.token_urlsafe(24),
        )

    def _mount_script(self, mount: dict[str, Any], state: RcloneMountState) -> str:
        source = mount.get("source") or {}
        source_type = str(source.get("type") or "")
        source_config = dict(source.get("config") or {})
        auth = mount.get("auth") or {}
        if auth.get("type") == "basic" and auth.get("password"):
            source_config["pass"] = auth["password"]
        if not source_type or not source_config:
            raise RcloneMountError(f"mount {state.mount_id} has no rclone source")

        create_args = [
            "rclone",
            "--config",
            state.config_path,
            "config",
            "create",
            state.remote_name,
            source_type,
        ]
        for key, value in source_config.items():
            create_args.extend([str(key), str(value)])
        create_args.extend(["--obscure", "--non-interactive"])

        cache = {**_DEFAULT_CACHE, **dict(mount.get("cache") or {})}
        mount_args = [
            "nohup",
            "rclone",
            "--config",
            state.config_path,
            "--cache-dir",
            state.cache_dir,
            "mount",
            self._remote_path(state.remote_name, str(source.get("root") or "")),
            state.target_path,
            "--rc",
            "--rc-addr",
            state.rc_addr,
            "--rc-user",
            state.rc_user,
            "--rc-pass",
            state.rc_pass,
            "--daemon-timeout",
            "30s",
        ]
        for key, flag in _CACHE_FLAG_MAP.items():
            if cache.get(key):
                mount_args.extend([flag, str(cache[key])])

        log_file = f"{state.state_dir}/rclone.log"
        mount_command = " ".join(shlex.quote(arg) for arg in mount_args)
        create_command = " ".join(shlex.quote(arg) for arg in create_args)
        target_parent = str(Path(state.target_path).parent)

        return f"""#!/usr/bin/env bash
set -euo pipefail
umask 077
trap 'rc=$?; echo "{_FAILED} rc=${{rc}}"; exit "${{rc}}"' ERR

command -v rclone >/dev/null
mkdir -p {shlex.quote(state.state_dir)} {shlex.quote(state.cache_dir)}
if ! mkdir -p {shlex.quote(target_parent)} {shlex.quote(state.target_path)} 2>/dev/null; then
  sudo mkdir -p {shlex.quote(target_parent)} {shlex.quote(state.target_path)}
  sudo chown "$(id -u):$(id -g)" /cloud {shlex.quote(target_parent)} {shlex.quote(state.target_path)}
fi

if mountpoint -q {shlex.quote(state.target_path)}; then
  fusermount3 -u {shlex.quote(state.target_path)} 2>/dev/null || fusermount -u {shlex.quote(state.target_path)} 2>/dev/null || true
fi
if mountpoint -q {shlex.quote(state.target_path)}; then
  fusermount3 -uz {shlex.quote(state.target_path)} 2>/dev/null || fusermount -uz {shlex.quote(state.target_path)} 2>/dev/null || true
fi

{create_command} >/dev/null
chmod 600 {shlex.quote(state.config_path)}

{mount_command} > {shlex.quote(log_file)} 2>&1 &
echo "$!" > {shlex.quote(state.pid_file)}

for _i in $(seq 1 30); do
  if mountpoint -q {shlex.quote(state.target_path)}; then
    echo "{_OK}"
    exit 0
  fi
  if ! kill -0 "$(cat {shlex.quote(state.pid_file)})" 2>/dev/null; then
    break
  fi
  sleep 1
done

cat {shlex.quote(log_file)} 2>/dev/null || true
exit 1
"""

    def _install_workspace_links(self, states: list[RcloneMountState]) -> None:
        script_lines = [
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            f"workspace={shlex.quote(self.workspace_root)}",
            'mkdir -p "${workspace}/.srw"',
            'entry="${workspace}/cloud"',
            'if [ -L "${entry}" ]; then rm "${entry}"; fi',
            'if [ -e "${entry}" ] && [ ! -d "${entry}" ]; then '
            'mv "${entry}" "${workspace}/.srw/cloud.pre-rclone.$(date +%s)"; fi',
        ]
        if len(states) == 1:
            script_lines.extend(
                [
                    'if [ -d "${entry}" ] && [ ! -L "${entry}" ]; then '
                    'mv "${entry}" "${workspace}/.srw/cloud.pre-rclone.$(date +%s)"; fi',
                    f"ln -sfn {shlex.quote(states[0].target_path)} " + '"${entry}"',
                ]
            )
        else:
            script_lines.append('mkdir -p "${entry}"')
            for state in states:
                script_lines.extend(
                    [
                        f'link="${{entry}}/{state.workspace_name}"',
                        'if [ -L "${link}" ]; then rm "${link}"; fi',
                        f"ln -sfn {shlex.quote(state.target_path)} " + '"${link}"',
                    ]
                )
        script_lines.append(f'echo "{_OK}"')
        self._run_remote_script("install_cloud_links.sh", "\n".join(script_lines))

    def _close_sync(self) -> None:
        for state in reversed(self._states):
            try:
                self._run_remote_script(
                    f"unmount_{state.remote_name}.sh",
                    self._unmount_script(state),
                    timeout=45,
                    require_ok=False,
                )
            except Exception:
                logger.debug(
                    "rclone cloud mount cleanup failed for %s",
                    state.mount_id,
                    exc_info=True,
                )
        self._states = []

    def _unmount_script(self, state: RcloneMountState) -> str:
        return f"""#!/usr/bin/env bash
set +e
rclone rc --rc-addr {shlex.quote(state.rc_addr)} --rc-user {shlex.quote(state.rc_user)} --rc-pass {shlex.quote(state.rc_pass)} core/quit >/dev/null 2>&1
sleep 2
if mountpoint -q {shlex.quote(state.target_path)}; then
  fusermount3 -u {shlex.quote(state.target_path)} 2>/dev/null || fusermount -u {shlex.quote(state.target_path)} 2>/dev/null
fi
if mountpoint -q {shlex.quote(state.target_path)}; then
  fusermount3 -uz {shlex.quote(state.target_path)} 2>/dev/null || fusermount -uz {shlex.quote(state.target_path)} 2>/dev/null
fi
if [ -s {shlex.quote(state.pid_file)} ]; then
  pid="$(cat {shlex.quote(state.pid_file)})"
  if kill -0 "${{pid}}" 2>/dev/null; then
    kill "${{pid}}" 2>/dev/null
    sleep 2
    kill -9 "${{pid}}" 2>/dev/null
  fi
fi
echo "{_OK}"
"""

    def _run_remote_script(
        self,
        name: str,
        script: str,
        *,
        timeout: int = 30,
        require_ok: bool = True,
    ) -> str:
        rel_path = f".cache/srw/rclone/{self.thread_id}/scripts/{name}"
        self.workspace_backend.write_home_file(rel_path, script)
        script_path = self.workspace_backend.resolve_home_path(rel_path)
        command = (
            f"chmod 700 {shlex.quote(script_path)} && "
            f"bash {shlex.quote(script_path)}; "
            f"rc=$?; rm -f {shlex.quote(script_path)}; exit $rc"
        )
        output = self.workspace_backend.exec_command(command, timeout=timeout)
        if require_ok and _OK not in output:
            raise RcloneMountError(output.strip() or "rclone mount failed")
        if _FAILED in output and require_ok:
            raise RcloneMountError(output.strip() or "rclone mount failed")
        return output

    @staticmethod
    def _remote_path(remote_name: str, root: str) -> str:
        root = root.strip("/")
        return f"{remote_name}:{root}" if root else f"{remote_name}:"


__all__ = ["RcloneMountError", "RcloneMountManager", "RcloneMountState"]
