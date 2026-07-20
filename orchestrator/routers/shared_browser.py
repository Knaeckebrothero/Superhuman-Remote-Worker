"""Shared-browser cold-start and recovery endpoint.

The endpoint rejects workspace-less sessions, ensures a cold workspace, asks
``browser-exec`` for the live stream identity over SSH, and then pins that
browser generation into the existing Canvas control plane.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from security.access import require_thread_owner
from services.browser_stream_broker import (
    BrowserStreamUnavailable,
    exec_stream_info,
    workspace_ready,
)
from services.browser_stream_config import browser_stream_config
from services.canvas import BrowserSource, CanvasService, CanvasSetInput
from src.core.backends.factory import LITE_BACKENDS

router = APIRouter(
    prefix="/api/persistent/threads/{thread_id}/browser",
    tags=["Shared browser"],
)


def _get_db() -> Any:
    """Late-resolve the application DB singleton to avoid an import cycle."""

    from main import postgres_db  # type: ignore

    return postgres_db


def _get_canvas_service(db: Any) -> CanvasService:
    return CanvasService(db)


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return {}
    return value if isinstance(value, dict) else {}


def _thread_backend(thread: dict[str, Any]) -> str:
    """Read both current session metadata and the legacy/direct fixture shape."""

    metadata = _mapping(thread.get("metadata"))
    direct = metadata.get("workspace_backend")
    if isinstance(direct, str) and direct:
        return direct
    config_override = _mapping(metadata.get("config_override"))
    workspace = _mapping(config_override.get("workspace"))
    backend = workspace.get("backend")
    return backend if isinstance(backend, str) and backend else "container"


def _is_lite_backend(thread: dict[str, Any]) -> bool:
    return _thread_backend(thread) in LITE_BACKENDS


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

    opened_by: Literal["user", "agent"] = "user"
    title: str | None = Field(default=None, max_length=200)


@router.post("/open")
async def open_shared_browser(
    thread_id: str,
    request: Request,
    body: BrowserOpenRequest,
) -> Any:
    config = browser_stream_config()
    if not config.enabled:
        raise HTTPException(status_code=404, detail="Shared browser is not enabled")

    db = _get_db()
    _, thread = await require_thread_owner(request, db, thread_id)
    if _is_lite_backend(thread):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "workspace_required",
                "message": (
                    "The shared browser needs a full workspace; this session "
                    "runs on a lite backend without one."
                ),
            },
        )

    if not workspace_ready(thread):
        _kick_workspace_provisioning(thread_id, db)
        return JSONResponse(
            status_code=202,
            content={"status": "provisioning"},
        )

    try:
        info = await exec_stream_info(thread, initial_baton=body.opened_by)
        generation = UUID(info["generation"])
    except BrowserStreamUnavailable as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=502,
            detail="browser-exec returned invalid stream identity",
        ) from exc

    await _get_canvas_service(db).set(
        thread_id,
        CanvasSetInput(
            source=BrowserSource(browser_generation=generation),
            title=(body.title or "").strip() or "Shared browser",
            renderer="auto",
            editable=False,
        ),
    )
    return {
        "status": "ready",
        "generation": str(generation),
        "stream_port": config.stream_port,
    }
