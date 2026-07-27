"""Authenticated dark endpoint for server-side product capabilities."""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from security.access import (
    log_security_event,
    mcp_scope_project_id,
    require_thread_owner,
)
from security.auth import require_approved_user
from services.product_capabilities import (
    DEFAULT_RESULT_LIMIT,
    MAX_EXPLICIT_CAPABILITY_IDS,
    ProductCapabilityService,
    ResolutionRequest,
    product_capabilities_endpoint_enabled,
)
from src.core.product_capabilities import ProductCapabilitiesResponse

logger = logging.getLogger(__name__)

CAPABILITY_PLANE_HEADER = "X-SRW-Product-Capabilities"
NO_STORE_HEADERS = {
    "Cache-Control": "private, no-store",
    "Pragma": "no-cache",
}

router = APIRouter(
    prefix="/api/users/me/product-capabilities",
    tags=["Product capabilities"],
)


def _get_db() -> Any:
    """Late-resolve the application DB singleton to avoid an import cycle."""

    from main import postgres_db  # type: ignore

    return postgres_db


def _get_service(postgres_db: Any) -> ProductCapabilityService:
    return ProductCapabilityService(postgres_db)


def _unavailable_response() -> JSONResponse:
    logger.info("product_capability_endpoint state=rollout_disabled")
    return JSONResponse(
        status_code=503,
        content={
            "detail": {
                "code": "product_capabilities_unavailable",
                "state": "rollout_disabled",
            }
        },
        headers={
            **NO_STORE_HEADERS,
            CAPABILITY_PLANE_HEADER: "rollout_disabled",
            "Retry-After": "60",
        },
    )


async def _deny_project_scoped_personal_query(
    request: Request,
    postgres_db: Any,
    user: dict[str, Any],
) -> None:
    """Keep a project-scoped MCP token from widening into user/global scope."""

    if mcp_scope_project_id(user) is None:
        return
    await log_security_event(
        postgres_db,
        resource_type="product_capabilities",
        user=user,
        detail="Project-scoped MCP token cannot query user-scoped capabilities",
        request=request,
    )
    raise HTTPException(
        status_code=403,
        detail="Access denied by MCP token scope",
    )


@router.get(
    "",
    response_model=ProductCapabilitiesResponse,
    responses={
        503: {
            "description": "Operator rollout gate is disabled",
        }
    },
)
async def get_product_capabilities(
    request: Request,
    thread_id: UUID | None = Query(default=None),
    topic: str | None = Query(default=None, max_length=80),
    capability_id: list[str] | None = Query(default=None),
    limit: int = Query(default=DEFAULT_RESULT_LIMIT, ge=1, le=50),
) -> JSONResponse:
    """Return a bounded advisory observation for the authenticated caller."""

    postgres_db = _get_db()
    if not product_capabilities_endpoint_enabled():
        await require_approved_user(request, postgres_db)
        return _unavailable_response()

    admitted_thread: dict[str, Any] | None
    if thread_id is not None:
        user, admitted_thread = await require_thread_owner(
            request,
            postgres_db,
            str(thread_id),
        )
    else:
        user = await require_approved_user(request, postgres_db)
        admitted_thread = None
        await _deny_project_scoped_personal_query(request, postgres_db, user)

    raw_capability_ids = tuple(capability_id or ())
    if len(raw_capability_ids) > MAX_EXPLICIT_CAPABILITY_IDS:
        raise HTTPException(
            status_code=422,
            detail=(
                f"At most {MAX_EXPLICIT_CAPABILITY_IDS} capability IDs may be requested"
            ),
        )
    try:
        resolution_request = ResolutionRequest.from_admitted(
            user=user,
            thread=admitted_thread,
            expected_thread_id=thread_id,
            topic=topic,
            capability_ids=raw_capability_ids,
            limit=limit,
        )
    except (ValidationError, ValueError) as exc:
        raise HTTPException(
            status_code=422,
            detail="Invalid product-capability query",
        ) from exc

    result = await _get_service(postgres_db).resolve(
        resolution_request,
        admitted_thread=admitted_thread,
    )
    return JSONResponse(
        content=result.model_dump(mode="json"),
        headers={
            **NO_STORE_HEADERS,
            CAPABILITY_PLANE_HEADER: "enabled",
        },
    )


__all__ = [
    "CAPABILITY_PLANE_HEADER",
    "NO_STORE_HEADERS",
    "get_product_capabilities",
    "router",
]
