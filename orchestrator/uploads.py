"""File upload API endpoints for job creation.

Provides endpoints for uploading:
- Documents: Files that will be processed by agents
- Config: YAML files that override agent defaults
- Instructions: Markdown files with task instructions

Files are stored in workspace/uploads/<upload_id>/ and referenced by upload_id
when creating jobs.
"""

import json
import logging
import re
import secrets
import shutil
import time
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import List

from fastapi import APIRouter, HTTPException, Query, UploadFile, File, status
from fastapi.responses import FileResponse
from pydantic import BaseModel


class UploadType(str, Enum):
    """Type of upload for validation and routing."""

    DOCUMENTS = "documents"
    CONFIG = "config"
    INSTRUCTIONS = "instructions"

router = APIRouter(prefix="/api/uploads", tags=["Uploads"])
logger = logging.getLogger(__name__)

# Storage directory (relative to where orchestrator runs)
UPLOADS_DIR = Path("workspace/uploads")

# Limits
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB per file
MAX_FILES_PER_UPLOAD = 100


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


# =============================================================================
# Endpoints
# =============================================================================


@router.post(
    "",
    response_model=UploadResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        400: {"description": "No files provided, too many files, or invalid file type"},
        413: {"description": "File exceeds maximum size"},
    },
)
async def upload_files(
    files: List[UploadFile] = File(...),
    upload_type: UploadType = Query(default=UploadType.DOCUMENTS),
) -> UploadResponse:
    """Upload files for job creation.

    Files are stored in workspace/uploads/<upload_id>/ and can be referenced
    when creating a job via the upload_id.

    Upload types:
    - documents: General documents for agent processing (default)
    - config: Single YAML file to override agent configuration
    - instructions: Single markdown/text file with task instructions

    Limits:
    - Maximum 50MB per file
    - Maximum 100 files per upload (documents only)
    - Config and instructions must be exactly 1 file

    Args:
        files: List of files to upload
        upload_type: Type of upload (documents, config, or instructions)

    Returns:
        UploadResponse with upload_id and file metadata
    """
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

    # Generate typed upload ID
    timestamp = int(time.time() * 1000)
    random_suffix = secrets.token_hex(8)
    upload_id = f"{upload_type.value}_{timestamp}_{random_suffix}"

    # Create upload directory
    upload_dir = UPLOADS_DIR / upload_id
    upload_dir.mkdir(parents=True, exist_ok=True)

    uploaded_files: List[UploadedFile] = []

    try:
        for file in files:
            # Read and validate size
            contents = await file.read()
            if len(contents) > MAX_FILE_SIZE:
                raise HTTPException(
                    status_code=413,
                    detail=f"File '{file.filename}' exceeds maximum size of 50MB",
                )

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

            # Save file
            file_path.write_bytes(contents)

            uploaded_files.append(
                UploadedFile(
                    name=file_path.name,
                    size=len(contents),
                    mime_type=file.content_type or "application/octet-stream",
                )
            )

            logger.info(
                f"Uploaded: {file.filename} -> {upload_id}/{file_path.name} "
                f"({len(contents)} bytes)"
            )

        # Save metadata
        metadata = {
            "upload_id": upload_id,
            "upload_type": upload_type.value,
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
    responses={404: {"description": "Upload not found"}},
)
async def get_upload_info(upload_id: str) -> UploadInfo:
    """Get information about an upload.

    Args:
        upload_id: Upload identifier

    Returns:
        UploadInfo with file list and metadata
    """
    upload_dir = UPLOADS_DIR / upload_id
    metadata_path = upload_dir / "metadata.json"

    if not metadata_path.exists():
        raise HTTPException(status_code=404, detail="Upload not found")

    metadata = json.loads(metadata_path.read_text())

    return UploadInfo(
        upload_id=metadata["upload_id"],
        upload_type=metadata.get("upload_type", "documents"),  # Default for legacy uploads
        files=[UploadedFile(**f) for f in metadata["files"]],
        created_at=metadata["created_at"],
    )


@router.get(
    "/{upload_id}/files",
    response_model=List[UploadedFile],
    responses={404: {"description": "Upload not found"}},
)
async def list_upload_files(upload_id: str) -> List[UploadedFile]:
    """List files in an upload.

    Args:
        upload_id: Upload identifier

    Returns:
        List of uploaded files with metadata
    """
    info = await get_upload_info(upload_id)
    return info.files


@router.get(
    "/{upload_id}/files/{filename}",
    responses={404: {"description": "File not found"}},
)
async def get_uploaded_file(upload_id: str, filename: str) -> FileResponse:
    """Download a specific file from an upload.

    Args:
        upload_id: Upload identifier
        filename: Name of file to download

    Returns:
        File content with appropriate Content-Type
    """
    # Sanitize filename to prevent path traversal
    safe_filename = _sanitize_filename(filename)
    file_path = UPLOADS_DIR / upload_id / safe_filename

    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    # Verify file is within upload directory (extra safety)
    try:
        file_path.resolve().relative_to((UPLOADS_DIR / upload_id).resolve())
    except ValueError:
        raise HTTPException(status_code=404, detail="File not found")

    media_type = _get_media_type(file_path)
    return FileResponse(path=file_path, media_type=media_type, filename=file_path.name)


@router.delete(
    "/{upload_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={404: {"description": "Upload not found"}},
)
async def delete_upload(upload_id: str) -> None:
    """Delete an upload and all its files.

    Args:
        upload_id: Upload identifier
    """
    upload_dir = UPLOADS_DIR / upload_id

    if not upload_dir.exists():
        raise HTTPException(status_code=404, detail="Upload not found")

    shutil.rmtree(upload_dir)
    logger.info(f"Deleted upload: {upload_id}")
