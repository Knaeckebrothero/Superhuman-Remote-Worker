"""HTTP adapter for the outbound media proxy.

One route, and the whole adapter is the boundary: the approved-user (and CSRF)
gate runs before any outbound network work, the fetch itself is
:mod:`orchestrator.services.remote_image`, and the response headers below are
the ruling — the bytes are served inert, uncached, same-origin only, and never
sniffed into another type.

Neither the requested URL nor the returned bytes are logged or persisted
anywhere in this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from orchestrator.schemas.media import RemoteImageRequest
from orchestrator.security.auth import require_approved_user
from orchestrator.services.remote_image import RemoteImageError, fetch_remote_image

router = APIRouter()


@dataclass(frozen=True)
class MediaDependencies:
    """Per-app auth store and the approved-user gate; no operations service."""

    store: Any
    require_approved_user: Callable[..., Awaitable[Any]] = require_approved_user


def get_media_dependencies(request: Request) -> MediaDependencies:
    """Resolve collaborators only from the application handling this request."""
    return request.app.state.media_dependencies_factory()


@router.post("/api/media/remote-image")
async def load_remote_image(
    request: Request,
    body: RemoteImageRequest,
    *,
    dependencies: MediaDependencies = Depends(get_media_dependencies),
) -> Response:
    """Fetch one user-reviewed public raster image through a safe boundary.

    The approved-user and CSRF gates apply before any outbound network work.
    The fetcher pins public DNS results, revalidates redirects, strips browser
    identity, and bounds/validates the bytes. URLs and image bytes are neither
    logged here nor persisted.
    """

    await dependencies.require_approved_user(request, dependencies.store)
    try:
        image = await fetch_remote_image(body.url)
    except RemoteImageError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": exc.message},
        ) from exc

    extension = {
        "image/png": "png",
        "image/jpeg": "jpg",
        "image/gif": "gif",
        "image/webp": "webp",
    }[image.media_type]
    return Response(
        content=image.content,
        media_type=image.media_type,
        headers={
            "Cache-Control": "private, no-store",
            "Content-Disposition": f'inline; filename="remote-image.{extension}"',
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "Cross-Origin-Resource-Policy": "same-origin",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        },
    )
