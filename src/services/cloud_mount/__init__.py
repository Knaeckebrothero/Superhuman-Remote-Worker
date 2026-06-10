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
import os
import re
import secrets
import shlex
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..keycloak_token import KeycloakTokenClient

logger = logging.getLogger(__name__)

_OK = "__SRW_RCLONE_MOUNT_OK__"
_FAILED = "__SRW_RCLONE_MOUNT_FAILED__"
_CACHE_USAGE = "__SRW_RCLONE_CACHE_USAGE__"
_RC_CORE = "__SRW_RCLONE_RC_CORE__"
_RC_VFS = "__SRW_RCLONE_RC_VFS__"

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
    "dir_cache_time": "5m",
    "poll_interval": "1m",
    "vfs_read_chunk_size": "16M",
    "vfs_read_chunk_size_limit": "128M",
    "hard_cache_limit": "20G",
}

# Bearer-token auth types resolved through the shared Keycloak token client
# (OpenCloud). The orchestrator never sends a static credential for these —
# the agent process mints short-lived tokens and pushes them to the
# workspace runtime, where rclone reads them via ``bearer_token_command``.
_KEYCLOAK_AUTH_TYPES = {
    "keycloak_client_credentials",
    "keycloak_user_impersonation",
}
# Re-push tokens this long before expiry; never spin faster than the floor.
_TOKEN_REFRESH_SAFETY_SECONDS = 60.0
_TOKEN_REFRESH_MIN_INTERVAL_SECONDS = 30.0

_SIZE_UNITS = {
    "": 1,
    "B": 1,
    "K": 1024,
    "KB": 1024,
    "M": 1024**2,
    "MB": 1024**2,
    "G": 1024**3,
    "GB": 1024**3,
    "T": 1024**4,
    "TB": 1024**4,
}

_CLOUDIGNORE_HELPERS = r"""
compile_cloudignore() {
  local src="$1"
  local dest="$2"
  awk '
    function trim(s) {
      gsub(/^[ \t]+|[ \t]+$/, "", s)
      return s
    }
    {
      sub(/\r$/, "", $0)
      line = trim($0)
      if (line == "" || line ~ /^#/) next
      if (line ~ /^!/) next
      if (line ~ /(^|\/)\.\.($|\/)/) next
      gsub(/^\/+/, "", line)
      if (line == "") next
      if (line ~ /\/$/) {
        sub(/\/+$/, "", line)
        if (line == "") next
        print line "/**"
        if (line !~ /\//) print "**/" line "/**"
      } else {
        print line
        if (line !~ /\// && line !~ /[*?]/ && index(line, "[") == 0) print "**/" line
      }
    }
  ' "$src" >> "$dest"
}
"""


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
    filter_path: str
    pid_file: str
    rc_addr: str
    rc_user: str
    rc_pass: str
    hard_cache_limit: str
    hard_cache_limit_bytes: int
    # Home-relative twin of state_dir, for write_home_file pushes.
    state_rel: str = ""
    # Bearer-token plumbing (Keycloak auth modes only).
    token_path: str = ""
    token_helper_path: str = ""
    uses_keycloak_auth: bool = False


@dataclass(frozen=True)
class RcloneCacheUsage:
    mount_id: str
    workspace_name: str
    target_path: str
    cache_dir: str
    cache_bytes: int
    hard_limit_bytes: int
    mounted: bool

    @property
    def over_hard_limit(self) -> bool:
        return self.hard_limit_bytes > 0 and self.cache_bytes >= self.hard_limit_bytes


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
        remote_root = getattr(workspace_backend, "root", None)
        self.workspace_root = str(remote_root or workspace_root)
        self._states: list[RcloneMountState] = []
        # Keycloak bearer plumbing, keyed by mount index in cloud_cfg order
        # (same enumerate order as _start_all_sync, which is all-or-nothing,
        # so self._states[i] corresponds to mount index i).
        self._token_clients: dict[int, KeycloakTokenClient] = {}
        self._initial_tokens: dict[int, str] = {}
        self._refresh_task: asyncio.Task | None = None

    @property
    def active(self) -> bool:
        return bool(self._states)

    @property
    def mounts(self) -> list[RcloneMountState]:
        return list(self._states)

    def status(self) -> str:
        """Return an agent-safe status summary for active cloud mounts."""
        if not self._states:
            return "No active rclone cloud mounts."
        try:
            rows = self._collect_status_sync()
        except Exception as exc:
            return f"Cloud mount status unavailable: {exc}"

        by_id = {row.get("mount_id"): row for row in rows}
        lines = ["Cloud mount status:"]
        for state in self._states:
            row = by_id.get(state.mount_id, {})
            cache_bytes = int(row.get("cache_bytes") or 0)
            mounted = row.get("mounted") == "yes"
            limit = state.hard_cache_limit_bytes
            usage = f"{_format_bytes(cache_bytes)}" + (
                f" / {_format_bytes(limit)} hard limit" if limit else ""
            )
            status = "mounted" if mounted else "not mounted"
            if limit and cache_bytes >= limit:
                status = "cache limit reached"
            lines.append(
                f"- {state.workspace_name} ({state.mount_kind}) at "
                f"{state.target_path}: {status}, cache {usage}"
            )
            core = str(row.get("rc_core") or "").strip()
            vfs = str(row.get("rc_vfs") or "").strip()
            if core:
                lines.append(f"  core/stats: {_compact_rc(core)}")
            if vfs:
                lines.append(f"  vfs/stats: {_compact_rc(vfs)}")
        return "\n".join(lines)

    def cache_usages(self) -> list[RcloneCacheUsage]:
        """Return current per-mount VFS cache usage."""
        if not self._states:
            return []
        return self._collect_cache_usage_sync()

    def cache_limit_message(self) -> str | None:
        """Return a blocking message when any mount is over its hard cache guard."""
        breaches = [usage for usage in self.cache_usages() if usage.over_hard_limit]
        if not breaches:
            return None

        lines = [
            "Cloud cache guard: this command was not run because the rclone "
            "VFS cache has reached its hard limit.",
            "",
        ]
        for usage in breaches:
            lines.append(
                f"- {usage.workspace_name} at {usage.target_path}: "
                f"{_format_bytes(usage.cache_bytes)} used, "
                f"limit {_format_bytes(usage.hard_limit_bytes)}"
            )
        lines.extend(
            [
                "",
                "Wait for rclone cache cleanup, narrow the cloud operation, "
                "or ask the operator to increase the one-time/cloud budget.",
            ]
        )
        return "\n".join(lines)

    async def start_all(self) -> None:
        await self._prepare_keycloak_tokens()
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, self._start_all_sync)
        except Exception:
            await self._aclose_token_clients()
            raise
        self._start_token_refresh()

    async def aclose(self) -> None:
        if self._refresh_task is not None:
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.debug("cloud mount token refresh task exit", exc_info=True)
            self._refresh_task = None
        await self._aclose_token_clients()
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._close_sync)

    # ------------------------------------------------------- Keycloak bearers

    async def _prepare_keycloak_tokens(self) -> None:
        """Mint initial bearer tokens for Keycloak-auth mounts.

        Runs in the agent's event loop *before* the blocking mount setup so
        the generated mount scripts can seed the workspace-side token file.
        The client secret never leaves the agent process — only the
        short-lived access token is shipped to the workspace runtime.
        """
        for index, mount in enumerate(self.cloud_cfg.get("mounts") or []):
            auth = mount.get("auth") or {}
            if auth.get("type") not in _KEYCLOAK_AUTH_TYPES:
                continue
            issuer = str(auth.get("issuer") or "")
            client_id = str(auth.get("client_id") or "")
            client_secret = str(auth.get("client_secret") or "")
            if not (issuer and client_id and client_secret):
                raise RcloneMountError(
                    f"mount {mount.get('mount_id') or index}: keycloak auth "
                    "payload is missing issuer/client_id/client_secret"
                )
            client = KeycloakTokenClient(
                issuer=issuer,
                client_id=client_id,
                client_secret=client_secret,
                target_user_sub=str(auth.get("target_user_sub") or "") or None,
            )
            try:
                bearer = await client.get_bearer()
            except Exception as exc:
                await client.aclose()
                await self._aclose_token_clients()
                raise RcloneMountError(
                    f"mount {mount.get('mount_id') or index}: could not mint "
                    f"initial bearer token: {exc}"
                ) from exc
            self._token_clients[index] = client
            self._initial_tokens[index] = bearer.token

    async def _aclose_token_clients(self) -> None:
        for client in self._token_clients.values():
            try:
                await client.aclose()
            except Exception:
                pass
        self._token_clients.clear()
        self._initial_tokens.clear()

    def _start_token_refresh(self) -> None:
        if not self._token_clients or self._refresh_task is not None:
            return
        self._refresh_task = asyncio.create_task(
            self._token_refresh_loop(),
            name=f"cloud-mount-token-refresh-{self.thread_id[:8]}",
        )

    def _next_refresh_delay(self) -> float:
        ttls = []
        for client in self._token_clients.values():
            # Same monotonic clock the token client stamps expiry with.
            expires_at = getattr(client, "_token_expires_at", 0.0)
            ttls.append(max(0.0, expires_at - time.monotonic()))
        if not ttls:
            return _TOKEN_REFRESH_MIN_INTERVAL_SECONDS
        return max(
            _TOKEN_REFRESH_MIN_INTERVAL_SECONDS,
            min(ttls) - _TOKEN_REFRESH_SAFETY_SECONDS,
        )

    async def _token_refresh_loop(self) -> None:
        """Re-mint and push bearer tokens before they expire.

        rclone re-runs ``bearer_token_command`` when a request 401s, so a
        push that lands late self-heals on the next remote call. Failures
        here are logged and retried — never fatal to the session.
        """
        loop = asyncio.get_running_loop()
        delay = self._next_refresh_delay()
        while True:
            try:
                await asyncio.sleep(delay)
                for index, client in self._token_clients.items():
                    state = self._state_for_index(index)
                    if state is None or not state.uses_keycloak_auth:
                        continue
                    bearer = await client.get_bearer(force_refresh=True)
                    await loop.run_in_executor(
                        None, self._push_token_sync, state, bearer.token
                    )
                delay = self._next_refresh_delay()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "cloud mount token refresh failed (thread=%s); retrying in %ss",
                    self.thread_id,
                    _TOKEN_REFRESH_MIN_INTERVAL_SECONDS,
                    exc_info=True,
                )
                delay = _TOKEN_REFRESH_MIN_INTERVAL_SECONDS

    def _state_for_index(self, index: int) -> RcloneMountState | None:
        if 0 <= index < len(self._states):
            return self._states[index]
        return None

    def _push_token_sync(self, state: RcloneMountState, token: str) -> None:
        """Atomically replace the workspace-side token file.

        Written to a sibling tmp path first, then ``mv``-ed over the live
        file so rclone's helper never reads a half-written token. chmod is
        re-applied because the tmp file is created with default SFTP mode.
        """
        tmp_rel = f"{state.state_rel}/bearer.token.new"
        self.workspace_backend.write_home_file(tmp_rel, token + "\n")
        tmp_abs = self.workspace_backend.resolve_home_path(tmp_rel)
        self.workspace_backend.exec_command(
            f"chmod 600 {shlex.quote(tmp_abs)} && "
            f"mv -f {shlex.quote(tmp_abs)} {shlex.quote(state.token_path)}",
            timeout=15,
        )

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
            script = self._mount_script(
                mount, state, initial_token=self._initial_tokens.get(index)
            )
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
        cache = self._cache_for_mount(mount)
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
        filter_path = f"{state_dir}/rclone-excludes.txt"
        pid_file = f"{state_dir}/rclone.pid"
        hard_cache_limit = str(cache.get("hard_cache_limit") or "")
        hard_cache_limit_bytes = _parse_size_to_bytes(hard_cache_limit) or 0
        port = 43000 + (
            int(
                hashlib.sha256(f"{self.thread_id}:{index}".encode()).hexdigest()[:8],
                16,
            )
            % 10000
        )
        auth_type = str((mount.get("auth") or {}).get("type") or "")
        return RcloneMountState(
            mount_id=mount_id,
            mount_kind=str(mount.get("mount_kind") or "cloud"),
            target_path=str(mount.get("target_path") or f"/cloud/{safe_name}"),
            workspace_name=workspace_name,
            remote_name=remote_name,
            state_dir=state_dir,
            cache_dir=cache_dir,
            config_path=config_path,
            filter_path=filter_path,
            pid_file=pid_file,
            rc_addr=f"127.0.0.1:{port}",
            rc_user=f"srw-{self.thread_id[:8]}",
            rc_pass=secrets.token_urlsafe(24),
            hard_cache_limit=hard_cache_limit,
            hard_cache_limit_bytes=hard_cache_limit_bytes,
            state_rel=state_rel,
            token_path=f"{state_dir}/bearer.token",
            token_helper_path=f"{state_dir}/bearer-helper.sh",
            uses_keycloak_auth=auth_type in _KEYCLOAK_AUTH_TYPES,
        )

    def _mount_script(
        self,
        mount: dict[str, Any],
        state: RcloneMountState,
        *,
        initial_token: str | None = None,
    ) -> str:
        source = mount.get("source") or {}
        source_type = str(source.get("type") or "")
        source_config = dict(source.get("config") or {})
        auth = mount.get("auth") or {}
        token_setup_block = ":"
        if auth.get("type") == "basic" and auth.get("password"):
            source_config["pass"] = auth["password"]
        elif auth.get("type") in _KEYCLOAK_AUTH_TYPES:
            if not initial_token:
                raise RcloneMountError(
                    f"mount {state.mount_id}: no initial bearer token prepared "
                    "for keycloak auth"
                )
            # rclone executes this helper to obtain (and on 401, re-obtain)
            # the bearer; the agent-side refresh loop keeps the token file
            # it reads fresh.
            source_config["bearer_token_command"] = state.token_helper_path
            token_setup_block = self._token_setup_block(state, initial_token)
        if not source_type or not source_config:
            raise RcloneMountError(f"mount {state.mount_id} has no rclone source")
        min_rclone_version = str(mount.get("min_rclone_version") or "").strip()
        version_check_block = (
            self._version_check_block(min_rclone_version) if min_rclone_version else ":"
        )

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

        cache = self._cache_for_mount(mount)
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
        cache_flag_lines = []
        for key, flag in _CACHE_FLAG_MAP.items():
            if cache.get(key):
                cache_flag_lines.append(
                    "append_mount_flag "
                    f"{shlex.quote(flag)} {shlex.quote(str(cache[key]))}"
                )
        cache_flag_block = "\n".join(cache_flag_lines) if cache_flag_lines else ":"
        if str(mount.get("access") or "").lower() == "read_only":
            mount_args.append("--read-only")
        for flag in mount.get("provider_flags") or []:
            if flag:
                mount_args.append(str(flag))

        log_file = f"{state.state_dir}/rclone.log"
        mount_array = self._bash_array("MOUNT_ARGS", mount_args)
        create_command = " ".join(shlex.quote(arg) for arg in create_args)
        cloudignore_ref = self._remote_child_path(
            state.remote_name, str(source.get("root") or ""), ".cloudignore"
        )
        target_parent = str(Path(state.target_path).parent)
        default_ignore_file = f"{state.state_dir}/default.cloudignore"
        default_ignore_content = "\n".join(self._default_ignore_lines(mount))
        default_ignore_block = self._write_text_file_block(
            default_ignore_file, default_ignore_content
        )

        return f"""#!/usr/bin/env bash
set -euo pipefail
umask 077
trap 'rc=$?; echo "{_FAILED} rc=${{rc}}"; exit "${{rc}}"' ERR

command -v rclone >/dev/null
{version_check_block}
mkdir -p {shlex.quote(state.state_dir)} {shlex.quote(state.cache_dir)}
if ! mkdir -p {shlex.quote(target_parent)} {shlex.quote(state.target_path)} 2>/dev/null; then
  sudo mkdir -p {shlex.quote(target_parent)} {shlex.quote(state.target_path)}
  sudo chown "$(id -u):$(id -g)" /cloud {shlex.quote(target_parent)} {shlex.quote(state.target_path)}
fi
{token_setup_block}

if mountpoint -q {shlex.quote(state.target_path)}; then
  fusermount3 -u {shlex.quote(state.target_path)} 2>/dev/null || fusermount -u {shlex.quote(state.target_path)} 2>/dev/null || true
fi
if mountpoint -q {shlex.quote(state.target_path)}; then
  fusermount3 -uz {shlex.quote(state.target_path)} 2>/dev/null || fusermount -uz {shlex.quote(state.target_path)} 2>/dev/null || true
fi

{create_command} >/dev/null
chmod 600 {shlex.quote(state.config_path)}

{_CLOUDIGNORE_HELPERS}
: > {shlex.quote(state.filter_path)}
{default_ignore_block}
if [ -s {shlex.quote(default_ignore_file)} ]; then
  compile_cloudignore {shlex.quote(default_ignore_file)} {shlex.quote(state.filter_path)}
fi
if rclone --config {shlex.quote(state.config_path)} cat {shlex.quote(cloudignore_ref)} > {shlex.quote(state.state_dir + "/cloudignore.raw")} 2>/dev/null; then
  compile_cloudignore {shlex.quote(state.state_dir + "/cloudignore.raw")} {shlex.quote(state.filter_path)}
fi
sort -u {shlex.quote(state.filter_path)} -o {shlex.quote(state.filter_path)}

{mount_array}
MOUNT_HELP="$(rclone mount --help 2>/dev/null || true)"
append_mount_flag() {{
  local flag="$1"
  local value="$2"
  if [ -z "${{MOUNT_HELP}}" ] || grep -Fq -- "${{flag}}" <<<"${{MOUNT_HELP}}"; then
    MOUNT_ARGS+=("${{flag}}" "${{value}}")
  else
    printf 'Skipping unsupported rclone mount flag: %s\\n' "${{flag}}" >&2
  fi
}}
{cache_flag_block}
if [ -s {shlex.quote(state.filter_path)} ]; then
  MOUNT_ARGS+=(--exclude-from {shlex.quote(state.filter_path)})
fi
"${{MOUNT_ARGS[@]}}" > {shlex.quote(log_file)} 2>&1 &
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

    def _cache_for_mount(self, mount: dict[str, Any]) -> dict[str, Any]:
        return {**_DEFAULT_CACHE, **dict(mount.get("cache") or {})}

    def _default_ignore_lines(self, mount: dict[str, Any]) -> list[str]:
        lines: list[str] = []
        for value in (
            os.getenv("SRW_CLOUD_MOUNT_DEFAULT_IGNORES"),
            self.cloud_cfg.get("default_ignores"),
            (mount.get("filters") or {}).get("default_ignores"),
            (mount.get("filters") or {}).get("exclude"),
            (mount.get("filters") or {}).get("excludes"),
        ):
            lines.extend(_coerce_ignore_lines(value))
        return lines

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
rm -f {shlex.quote(state.token_path)} {shlex.quote(state.token_helper_path)}
echo "{_OK}"
"""

    def _collect_cache_usage_sync(self) -> list[RcloneCacheUsage]:
        output = self._run_remote_script(
            "cloud_cache_usage.sh",
            self._cache_usage_script(include_rc=False),
            timeout=30,
        )
        usages: list[RcloneCacheUsage] = []
        for row in self._parse_status_rows(output):
            usages.append(
                RcloneCacheUsage(
                    mount_id=str(row.get("mount_id") or ""),
                    workspace_name=str(row.get("workspace_name") or ""),
                    target_path=str(row.get("target_path") or ""),
                    cache_dir=str(row.get("cache_dir") or ""),
                    cache_bytes=int(row.get("cache_bytes") or 0),
                    hard_limit_bytes=int(row.get("hard_limit_bytes") or 0),
                    mounted=row.get("mounted") == "yes",
                )
            )
        return usages

    def _collect_status_sync(self) -> list[dict[str, Any]]:
        output = self._run_remote_script(
            "cloud_mount_status.sh",
            self._cache_usage_script(include_rc=True),
            timeout=30,
        )
        return self._parse_status_rows(output)

    def _cache_usage_script(self, *, include_rc: bool) -> str:
        lines = [
            "#!/usr/bin/env bash",
            "set -euo pipefail",
        ]
        for state in self._states:
            lines.extend(
                [
                    f"bytes=$(du -sb {shlex.quote(state.cache_dir)} 2>/dev/null | awk '{{print $1}}' || true)",
                    'bytes="${bytes:-0}"',
                    'mounted="no"',
                    f'if mountpoint -q {shlex.quote(state.target_path)}; then mounted="yes"; fi',
                    (
                        "printf '%s\\t%s\\t%s\\t%s\\t%s\\t%s\\t%s\\t%s\\t%s\\n' "
                        f"{shlex.quote(_CACHE_USAGE)} "
                        f"{shlex.quote(state.mount_id)} "
                        f"{shlex.quote(state.workspace_name)} "
                        f"{shlex.quote(state.target_path)} "
                        f"{shlex.quote(state.cache_dir)} "
                        '"${bytes}" '
                        f"{shlex.quote(str(state.hard_cache_limit_bytes))} "
                        f"{shlex.quote(state.hard_cache_limit)} "
                        '"${mounted}"'
                    ),
                ]
            )
            if include_rc:
                lines.extend(
                    [
                        "core_stats=$(rclone rc "
                        f"--rc-addr {shlex.quote(state.rc_addr)} "
                        f"--rc-user {shlex.quote(state.rc_user)} "
                        f"--rc-pass {shlex.quote(state.rc_pass)} "
                        "core/stats 2>/dev/null | tr '\\n' ' ' || true)",
                        (
                            "printf '%s\\t%s\\t%s\\n' "
                            f"{shlex.quote(_RC_CORE)} "
                            f"{shlex.quote(state.mount_id)} "
                            '"${core_stats}"'
                        ),
                        "vfs_stats=$(rclone rc "
                        f"--rc-addr {shlex.quote(state.rc_addr)} "
                        f"--rc-user {shlex.quote(state.rc_user)} "
                        f"--rc-pass {shlex.quote(state.rc_pass)} "
                        "vfs/stats 2>/dev/null | tr '\\n' ' ' || true)",
                        (
                            "printf '%s\\t%s\\t%s\\n' "
                            f"{shlex.quote(_RC_VFS)} "
                            f"{shlex.quote(state.mount_id)} "
                            '"${vfs_stats}"'
                        ),
                    ]
                )
        lines.append(f'echo "{_OK}"')
        return "\n".join(lines)

    def _parse_status_rows(self, output: str) -> list[dict[str, Any]]:
        rows: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        for line in output.splitlines():
            if line.startswith(f"{_CACHE_USAGE}\t"):
                parts = line.split("\t", 8)
                if len(parts) != 9:
                    continue
                (
                    _marker,
                    mount_id,
                    workspace_name,
                    target_path,
                    cache_dir,
                    cache_bytes,
                    hard_limit_bytes,
                    hard_limit,
                    mounted,
                ) = parts
                row = rows.setdefault(mount_id, {"mount_id": mount_id})
                if mount_id not in order:
                    order.append(mount_id)
                row.update(
                    {
                        "workspace_name": workspace_name,
                        "target_path": target_path,
                        "cache_dir": cache_dir,
                        "cache_bytes": int(cache_bytes or 0),
                        "hard_limit_bytes": int(hard_limit_bytes or 0),
                        "hard_limit": hard_limit,
                        "mounted": mounted,
                    }
                )
            elif line.startswith(f"{_RC_CORE}\t"):
                parts = line.split("\t", 2)
                if len(parts) == 3:
                    rows.setdefault(parts[1], {"mount_id": parts[1]})["rc_core"] = (
                        parts[2]
                    )
            elif line.startswith(f"{_RC_VFS}\t"):
                parts = line.split("\t", 2)
                if len(parts) == 3:
                    rows.setdefault(parts[1], {"mount_id": parts[1]})["rc_vfs"] = parts[
                        2
                    ]
        return [rows[mount_id] for mount_id in order]

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

    @staticmethod
    def _remote_child_path(remote_name: str, root: str, child: str) -> str:
        root = root.strip("/")
        child = child.lstrip("/")
        return f"{remote_name}:{root}/{child}" if root else f"{remote_name}:{child}"

    @staticmethod
    def _bash_array(name: str, args: list[str]) -> str:
        return f"{name}=(" + " ".join(shlex.quote(arg) for arg in args) + ")"

    def _token_setup_block(self, state: RcloneMountState, token: str) -> str:
        """Seed the workspace-side bearer token file + read-only helper.

        The helper only ever ``cat``s the token file; minting/refreshing
        stays in the agent process. Created before ``rclone config create``
        because the first remote call (the ``.cloudignore`` fetch) already
        needs the bearer.
        """
        helper_script = (
            "#!/usr/bin/env bash\n" + f"exec cat {shlex.quote(state.token_path)}\n"
        )
        return "\n".join(
            [
                self._write_text_file_block(state.token_path, token),
                f"chmod 600 {shlex.quote(state.token_path)}",
                self._write_text_file_block(state.token_helper_path, helper_script),
                f"chmod 700 {shlex.quote(state.token_helper_path)}",
            ]
        )

    @staticmethod
    def _version_check_block(min_version: str) -> str:
        """Fail fast with a clear message on too-old workspace rclone.

        A mount failure here does not renegotiate the orchestrator's
        fallback decision, so silent vendor misbehavior on an old rclone
        would be confusing — better to name the version gap outright.
        """
        return "\n".join(
            [
                f"required_ver={shlex.quote(min_version)}",
                'have_ver="$(rclone version 2>/dev/null'
                " | sed -nE '1s/^rclone v([0-9]+(\\.[0-9]+)*).*/\\1/p')\"",
                'if [ -z "${have_ver}" ]; then',
                '  echo "cannot determine rclone version on the workspace runtime" >&2',
                "  exit 1",
                "fi",
                'if [ "$(printf \'%s\\n%s\\n\' "${required_ver}" "${have_ver}"'
                ' | sort -V | head -n1)" != "${required_ver}" ]; then',
                '  echo "rclone ${have_ver} on the workspace runtime is older than'
                " ${required_ver} required by this cloud mount -"
                ' update the workspace image" >&2',
                "  exit 1",
                "fi",
            ]
        )

    @staticmethod
    def _write_text_file_block(path: str, content: str) -> str:
        if not content:
            return f": > {shlex.quote(path)}"
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]
        delimiter = f"SRW_CLOUDIGNORE_{digest}"
        return (
            f"cat > {shlex.quote(path)} <<'{delimiter}'\n"
            f"{content.rstrip()}\n"
            f"{delimiter}"
        )


def _parse_size_to_bytes(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    text = str(value).strip()
    if not text:
        return None
    match = re.fullmatch(r"(?i)\s*(\d+(?:\.\d+)?)\s*([kmgt]?b?)?\s*", text)
    if not match:
        return None
    number = float(match.group(1))
    unit = (match.group(2) or "").upper()
    if unit not in _SIZE_UNITS:
        return None
    return int(number * _SIZE_UNITS[unit])


def _format_bytes(value: int) -> str:
    size = float(max(value, 0))
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{value} B"


def _compact_rc(value: str, max_chars: int = 240) -> str:
    compact = " ".join(value.split())
    if len(compact) <= max_chars:
        return compact
    return compact[: max_chars - 3] + "..."


def _coerce_ignore_lines(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        raw: list[str] = []
        for chunk in value.replace(",", "\n").splitlines():
            raw.append(chunk)
    elif isinstance(value, (list, tuple, set)):
        raw = [str(item) for item in value]
    else:
        raw = [str(value)]
    return [line.strip() for line in raw if line and line.strip()]


__all__ = [
    "RcloneCacheUsage",
    "RcloneMountError",
    "RcloneMountManager",
    "RcloneMountState",
]
