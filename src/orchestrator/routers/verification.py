"""Internal verification-ledger and completion-decision HTTP adapters."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Request

from orchestrator.services import verification_workflow

router = APIRouter()


@dataclass(frozen=True)
class VerificationRouteDependencies:
    """Per-application auth gate and verification workflow dependencies."""

    workflow: verification_workflow.VerificationDependencies
    require_internal: Callable[[Request], Awaitable[None]]


def get_verification_route_dependencies(
    request: Request,
) -> VerificationRouteDependencies:
    """Resolve collaborators only from the application serving the request."""

    return request.app.state.verification_route_dependencies_factory()


@router.post("/api/jobs/{target_job_id}/verification/rounds")
async def record_verification_round(
    request: Request, target_job_id: str
) -> dict[str, Any]:
    """Record one verification round on the TARGET job's durable ledger.

    **Internal** (P4b) — requires ``X-Internal-Key``. Ingress strips this path.
    Called by the critic's verdict tools BEFORE they return, so the verdict is
    durable before anything observes it (journal-before-observe). The verdict in
    the response is COMPUTED from the open findings, not taken from the caller.
    """

    dependencies = get_verification_route_dependencies(request)
    await dependencies.require_internal(request)
    body = await request.json()
    return await verification_workflow.record_verification_round(
        target_job_id=target_job_id,
        critic_job_id=str(body.get("critic_job_id") or ""),
        asserted_verdict=str(body.get("asserted_verdict") or ""),
        opened=body.get("opened") or [],
        dispositions=body.get("dispositions") or [],
        head_commit=body.get("head_commit"),
        content_tree=body.get("content_tree"),
        dependencies=dependencies.workflow,
    )


@router.post("/api/jobs/{job_id}/completion-decision")
async def record_completion_decision(request: Request, job_id: str) -> dict[str, Any]:
    """Durably journal the agent's job_complete decision on the job row.

    **Internal** (P4b) — requires ``X-Internal-Key``. Ingress strips this path.
    Called by the worker's ``job_complete`` tool BEFORE it returns, so the
    decision survives any agent restart (journal-before-observe — the sibling
    of ``/verification/rounds`` for the worker's own terminating decision).
    """

    dependencies = get_verification_route_dependencies(request)
    await dependencies.require_internal(request)
    body = await request.json()
    try:
        confidence = float(body.get("confidence", 1.0))
    except (TypeError, ValueError):
        confidence = 1.0
    return await verification_workflow.record_completion_decision(
        job_id=job_id,
        tool_call_id=str(body.get("tool_call_id") or ""),
        summary=str(body.get("summary") or ""),
        deliverables=body.get("deliverables") or [],
        confidence=confidence,
        notes=body.get("notes"),
        dependencies=dependencies.workflow,
    )


@router.get("/api/jobs/{job_id}/completion-decision")
async def get_completion_decision(request: Request, job_id: str) -> dict[str, Any]:
    """Read back the journaled job_complete decision (or null).

    **Internal** (P4b) — requires ``X-Internal-Key``. Used by the agent's
    resume hydration so a restarted process re-seeds its in-memory cache from
    the durable record instead of treating "I decided" as "no decision".
    """

    dependencies = get_verification_route_dependencies(request)
    await dependencies.require_internal(request)
    return await verification_workflow.get_completion_decision(
        job_id, dependencies=dependencies.workflow
    )


__all__ = [
    "VerificationRouteDependencies",
    "get_completion_decision",
    "get_verification_route_dependencies",
    "record_completion_decision",
    "record_verification_round",
    "router",
]
