"""Runtime-actor authorization/refresh and the Officer verification plan API.

Extracted verbatim from ``orchestrator.main`` (R1.B06, lane C, census group
``J_runtime_verification``). Five routes and two helpers, and what holds them
together is a single rule: **none of these responses may carry identity.**

Three properties are load-bearing and moved unchanged:

* **A refused refresh answers 503 and says nothing else.** Both the deliberate
  pre-mutation refusal (``retryable_failure_code``) and the committed-but-
  withheld payload (``response_lost``) collapse to the same
  ``{"code": "runtime_maintenance_unavailable", "retryable": True}`` body. The
  two cases are distinguishable server-side and deliberately indistinguishable
  on the runtime transport, because telling them apart would leak whether a
  plan, binding or credential exists (port contract §P7).
* **The disabled feature is a 404, not a 403.** ``RuntimeVerificationPlanError``
  carries its own status code and ``code``/``message`` body;
  :func:`runtime_verification_http_error` is the single place that shape is
  built, so a new error code cannot accidentally acquire a different status.
* **The audit record is written only after the action committed**, and it names
  the plan id, exercise, action and replay flag — never a credential.

``OFFICER_RUNTIME_VERIFICATION_ENABLED`` arrives as a *callable* on the
dependency dataclass rather than a value, so a flag rebound on the application
(or by a test) still steers these routes (port contract §P1).

**The access gate belongs to the caller, not to these functions.** Each was the
body of a route whose first statement was ``require_internal`` / ``_require_admin``;
that statement now lives in
``orchestrator.routers.officer_runtime_verification`` (and in the application's
compatibility wrapper), which is where the endpoint-inventory gate scanner reads
the audited policy identity. The two admin actions that need the caller's
identity take it as an explicit ``admin`` argument — the *verified* row the gate
returned. Passing an unverified value there defeats the gate, so do not call
these from an ungated path.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal

from fastapi import HTTPException, Request

from orchestrator.schemas.officer_runtime_verification import (
    OfficerRuntimeVerificationPlanRequest,
    RuntimeActorAuthorizationRequest,
)
from orchestrator.services.runtime_actor_verification import (
    RuntimeVerificationPlanError,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OfficerRuntimeVerificationDependencies:
    """Collaborators for one runtime-verification request, per invocation.

    Nothing here is captured at import. ``store`` is rebound during
    ``lifespan``; the guards, the audit sink, the three plan operations and the
    Officer drain kick are all names tests rebind on the application module, and
    an extracted module that imported them directly would resolve a different
    object than the one a caller patched.
    """

    store: Any
    logger: logging.Logger

    # Guards and audit (owned by orchestrator.security.access, bound by main).
    require_internal: Callable[[Request], Awaitable[None]]
    require_admin: Callable[[Request], Awaitable[dict[str, Any]]]
    log_security_event: Callable[..., Awaitable[Any]]

    # Import-time flag as a callable (port contract §P1).
    officer_runtime_verification_enabled: Callable[[], bool]

    # Runtime actor exchange (orchestrator.services.runtime_actor).
    authorize_runtime_actor_request: Callable[..., Awaitable[Any]]
    refresh_runtime_actor_exchange: Callable[..., Awaitable[Any]]

    # Verification plan operations (orchestrator.services.runtime_actor_verification).
    create_runtime_verification_plan: Callable[..., Awaitable[dict[str, Any]]]
    get_runtime_verification_plan: Callable[..., Awaitable[dict[str, Any]]]
    transition_runtime_verification_plan: Callable[..., Awaitable[dict[str, Any]]]

    # B07 Officer conference lane — consumed, never re-implemented (§P10).
    kick_officer_event_drain: Callable[[Any], Any]


def runtime_verification_http_error(
    exc: RuntimeVerificationPlanError,
) -> HTTPException:
    return HTTPException(
        status_code=exc.status_code,
        detail={"code": exc.code, "message": exc.message},
    )


async def audit_runtime_verification_action(
    *,
    request: Request,
    admin: dict[str, Any],
    project_id: str,
    plan: dict[str, Any],
    action: Literal["create", "recover", "disarm"],
    dependencies: OfficerRuntimeVerificationDependencies,
) -> None:
    """Record a successful secret-free admin verification action."""

    event_type = {
        "create": "officer_runtime_verification_created",
        "recover": "officer_runtime_verification_recovery_requested",
        "disarm": "officer_runtime_verification_disarmed",
    }[action]
    await dependencies.log_security_event(
        dependencies.store,
        event_type=event_type,
        user=admin,
        resource_type="officer_runtime_verification",
        resource_id=str(plan.get("plan_id") or ""),
        detail=(
            f"project_id={project_id} plan_id={plan.get('plan_id')} "
            f"exercise={plan.get('exercise')} action={action} "
            f"replayed={str(bool(plan.get('replayed'))).lower()}"
        ),
        request=request,
    )


async def authorize_runtime_actor(
    request: Request,
    body: RuntimeActorAuthorizationRequest,
    *,
    dependencies: OfficerRuntimeVerificationDependencies,
) -> dict[str, Any]:
    """Authorize a hidden runtime actor against current server-side state."""

    actor = await dependencies.authorize_runtime_actor_request(
        dependencies.store,
        request,
        action=body.action,
        project_id=body.project_id,
    )
    return {
        "authorized": True,
        "code": "authorized",
        "action": body.action,
        "actor": actor.audit_payload(),
        "message": "Runtime actor is authorized.",
    }


async def refresh_runtime_actor(
    request: Request,
    *,
    dependencies: OfficerRuntimeVerificationDependencies,
) -> dict[str, Any]:
    """Refresh a short-lived actor access token after revalidating identity."""

    exchange = await dependencies.refresh_runtime_actor_exchange(
        dependencies.store,
        request,
        verification_enabled=dependencies.officer_runtime_verification_enabled(),
    )
    if exchange.retryable_failure_code or exchange.response_lost:
        # The authoritative refresh transaction has either been deliberately
        # refused before mutation or committed with its credential payload
        # intentionally withheld. Never echo plan/binding/credential details
        # onto the runtime transport.
        raise HTTPException(
            status_code=503,
            detail={
                "code": "runtime_maintenance_unavailable",
                "retryable": True,
            },
        )
    actor = exchange.actor
    if actor is None:  # defensive: every non-fault exchange carries an actor
        raise HTTPException(status_code=503, detail="Runtime maintenance unavailable")
    if actor.caller_kind == "officer":
        dependencies.kick_officer_event_drain(dependencies.store)
    return {"runtime_actor": actor.to_payload()}


async def create_officer_runtime_verification(
    project_id: str,
    body: OfficerRuntimeVerificationPlanRequest,
    request: Request,
    *,
    admin: dict[str, Any],
    dependencies: OfficerRuntimeVerificationDependencies,
) -> dict[str, Any]:
    """Arm one exact commissioned-Officer verification plan (admin only).

    ``admin`` is the row the caller's admin gate already verified.
    """

    try:
        plan = await dependencies.create_runtime_verification_plan(
            dependencies.store,
            enabled=dependencies.officer_runtime_verification_enabled(),
            project_id=project_id,
            idempotency_key=str(body.idempotency_key),
            exercise=body.exercise,
            created_by=str(admin["id"]),
            expires_in_seconds=body.expires_in_seconds,
            logical_window_seconds=body.logical_window_seconds,
            response_losses=body.response_losses,
            response_loss_gap_seconds=body.response_loss_gap_seconds,
        )
    except RuntimeVerificationPlanError as exc:
        raise runtime_verification_http_error(exc) from exc
    await audit_runtime_verification_action(
        request=request,
        admin=admin,
        project_id=project_id,
        plan=plan,
        action="create",
        dependencies=dependencies,
    )
    return {"enabled": True, "plan": plan}


async def read_officer_runtime_verification(
    project_id: str,
    request: Request,
    *,
    dependencies: OfficerRuntimeVerificationDependencies,
) -> dict[str, Any]:
    """Read the secret-free durable plan projection (admin only)."""

    try:
        plan = await dependencies.get_runtime_verification_plan(
            dependencies.store,
            enabled=dependencies.officer_runtime_verification_enabled(),
            project_id=project_id,
        )
    except RuntimeVerificationPlanError as exc:
        raise runtime_verification_http_error(exc) from exc
    return {"enabled": True, "plan": plan}


async def transition_officer_runtime_verification(
    project_id: str,
    plan_id: str,
    action: Literal["recover", "disarm"],
    request: Request,
    *,
    admin: dict[str, Any],
    dependencies: OfficerRuntimeVerificationDependencies,
) -> dict[str, Any]:
    """Recover or disarm the exact plan; neither operation carries identity.

    ``admin`` is the row the caller's admin gate already verified.
    """

    try:
        plan = await dependencies.transition_runtime_verification_plan(
            dependencies.store,
            enabled=dependencies.officer_runtime_verification_enabled(),
            project_id=project_id,
            plan_id=plan_id,
            action=action,
            actor_id=str(admin["id"]),
        )
    except RuntimeVerificationPlanError as exc:
        raise runtime_verification_http_error(exc) from exc
    await audit_runtime_verification_action(
        request=request,
        admin=admin,
        project_id=project_id,
        plan=plan,
        action=action,
        dependencies=dependencies,
    )
    return {"enabled": True, "plan": plan}


__all__ = [
    "OfficerRuntimeVerificationDependencies",
    "audit_runtime_verification_action",
    "authorize_runtime_actor",
    "create_officer_runtime_verification",
    "read_officer_runtime_verification",
    "refresh_runtime_actor",
    "runtime_verification_http_error",
    "transition_officer_runtime_verification",
]
