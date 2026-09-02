"""File upload API endpoints for job creation.

Provides endpoints for uploading:
- Documents: Files that will be processed by agents
- Config: YAML files that override agent defaults
- Instructions: Markdown files with task instructions

Files are stored in workspace/uploads/<upload_id>/ and referenced by upload_id
when creating jobs.

Access model (security audit 2026-08-27, "uploads router takes no identity"):

* Every route is dual-callable the way ``POST /api/jobs`` is: an approved
  user (cookie / bearer / MCP-forwarded) or an ``X-Internal-Key`` service
  caller. The agent runtime fetches job inputs with the internal key only
  (``src/api/orchestrator_client.py`` attaches no ``X-MCP-User-Id`` on that
  path), so an internal caller carries no user identity and is not
  ownership-checked — the shared key already reaches every upload the
  dispatcher hands out.
* An upload is bound to its creator: ``metadata.json`` records ``user_id``
  (``"internal"`` for service callers). A user can read, delete, or reference
  from ``POST /api/jobs`` only their own uploads; admins and internal callers
  reach any. Uploads written before this binding carry no ``user_id`` and are
  reachable by admins and internal callers only — nobody can prove they own
  them, so the safe default is to refuse regular users.
* ``upload_id`` is checked against the exact shape ``upload_files`` mints
  before any path is built from it, so a caller-supplied id can never carry a
  path separator or a dot segment out of ``UPLOADS_DIR``.
* Bodies are copied to disk in chunks and the running size is checked after
  every chunk: an oversized file is refused when it crosses the limit, never
  after the whole file has been read into memory.
"""

import json
import logging
import os
import re
import secrets
import shutil
import time
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, List, Optional

from fastapi import (
    APIRouter,
    File,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse
from pydantic import BaseModel

from security.access import is_internal_call
from security.auth import require_approved_user


class UploadType(str, Enum):
    """Type of upload for validation and routing."""

    DOCUMENTS = "documents"
    CONFIG = "config"
    INSTRUCTIONS = "instructions"


router = APIRouter(prefix="/api/uploads", tags=["Uploads"])
logger = logging.getLogger(__name__)


def _get_uploads_dir() -> Path:
    """Resolve uploads directory, respecting WORKSPACE_PATH env var."""
    env_path = os.getenv("WORKSPACE_PATH")
    if env_path:
        return Path(env_path) / "uploads"
    # Development fallback: ./workspace relative to project root
    return Path(__file__).resolve().parent.parent / "workspace" / "uploads"


UPLOADS_DIR = _get_uploads_dir()

# Limits
MAX_FILE_SIZE = 5 * 1024 * 1024 * 1024  # 5GB per file
MAX_FILES_PER_UPLOAD = 100
# Human form of MAX_FILE_SIZE for the 413 detail; the cockpit mirrors the
# same number (5120 MB) in its own pre-check.
_MAX_FILE_SIZE_LABEL = f"{MAX_FILE_SIZE // (1024 * 1024)} MB"
# Upload bodies are copied to disk this many bytes at a time; the running
# total is compared against MAX_FILE_SIZE after every chunk.
UPLOAD_CHUNK_SIZE = 1024 * 1024

# Recorded as ``metadata.json["user_id"]`` for uploads created by an
# ``X-Internal-Key`` caller, which has no user identity to record.
INTERNAL_UPLOADER = "internal"

# ``upload_files`` mints ids as ``<type>_<epoch-ms>_<8 random bytes as hex>``:
# one of the UploadType values, a 13-digit millisecond timestamp, 16 hex
# characters. Anything else is refused before a path is built from it.
_UPLOAD_ID_RE = re.compile(
    r"^(?:"
    + "|".join(re.escape(upload_type.value) for upload_type in UploadType)
    + r")_[0-9]{13}_[0-9a-f]{16}$"
)


# =============================================================================
# Pydantic Models
# =============================================================================


class UploadedFile(BaseModel):
    """Metadata for a single uploaded file."""

    name: str
    size: int
    mime_type: str


class UploadResponse(BaseModel):
    """Response after uploading files."""

    upload_id: str
    files: List[UploadedFile]


class UploadInfo(BaseModel):
    """Information about an existing upload."""

    upload_id: str
    upload_type: str
    files: List[UploadedFile]
    created_at: str


# =============================================================================
# Helper Functions
# =============================================================================


def _get_db() -> Any:
    # Same late import the routers use (orchestrator/routers/contacts.py):
    # main imports this module, so the db handle is resolved per call.
    import main

    return main.postgres_db


async def _authenticate(request: Request) -> Optional[dict[str, Any]]:
    """Dual gate shared by every route — the ``POST /api/jobs`` pattern.

    Returns ``None`` for an ``X-Internal-Key`` caller (the agent runtime,
    which carries no user identity) and the approved user dict otherwise;
    ``require_approved_user`` raises 401/403 for anyone else.
    """
    if is_internal_call(request):
        return None
    return await require_approved_user(request, _get_db())


def _validate_upload_id(upload_id: str) -> str:
    """Refuse anything that is not an id ``upload_files`` could have minted."""
    if not isinstance(upload_id, str) or not _UPLOAD_ID_RE.fullmatch(upload_id):
        raise HTTPException(status_code=400, detail="Invalid upload id")
    return upload_id


def _upload_dir(upload_id: str) -> Path:
    """The only way this module builds a path from a caller-supplied id."""
    return UPLOADS_DIR / _validate_upload_id(upload_id)


def _read_metadata(upload_id: str) -> dict[str, Any]:
    """Load ``metadata.json`` for an upload; 404 when there is no such upload.

    ``metadata.json`` is written last by ``upload_files``, so a directory
    without one is an upload that never completed, not one to serve.
    """
    metadata_path = _upload_dir(upload_id) / "metadata.json"
    if not metadata_path.is_file():
        raise HTTPException(status_code=404, detail="Upload not found")
    return json.loads(metadata_path.read_text())


def _uploader_id(caller: Optional[dict[str, Any]]) -> str:
    return INTERNAL_UPLOADER if caller is None else str(caller["id"])


def _authorize_upload(
    caller: Optional[dict[str, Any]], metadata: dict[str, Any]
) -> None:
    """Internal callers and admins reach any upload; a user only their own.

    ``is_admin`` (not ``real_is_admin``) so the admin "view as user" shadow
    narrows this the way it narrows every other visibility check. Uploads
    written before ownership was recorded have no ``user_id``; they fall
    through to the refusal, so only admins and internal callers reach them.
    """
    if caller is None or caller.get("is_admin"):
        return
    owner = metadata.get("user_id")
    if owner and str(owner) == str(caller.get("id")):
        return
    raise HTTPException(status_code=403, detail="Upload belongs to another user")


def authorize_upload_reference(
    caller: Optional[dict[str, Any]], upload_id: str, *, internal: bool = False
) -> dict[str, Any]:
    """Ownership check for an upload referenced from job creation.

    ``POST /api/jobs`` calls this for ``upload_id`` / ``config_upload_id`` /
    ``instructions_upload_id`` before anything is persisted, so a job is
    never built on — and an agent never loads its config from — an upload
    the requester does not own. ``internal`` marks a userless system caller
    (an ``X-Internal-Key`` job creation whose origin has no user at all);
    that path is not ownership-checked for the same reason the download
    routes are not, there is no identity to check against. Raises 400
    (malformed id), 404 (no such upload) or 403 (another user's upload, or
    a legacy ownerless upload and the caller is not an admin). Returns the
    upload's metadata.
    """
    metadata = _read_metadata(upload_id)
    if internal:
        return metadata
    if caller is None:
        raise HTTPException(status_code=403, detail="Upload belongs to another user")
    _authorize_upload(caller, metadata)
    return metadata


def _sanitize_filename(filename: str) -> str:
    """Sanitize filename to prevent path traversal and other issues.

    Args:
        filename: Original filename from upload

    Returns:
        Safe filename with problematic characters removed
    """
    # Remove path components (prevent path traversal)
    filename = Path(filename).name

    # Replace problematic characters with underscore
    filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", filename)

    # Remove leading/trailing dots and spaces
    filename = filename.strip(". ")

    # Limit length while preserving extension
    if len(filename) > 200:
        stem = Path(filename).stem[:190]
        suffix = Path(filename).suffix
        filename = f"{stem}{suffix}"

    return filename or "unnamed"


def _get_media_type(file_path: Path) -> str:
    """Determine media type from file extension.

    Args:
        file_path: Path to file

    Returns:
        MIME type string
    """
    extension_to_media_type = {
        # Images
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".svg": "image/svg+xml",
        # Documents
        ".pdf": "application/pdf",
        ".txt": "text/plain",
        ".md": "text/markdown",
        ".json": "application/json",
        ".doc": "application/msword",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".xls": "application/vnd.ms-excel",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        # Archives
        ".zip": "application/zip",
    }
    return extension_to_media_type.get(
        file_path.suffix.lower(), "application/octet-stream"
    )


def _too_large(file: UploadFile) -> HTTPException:
    return HTTPException(
        status_code=413,
        detail=(
            f"File '{file.filename}' exceeds maximum size of {_MAX_FILE_SIZE_LABEL}"
        ),
    )


async def _write_upload(file: UploadFile, dest: Path) -> int:
    """Copy one upload to ``dest`` in chunks, enforcing MAX_FILE_SIZE as it goes.

    Starlette's multipart parser has already spooled the part to a temp file
    (memory above 1 MiB rolls to disk), so the only whole-file buffering was
    the handler's own ``await file.read()``; reading in chunks keeps memory
    bounded by UPLOAD_CHUNK_SIZE and stops at the first chunk past the limit.
    The caller removes the upload directory on any HTTPException, which also
    discards the partial file.
    """
    declared = getattr(file, "size", None)
    if declared is not None and declared > MAX_FILE_SIZE:
        raise _too_large(file)
    total = 0
    with dest.open("wb") as out:
        while True:
            chunk = await file.read(UPLOAD_CHUNK_SIZE)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_FILE_SIZE:
                raise _too_large(file)
            out.write(chunk)
    return total


# =============================================================================
# Endpoints
# =============================================================================


@router.post(
    "",
    response_model=UploadResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        400: {"description": "No files provided, too many files, or invalid file type"},
        401: {"description": "Not authenticated"},
        413: {"description": "File exceeds maximum size"},
    },
)
async def upload_files(
    request: Request,
    files: List[UploadFile] = File(...),
    upload_type: UploadType = Query(default=UploadType.DOCUMENTS),
) -> UploadResponse:
    """Upload files for job creation.

    Files are stored in workspace/uploads/<upload_id>/ and can be referenced
    when creating a job via the upload_id. The upload is bound to the
    uploader: only they (or an admin) can read, delete, or build a job on it.

    Upload types:
    - documents: General documents for agent processing (default)
    - config: Single YAML file to override agent configuration
    - instructions: Single markdown/text file with task instructions

    Limits:
    - Maximum 5120 MB (5 GiB) per file
    - Maximum 100 files per upload (documents only)
    - Config and instructions must be exactly 1 file

    Args:
        request: Incoming request (approved user or internal caller)
        files: List of files to upload
        upload_type: Type of upload (documents, config, or instructions)

    Returns:
        UploadResponse with upload_id and file metadata
    """
    caller = await _authenticate(request)

    if not files:
        raise HTTPException(status_code=400, detail="No files provided")

    # Validate based on upload type
    if upload_type == UploadType.CONFIG:
        if len(files) != 1:
            raise HTTPException(
                status_code=400,
                detail="Config upload must be exactly 1 file",
            )
        filename = files[0].filename or ""
        if not filename.lower().endswith((".yaml", ".yml")):
            raise HTTPException(
                status_code=400,
                detail="Config file must be a YAML file (.yaml or .yml)",
            )

    elif upload_type == UploadType.INSTRUCTIONS:
        if len(files) != 1:
            raise HTTPException(
                status_code=400,
                detail="Instructions upload must be exactly 1 file",
            )
        filename = files[0].filename or ""
        if not filename.lower().endswith((".md", ".txt")):
            raise HTTPException(
                status_code=400,
                detail="Instructions file must be markdown or text (.md or .txt)",
            )

    else:  # DOCUMENTS
        if len(files) > MAX_FILES_PER_UPLOAD:
            raise HTTPException(
                status_code=400,
                detail=f"Maximum {MAX_FILES_PER_UPLOAD} files per upload",
            )

    # Generate typed upload ID (the shape _UPLOAD_ID_RE pins)
    timestamp = int(time.time() * 1000)
    random_suffix = secrets.token_hex(8)
    upload_id = f"{upload_type.value}_{timestamp}_{random_suffix}"

    # Create upload directory
    upload_dir = _upload_dir(upload_id)
    upload_dir.mkdir(parents=True, exist_ok=True)

    uploaded_files: List[UploadedFile] = []

    try:
        for file in files:
            # Sanitize filename
            safe_filename = _sanitize_filename(file.filename or "unnamed")
            file_path = upload_dir / safe_filename

            # Handle duplicate filenames by appending counter
            counter = 1
            original_stem = Path(safe_filename).stem
            suffix = Path(safe_filename).suffix
            while file_path.exists():
                file_path = upload_dir / f"{original_stem}_{counter}{suffix}"
                counter += 1

            # Stream to disk; the size limit is enforced per chunk
            size = await _write_upload(file, file_path)

            uploaded_files.append(
                UploadedFile(
                    name=file_path.name,
                    size=size,
                    mime_type=file.content_type or "application/octet-stream",
                )
            )

            logger.info(
                f"Uploaded: {file.filename} -> {upload_id}/{file_path.name} "
                f"({size} bytes)"
            )

        # Save metadata
        metadata = {
            "upload_id": upload_id,
            "upload_type": upload_type.value,
            "user_id": _uploader_id(caller),
            "files": [f.model_dump() for f in uploaded_files],
            "created_at": datetime.utcnow().isoformat(),
        }
        (upload_dir / "metadata.json").write_text(json.dumps(metadata, indent=2))

        logger.info(
            f"Upload complete: {upload_id} (type={upload_type.value}, "
            f"{len(uploaded_files)} files)"
        )
        return UploadResponse(upload_id=upload_id, files=uploaded_files)

    except HTTPException:
        # Clean up on HTTP error (validation failures)
        if upload_dir.exists():
            shutil.rmtree(upload_dir)
        raise
    except Exception as e:
        # Clean up on unexpected error
        if upload_dir.exists():
            shutil.rmtree(upload_dir)
        logger.error(f"Upload failed: {e}")
        raise HTTPException(status_code=500, detail="Upload failed") from e


@router.get(
    "/{upload_id}",
    response_model=UploadInfo,
    responses={
        400: {"description": "Invalid upload id"},
        401: {"description": "Not authenticated"},
        403: {"description": "Upload belongs to another user"},
        404: {"description": "Upload not found"},
    },
)
async def get_upload_info(request: Request, upload_id: str) -> UploadInfo:
    """Get information about an upload.

    Args:
        request: Incoming request (owner, admin, or internal caller)
        upload_id: Upload identifier

    Returns:
        UploadInfo with file list and metadata
    """
    caller = await _authenticate(request)
    metadata = _read_metadata(upload_id)
    _authorize_upload(caller, metadata)

    return UploadInfo(
        upload_id=metadata["upload_id"],
        upload_type=metadata.get(
            "upload_type", "documents"
        ),  # Default for legacy uploads
        files=[UploadedFile(**f) for f in metadata["files"]],
        created_at=metadata["created_at"],
    )


@router.get(
    "/{upload_id}/files",
    response_model=List[UploadedFile],
    responses={
        400: {"description": "Invalid upload id"},
        401: {"description": "Not authenticated"},
        403: {"description": "Upload belongs to another user"},
        404: {"description": "Upload not found"},
    },
)
async def list_upload_files(request: Request, upload_id: str) -> List[UploadedFile]:
    """List files in an upload.

    Args:
        request: Incoming request (owner, admin, or internal caller)
        upload_id: Upload identifier

    Returns:
        List of uploaded files with metadata
    """
    info = await get_upload_info(request, upload_id)
    return info.files


@router.get(
    "/{upload_id}/files/{filename}",
    responses={
        400: {"description": "Invalid upload id"},
        401: {"description": "Not authenticated"},
        403: {"description": "Upload belongs to another user"},
        404: {"description": "File not found"},
    },
)
async def get_uploaded_file(
    request: Request, upload_id: str, filename: str
) -> FileResponse:
    """Download a specific file from an upload.

    Args:
        request: Incoming request (owner, admin, or internal caller)
        upload_id: Upload identifier
        filename: Name of file to download

    Returns:
        File content with appropriate Content-Type
    """
    caller = await _authenticate(request)
    _authorize_upload(caller, _read_metadata(upload_id))
    upload_dir = _upload_dir(upload_id)

    # Sanitize filename to prevent path traversal
    safe_filename = _sanitize_filename(filename)
    file_path = upload_dir / safe_filename

    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    # Verify file is within upload directory (extra safety)
    try:
        file_path.resolve().relative_to(upload_dir.resolve())
    except ValueError:
        raise HTTPException(status_code=404, detail="File not found")

    media_type = _get_media_type(file_path)
    return FileResponse(path=file_path, media_type=media_type, filename=file_path.name)


@router.delete(
    "/{upload_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        400: {"description": "Invalid upload id"},
        401: {"description": "Not authenticated"},
        403: {"description": "Upload belongs to another user"},
        404: {"description": "Upload not found"},
    },
)
async def delete_upload(request: Request, upload_id: str) -> None:
    """Delete an upload and all its files.

    Args:
        request: Incoming request (owner, admin, or internal caller)
        upload_id: Upload identifier
    """
    caller = await _authenticate(request)
    _authorize_upload(caller, _read_metadata(upload_id))

    shutil.rmtree(_upload_dir(upload_id))
    logger.info(f"Deleted upload: {upload_id}")
