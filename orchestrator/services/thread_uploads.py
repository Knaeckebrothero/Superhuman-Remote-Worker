"""Upload files into a persistent thread's live workspace via SFTP.

Files land in ``<workspace_path>/uploads/`` on the remote workspace
container/VM. Filenames are sanitized to prevent path traversal and
collisions are resolved by appending a counter.

The agent learns about new uploads when the cockpit appends an
``Attached files: …`` hint to the next user message — see
``persistent-chat.service.ts``.
"""

from __future__ import annotations

import asyncio
import logging
import posixpath
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Iterable

try:
    import paramiko
except ImportError:  # pragma: no cover - paramiko is a hard dep elsewhere
    paramiko = None  # type: ignore[assignment]

from . import resolve_ssh_key_path

logger = logging.getLogger(__name__)

# Cap individual files at 100MB. The cockpit allows larger uploads for
# job creation (5GB), but those go into local orchestrator storage. Live
# workspace pushes flow through SFTP into a container/VM, where huge
# blobs are rarely useful and tie up the orchestrator process.
MAX_FILE_SIZE = 100 * 1024 * 1024
MAX_FILES_PER_REQUEST = 20

DEFAULT_USERNAME = "agent-host"
DEFAULT_WORKSPACE_PATH = "/home/agent-host/workspace"
UPLOADS_SUBDIR = "uploads"


@dataclass
class UploadedFile:
    """One file successfully written into the workspace's uploads/ dir."""

    name: str
    size: int
    mime_type: str
    path: str  # Posix path relative to workspace root, e.g. "uploads/photo.jpg"


@dataclass
class _SshTarget:
    host: str
    port: int
    username: str
    key_path: str
    workspace_path: str


class ThreadUploadError(Exception):
    """Raised when a thread workspace upload cannot proceed.

    Carries an HTTP status code so the API layer can map it directly.
    """

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _sanitize_filename(filename: str) -> str:
    """Strip path components and dangerous characters from an upload name."""
    filename = PurePosixPath(filename).name
    filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", filename)
    filename = filename.strip(". ")
    if len(filename) > 200:
        p = PurePosixPath(filename)
        filename = f"{p.stem[:190]}{p.suffix}"
    return filename or "unnamed"


def _resolve_target(thread: dict[str, Any]) -> _SshTarget:
    """Pick SSH connection details from the thread's metadata.

    Preference order:
      1. ``metadata.vm`` (live VM via QEMU/NATS)
      2. ``metadata.workspace_container.host`` (Docker compose pool)
      3. ``metadata.workspace_container.pod_ip`` (K8s pod)
    """
    metadata = thread.get("metadata") or {}
    if isinstance(metadata, str):
        import json

        try:
            metadata = json.loads(metadata)
        except (ValueError, TypeError):
            metadata = {}

    vm_ctx = metadata.get("vm") or {}
    ws_ctx = metadata.get("workspace_container") or {}

    host: str | None = None
    port = 22
    workspace_path = DEFAULT_WORKSPACE_PATH

    if vm_ctx.get("status") == "ready":
        host = vm_ctx.get("ssh_host") or vm_ctx.get("pod_ip")
        port = int(vm_ctx.get("ssh_port") or 22)
        workspace_path = vm_ctx.get("workspace_path") or DEFAULT_WORKSPACE_PATH
    elif ws_ctx.get("status") == "ready":
        host = ws_ctx.get("host") or ws_ctx.get("pod_ip")
        port = int(ws_ctx.get("port") or 22)
        workspace_path = ws_ctx.get("workspace_path") or DEFAULT_WORKSPACE_PATH

    if not host:
        raise ThreadUploadError(
            status_code=409,
            detail="Workspace is not ready — try again in a moment",
        )

    key_path = resolve_ssh_key_path()
    if not key_path:
        raise ThreadUploadError(
            status_code=503,
            detail="SSH key for workspace upload is not configured",
        )

    return _SshTarget(
        host=host,
        port=port,
        username=DEFAULT_USERNAME,
        key_path=key_path,
        workspace_path=workspace_path,
    )


def _ensure_remote_dir(sftp: Any, path: str) -> None:
    """mkdir -p over SFTP. Tolerates parts that already exist."""
    parts = [p for p in path.split("/") if p]
    cursor = "/" if path.startswith("/") else ""
    for part in parts:
        cursor = posixpath.join(cursor, part) if cursor else part
        try:
            sftp.stat(cursor)
        except FileNotFoundError:
            sftp.mkdir(cursor)


def _next_available_name(sftp: Any, directory: str, name: str) -> str:
    """Return ``name`` if free, else ``name_1.ext``, ``name_2.ext`` …"""
    p = PurePosixPath(name)
    stem, suffix = p.stem, p.suffix
    candidate = name
    counter = 1
    while True:
        try:
            sftp.stat(posixpath.join(directory, candidate))
        except FileNotFoundError:
            return candidate
        candidate = f"{stem}_{counter}{suffix}"
        counter += 1


def _sftp_write_files(
    target: _SshTarget,
    payloads: list[tuple[str, bytes, str]],
) -> list[UploadedFile]:
    """Synchronous helper that opens one SFTP session and writes all files.

    Each payload tuple is ``(filename, data, mime_type)``.
    """
    if paramiko is None:  # pragma: no cover - import guard
        raise ThreadUploadError(
            status_code=503, detail="paramiko is not installed on the orchestrator"
        )

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=target.host,
            port=target.port,
            username=target.username,
            key_filename=target.key_path,
            timeout=15,
            banner_timeout=15,
            auth_timeout=15,
            allow_agent=False,
            look_for_keys=False,
        )
    except Exception as e:
        logger.warning(
            "SSH connect failed for %s@%s:%d (%s)",
            target.username,
            target.host,
            target.port,
            e,
        )
        raise ThreadUploadError(
            status_code=502,
            detail=f"Could not reach workspace ({target.host}:{target.port})",
        ) from e

    try:
        sftp = client.open_sftp()
        try:
            uploads_dir = posixpath.join(target.workspace_path, UPLOADS_SUBDIR)
            _ensure_remote_dir(sftp, uploads_dir)

            results: list[UploadedFile] = []
            for filename, data, mime_type in payloads:
                safe = _sanitize_filename(filename)
                final_name = _next_available_name(sftp, uploads_dir, safe)
                remote_path = posixpath.join(uploads_dir, final_name)
                with sftp.open(remote_path, "wb") as f:
                    f.write(data)
                results.append(
                    UploadedFile(
                        name=final_name,
                        size=len(data),
                        mime_type=mime_type or "application/octet-stream",
                        path=posixpath.join(UPLOADS_SUBDIR, final_name),
                    )
                )
                logger.info(
                    "Uploaded %s (%d bytes) to %s",
                    final_name,
                    len(data),
                    target.host,
                )
            return results
        finally:
            sftp.close()
    finally:
        client.close()


async def upload_files_to_thread_workspace(
    thread: dict[str, Any],
    files: Iterable[tuple[str, bytes, str]],
) -> list[UploadedFile]:
    """Push files into the thread workspace's ``uploads/`` directory.

    Args:
        thread: Thread row (must contain ``metadata`` JSONB column).
        files: Iterable of ``(filename, content_bytes, mime_type)`` tuples.

    Raises:
        ThreadUploadError: When the workspace is not reachable, the SSH
            key is missing, or per-file/total limits are exceeded.

    Returns:
        Metadata for each successfully uploaded file.
    """
    payloads = list(files)
    if not payloads:
        raise ThreadUploadError(status_code=400, detail="No files provided")
    if len(payloads) > MAX_FILES_PER_REQUEST:
        raise ThreadUploadError(
            status_code=400,
            detail=f"Maximum {MAX_FILES_PER_REQUEST} files per request",
        )
    for name, data, _ in payloads:
        if len(data) > MAX_FILE_SIZE:
            raise ThreadUploadError(
                status_code=413,
                detail=f"File '{name}' exceeds {MAX_FILE_SIZE // (1024 * 1024)}MB",
            )

    target = _resolve_target(thread)
    return await asyncio.to_thread(_sftp_write_files, target, payloads)
