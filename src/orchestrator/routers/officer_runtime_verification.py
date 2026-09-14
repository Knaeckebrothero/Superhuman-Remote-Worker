"""HTTP adapter for runtime-actor exchange and Officer runtime verification.

Extracted from ``orchestrator.main`` (R1.B06, lane C, census group
``J_runtime_verification``). Five routes: two internal runtime-actor exchanges
and three admin-only verification-plan operations.

No ``tags=``, no ``operation_id=``, no ``status_code=``, no route-level
``dependencies=`` — the declarations this replaces carried none, and every one
of those would change the published OpenAPI operation for a route this batch is
required to leave byte-identical. The dependency dataclass is resolved by a
plain call rather than ``Depends`` for the same reason: a ``Depends`` default
would add an entry to the route's dependant.

**The access gate is called here, as each handler's first statement**, exactly as
it was in the application module. That placement preserves the pre-extraction
ordering and is where ``scripts/check_endpoint_auth.py`` reads the audited gate
identity for ``policy/endpoint_inventory.txt``. The two admin actions that need
the caller's identity hand the *verified* admin row down to the service rather
than letting it re-derive one.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Request

from orchestrator.schemas.officer_runtime_verification import (
    OfficerRuntimeVerificationPlanRequest,
    RuntimeActorAuthorizationRequest,
)
from orchestrator.services import officer_runtime_verification

router = APIRouter()


def get_officer_runtime_verification_dependencies(
    request: Request,
) -> officer_runtime_verification.OfficerRuntimeVerificationDependencies:
    """Resolve collaborators only from the application handling this request."""
    return request.app.state.officer_runtime_verification_dependencies_factory()


@router.post("/api/runtime-actors/authorize")
async def authorize_runtime_actor(
    request: Request, body: RuntimeActorAuthorizationRequest
) -> dict[str, Any]:
    """Authorize a hidden runtime actor against current server-side state."""

    dependencies = get_officer_runtime_verification_dependencies(request)
    await dependencies.require_internal(request)
    return await officer_runtime_verification.authorize_runtime_actor(
        request, body, dependencies=dependencies
    )


@router.post("/api/runtime-actors/refresh")
async def refresh_runtime_actor(request: Request) -> dict[str, Any]:
    """Refresh a short-lived actor access token after revalidating identity."""

    dependencies = get_officer_runtime_verification_dependencies(request)
    await dependencies.require_internal(request)
    return await officer_runtime_verification.refresh_runtime_actor(
        request, dependencies=dependencies
    )


@router.post("/api/admin/projects/{project_id}/officer/runtime-verification")
async def create_officer_runtime_verification(
    project_id: str,
    body: OfficerRuntimeVerificationPlanRequest,
    request: Request,
) -> dict[str, Any]:
    """Arm one exact commissioned-Officer verification plan (admin only)."""

    dependencies = get_officer_runtime_verification_dependencies(request)
    admin = await dependencies.require_admin(request)
    return await officer_runtime_verification.create_officer_runtime_verification(
        project_id, body, request, admin=admin, dependencies=dependencies
    )


@router.get("/api/admin/projects/{project_id}/officer/runtime-verification")
async def read_officer_runtime_verification(
    project_id: str, request: Request
) -> dict[str, Any]:
    """Read the secret-free durable plan projection (admin only)."""

    dependencies = get_officer_runtime_verification_dependencies(request)
    await dependencies.require_admin(request)
    return await officer_runtime_verification.read_officer_runtime_verification(
        project_id, request, dependencies=dependencies
    )


@router.post(
    "/api/admin/projects/{project_id}/officer/runtime-verification/{plan_id}/{action}"
)
async def transition_officer_runtime_verification(
    project_id: str,
    plan_id: str,
    action: Literal["recover", "disarm"],
    request: Request,
) -> dict[str, Any]:
    """Recover or disarm the exact plan; neither operation carries identity."""

    dependencies = get_officer_runtime_verification_dependencies(request)
    admin = await dependencies.require_admin(request)
    return await officer_runtime_verification.transition_officer_runtime_verification(
        project_id, plan_id, action, request, admin=admin, dependencies=dependencies
    )


__all__ = [
    "authorize_runtime_actor",
    "create_officer_runtime_verification",
    "get_officer_runtime_verification_dependencies",
    "read_officer_runtime_verification",
    "refresh_runtime_actor",
    "router",
    "transition_officer_runtime_verification",
]
