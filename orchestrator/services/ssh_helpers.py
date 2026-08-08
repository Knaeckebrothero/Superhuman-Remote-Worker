"""Shared SSH helpers for agent-host workspace operations.

Provides the agent SSH command builder and a memory-safe streaming snapshot
extractor used by workspace/IDE session restore.

Memory note: ``stream_extract_snapshot`` passes the local snapshot file to the
child process as its stdin file descriptor, so the kernel feeds it to ``ssh``
directly — the orchestrator never loads the tarball into its heap. This is the
fix for the resume-time OOM where ``f.read()`` + ``communicate(input=...)``
buffered the whole ``.tar.zst`` in RAM. Capture (snapshot_service) already
streams; this brings restore to the same O(1)-memory footprint.

Import-light by design (stdlib + Paramiko + ``resolve_ssh_key_path`` — no boto3
or DB), so it is safe to import from tests and any service module.
"""

import asyncio
import base64
import hashlib
import ipaddress
import logging
import os
from pathlib import Path
from typing import Optional

import paramiko

from services import resolve_ssh_key_path

logger = logging.getLogger(__name__)


class SSHPrivateKeyError(RuntimeError):
    """Configured workspace private key is absent, unreadable, or invalid."""


def workspace_private_key_fingerprint(key_path: Optional[str]) -> str:
    """Validate a workspace private key and return its public SHA256 fingerprint.

    Validation runs as the current process, so it catches real mount/UID
    permission problems. Only the public fingerprint is returned; key bytes and
    parser details are never logged or included in an error.
    """
    if not key_path:
        raise SSHPrivateKeyError(
            "Workspace SSH private key is not configured (SSH_KEY_PATH is empty)"
        )

    path = Path(key_path)
    if not path.is_file():
        raise SSHPrivateKeyError(
            f"Workspace SSH private key file does not exist: {path}"
        )
    try:
        # Open explicitly before parsing so the diagnostic names an actual
        # current-UID readability problem rather than a generic auth failure.
        with path.open("rb"):
            pass
    except OSError as exc:
        raise SSHPrivateKeyError(
            f"Workspace SSH private key is not readable by this process: {path}"
        ) from exc

    try:
        private_key = paramiko.PKey.from_path(path)
    except (paramiko.SSHException, OSError, ValueError) as exc:
        raise SSHPrivateKeyError(
            "Workspace SSH private key is invalid or passphrase-protected"
        ) from exc

    digest = hashlib.sha256(private_key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


# Remote command run on the agent host to inflate + unpack a snapshot.
# --xattrs/--acls so fuse-overlayfs opaque-dir xattrs + whiteouts round-trip
# (protected cloud mode, design §11.3). char(0,0) whiteouts survive without
# them on emptyDir, but opaque markers and some rootfs variants need them.
# `bash -c 'set -o pipefail; ...'` so a masked `zstd -d` decompression
# failure on a corrupt/truncated archive is no longer hidden by `tar` (the
# last stage) exiting 0 — see docs/features/workspace_durability_tiering.md
# §C1 (C1c). `tar` is already the last stage, so its own rc handling is
# unchanged (including the benign full-extract rc==2 below); `pipefail` only
# adds "an earlier stage failing also fails the pipeline." Plain `pipefail`
# is deliberate here, not PIPESTATUS discrimination — unlike capture's
# `tar | zstd` (C1b), there is no tar-rc==1-style benign code from an
# earlier stage to tolerate. `bash -c` guarantees `pipefail` support
# regardless of the agent-host login shell (`pipefail` is bash/ksh/zsh, not
# POSIX sh/dash). The `-c` body is single-quoted, so `--xattrs-include`
# switches to double quotes to still reach tar as a literal `*`.
EXTRACT_REMOTE_CMD = (
    "bash -c 'set -o pipefail; "
    'zstd -d | tar --xattrs --xattrs-include="*" --acls -xf - -C /\''
)

# Scoped variant for in-cluster IDE pods: extract only the agent-host home.
# Snapshots also carry /usr/local (VM restores need it), but in a
# workspace-image pod those files are root-owned and image-provided —
# extracting them as agent-host yields per-file "Cannot open: File exists"
# noise and tar rc=2 while the home content extracts fine. Members are
# archived without a leading slash (tar strips it at capture), so the
# member pattern is ``home/agent-host``. Same `pipefail` wrapper as
# EXTRACT_REMOTE_CMD above, for the same masked-zstd-failure reason.
EXTRACT_HOME_REMOTE_CMD = (
    "bash -c 'set -o pipefail; zstd -d | tar -xf - -C / home/agent-host'"
)

# Headscale/Tailscale mesh address space (CGNAT range). VM workspaces get
# their SSH host from this range; only tailnet members (agent-pod sidecars)
# can route to it.
_TAILNET_NET = ipaddress.ip_network("100.64.0.0/10")


def is_tailnet_addr(host: Optional[str]) -> bool:
    """True when ``host`` is an IP inside the tailnet range (100.64.0.0/10).

    Hostnames and non-tailnet IPs return False.
    """
    if not host:
        return False
    try:
        return ipaddress.ip_address(host) in _TAILNET_NET
    except ValueError:
        return False


def orchestrator_can_reach(host: Optional[str]) -> bool:
    """Whether the orchestrator process can open a TCP/SSH connection to ``host``.

    The orchestrator pod is NOT a tailnet member — it has no route to
    100.64.0.0/10, so SSH to VM workspaces from here black-holes (see
    docs/issues/vm_ssh_readiness_probe_unroutable_from_orchestrator.md).
    Callers must skip tailnet targets visibly instead of timing out quietly.

    Escape hatch: set ORCHESTRATOR_HAS_TAILNET_ROUTE=true on deployments that
    give the orchestrator tailnet membership (e.g. a future tailscale sidecar).
    """
    if not is_tailnet_addr(host):
        return True
    return os.getenv("ORCHESTRATOR_HAS_TAILNET_ROUTE", "").lower() == "true"


def build_agent_ssh_cmd(
    ssh_host: str,
    ssh_port: int,
    remote_cmd: str,
    *,
    key_path: Optional[str] = None,
    connect_timeout_s: int = 10,
    batch_mode: bool = False,
) -> list[str]:
    """Build the SSH argv used to run ``remote_cmd`` on an agent host.

    Mirrors the options used across snapshot capture/restore. If ``key_path``
    is None it is resolved via ``resolve_ssh_key_path()``; an empty key path
    omits the ``-i`` flag. ``batch_mode`` is useful for readiness probes where
    auth prompts must fail fast instead of hanging the probe.
    """
    if key_path is None:
        key_path = resolve_ssh_key_path()
    return [
        "ssh",
        *(["-i", key_path] if key_path else []),
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        f"ConnectTimeout={int(connect_timeout_s)}",
        *(
            [
                "-o",
                "BatchMode=yes",
                "-o",
                "IdentitiesOnly=yes",
                "-o",
                "PreferredAuthentications=publickey",
            ]
            if batch_mode
            else []
        ),
        "-p",
        str(ssh_port),
        f"agent-host@{ssh_host}",
        remote_cmd,
    ]


async def stream_extract_snapshot(
    ssh_host: str,
    ssh_port: int,
    tar_path: str,
    *,
    key_path: Optional[str] = None,
    remote_cmd: str = EXTRACT_REMOTE_CMD,
) -> tuple[int, bytes]:
    """Stream a local ``.tar.zst`` into ``zstd -d | tar -xf - -C /`` over SSH.

    The file is handed to the child as its stdin file descriptor, so the kernel
    streams it to ``ssh`` without the orchestrator ever holding the snapshot in
    memory (O(1) memory regardless of snapshot size).

    Returns ``(returncode, stderr_bytes)``; callers handle logging.
    """
    if not orchestrator_can_reach(ssh_host):
        # Tailnet target — SSH from the orchestrator would black-hole. Fail
        # fast and visibly instead of hanging on a doomed connect (see
        # docs/issues/vm_ssh_readiness_probe_unroutable_from_orchestrator.md).
        logger.info(
            "Skipping snapshot restore to %s:%d: orchestrator has no route "
            "to tailnet targets",
            ssh_host,
            ssh_port,
        )
        return 255, b"skipped: unroutable tailnet target from orchestrator"

    ssh_cmd = build_agent_ssh_cmd(ssh_host, ssh_port, remote_cmd, key_path=key_path)
    with open(tar_path, "rb") as f:
        proc = await asyncio.create_subprocess_exec(
            *ssh_cmd,
            stdin=f,  # OS streams the fd to the child; never read into our heap
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _stdout, stderr = await proc.communicate()
    return proc.returncode, stderr


async def wait_for_agent_ssh(
    ssh_host: str,
    ssh_port: int,
    *,
    deadline_s: float,
    connect_timeout_s: int,
    interval_s: float,
    key_path: Optional[str] = None,
) -> tuple[bool, int, str]:
    """Poll until ``agent-host@ssh_host`` accepts an authenticated SSH command.

    Returns ``(ready, attempts, last_error)``. The probe proves route + sshd +
    key authorization by running ``true`` with ``BatchMode=yes``.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, float(deadline_s))
    attempts = 0
    last_error = ""

    while True:
        attempts += 1
        ssh_cmd = build_agent_ssh_cmd(
            ssh_host,
            ssh_port,
            "true",
            key_path=key_path,
            connect_timeout_s=connect_timeout_s,
            batch_mode=True,
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                *ssh_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            return (
                False,
                attempts,
                f"ssh readiness probe could not start: {type(exc).__name__}",
            )
        try:
            _stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=max(1, int(connect_timeout_s)) + 5
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            stderr = b"ssh readiness probe process timed out"

        if proc.returncode == 0:
            return True, attempts, ""

        last_error = stderr.decode("utf-8", errors="replace").strip()
        if len(last_error) > 500:
            last_error = last_error[-500:]
        if loop.time() >= deadline:
            return False, attempts, last_error or f"ssh exited {proc.returncode}"

        remaining = max(0.0, deadline - loop.time())
        await asyncio.sleep(min(max(0.0, float(interval_s)), remaining))
