"""HTTP adapters for job delivery review — Mode B export, PR status, review session.

All three routes are gated by ``require_job_access``, which enforces both user
visibility AND the MCP/officer ``project:<uuid>`` scope. The export needs the
authenticated *user* as well as the row (it provisions and shares a cloud
folder in that user's name), so it keeps both halves of the gate's result; the
other two need only the row.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Depends, Request

from orchestrator.security.access import require_job_access
from orchestrator.services import job_export, job_review_session

router = APIRouter()


@dataclass(frozen=True)
class JobReviewDependencies:
    """Per-app auth store and the two operation bundles; no store ownership."""

    store: Any
    export: job_export.JobExportDependencies
    review_session: job_review_session.JobReviewSessionDependencies
    require_job_access: Callable[..., Awaitable[Any]] = require_job_access


def get_job_review_dependencies(request: Request) -> JobReviewDependencies:
    """Resolve collaborators only from the application handling this request."""
    return request.app.state.job_review_dependencies_factory()


@router.post("/api/jobs/{job_id}/export-to-shared-folder")
async def export_job_to_shared_folder(
    request: Request,
    job_id: str,
    *,
    dependencies: JobReviewDependencies = Depends(get_job_review_dependencies),
) -> dict[str, Any]:
    """Mode B of the job cloud workflow — copy a job's deliverables into a
    shared cloud folder ("Open cloud folder") and return its browser URL.

    Valid for ``completed`` or ``pending_review`` jobs whose project has **no**
    main-cloud folder (loose jobs and default-project / no-cloud-folder jobs);
    jobs whose project *does* have a cloud folder go through the Mode A
    diff-review flow instead. Copies the agent's declared deliverables (from
    ``freeze_data.deliverables``; falls back to ``output/`` for jobs without a
    deliverables list) into a per-job session-style cloud folder shared with the
    calling user. Workspace-relative paths are kept **except** for the leading
    directories every deliverable shares, which are collapsed
    (``_common_dir_prefix``) so a lone ``output/digest.md`` opens as
    ``digest.md`` instead of hiding a level down.

    Re-syncable: a repeat call overwrites the same folder and re-stamps
    ``exported_at`` as "last synced at" (e.g. after resume-with-feedback). The
    folder name is derived deterministically from the job, and a job that has
    been exported before reuses its stored handle. v1 overwrites in place and
    does not prune files removed between syncs — note that a re-sync after the
    deliverable set changes can therefore leave files from the previous shape
    behind, since the collapsed prefix is recomputed per call.

    See knowledge-history/done/job_cloud_export.md §3.2.
    """
    user, job = await dependencies.require_job_access(
        request, dependencies.store, job_id
    )
    return await job_export.export_job_to_shared_folder(
        job_id=job_id,
        user=user,
        authorized_job=job,
        dependencies=dependencies.export,
    )


@router.get("/api/jobs/{job_id}/pull-request")
async def get_job_pull_request_status(
    request: Request,
    job_id: str,
    *,
    dependencies: JobReviewDependencies = Depends(get_job_review_dependencies),
) -> dict[str, Any]:
    """Read the live state of the PR recorded by ``repo_open_pr``.

    The caller supplies only a job id. The server resolves both the persisted
    PR identity and its credential-bearing repository connector after the
    ordinary job-access check; connector credentials never cross REST.
    """
    _, job = await dependencies.require_job_access(request, dependencies.store, job_id)
    return await job_review_session.get_job_pull_request_status(
        job_id=job_id,
        authorized_job=job,
        dependencies=dependencies.review_session,
    )


@router.post("/api/jobs/{job_id}/review-session")
async def create_job_review_session(
    request: Request,
    job_id: str,
    *,
    dependencies: JobReviewDependencies = Depends(get_job_review_dependencies),
) -> dict[str, Any]:
    """Create a fresh interactive review from an access-checked job id.

    There is intentionally no request body. Model, scope, connectors and the
    delivered branch are all derived from stored server state; the MCP session
    tool remains unchanged and has no route to ``config_override``.
    """
    _, job = await dependencies.require_job_access(request, dependencies.store, job_id)
    return await job_review_session.create_job_review_session(
        request=request,
        job_id=job_id,
        authorized_job=job,
        dependencies=dependencies.review_session,
    )
