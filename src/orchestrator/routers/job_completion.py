"""HTTP adapter for agent job-completion reports."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from orchestrator.schemas.job_runtime import JobCompleteRequest
from orchestrator.services import job_completion
from orchestrator.services.job_completion import JobCompletionDependencies

router = APIRouter()


def get_job_completion_dependencies(request: Request) -> JobCompletionDependencies:
    """Resolve collaborators from the application handling this request."""

    return request.app.state.job_completion_dependencies_factory()


@router.post("/api/jobs/{job_id}/complete")
async def complete_job(
    request: Request,
    job_id: str,
    body: JobCompleteRequest,
    *,
    dependencies: JobCompletionDependencies = Depends(get_job_completion_dependencies),
) -> Any:
    """Authenticate, optionally admit a durable command, then run legacy effects.

    With the default-off gate closed this calls the pre-Gate-3 implementation
    directly and never reads or writes any completion-command relation.
    """

    return await job_completion.complete_job(
        request,
        job_id,
        body,
        dependencies=dependencies,
    )


__all__ = ["complete_job", "get_job_completion_dependencies", "router"]
