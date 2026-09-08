"""HTTP adapters for the job workspace-browser surface (Gitea proxy).

Every route is gated by ``require_job_access``, which enforces both user
visibility AND the MCP/officer ``project:<uuid>`` scope. The gate is awaited
first and its result discarded — these reads take the job id, not the row, so
the operation re-resolves the repo through ``resolve_job_repo``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Depends, Query, Request

from orchestrator.security.access import require_job_access
from orchestrator.services import job_repo_reads

router = APIRouter()


@dataclass(frozen=True)
class JobRepoDependencies:
    """Per-app auth store and repo-read operations; no store ownership here."""

    store: Any
    repo_reads: job_repo_reads.JobRepoReadDependencies
    require_job_access: Callable[..., Awaitable[Any]] = require_job_access


def get_job_repo_dependencies(request: Request) -> JobRepoDependencies:
    """Resolve collaborators only from the application handling this request."""
    return request.app.state.job_repo_dependencies_factory()


# =============================================================================
# Workspace Browser Endpoints (Gitea proxy)
# =============================================================================


@router.get("/api/jobs/{job_id}/repo/contents")
async def list_repo_contents(
    request: Request,
    job_id: str,
    path: str = Query(default="", description="Directory path within the repo"),
    ref: str | None = Query(default=None, description="Branch, tag, or commit SHA"),
    *,
    dependencies: JobRepoDependencies = Depends(get_job_repo_dependencies),
) -> list[dict[str, Any]]:
    """List directory contents of a job's Gitea repository.

    Proxies the Gitea contents API so the cockpit doesn't need Gitea credentials.

    Returns:
        List of entries, each with: name, path, type ("file"|"dir"), size
    """
    await dependencies.require_job_access(request, dependencies.store, job_id)
    return await job_repo_reads.list_repo_contents(
        job_id=job_id,
        path=path,
        ref=ref,
        dependencies=dependencies.repo_reads,
    )


@router.get("/api/jobs/{job_id}/repo/file")
async def get_repo_file(
    request: Request,
    job_id: str,
    path: str = Query(..., description="File path within the repo"),
    ref: str | None = Query(default=None, description="Branch, tag, or commit SHA"),
    *,
    dependencies: JobRepoDependencies = Depends(get_job_repo_dependencies),
) -> dict[str, Any]:
    """Get file content from a job's Gitea repository.

    Returns:
        Dict with path, content (text), and size
    """
    await dependencies.require_job_access(request, dependencies.store, job_id)
    return await job_repo_reads.get_repo_file(
        job_id=job_id,
        path=path,
        ref=ref,
        dependencies=dependencies.repo_reads,
    )


@router.get("/api/jobs/{job_id}/repo/commits")
async def list_repo_commits(
    request: Request,
    job_id: str,
    sha: str = Query(
        default="main", description="Branch, tag, or commit SHA to list from"
    ),
    since_ref: str | None = Query(
        default=None, description="Only show commits after this ref"
    ),
    page: int = Query(default=1, ge=1, description="Page number"),
    limit: int = Query(default=20, ge=1, le=100, description="Max commits per page"),
    *,
    dependencies: JobRepoDependencies = Depends(get_job_repo_dependencies),
) -> dict[str, Any]:
    """List git commits for a job's repository.

    If since_ref is provided, returns only commits between since_ref and sha
    using git compare. Otherwise lists commits from sha.

    Returns:
        Dict with commits list and total count
    """
    await dependencies.require_job_access(request, dependencies.store, job_id)
    return await job_repo_reads.list_repo_commits(
        job_id=job_id,
        sha=sha,
        since_ref=since_ref,
        page=page,
        limit=limit,
        dependencies=dependencies.repo_reads,
    )


@router.get("/api/jobs/{job_id}/repo/diff")
async def get_repo_diff(
    request: Request,
    job_id: str,
    base: str = Query(..., description="Base ref (commit SHA, tag, or branch)"),
    head: str = Query(default="HEAD", description="Head ref"),
    *,
    dependencies: JobRepoDependencies = Depends(get_job_repo_dependencies),
) -> dict[str, str]:
    """Get unified diff between two refs in a job's repository.

    Returns:
        Dict with base, head, and diff text
    """
    await dependencies.require_job_access(request, dependencies.store, job_id)
    return await job_repo_reads.get_repo_diff(
        job_id=job_id,
        base=base,
        head=head,
        dependencies=dependencies.repo_reads,
    )


@router.get("/api/jobs/{job_id}/repo/tags")
async def list_repo_tags(
    request: Request,
    job_id: str,
    all_jobs: bool = False,
    *,
    dependencies: JobRepoDependencies = Depends(get_job_repo_dependencies),
) -> list[dict[str, Any]]:
    """List tags in a job's repository.

    By default, only returns tags for the specified job (namespaced by
    job short ID prefix). Set all_jobs=True to return all tags in the repo.

    Returns:
        List of tags with name, sha, and message
    """
    await dependencies.require_job_access(request, dependencies.store, job_id)
    return await job_repo_reads.list_repo_tags(
        job_id=job_id,
        all_jobs=all_jobs,
        dependencies=dependencies.repo_reads,
    )
