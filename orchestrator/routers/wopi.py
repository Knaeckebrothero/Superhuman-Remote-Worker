"""Token-authenticated, read-only WOPI host for Canvas Office Slice 1."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import PurePosixPath
import secrets
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse

from services.canvas_files import (
    CanvasFileError,
    CanvasResponseLease,
    ThreadWorkspaceFileGateway,
    acquire_canvas_response_lease,
)
from services.canvas_office import (
    CanvasOfficeError,
    WopiAccess,
    collabora_config,
    create_wopi_token_service,
)

router = APIRouter(prefix="/wopi", tags=["WOPI"])


class _LeasedWopiResponse(Response):
    """Retain bounded response capacity until the Office bytes finish sending."""

    def __init__(self, *args: Any, lease: CanvasResponseLease, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._canvas_lease = lease

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._canvas_lease.release()


def _get_db() -> Any:
    from main import postgres_db  # type: ignore

    return postgres_db


def _get_token_service():
    return create_wopi_token_service(_get_db())


def _get_file_gateway() -> ThreadWorkspaceFileGateway:
    db = _get_db()
    return ThreadWorkspaceFileGateway(thread_loader=getattr(db, "get_thread", None))


def _get_collabora_config():
    return collabora_config()


def _raise_office_error(error: CanvasOfficeError) -> None:
    raise HTTPException(
        status_code=error.status_code,
        detail={"code": error.code, "message": error.message},
    ) from error


def _raise_file_error(error: CanvasFileError) -> None:
    raise HTTPException(
        status_code=error.status_code,
        detail={"code": error.code, "message": error.message},
    ) from error


def _access_token(request: Request, query_token: str | None) -> str:
    bearer: str | None = None
    authorization = request.headers.get("Authorization")
    if authorization is not None:
        scheme, separator, value = authorization.strip().partition(" ")
        if separator and scheme.lower() == "bearer" and value.strip():
            bearer = value.strip()
        else:
            raise CanvasOfficeError(
                401, "wopi_token_invalid", "A valid WOPI bearer token is required"
            )
    token = (query_token or "").strip()
    if bearer is not None and token and not secrets.compare_digest(bearer, token):
        raise CanvasOfficeError(
            401, "wopi_token_invalid", "Conflicting WOPI access tokens were supplied"
        )
    resolved = bearer or token
    if not resolved or len(resolved) > 8192:
        raise CanvasOfficeError(
            401, "wopi_token_invalid", "A WOPI access token is required"
        )
    return resolved


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


async def _authenticate(
    request: Request,
    file_id: str,
    access_token: str | None,
) -> WopiAccess:
    token = _access_token(request, access_token)
    return await _get_token_service().authenticate(
        token,
        file_id=file_id,
        require_write=False,
    )


async def _recheck(
    request: Request,
    file_id: str,
    access_token: str | None,
) -> WopiAccess:
    """Repeat live admission after the potentially slow workspace read."""

    return await _authenticate(request, file_id, access_token)


@router.get("/files/{file_id}")
async def check_file_info(
    file_id: str,
    request: Request,
    access_token: str | None = Query(default=None),
) -> Response:
    """WOPI CheckFileInfo for one current view-only Office Canvas."""

    response_lease: CanvasResponseLease | None = None
    try:
        config = _get_collabora_config().require_enabled()
        access = await _authenticate(request, file_id, access_token)
        response_lease = await acquire_canvas_response_lease()
        file = await _get_file_gateway().materialize_binary(
            access.thread,
            access.record,
        )
        access = await _recheck(request, file_id, access_token)
        owner_id = str(access.thread.get("user_id") or access.user["id"])
        modified = file.last_modified or access.record.updated_at
        payload = {
            "BaseFileName": PurePosixPath(file.path).name,
            "OwnerId": owner_id,
            "Size": len(file.data),
            "UserId": str(access.user["id"]),
            "UserFriendlyName": str(
                access.user.get("display_name") or access.user["id"]
            ),
            "LastModifiedTime": _utc_iso(modified),
            "PostMessageOrigin": config.cockpit_origin,
            "SupportsLocks": False,
            "UserCanNotWriteRelative": True,
        }
        return JSONResponse(
            payload,
            headers={"Cache-Control": "private, no-store"},
        )
    except CanvasOfficeError as exc:
        _raise_office_error(exc)
    except CanvasFileError as exc:
        _raise_file_error(exc)
    finally:
        if response_lease is not None:
            response_lease.release()


@router.get("/files/{file_id}/contents")
async def get_file(
    file_id: str,
    request: Request,
    access_token: str | None = Query(default=None),
) -> Response:
    """WOPI GetFile through the binary-only bounded Canvas gateway."""

    response_lease: CanvasResponseLease | None = None
    try:
        _get_collabora_config().require_enabled()
        access = await _authenticate(request, file_id, access_token)
        response_lease = await acquire_canvas_response_lease()
        file = await _get_file_gateway().materialize_binary(
            access.thread,
            access.record,
        )
        await _recheck(request, file_id, access_token)
        headers = {
            "Cache-Control": "private, no-store",
            "Content-Disposition": (
                "inline; filename*=UTF-8''" + quote(file.filename, safe="")
            ),
            "Content-Length": str(len(file.data)),
            "Content-Type": file.media_type,
            "ETag": f'"{file.source_version}"',
            "X-Content-Type-Options": "nosniff",
        }
        assert response_lease is not None
        response = _LeasedWopiResponse(
            content=file.data,
            status_code=200,
            headers=headers,
            lease=response_lease,
        )
        response_lease = None
        return response
    except CanvasOfficeError as exc:
        _raise_office_error(exc)
    except CanvasFileError as exc:
        _raise_file_error(exc)
    finally:
        if response_lease is not None:
            response_lease.release()


__all__ = ["check_file_info", "get_file", "router"]
