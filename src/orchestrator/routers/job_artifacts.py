"""HTTP adapters for job artifacts with per-app dependencies.

Access on every route goes through ``require_job_access``, which enforces both
user visibility AND the MCP/officer ``project:<uuid>`` scope
(``_scope_permits_project``) — an opaque evidence ID alone conveys no access,
and a guessed ID from another project 403s before the manifest is touched.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Depends, Query, Request

from orchestrator.security.access import require_job_access
from orchestrator.services import job_artifacts

router = APIRouter()


@dataclass(frozen=True)
class JobArtifactDependencies:
    store: Any
    artifacts: job_artifacts.JobArtifactDependencies
    require_job_access: Callable[..., Awaitable[Any]] = require_job_access


def get_job_artifact_dependencies(request: Request) -> JobArtifactDependencies:
    return request.app.state.job_artifacts_dependencies_factory()


@router.get("/api/jobs/{job_id}/evidence")
async def list_job_evidence_route(
    request: Request,
    job_id: str,
    *,
    dependencies: JobArtifactDependencies = Depends(get_job_artifact_dependencies),
) -> dict[str, Any]:
    """List the typed evidence manifest recorded at completion."""
    _, job = await dependencies.require_job_access(request, dependencies.store, job_id)
    return await job_artifacts.list_job_evidence_route(
        job_id=job_id, authorized_job=job, dependencies=dependencies.artifacts
    )


@router.get("/api/jobs/{job_id}/completion-report")
async def get_job_completion_report_route(
    request: Request,
    job_id: str,
    *,
    dependencies: JobArtifactDependencies = Depends(get_job_artifact_dependencies),
) -> dict[str, Any]:
    """The server-recorded completion report entry (404 when none exists)."""
    _, job = await dependencies.require_job_access(request, dependencies.store, job_id)
    return await job_artifacts.get_job_completion_report_route(
        job_id=job_id, authorized_job=job, dependencies=dependencies.artifacts
    )


@router.get("/api/jobs/{job_id}/evidence/{evidence_id}")
async def read_job_evidence_route(
    request: Request,
    job_id: str,
    evidence_id: str,
    offset: int = Query(default=0, ge=0),
    *,
    dependencies: JobArtifactDependencies = Depends(get_job_artifact_dependencies),
) -> dict[str, Any]:
    """Read one evidence entry by opaque ID, resolved at its pinned revision.

    The ID is authorized against (caller project, job project, evidence job)
    on every read; the server never accepts a model-supplied path and never
    reads a revision other than the one pinned in the manifest.
    """
    _, job = await dependencies.require_job_access(request, dependencies.store, job_id)
    return await job_artifacts.read_job_evidence_route(
        job_id=job_id,
        evidence_id=evidence_id,
        offset=offset,
        authorized_job=job,
        dependencies=dependencies.artifacts,
    )


@router.get("/api/jobs/{job_id}/todos")
async def get_job_todos(
    request: Request,
    job_id: str,
    *,
    dependencies: JobArtifactDependencies = Depends(get_job_artifact_dependencies),
) -> dict[str, Any]:
    """Get all todos for a job (current + archives) from its Gitea repo.

    Reads the committed state as of the worker's last phase-boundary push:
    ``todos.yaml`` at the job branch head plus ``archive/todos_*.md`` phase
    archives.

    Gracefully degrades — Gitea unavailable or repo/files missing yields the
    empty shape instead of an error, because the cockpit todo view renders
    this response directly.

    Returns:
        Dict with:
        - job_id: Job UUID
        - current: Current todos from todos.yaml (if the worker pushed one)
        - archives: List of archived todo files
        - has_workspace: Whether the job's Gitea repo was reachable
    """
    await dependencies.require_job_access(request, dependencies.store, job_id)
    return await job_artifacts.get_job_todos(
        job_id=job_id, dependencies=dependencies.artifacts
    )


@router.get("/api/jobs/{job_id}/todos/current")
async def get_current_todos(
    request: Request,
    job_id: str,
    *,
    dependencies: JobArtifactDependencies = Depends(get_job_artifact_dependencies),
) -> dict[str, Any]:
    """Get current active todos from todos.yaml in the job's Gitea repo.

    Committed state as of the worker's last phase-boundary push.

    Returns:
        Dict with todos list and metadata, or 404 if not found
    """
    await dependencies.require_job_access(request, dependencies.store, job_id)
    return await job_artifacts.get_current_todos(
        job_id=job_id, dependencies=dependencies.artifacts
    )


@router.get("/api/jobs/{job_id}/todos/archives")
async def list_todo_archives(
    request: Request,
    job_id: str,
    *,
    dependencies: JobArtifactDependencies = Depends(get_job_artifact_dependencies),
) -> list[dict[str, Any]]:
    """List archived todo files from the job repo's ``archive/`` directory.

    Committed state as of the worker's last phase-boundary push. Empty list
    when Gitea is unavailable or the repo has no archives yet.

    Returns:
        List of archive metadata (filename, phase_name, timestamp)
    """
    await dependencies.require_job_access(request, dependencies.store, job_id)
    return await job_artifacts.list_todo_archives(
        job_id=job_id, dependencies=dependencies.artifacts
    )


@router.get("/api/jobs/{job_id}/todos/archives/{filename}")
async def get_archived_todos(
    request: Request,
    job_id: str,
    filename: str,
    *,
    dependencies: JobArtifactDependencies = Depends(get_job_artifact_dependencies),
) -> dict[str, Any]:
    """Get parsed content of an archived todo file from the job's Gitea repo.

    Committed state as of the worker's last phase-boundary push.

    Args:
        job_id: Job UUID
        filename: Archive filename (e.g., "todos_phase1_20260124_183618.md")

    Returns:
        Dict with parsed todos, summary, and metadata
    """
    await dependencies.require_job_access(request, dependencies.store, job_id)
    return await job_artifacts.get_archived_todos(
        job_id=job_id, filename=filename, dependencies=dependencies.artifacts
    )
