"""HTTP adapter for the administrative job-assignment override.

Extracted verbatim from ``orchestrator.main`` (R1.B05 lane J, census group
``J_assignment``). One route: ``POST /api/jobs/{job_id}/assign/{agent_id}``.

The endpoint is admin-only (P4c) and is *not* the normal path — automatic
dispatch is. Its job is to refuse cleanly rather than push an incomplete bundle
at an agent: a stateless job cannot be assigned to a registered agent at all, a
job whose Kubernetes workspace authority has not converged answers 409 without
reserving anything, and a job with no live workspace sheds the stale workspace
context and hands itself back to the dispatcher instead of dispatching an SSH
config that names a pod which no longer exists.

The completion-control claim around the workspace-resume queueing is a pair:
every failure path after a successful claim aborts it, so a refused queueing
never leaves a job holding a control claim it does not own.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from orchestrator.services.job_assignment import (
    JobAssignmentDependencies,
    JobAssignmentOperations,
    JobAssignmentStore,
)

router = APIRouter()

__all__ = [
    "JobAssignmentDependencies",
    "JobAssignmentStore",
    "assign_job_to_agent",
    "get_job_assignment_dependencies",
    "router",
]


def get_job_assignment_dependencies(request: Request) -> JobAssignmentDependencies:
    """Resolve collaborators only from the application handling this request."""
    return request.app.state.job_assignment_dependencies_factory()


@router.post("/api/jobs/{job_id}/assign/{agent_id}")
async def assign_job_to_agent(
    request: Request,
    job_id: str,
    agent_id: str,
    *,
    dependencies: JobAssignmentDependencies = Depends(get_job_assignment_dependencies),
) -> dict[str, str]:
    """Administrative scheduling override for a job.

    **Admin only** (P4c). Normal callers should rely on automatic dispatch.
    If the managed workspace is not live, this endpoint sheds stale workspace
    state and queues normal provisioning instead of dispatching an incomplete
    SSH configuration. The requested agent is not reserved in that case.

    With a live workspace, validates the agent and delegates to the shared
    start/resume helper. Accepts 'created', 'failed', or 'paused' jobs.
    """
    await dependencies.require_admin(request)
    return await JobAssignmentOperations(dependencies).assign(job_id, agent_id)
