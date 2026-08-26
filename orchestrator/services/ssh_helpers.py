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
import secrets
import shlex
import tempfile
from pathlib import Path
from typing import Optional

import paramiko

from services import resolve_ssh_key_path

logger = logging.getLogger(__name__)


class SSHPrivateKeyError(RuntimeError):
    """Configured workspace private key is absent, unreadable, or invalid."""


async def _read_stream_tail(stream, *, limit: int = 64 * 1024) -> bytes:
    """Drain a child pipe continuously while retaining only a bounded tail."""

    tail = bytearray()
    while True:
        chunk = await stream.read(16 * 1024)
        if not chunk:
            break
        tail.extend(chunk)
        if len(tail) > limit:
            del tail[: len(tail) - limit]
    return bytes(tail)


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
# last stage) exiting 0 — see knowledge-base/knowledge/features/workspace_durability_tiering.md
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
# EXTRACT_REMOTE_CMD above, for the same masked-zstd-failure reason. Retain
# xattrs/ACLs on the scoped home restore as well.
EXTRACT_HOME_REMOTE_CMD = (
    "bash -c 'set -o pipefail; "
    'zstd -d | tar --xattrs --xattrs-include="*" --acls '
    "-xf - -C / home/agent-host'"
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
    knowledge-base/knowledge/issues/vm_ssh_readiness_probe_unroutable_from_orchestrator.md).
    Callers must skip tailnet targets visibly instead of timing out quietly.

    Escape hatch: set ORCHESTRATOR_HAS_TAILNET_ROUTE=true on deployments that
    give the orchestrator tailnet membership (e.g. a future tailscale sidecar).
    """
    if os.getenv("VM_MODE", "off").strip().lower() == "same-cluster":
        return True
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
    known_hosts_path: Optional[str] = None,
) -> list[str]:
    """Build the SSH argv used to run ``remote_cmd`` on an agent host.

    Mirrors the options used across snapshot capture/restore. If ``key_path``
    is None it is resolved via ``resolve_ssh_key_path()``; an empty key path
    omits the ``-i`` flag. ``batch_mode`` is useful for readiness probes where
    auth prompts must fail fast instead of hanging the probe.
    """
    if key_path is None:
        key_path = resolve_ssh_key_path()
    host_key_options = (
        [
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={known_hosts_path}",
        ]
        if known_hosts_path
        else [
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
        ]
    )
    return [
        "ssh",
        *(["-i", key_path] if key_path else []),
        *host_key_options,
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


def _fingerprint_host_key(encoded_key: str) -> str:
    """Return the OpenSSH SHA256 fingerprint for one base64 public-key blob."""

    key_bytes = base64.b64decode(encoded_key.encode("ascii"), validate=True)
    digest = hashlib.sha256(key_bytes).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


async def _scan_pinned_host_key(
    ssh_host: str,
    ssh_port: int,
    expected_fingerprint: str,
    *,
    timeout_s: int = 10,
) -> tuple[Optional[str], bytes]:
    """Fetch one host key, verify its fingerprint, and return its known-hosts line.

    The returned public line is still enforced by the subsequent ``ssh``
    process, closing the scan/connect key-swap window. A scan is never treated
    as trust by itself.
    """

    encoded_fingerprint = (
        expected_fingerprint[len("SHA256:") :]
        if expected_fingerprint.startswith("SHA256:")
        else ""
    )
    if len(encoded_fingerprint) != 43 or any(
        char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
        for char in encoded_fingerprint
    ):
        return None, b"invalid pinned SSH host-key fingerprint"

    try:
        proc = await asyncio.create_subprocess_exec(
            "ssh-keyscan",
            "-T",
            str(max(1, int(timeout_s))),
            "-p",
            str(int(ssh_port)),
            "-t",
            "ed25519",
            ssh_host,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        return None, f"SSH host-key scan could not start: {type(exc).__name__}".encode()

    try:
        try:
            stdout, _stderr = await asyncio.wait_for(
                proc.communicate(), timeout=max(1, int(timeout_s)) + 5
            )
        except asyncio.TimeoutError:
            return None, b"SSH host-key scan timed out"
    finally:
        # Cancellation of the lifecycle owner must not orphan a keyscan child
        # while the advisory lock is released to a successor.
        if proc.returncode is None:
            proc.kill()
            await proc.wait()

    saw_candidate = False
    for raw_line in stdout.splitlines():
        line = raw_line.decode("ascii", errors="ignore").strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        if len(fields) != 3 or fields[1] != "ssh-ed25519":
            continue
        try:
            actual = _fingerprint_host_key(fields[2])
        except (ValueError, UnicodeError):
            continue
        saw_candidate = True
        if secrets.compare_digest(actual, expected_fingerprint):
            return line, b""

    if saw_candidate:
        # The server PRESENTED an ed25519 key and it is not ours. This exact
        # wording is an identity verdict: vm_readiness demotes a ready VM on
        # it, and test_thread_uploads_stateless asserts it. Keep it stable.
        return None, b"SSH server host key did not match the pinned fingerprint"
    # No ed25519 key was presented at all: unreachable host, closed port, or
    # sshd not yet up. That is an AVAILABILITY outcome, not an identity one —
    # the wording must not contain "fingerprint" or vm_readiness would demote
    # a ready VM on every blip (the k3d gate proved it did).
    return None, b"SSH host-key scan found no ed25519 host key"


async def stream_extract_snapshot(
    ssh_host: str,
    ssh_port: int,
    tar_path: str,
    *,
    key_path: Optional[str] = None,
    remote_cmd: str = EXTRACT_REMOTE_CMD,
    expected_host_key_fingerprint: Optional[str] = None,
    require_pipefail: bool = False,
) -> tuple[int, bytes]:
    """Stream a local ``.tar.zst`` into ``zstd -d | tar -xf - -C /`` over SSH.

    The file is handed to the child as its stdin file descriptor, so the kernel
    streams it to ``ssh`` without the orchestrator ever holding the snapshot in
    memory (O(1) memory regardless of snapshot size).

    Returns ``(returncode, stderr_bytes)``; callers handle logging.
    """
    if require_pipefail:
        encoded_fingerprint = (
            expected_host_key_fingerprint[len("SHA256:") :]
            if isinstance(expected_host_key_fingerprint, str)
            and expected_host_key_fingerprint.startswith("SHA256:")
            else ""
        )
        if len(encoded_fingerprint) != 43 or any(
            char
            not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
            for char in encoded_fingerprint
        ):
            return 255, b"strict snapshot extraction requires a pinned SSH host key"
    if not orchestrator_can_reach(ssh_host):
        # Tailnet target — SSH from the orchestrator would black-hole. Fail
        # fast and visibly instead of hanging on a doomed connect (see
        # knowledge-base/knowledge/issues/vm_ssh_readiness_probe_unroutable_from_orchestrator.md).
        logger.info(
            "Skipping snapshot restore to %s:%d: orchestrator has no route "
            "to tailnet targets",
            ssh_host,
            ssh_port,
        )
        return 255, b"skipped: unroutable tailnet target from orchestrator"

    strict_timeout_s: float | None = None
    if require_pipefail:
        try:
            strict_timeout_s = max(
                0.01,
                float(os.environ.get("STATELESS_SNAPSHOT_RESTORE_TIMEOUT_S", "300")),
            )
        except (TypeError, ValueError):
            strict_timeout_s = 300.0
        try:
            remote_lock_timeout_s = max(
                1,
                int(os.environ.get("STATELESS_SNAPSHOT_RESTORE_LOCK_TIMEOUT_S", "300")),
            )
        except (TypeError, ValueError):
            remote_lock_timeout_s = 300
        # The lock lives outside the captured home tree. A cancelled SSH
        # transport can leave its remote tar briefly draining; the next HA
        # owner must serialize behind it rather than extract concurrently.
        remote_cmd = (
            f"flock -w {remote_lock_timeout_s} "
            "/tmp/.srw-terminal-snapshot-restore.lock "
            f"bash -o pipefail -c {shlex.quote(remote_cmd)}"
        )

    known_hosts_line: Optional[str] = None
    if expected_host_key_fingerprint is not None:
        known_hosts_line, scan_error = await _scan_pinned_host_key(
            ssh_host,
            ssh_port,
            expected_host_key_fingerprint,
        )
        if known_hosts_line is None:
            return 255, scan_error

    # Keep the one-use known_hosts file alive until ssh exits. OpenSSH checks
    # the exact public key selected above, so a different key presented after
    # ssh-keyscan is rejected rather than silently trusted.
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="ascii", prefix="srw-known-host-", delete=True
    ) as known_hosts:
        known_hosts_path: Optional[str] = None
        if known_hosts_line is not None:
            known_hosts.write(known_hosts_line + "\n")
            known_hosts.flush()
            known_hosts_path = known_hosts.name

        ssh_cmd = build_agent_ssh_cmd(
            ssh_host,
            ssh_port,
            remote_cmd,
            key_path=key_path,
            known_hosts_path=known_hosts_path,
        )
        proc: asyncio.subprocess.Process | None = None
        stderr_task: asyncio.Task[bytes] | None = None
        try:
            with open(tar_path, "rb") as f:
                proc = await asyncio.create_subprocess_exec(
                    *ssh_cmd,
                    stdin=f,  # OS streams the fd; never read into our heap
                    stdout=(
                        asyncio.subprocess.DEVNULL
                        if require_pipefail
                        else asyncio.subprocess.PIPE
                    ),
                    stderr=asyncio.subprocess.PIPE,
                )
                try:
                    if strict_timeout_s is None:
                        _stdout, stderr = await proc.communicate()
                    else:
                        stderr_task = asyncio.create_task(
                            _read_stream_tail(proc.stderr)
                        )
                        await asyncio.wait_for(proc.wait(), timeout=strict_timeout_s)
                        stderr = await stderr_task
                except asyncio.TimeoutError:
                    return 124, b"strict snapshot extraction timed out"
            return proc.returncode, stderr
        finally:
            # This also runs on caller cancellation. Reap before returning the
            # lifecycle/advisory lock so no local SSH child continues feeding
            # a remote tar concurrently with a retry.
            if proc is not None and proc.returncode is None:
                proc.kill()
                await proc.wait()
            if stderr_task is not None and not stderr_task.done():
                stderr_task.cancel()
                try:
                    await stderr_task
                except (asyncio.CancelledError, Exception):
                    pass


async def wait_for_agent_ssh(
    ssh_host: str,
    ssh_port: int,
    *,
    deadline_s: float,
    connect_timeout_s: int,
    interval_s: float,
    key_path: Optional[str] = None,
    expected_host_key_fingerprint: Optional[str] = None,
) -> tuple[bool, int, str]:
    """Poll until ``agent-host@ssh_host`` accepts an authenticated SSH command.

    Returns ``(ready, attempts, last_error)``. The probe proves route + sshd +
    key authorization by running ``true`` with ``BatchMode=yes``. When an
    expected fingerprint is supplied, every attempt first selects that exact
    public key and the SSH process enforces it through a one-use known_hosts
    file. There is no trust-on-first-use fallback in that mode.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, float(deadline_s))
    attempts = 0
    last_error = ""

    while True:
        attempts += 1
        known_hosts_line: Optional[str] = None
        if expected_host_key_fingerprint is not None:
            known_hosts_line, scan_error = await _scan_pinned_host_key(
                ssh_host,
                ssh_port,
                expected_host_key_fingerprint,
                timeout_s=connect_timeout_s,
            )
            if known_hosts_line is None:
                last_error = scan_error.decode("utf-8", errors="replace").strip()
                if loop.time() >= deadline:
                    return False, attempts, last_error
                remaining = max(0.0, deadline - loop.time())
                await asyncio.sleep(min(max(0.0, float(interval_s)), remaining))
                continue

        with tempfile.NamedTemporaryFile(
            mode="w", encoding="ascii", prefix="srw-ready-known-host-", delete=True
        ) as known_hosts:
            known_hosts_path: Optional[str] = None
            if known_hosts_line is not None:
                known_hosts.write(known_hosts_line + "\n")
                known_hosts.flush()
                known_hosts_path = known_hosts.name
            ssh_cmd = build_agent_ssh_cmd(
                ssh_host,
                ssh_port,
                "true",
                key_path=key_path,
                connect_timeout_s=connect_timeout_s,
                batch_mode=True,
                known_hosts_path=known_hosts_path,
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
