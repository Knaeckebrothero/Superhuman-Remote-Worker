"""``/api/notifications`` — the unified feed.

Extracted from ``orchestrator.main`` (R1.B07 lane N). Seven route declarations,
moved with their handler names, paths, methods, parameter order and docstrings
intact — the docstring is the published OpenAPI description, so it is part of
the route's identity and not editorial text. None carried ``tags``,
``response_model``, ``status_code`` or a ``dependencies`` list, and none
acquires one here.

**Declaration order is load-bearing.** ``GET /api/notifications/events`` is
registered BEFORE ``GET /api/notifications/{notification_id}``: FastAPI matches
in declaration order and the path parameter would otherwise swallow the SSE
path. That was a comment on the moved handler; it is a contract of this module.

Each declaration keeps two things the operation deliberately does not: the
approval gate (``scripts/check_endpoint_auth.py`` reads the audited gate from
the route it is declared on and does not follow a call into a service module)
and the error envelope — ``HTTPException`` re-raised, anything else logged and
answered as a 500 with its text. Both ran in exactly these positions before the
extraction.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from orchestrator.schemas.notifications import (
    NotificationActRequest,
    NotificationSeenRequest,
)
from orchestrator.security.auth import require_approved_user
from orchestrator.services import notification_api

logger = logging.getLogger(__name__)

router = APIRouter()


def get_notification_api_dependencies(
    request: Request,
) -> notification_api.NotificationApiDependencies:
    """Resolve collaborators only from the application handling this request."""
    return request.app.state.notification_api_dependencies_factory()


@router.get("/api/notifications")
async def list_notifications(
    request: Request,
    before: str | None = Query(None),
    limit: int = Query(50, le=200),
    category: list[str] | None = Query(None),
    status: str = Query("all"),
    source_kind: str | None = Query(None),
    source_id: str | None = Query(None),
) -> dict[str, Any]:
    """The current user's notification feed (unified notification system).

    ``items`` is the durable feed: keyset-paged newest first (``before`` is
    the ``next_before`` cursor of the previous page), filterable by
    ``category`` (repeatable), ``status`` (pending | resolved | unread |
    unseen | archived | all) and a ``source_kind`` + ``source_id`` pair
    (e.g. the officer card listing the pages about one officer thread).
    ``counts`` drives the bell. The feed is the only store: every producer
    records here, so there is no legacy view to merge any more.
    """
    dependencies = get_notification_api_dependencies(request)
    try:
        user = await require_approved_user(request, dependencies.store)
        return await notification_api.list_notifications(
            request,
            before,
            limit,
            category,
            status,
            source_kind,
            source_id,
            dependencies=dependencies,
            user=user,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Failed to list notifications: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/api/notifications/seen")
async def mark_notifications_seen(
    request: Request, body: NotificationSeenRequest
) -> dict[str, Any]:
    """Batch seen-stamp — the cockpit posts the ids that rendered in the feed.
    Never regresses an earlier stamp; unknown or foreign ids are ignored."""
    dependencies = get_notification_api_dependencies(request)
    try:
        user = await require_approved_user(request, dependencies.store)
        return await notification_api.mark_notifications_seen(
            request, body, dependencies=dependencies, user=user
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Failed to mark notifications seen: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/api/notifications/events")
async def notification_sse_events(request: Request) -> StreamingResponse:
    """SSE endpoint for real-time notification updates.

    Clients connect via EventSource to receive live notification events.
    Events: new_message, reply_delivered.
    """
    dependencies = get_notification_api_dependencies(request)
    try:
        user = await require_approved_user(request, dependencies.store)
        user_id = str(user["id"])
    except Exception:
        # Allow unauthenticated connections for development
        user_id = "anonymous"

    return await notification_api.notification_sse_events(request, user_id=user_id)


# Registered AFTER /api/notifications/events on purpose: FastAPI matches in
# declaration order, and a `{notification_id}` segment would otherwise
# swallow the SSE path.
@router.get("/api/notifications/{notification_id}")
async def get_notification_detail(
    request: Request, notification_id: str
) -> dict[str, Any]:
    """One feed row plus its source's presentation payload (the detail pane).
    The source loader is registered per ``source_kind``; the center never
    learns what a job or a sudo request is."""
    dependencies = get_notification_api_dependencies(request)
    try:
        user = await require_approved_user(request, dependencies.store)
        return await notification_api.get_notification_detail(
            request, notification_id, dependencies=dependencies, user=user
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Failed to load notification {notification_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.patch("/api/notifications/{notification_id}/read")
async def mark_notification_read_v2(
    request: Request, notification_id: str
) -> dict[str, Any]:
    """Explicit read stamp (also stamps seen). Idempotent."""
    dependencies = get_notification_api_dependencies(request)
    try:
        user = await require_approved_user(request, dependencies.store)
        return await notification_api.mark_notification_read_v2(
            request, notification_id, dependencies=dependencies, user=user
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Failed to mark notification read: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.patch("/api/notifications/{notification_id}/archive")
async def archive_notification(
    request: Request, notification_id: str
) -> dict[str, Any]:
    """Hide a row from the feed without touching its resolution. Idempotent."""
    dependencies = get_notification_api_dependencies(request)
    try:
        user = await require_approved_user(request, dependencies.store)
        return await notification_api.archive_notification(
            request, notification_id, dependencies=dependencies, user=user
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Failed to archive notification: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/api/notifications/{notification_id}/act")
async def act_on_notification(
    request: Request, notification_id: str, body: NotificationActRequest
) -> dict[str, Any]:
    """Run one of the row's declared actions through its registered handler.

    404 when the row is not this user's; 400 when the row does not declare
    the action; 500 when the category declares it but nothing handles it —
    loud, like ``_run_completion_effect``'s registry gate, because a silent
    no-op here would look exactly like a working button.
    """
    dependencies = get_notification_api_dependencies(request)
    try:
        user = await require_approved_user(request, dependencies.store)
        return await notification_api.act_on_notification(
            request, notification_id, body, dependencies=dependencies, user=user
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Notification action failed for {notification_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


__all__ = [
    "act_on_notification",
    "archive_notification",
    "get_notification_api_dependencies",
    "get_notification_detail",
    "list_notifications",
    "mark_notification_read_v2",
    "mark_notifications_seen",
    "notification_sse_events",
    "router",
]
