"""Upload files into a persistent thread's live workspace.

Files land in ``<workspace>/uploads/``. Which transport gets them there is
decided by the thread's workspace tier:

- ``sandbox`` / ``vm`` — SFTP into the workspace container or VM (paramiko).
- ``virtual`` — object-store ``put`` under ``threads/<id>/uploads/``, the same
  durable namespace the agent's lite backend is attached to at dispatch, so the
  agent's own ``read_file("uploads/x")`` resolves what we wrote.

``none`` (a disposable agent-local tmpdir) has no orchestrator-reachable
storage and is refused permanently rather than pretending to be transient.

Filenames are sanitized to prevent path traversal and collisions are resolved by
appending a counter.

The agent learns about new uploads when the cockpit appends an
``Attached files: …`` hint to the next user message — see
``persistent-chat.service.ts``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import posixpath
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Iterable, Iterator

try:
    import paramiko
except ImportError:  # pragma: no cover - import-time guard
    paramiko = None  # type: ignore[assignment]
    logging.getLogger(__name__).warning(
        "paramiko is not installed — thread workspace uploads "
        "(POST /api/persistent/threads/{id}/uploads) will fail with HTTP 503"
    )

from . import resolve_ssh_key_path
from .workspace_binding import VirtualBackingUnavailable, resolve_virtual_thread_backing

logger = logging.getLogger(__name__)

# Cap individual files at 100MB. The cockpit allows larger uploads for
# job creation (5GB), but those go into local orchestrator storage. Live
# workspace pushes flow through the orchestrator process into a container/VM or
# object store, where huge blobs are rarely useful and tie up that process.
MAX_FILE_SIZE = 100 * 1024 * 1024
MAX_FILES_PER_REQUEST = 20

DEFAULT_USERNAME = "agent-host"
DEFAULT_WORKSPACE_PATH = "/home/agent-host/workspace"
UPLOADS_SUBDIR = "uploads"

# Tiers with no orchestrator-reachable workspace storage at all.
UNREACHABLE_BACKENDS = frozenset({"none"})

# Bound concurrent object-store uploads: each one holds a full request body in
# memory and spawns rclone children. Mirrors the ceiling Canvas puts on its own
# virtual materializations, with a bounded queue wait so a saturated
# orchestrator fails fast instead of piling up.
MAX_CONCURRENT_VIRTUAL_UPLOADS = int(os.getenv("THREAD_UPLOAD_MAX_CONCURRENT", "4"))
VIRTUAL_UPLOAD_QUEUE_TIMEOUT = float(os.getenv("THREAD_UPLOAD_QUEUE_TIMEOUT", "10"))
_VIRTUAL_UPLOAD_SEMAPHORE = asyncio.Semaphore(max(1, MAX_CONCURRENT_VIRTUAL_UPLOADS))


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


@dataclass
class _VirtualTarget:
    """Object-store destination for a ``virtual`` (lite) thread."""

    spec: dict[str, Any]  # rclone {type, config, root}
    prefix: str  # "threads/<thread_id>/"


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


def _name_candidates(name: str) -> Iterator[str]:
    """Yield ``name``, then ``name_1.ext``, ``name_2.ext``, … indefinitely.

    Shared by both transports so SFTP and object-store uploads resolve
    collisions to the same names.
    """
    yield name
    p = PurePosixPath(name)
    stem, suffix = p.stem, p.suffix
    counter = 1
    while True:
        yield f"{stem}_{counter}{suffix}"
        counter += 1


def _claim_name(taken: set[str], name: str) -> str:
    """Pick the first free candidate and reserve it in ``taken``.

    Reserving as we go is what keeps two identically-named files *in one batch*
    from overwriting each other — the SFTP path gets that for free by stat-ing
    after each write, a flat key space does not.
    """
    for candidate in _name_candidates(name):
        if candidate not in taken:
            taken.add(candidate)
            return candidate
    raise AssertionError("unreachable: _name_candidates is infinite")


def _thread_metadata(thread: dict[str, Any]) -> dict[str, Any]:
    """Thread ``metadata`` as a dict, tolerating a JSON-string column."""
    metadata = thread.get("metadata") or {}
    if isinstance(metadata, str):
        import json

        try:
            metadata = json.loads(metadata)
        except (ValueError, TypeError):
            metadata = {}
    return metadata if isinstance(metadata, dict) else {}


def _workspace_backend(metadata: dict[str, Any]) -> str | None:
    """The thread's selected tier from ``config_override.workspace.backend``."""
    config_override = metadata.get("config_override")
    if not isinstance(config_override, dict):
        return None
    workspace = config_override.get("workspace")
    if not isinstance(workspace, dict):
        return None
    backend = workspace.get("backend")
    return backend if isinstance(backend, str) else None


def resolve_thread_upload_destination(
    thread: dict[str, Any],
) -> _SshTarget | _VirtualTarget:
    """Decide where this thread's uploads go, without moving any bytes.

    Exposed separately so the API layer can reject an impossible upload before
    materializing request bodies.

    Preference order for SSH tiers:
      1. ``metadata.vm`` (live VM via QEMU/NATS)
      2. ``metadata.workspace_container.host`` (Docker compose pool)
      3. ``metadata.workspace_container.pod_ip`` (K8s pod)

    Raises:
        ThreadUploadError: With a status and message honest about *why* — a
            permanent refusal for tiers that can never accept files, an
            operator-facing 503 for misconfigured storage, and the original
            transient 409 only where waiting can actually help.
    """
    metadata = _thread_metadata(thread)
    backend = _workspace_backend(metadata)

    if backend in UNREACHABLE_BACKENDS:
        raise ThreadUploadError(
            status_code=409,
            detail=(
                "This session has no workspace, so files cannot be attached to "
                "it. Start a session with a workspace to upload files."
            ),
        )

    if backend == "virtual":
        try:
            spec, prefix = resolve_virtual_thread_backing(thread)
        except VirtualBackingUnavailable as e:
            # not_configured / transport_missing are deployment problems the
            # user cannot retry their way out of; backing_changed needs a
            # reopened session, not a wait.
            status = 409 if e.reason == "backing_changed" else 503
            raise ThreadUploadError(status_code=status, detail=e.detail) from e
        return _VirtualTarget(spec=spec, prefix=prefix)

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
        # Genuinely transient now: this branch is only reachable for tiers that
        # do provision a pod/VM, so waiting is the right advice.
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
    for candidate in _name_candidates(name):
        try:
            sftp.stat(posixpath.join(directory, candidate))
        except FileNotFoundError:
            return candidate
    raise AssertionError("unreachable: _name_candidates is infinite")


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


def _virtual_write_files(
    target: _VirtualTarget,
    payloads: list[tuple[str, bytes, str]],
    *,
    store: Any | None = None,
) -> list[UploadedFile]:
    """Synchronous helper that writes all files into the thread's object store.

    One store for the whole batch, and **one** listing to seed collision
    resolution — probing per file would cost an rclone subprocess per probe.

    No directory marker is written: ``VirtualWorkspaceBackend.is_dir`` derives
    directories from key prefixes, so the first object makes ``uploads/`` exist
    for the agent's ``list_dir``/``walk``.

    Args:
        store: Injected object store, for tests. Built from ``target.spec`` and
            closed here when not supplied.
    """
    owned = store is None
    if store is None:
        from src.core.backends.rclone import RcloneObjectStore

        store = RcloneObjectStore(
            remote_type=str(target.spec["type"]),
            config=target.spec.get("config") or {},
            root=str(target.spec.get("root") or ""),
        )

    uploads_prefix = f"{target.prefix}{UPLOADS_SUBDIR}/"
    try:
        taken: set[str] = set()
        for info in store.list(uploads_prefix):
            remainder = info.key[len(uploads_prefix) :]
            if remainder and "/" not in remainder:
                taken.add(remainder)

        results: list[UploadedFile] = []
        for filename, data, mime_type in payloads:
            final_name = _claim_name(taken, _sanitize_filename(filename))
            store.put(f"{uploads_prefix}{final_name}", data)
            results.append(
                UploadedFile(
                    name=final_name,
                    size=len(data),
                    mime_type=mime_type or "application/octet-stream",
                    path=posixpath.join(UPLOADS_SUBDIR, final_name),
                )
            )
            logger.info(
                "Uploaded %s (%d bytes) to %s%s",
                final_name,
                len(data),
                uploads_prefix,
                final_name,
            )
        return results
    finally:
        if owned:
            store.close()


@asynccontextmanager
async def _virtual_upload_slot():
    """Acquire an object-store upload slot with a bounded queue wait."""
    acquired = False
    try:
        try:
            await asyncio.wait_for(
                _VIRTUAL_UPLOAD_SEMAPHORE.acquire(),
                timeout=max(0.1, VIRTUAL_UPLOAD_QUEUE_TIMEOUT),
            )
            acquired = True
        except (TimeoutError, asyncio.TimeoutError) as e:
            raise ThreadUploadError(
                status_code=503,
                detail="Upload capacity is currently exhausted — try again shortly",
            ) from e
        yield
    finally:
        if acquired:
            _VIRTUAL_UPLOAD_SEMAPHORE.release()


async def upload_files_to_thread_workspace(
    thread: dict[str, Any],
    files: Iterable[tuple[str, bytes, str]],
    *,
    destination: _SshTarget | _VirtualTarget | None = None,
) -> list[UploadedFile]:
    """Push files into the thread workspace's ``uploads/`` directory.

    Args:
        thread: Thread row (must contain ``metadata`` JSONB column).
        files: Iterable of ``(filename, content_bytes, mime_type)`` tuples.
        destination: Pre-resolved destination from
            ``resolve_thread_upload_destination``. Resolved here when omitted.

    Raises:
        ThreadUploadError: When the workspace is not reachable, the tier cannot
            accept files, the SSH key is missing, or per-file/total limits are
            exceeded.

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

    if destination is None:
        destination = resolve_thread_upload_destination(thread)

    if isinstance(destination, _VirtualTarget):
        async with _virtual_upload_slot():
            return await asyncio.to_thread(_virtual_write_files, destination, payloads)
    return await asyncio.to_thread(_sftp_write_files, destination, payloads)
