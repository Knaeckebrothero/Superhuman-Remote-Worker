"""``/api/jobs/{job_id}/loop-plan`` — a planner loop's campaign plan.

Extracted from ``orchestrator.main`` (R1.B07 lane L). One internal route
declaration, moved with its handler name, path, method, parameter order and
docstring intact. It carried no ``tags``, ``response_model``, ``status_code``
or ``dependencies`` list and acquires none here; the internal-key gate is
called in the declaration body exactly where it ran before.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from orchestrator.schemas.project_loops import LoopPlanRequest
from orchestrator.security.access import require_internal
from orchestrator.services import loop_plan_filing

router = APIRouter()


def get_loop_plan_filing_dependencies(
    request: Request,
) -> loop_plan_filing.LoopPlanFilingDependencies:
    """Resolve collaborators only from the application handling this request."""
    return request.app.state.loop_plan_filing_dependencies_factory()


@router.post("/api/jobs/{job_id}/loop-plan")
async def file_loop_plan(
    request: Request,
    job_id: str,
    body: LoopPlanRequest,
) -> dict[str, Any]:
    """File a campaign plan from a planner loop's checkpoint critic. **Internal**
    (P4b) — requires ``X-Internal-Key``. Ingress strips this path.

    Validated at call time so the critic learns about a malformed plan while it
    can still fix it (the whole point of tool-transport over freeze_data). The
    normalized plan is stored in the job's context (``loop_plan``) via the
    atomic context merge; the loop's advance applies it when the critic job
    completes. Idempotent: re-filing replaces the stored plan.

    Gating (defense in depth with the P1 spawn-time tool injection): only the
    loop's in-flight job may file, only on a running planner-scheduled loop,
    and only from the checkpoint-critic stage — a campaign member occupies the
    execution slot, so it is rejected structurally, not by role-string check.
    """
    await require_internal(request)
    return await loop_plan_filing.file_loop_plan(
        request,
        job_id,
        body,
        dependencies=get_loop_plan_filing_dependencies(request),
    )


__all__ = ["file_loop_plan", "get_loop_plan_filing_dependencies", "router"]
