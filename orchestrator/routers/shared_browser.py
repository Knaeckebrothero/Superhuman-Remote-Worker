"""Owner-scoped shared-browser capability, cold start, and recovery actions."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from routers.canvases import _represent, _state_response
from security.access import require_thread_owner
from services.shared_browser_canvas import (
    BrowserCanvasError,
    BrowserCapabilityResponse,
    browser_capability,
    commit_browser_canvas,
    prepare_browser_canvas,
    require_browser_capability,
)

router = APIRouter(
    prefix="/api/persistent/threads/{thread_id}/browser",
    tags=["Shared browser"],
)


def _get_db() -> Any:
    """Late-resolve the application DB singleton to avoid an import cycle."""

    from main import postgres_db  # type: ignore

    return postgres_db


def _kick_workspace_provisioning(thread_id: str, db: Any) -> None:
    """Fire-and-forget the idempotent session workspace reconcile."""

    from main import (  # type: ignore
        container_provisioner,
        ensure_session_workspace,
        workspace_suspension_service,
    )

    asyncio.create_task(
        ensure_session_workspace(
            thread_id,
            db=db,
            provisioner=container_provisioner,
            suspension=workspace_suspension_service,
        )
    )


class BrowserOpenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=200)
    expected_presentation_revision: int | None = Field(default=None, ge=1)


def _raise_browser_error(error: BrowserCanvasError) -> None:
    raise HTTPException(
        status_code=error.status_code,
        detail={"code": error.code, "message": error.message},
    ) from error


def _require_openable(
    capability: BrowserCapabilityResponse,
    *,
    require_ready: bool,
) -> None:
    try:
        require_browser_capability(capability, require_ready=require_ready)
    except BrowserCanvasError as exc:
        _raise_browser_error(exc)


@router.get("/capability", response_model=BrowserCapabilityResponse)
async def get_browser_capability(
    thread_id: str,
    request: Request,
) -> BrowserCapabilityResponse:
    """Return the closed pre-source capability only after owner admission."""

    db = _get_db()
    _, thread = await require_thread_owner(request, db, thread_id)
    return browser_capability(thread)


@router.post("/open")
async def open_shared_browser(
    thread_id: str,
    request: Request,
    body: BrowserOpenRequest,
) -> Any:
    """Provision/reuse a browser and return ordinary redacted Canvas state."""

    db = _get_db()
    _, thread = await require_thread_owner(request, db, thread_id)
    capability = browser_capability(thread)
    _require_openable(capability, require_ready=False)
    if not capability.workspace_ready:
        _kick_workspace_provisioning(thread_id, db)
        return JSONResponse(
            status_code=202,
            content={"status": "provisioning"},
            headers={"Retry-After": "1"},
        )

    async def generation_resolver() -> dict[str, Any]:
        current = await db.get_thread(thread_id)
        return dict(current) if current else {}

    try:
        prepared = await prepare_browser_canvas(
            thread,
            initial_baton="user",
            generation_resolver=generation_resolver,
        )
        # Browser startup can be long. Re-run owner and capability admission
        # immediately before the durable transition.
        _, current = await require_thread_owner(request, db, thread_id)
        _require_openable(browser_capability(current), require_ready=True)
        mutation = await commit_browser_canvas(
            db,
            thread_id,
            prepared,
            title=(body.title or "").strip() or "Shared browser",
            expected_presentation_revision=body.expected_presentation_revision,
        )
    except BrowserCanvasError as exc:
        _raise_browser_error(exc)

    assert mutation.record is not None
    _, visible_thread = await require_thread_owner(request, db, thread_id)
    representation = await _represent(
        thread_id,
        visible_thread,
        mutation.record,
        browser_content_url=True,
        db=db,
    )
    return _state_response(
        representation.payload,
        representation.etag,
        extra_headers={
            "X-Canvas-Mutation-Changed": str(mutation.changed).lower(),
        },
    )


__all__ = [
    "BrowserOpenRequest",
    "get_browser_capability",
    "open_shared_browser",
    "router",
]
