"""Owner-facing Dynamic Canvas state routes (Slice 0).

Only authoritative state reads and conditional clear are public in this slice.
There is intentionally no public source setter, no internal agent adapter, and
no file/live-app content route until a usable source validator ships.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response

from security.access import require_thread_owner
from services.canvas import (
    CanvasPreconditionFailed,
    CanvasPreconditionRequired,
    CanvasPublicState,
    CanvasService,
    build_public_canvas_representation,
)

router = APIRouter(
    prefix="/api/persistent/threads/{thread_id}/canvases",
    tags=["Dynamic Canvas"],
)

_STATE_CACHE_CONTROL = "private, no-cache"


def _get_db() -> Any:
    """Late-resolve the main app DB singleton and avoid an import cycle."""

    from main import postgres_db  # type: ignore

    return postgres_db


def _get_canvas_service(db: Any) -> CanvasService:
    return CanvasService(db)


def _state_response(payload: bytes, etag: str, *, status_code: int = 200) -> Response:
    return Response(
        content=payload,
        status_code=status_code,
        media_type="application/json",
        headers={
            "ETag": etag,
            "Cache-Control": _STATE_CACHE_CONTROL,
        },
    )


def _no_content_response() -> Response:
    return Response(
        status_code=204,
        headers={"Cache-Control": _STATE_CACHE_CONTROL},
    )


def _if_none_match_matches(header_value: str | None, current_etag: str) -> bool:
    """Apply weak comparison for a conditional GET (RFC 9110 section 13.1.2)."""

    if not header_value:
        return False
    for candidate in header_value.split(","):
        candidate = candidate.strip()
        if candidate == "*":
            return True
        if candidate.startswith("W/"):
            candidate = candidate[2:].lstrip()
        if candidate == current_etag:
            return True
    return False


@router.get(
    "/main",
    response_model=CanvasPublicState,
    responses={
        204: {"description": "No Canvas has been created for this thread"},
        304: {"description": "The caller's cached representation is current"},
    },
)
async def get_main_canvas(thread_id: str, request: Request) -> Response:
    """Return authoritative state, or 204 if this thread never had a Canvas."""

    db = _get_db()
    await require_thread_owner(request, db, thread_id)
    record = await _get_canvas_service(db).get(thread_id)
    if record is None:
        return _no_content_response()

    representation = build_public_canvas_representation(record)
    if _if_none_match_matches(
        request.headers.get("If-None-Match"), representation.etag
    ):
        return Response(
            status_code=304,
            headers={
                "ETag": representation.etag,
                "Cache-Control": _STATE_CACHE_CONTROL,
            },
        )
    return _state_response(representation.payload, representation.etag)


@router.delete(
    "/main",
    response_model=CanvasPublicState,
    responses={
        204: {"description": "Canvas was absent or already cleared"},
        412: {"description": "If-Match does not match current Canvas state"},
        428: {"description": "If-Match is required for a populated Canvas"},
    },
)
async def clear_main_canvas(thread_id: str, request: Request) -> Response:
    """Clear the current source without deleting it.

    A populated row requires the exact current public state ETag.  An absent or
    already-cleared row is an idempotent 204 even without ``If-Match``.
    """

    db = _get_db()
    await require_thread_owner(request, db, thread_id)
    service = _get_canvas_service(db)
    try:
        mutation = await service.clear(
            thread_id,
            expected_etag=request.headers.get("If-Match"),
        )
    except CanvasPreconditionRequired as exc:
        raise HTTPException(
            status_code=428,
            detail={
                "code": "canvas_precondition_required",
                "message": str(exc),
            },
        ) from exc
    except CanvasPreconditionFailed as exc:
        raise HTTPException(
            status_code=412,
            detail={
                "code": "canvas_precondition_failed",
                "message": str(exc),
            },
        ) from exc

    if not mutation.changed:
        return _no_content_response()

    # ``changed`` implies a committed cleared record.
    assert mutation.record is not None
    representation = build_public_canvas_representation(mutation.record)
    return _state_response(representation.payload, representation.etag)


__all__ = ["router"]
