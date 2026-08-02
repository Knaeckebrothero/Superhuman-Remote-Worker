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

A ``.zip`` upload is expanded into ``uploads/<stem>/…`` rather than stored as
a single opaque archive, mirroring the worker-job path's ``_extract_zip``
(``src/agent.py:4067``) so an attached archive is immediately readable by
``read_file``/``list_files`` with no unzip capability required. See
``_expand_payloads_for_extraction`` below.

The agent learns about new uploads when the cockpit appends an
``Attached files: …`` hint to the next user message — see
``persistent-chat.service.ts``.
"""

from __future__ import annotations

import asyncio
import io
import logging
import mimetypes
import os
import posixpath
import re
import stat as stat_module
import zipfile
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


# =============================================================================
# Zip extraction
#
# The worker-job path already does this (``_extract_zip``, src/agent.py:4067,
# called at :2625/:2676/:2718): a `.zip` document is expanded into
# `documents/…` instead of left as an opaque archive. This mirrors that for
# session uploads, but hardens what it borrows: the worker's dotfile skip
# blocks ".." only *incidentally* (because ".." happens to start with ".")
# and it has no entry-count or size caps at all.
# =============================================================================


class ZipExtractionRefused(Exception):
    """A zip payload failed validation and must not be partially expanded.

    Raised for anything that makes safe, COMPLETE extraction impossible: a
    corrupt/non-zip payload, a path-traversal entry, too many entries, or an
    uncompressed size over either cap below. Callers catch this and fall
    back to storing the archive verbatim — a zip either extracts completely
    and safely, or the user keeps exactly what they uploaded. Never a
    silently half-extracted result.
    """


# MAX_FILE_SIZE (100MB, above) already bounds the compressed upload; these
# bound what it may explode into, so a corrupt or hostile archive fails
# cleanly instead of filling the workspace, blowing up orchestrator memory,
# or hanging the write loop on thousands of tiny entries.
#
#   MAX_ZIP_ENTRIES: generous for a folder of documents/photos, small enough
#   to bound total SFTP/object-store round trips for one request.
#
#   MAX_ZIP_ENTRY_UNCOMPRESSED_BYTES: no single extracted member may be
#   bigger than a standalone upload would have been allowed to be.
#
#   MAX_ZIP_TOTAL_UNCOMPRESSED_BYTES: real documents/images rarely compress
#   more than 2-3x, so 3x the compressed cap is generous headroom while
#   keeping the in-memory extraction buffer bounded — this happens inside
#   the orchestrator process, up to MAX_CONCURRENT_VIRTUAL_UPLOADS at once.
MAX_ZIP_ENTRIES = 2_000
MAX_ZIP_ENTRY_UNCOMPRESSED_BYTES = MAX_FILE_SIZE
MAX_ZIP_TOTAL_UNCOMPRESSED_BYTES = 3 * MAX_FILE_SIZE

# Chunk size for the capped incremental read in _read_zip_entry_capped.
_ZIP_READ_CHUNK_SIZE = 65536


def _is_symlink_zip_entry(info: zipfile.ZipInfo) -> bool:
    """True when a zip entry's Unix mode bits mark it as a symlink.

    Extraction never calls a symlink-creation API, but a symlink entry's
    "content" is just its target path string — writing that as if it were
    real file content would be misleading, so it's skipped like a directory
    entry instead of materialized as a bogus file.
    """
    mode = info.external_attr >> 16
    return stat_module.S_ISLNK(mode) if mode else False


def _safe_zip_member_path(stem: str, entry_name: str) -> str | None:
    """Resolve a zip entry name to a path under ``stem``, or None if unsafe.

    Deliberate, unlike the worker's ``_extract_zip`` (``src/agent.py:4067``),
    whose dotfile skip incidentally also blocks ".." only because ".."
    happens to start with ".". Rejects absolute paths, Windows drive
    letters/backslash separators (never legitimate in a zip's stored POSIX
    names), and any entry whose normalized destination would resolve outside
    ``stem`` — the "Zip-Slip" family of path-traversal entries.
    """
    if not entry_name or "\x00" in entry_name or entry_name.startswith("/"):
        return None
    if "\\" in entry_name or re.match(r"^[A-Za-z]:", entry_name):
        return None

    joined = posixpath.normpath(posixpath.join(stem, entry_name))
    if joined == stem or not joined.startswith(stem + "/"):
        return None
    return joined


def _read_zip_entry_capped(fh: Any, cap: int, entry_name: str) -> bytes:
    """Read a zip entry's decompressed bytes, aborting past ``cap``.

    A zip's declared ``file_size``/CRC can lie about what its compressed
    stream actually decompresses to, so the real defense against a zip bomb
    is bounding bytes as they come out of the decompressor — not trusting
    the archive's own metadata (the pre-check against declared size in
    ``_plan_zip_extraction`` is a cheap fast-fail, not the backstop).
    """
    total = 0
    chunks: list[bytes] = []
    while True:
        chunk = fh.read(_ZIP_READ_CHUNK_SIZE)
        if not chunk:
            break
        total += len(chunk)
        if total > cap:
            raise ZipExtractionRefused(
                f"entry {entry_name!r} decompresses past the {cap}-byte per-entry limit"
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _plan_zip_extraction(data: bytes, stem: str) -> list[tuple[str, bytes]]:
    """Validate and expand a zip's bytes into ``(name, content)`` pairs.

    ``name`` is relative to the uploads/ directory root (e.g.
    ``"myarchive/sub/file.txt"`` for ``stem="myarchive"``) — ready to become
    ``UploadedFile.name`` directly, exactly like a flat upload's name.

    Raises ``ZipExtractionRefused`` for anything that makes safe, complete
    extraction impossible. Never returns a partial result: a caller either
    gets every safe entry or none of them ("rather than half-extracted").
    """
    try:
        return _plan_zip_extraction_unsafe(data, stem)
    except ZipExtractionRefused:
        raise
    except Exception as e:
        # zipfile can raise more than BadZipFile for a hostile or corrupt
        # archive (NotImplementedError for an exotic compression method,
        # RuntimeError for an encrypted entry, ...). None of those should
        # ever crash an upload request — they all mean the same thing here:
        # this zip cannot be safely and completely extracted.
        raise ZipExtractionRefused(f"could not process zip: {e}") from e


def _plan_zip_extraction_unsafe(data: bytes, stem: str) -> list[tuple[str, bytes]]:
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise ZipExtractionRefused(f"not a valid zip file: {e}") from e

    with zf:
        infos = zf.infolist()
        if len(infos) > MAX_ZIP_ENTRIES:
            raise ZipExtractionRefused(
                f"{len(infos)} entries exceeds the {MAX_ZIP_ENTRIES}-entry limit"
            )

        plan: list[tuple[str, bytes]] = []
        total_uncompressed = 0

        for info in infos:
            name = info.filename
            if info.is_dir():
                continue
            if not PurePosixPath(name).name:
                continue  # empty/degenerate filename — structural, not a
                # traversal attempt

            # Deliberate security boundary FIRST, independent of the content
            # filters below — otherwise a traversal entry built purely from
            # ".." components (e.g. "../../etc/passwd") would be silently
            # *skipped* by the dotfile filter instead of refusing the whole
            # archive, since ".." happens to start with ".". That is
            # precisely the worker's incidental behaviour this must not
            # reproduce.
            dest = _safe_zip_member_path(stem, name)
            if dest is None:
                raise ZipExtractionRefused(
                    f"entry {name!r} resolves outside the destination directory"
                )

            # Content filters: routine exclusions, not safety violations.
            parts = PurePosixPath(name).parts
            if any(part.startswith(".") for part in parts):
                continue  # dotfiles/hidden
            if "__MACOSX" in parts:
                continue
            if _is_symlink_zip_entry(info):
                continue

            if info.file_size > MAX_ZIP_ENTRY_UNCOMPRESSED_BYTES:
                raise ZipExtractionRefused(
                    f"entry {name!r} declares {info.file_size} bytes "
                    f"uncompressed, over the {MAX_ZIP_ENTRY_UNCOMPRESSED_BYTES} "
                    "byte per-entry limit"
                )

            with zf.open(info) as fh:
                content = _read_zip_entry_capped(
                    fh, MAX_ZIP_ENTRY_UNCOMPRESSED_BYTES, name
                )

            total_uncompressed += len(content)
            if total_uncompressed > MAX_ZIP_TOTAL_UNCOMPRESSED_BYTES:
                raise ZipExtractionRefused(
                    "uncompressed contents exceed the "
                    f"{MAX_ZIP_TOTAL_UNCOMPRESSED_BYTES}-byte total limit"
                )

            plan.append((dest, content))

    return plan


def _expand_payloads_for_extraction(
    payloads: list[tuple[str, bytes, str]],
    taken: set[str],
) -> list[tuple[str, bytes, str]]:
    """Resolve final names for a batch of uploads, expanding any ``.zip``.

    ``taken`` is mutated as names are claimed — files and zip stems share one
    namespace (a directory and a file can't coexist under the same name in
    ``uploads/`` either way). Returns ``(name, data, mime_type)`` triples
    ready to write verbatim at ``uploads/<name>``: a regular upload gets its
    usual sanitized, collision-resolved flat name; a zip's members get
    ``<stem>/<relative path within the archive>``.

    A zip that fails validation (corrupt, unsafe entry, over a cap) falls
    back to its original bytes stored under the claimed stem plus its
    original suffix — never a half-extracted result, never a vanished
    upload (docs/issues/session_uploads_never_extract_archives.md).
    """
    expanded: list[tuple[str, bytes, str]] = []
    for filename, data, mime_type in payloads:
        safe_name = _sanitize_filename(filename)
        suffix = PurePosixPath(safe_name).suffix

        if suffix.lower() != ".zip":
            expanded.append((_claim_name(taken, safe_name), data, mime_type))
            continue

        # Claim the slot once, whether extraction succeeds or falls back —
        # so a fallback can never collide with something claimed in between.
        stem = _claim_name(taken, PurePosixPath(safe_name).stem or "archive")
        plan: list[tuple[str, bytes]] = []
        try:
            plan = _plan_zip_extraction(data, stem)
        except ZipExtractionRefused as e:
            logger.warning(
                "Zip extraction refused for %r (%s) — storing the upload "
                "as-is instead of extracting it",
                filename,
                e,
            )

        if not plan:
            expanded.append((f"{stem}{suffix}", data, mime_type))
            continue

        for member_name, content in plan:
            mime, _ = mimetypes.guess_type(member_name)
            expanded.append((member_name, content, mime or "application/octet-stream"))

    return expanded


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


def _sftp_write_files(
    target: _SshTarget,
    payloads: list[tuple[str, bytes, str]],
) -> list[UploadedFile]:
    """Synchronous helper that opens one SFTP session and writes all files.

    Each payload tuple is ``(filename, data, mime_type)``. A ``.zip`` payload
    is expanded into ``uploads/<stem>/…`` — see
    ``_expand_payloads_for_extraction``; a corrupt/unsafe zip falls back to
    being written verbatim like any other file.
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

            # One listing, resolved in-memory from here — equivalent to the
            # old per-candidate sftp.stat() probe for this single connection
            # (nothing else writes through it concurrently), and it's what a
            # zip's stem name needs anyway: claiming a whole subtree can't be
            # done with a single stat probe the way one flat name can.
            taken: set[str] = set()
            try:
                taken.update(sftp.listdir(uploads_dir))
            except OSError:
                pass  # just created above; nothing to collide with yet

            expanded = _expand_payloads_for_extraction(payloads, taken)

            results: list[UploadedFile] = []
            for name, data, mime_type in expanded:
                remote_path = posixpath.join(uploads_dir, name)
                parent = posixpath.dirname(remote_path)
                if parent != uploads_dir:
                    _ensure_remote_dir(sftp, parent)
                with sftp.open(remote_path, "wb") as f:
                    f.write(data)
                results.append(
                    UploadedFile(
                        name=name,
                        size=len(data),
                        mime_type=mime_type or "application/octet-stream",
                        path=posixpath.join(UPLOADS_SUBDIR, name),
                    )
                )
                logger.info(
                    "Uploaded %s (%d bytes) to %s",
                    name,
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
    A ``.zip`` payload is expanded into ``uploads/<stem>/…`` — see
    ``_expand_payloads_for_extraction``; a corrupt/unsafe zip falls back to
    being written verbatim like any other file.

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
        # Top-level component of every existing key, flat or nested — a zip
        # extracted earlier left "bundle/..." keys with no "bundle" key of
        # its own, so a plain remainder-has-no-slash filter (the pre-zip
        # version of this loop) would miss it and let a new "bundle.zip"
        # collide with — and mix its members into — that directory.
        taken: set[str] = set()
        for info in store.list(uploads_prefix):
            remainder = info.key[len(uploads_prefix) :]
            if remainder:
                taken.add(remainder.split("/", 1)[0])

        expanded = _expand_payloads_for_extraction(payloads, taken)

        results: list[UploadedFile] = []
        for name, data, mime_type in expanded:
            store.put(f"{uploads_prefix}{name}", data)
            results.append(
                UploadedFile(
                    name=name,
                    size=len(data),
                    mime_type=mime_type or "application/octet-stream",
                    path=posixpath.join(UPLOADS_SUBDIR, name),
                )
            )
            logger.info(
                "Uploaded %s (%d bytes) to %s%s",
                name,
                len(data),
                uploads_prefix,
                name,
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
