"""Shared SSH helpers for agent-host workspace operations.

Provides the agent SSH command builder and a memory-safe streaming snapshot
extractor used by workspace/IDE session restore.

Memory note: ``stream_extract_snapshot`` passes the local snapshot file to the
child process as its stdin file descriptor, so the kernel feeds it to ``ssh``
directly — the orchestrator never loads the tarball into its heap. This is the
fix for the resume-time OOM where ``f.read()`` + ``communicate(input=...)``
buffered the whole ``.tar.zst`` in RAM. Capture (snapshot_service) already
streams; this brings restore to the same O(1)-memory footprint.

Import-light by design (stdlib + ``resolve_ssh_key_path`` only — no boto3, no
DB), so it is safe to import from tests and any service module.
"""

import asyncio
from typing import Optional

from services import resolve_ssh_key_path

# Remote command run on the agent host to inflate + unpack a snapshot.
EXTRACT_REMOTE_CMD = "zstd -d | tar -xf - -C /"


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
        *(["-o", "BatchMode=yes"] if batch_mode else []),
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
) -> tuple[int, bytes]:
    """Stream a local ``.tar.zst`` into ``zstd -d | tar -xf - -C /`` over SSH.

    The file is handed to the child as its stdin file descriptor, so the kernel
    streams it to ``ssh`` without the orchestrator ever holding the snapshot in
    memory (O(1) memory regardless of snapshot size).

    Returns ``(returncode, stderr_bytes)``; callers handle logging.
    """
    ssh_cmd = build_agent_ssh_cmd(
        ssh_host, ssh_port, EXTRACT_REMOTE_CMD, key_path=key_path
    )
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
        proc = await asyncio.create_subprocess_exec(
            *ssh_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
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
