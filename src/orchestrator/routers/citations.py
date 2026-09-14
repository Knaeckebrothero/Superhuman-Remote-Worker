"""HTTP adapters for the citation and source library.

The application owns stores and lifecycle; each request resolves its factory
from the app handling it, so importing this router never imports application
startup.

Two visibility gates run *inside* a query flow rather than before it, because
the row has to be read before the caller's access to it can be decided. Each is
handed to the operation as an ``authorize`` callback built here from the
injected gate, so the gate stays the router's and the query stays the
service's:

* ``user_can_access_any_job`` — a source is visible when the caller can reach
  at least one job that links it (G3);
* ``user_can_access_job_or_thread`` — a citation's ``job_id`` may be a *thread*
  id with no ``jobs`` row (the session citation panel), which is exactly the
  case that resolver exists for.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from orchestrator.security.access import (
    require_internal,
    require_job_access,
    require_project_member,
    user_can_access_any_job,
    user_can_access_job_or_thread,
)
from orchestrator.security.auth import require_approved_user
from orchestrator.services import citations

router = APIRouter()


@dataclass(frozen=True)
class CitationsDependencies:
    """Per-app auth store, operations and explicit authorization gates."""

    store: Any
    operations: citations.CitationDependencies
    require_approved_user: Callable[..., Awaitable[Any]] = require_approved_user
    require_job_access: Callable[..., Awaitable[Any]] = require_job_access
    require_project_member: Callable[..., Awaitable[Any]] = require_project_member
    require_internal: Callable[..., Awaitable[Any]] = require_internal
    user_can_access_any_job: Callable[..., Awaitable[bool]] = user_can_access_any_job
    user_can_access_job_or_thread: Callable[..., Awaitable[bool]] = (
        user_can_access_job_or_thread
    )


def get_citations_dependencies(request: Request) -> CitationsDependencies:
    """Resolve collaborators only from the application handling this request."""
    return request.app.state.citations_dependencies_factory()


@router.get("/api/sources")
async def list_sources(
    request: Request,
    job_id: str | None = Query(default=None, description="Filter by job ID"),
    type: str | None = Query(default=None, description="Filter by source type"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    *,
    dependencies: CitationsDependencies = Depends(get_citations_dependencies),
) -> dict[str, Any]:
    """List sources, optionally filtered by job and/or type.

    Visibility model (G3):
        * With ``?job_id=``: gate on ``require_job_access``. The job's
          sources are returned (visible if the caller can access the job).
        * Without ``?job_id=``: admin-only. Non-admins must filter by a
          specific job they can access. (Cross-job source enumeration
          would require a vector_db ⇆ postgres_db JOIN we deliberately
          don't do; the per-job path covers the legitimate cockpit use
          case without that complexity.)
    """
    if job_id:
        await dependencies.require_job_access(request, dependencies.store, job_id)
    else:
        caller = await dependencies.require_approved_user(request, dependencies.store)
        if not caller.get("is_admin"):
            raise HTTPException(
                status_code=403,
                detail="Cross-job source listing requires admin role; "
                "non-admins must pass ?job_id=",
            )
    return await citations.list_sources(
        job_id=job_id,
        type=type,
        limit=limit,
        offset=offset,
        dependencies=dependencies.operations,
    )


@router.get("/api/sources/{source_id}")
async def get_source_detail(
    request: Request,
    source_id: int,
    content_limit: int = Query(default=2000, ge=0, le=100000),
    *,
    dependencies: CitationsDependencies = Depends(get_citations_dependencies),
) -> dict[str, Any]:
    """Get full detail for a single source.

    Visibility (G3): the source is visible if the caller can access at
    least one job linked to it via ``job_sources``. Admins (without an
    MCP project: scope) bypass without enumerating links.
    """
    caller = await dependencies.require_approved_user(request, dependencies.store)

    async def _authorize(job_ids: Sequence[str]) -> bool:
        return await dependencies.user_can_access_any_job(
            caller, dependencies.store, job_ids
        )

    return await citations.get_source_detail(
        source_id,
        content_limit=content_limit,
        authorize=_authorize,
        dependencies=dependencies.operations,
    )


@router.get("/api/jobs/{job_id}/citations")
async def list_job_citations(
    request: Request,
    job_id: str,
    source_id: int | None = Query(default=None),
    status: str | None = Query(
        default=None, description="Filter by verification_status"
    ),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    *,
    dependencies: CitationsDependencies = Depends(get_citations_dependencies),
) -> dict[str, Any]:
    """List citations for a job with optional filters."""
    await dependencies.require_job_access(request, dependencies.store, job_id)
    return await citations.list_job_citations(
        job_id,
        source_id=source_id,
        status=status,
        limit=limit,
        offset=offset,
        dependencies=dependencies.operations,
    )


@router.post("/api/citations/snapshot")
async def store_citation_snapshot(
    request: Request,
    *,
    dependencies: CitationsDependencies = Depends(get_citations_dependencies),
) -> dict[str, Any]:
    """Persist the original bytes of a cited cloud document (Phase 3, D7).

    **Internal** (P4b) — requires ``X-Internal-Key``; the agent calls this at
    cite-time because it has no S3 credentials of its own. The request body is
    the raw file bytes; ``content_type`` is an optional query param used when
    the blob is later served back. Returns a content-addressed
    ``snapshot_blob_key`` the agent records onto the citation source's
    ``metadata.cloud`` so the original can be retrieved on view.
    """
    await dependencies.require_internal(request)
    return await citations.store_citation_snapshot(
        read_body=request.body,
        content_type=request.query_params.get("content_type"),
        dependencies=dependencies.operations,
    )


@router.get("/api/citations/{citation_id}")
async def get_citation_detail(
    request: Request,
    citation_id: int,
    *,
    dependencies: CitationsDependencies = Depends(get_citations_dependencies),
) -> dict[str, Any]:
    """Get full citation record with source info and verification details.

    **P4e** — visible if the caller can access the citation's linked job
    (mirrors G3's ``get_source_detail`` pattern). Admins without an MCP
    ``project:<uuid>`` scope bypass. Missing/unauthorized → 404 to avoid
    leaking citation existence via probe.
    """
    caller = await dependencies.require_approved_user(request, dependencies.store)

    async def _authorize(job_id: str | None) -> bool:
        return await dependencies.user_can_access_job_or_thread(
            caller, dependencies.store, job_id
        )

    return await citations.get_citation_detail(
        citation_id, authorize=_authorize, dependencies=dependencies.operations
    )


@router.get("/api/citations/{citation_id}/snapshot")
async def get_citation_snapshot(
    request: Request,
    citation_id: int,
    *,
    dependencies: CitationsDependencies = Depends(get_citations_dependencies),
) -> Response:
    """Serve the backed-up original bytes of a cited cloud document (Phase 3c, D7).

    Viewing-user auth (same gate as ``get_citation_detail``). Returns the copy
    SRW stored at cite-time (``metadata.cloud.snapshot_blob_key``) so a citation
    can show the exact version cited even when the live source changed or is
    unreachable. 404 if the citation is unknown/unauthorized or has no snapshot
    (404 over 403 so citation existence isn't leaked by probing).
    """
    caller = await dependencies.require_approved_user(request, dependencies.store)

    async def _authorize(job_id: str | None) -> bool:
        return await dependencies.user_can_access_job_or_thread(
            caller, dependencies.store, job_id
        )

    return await citations.get_citation_snapshot(
        citation_id, authorize=_authorize, dependencies=dependencies.operations
    )


@router.get("/api/citations/{citation_id}/drift")
async def get_citation_drift(
    request: Request,
    citation_id: int,
    *,
    dependencies: CitationsDependencies = Depends(get_citations_dependencies),
) -> dict[str, Any]:
    """On-view drift check for a cited cloud document (Phase 3c, D7).

    Viewing-user auth. Compares the live source against what was cited. A
    best-effort re-fetch runs **only** when the cited file is provably inside the
    viewing user's own cloud home (so the credentials are the user's, never the
    agent's expired ones, and we never compare a same-named different file).
    Returns:

    - ``live_state``: ``unchanged`` | ``changed`` | ``unreachable`` | ``unknown``
    - ``snapshot_available``: whether ``/snapshot`` can serve the backed-up copy
    - ``cited``: the drift fingerprint captured at cite-time

    ``unreachable`` (external datasource / different cloud / no access) is the
    spec's "fall back to the snapshot" branch.
    """
    caller = await dependencies.require_approved_user(request, dependencies.store)

    async def _authorize(job_id: str | None) -> bool:
        return await dependencies.user_can_access_job_or_thread(
            caller, dependencies.store, job_id
        )

    return await citations.get_citation_drift(
        citation_id,
        caller=caller,
        authorize=_authorize,
        dependencies=dependencies.operations,
    )


@router.get("/api/jobs/{job_id}/sources/{source_id}/annotations")
async def get_source_annotations(
    request: Request,
    job_id: str,
    source_id: int,
    type: str | None = Query(default=None, description="Filter by annotation_type"),
    *,
    dependencies: CitationsDependencies = Depends(get_citations_dependencies),
) -> list[dict[str, Any]]:
    """Get annotations for a source within a job."""
    await dependencies.require_job_access(request, dependencies.store, job_id)
    return await citations.get_source_annotations(
        job_id, source_id, type=type, dependencies=dependencies.operations
    )


@router.get("/api/jobs/{job_id}/sources/{source_id}/tags")
async def get_source_tags(
    request: Request,
    job_id: str,
    source_id: int,
    *,
    dependencies: CitationsDependencies = Depends(get_citations_dependencies),
) -> list[str]:
    """Get tags for a source within a job."""
    await dependencies.require_job_access(request, dependencies.store, job_id)
    return await citations.get_source_tags(
        job_id, source_id, dependencies=dependencies.operations
    )


@router.get("/api/jobs/{job_id}/citations/stats")
async def get_citation_stats(
    request: Request,
    job_id: str,
    *,
    dependencies: CitationsDependencies = Depends(get_citations_dependencies),
) -> dict[str, Any]:
    """Get citation statistics for a job."""
    await dependencies.require_job_access(request, dependencies.store, job_id)
    return await citations.get_citation_stats(
        job_id, dependencies=dependencies.operations
    )


@router.get("/api/jobs/{job_id}/memory/stats")
async def get_memory_stats(
    request: Request,
    job_id: str,
    *,
    dependencies: CitationsDependencies = Depends(get_citations_dependencies),
) -> dict[str, Any]:
    """Get memory statistics for a job."""
    await dependencies.require_job_access(request, dependencies.store, job_id)
    return await citations.get_memory_stats(
        job_id, dependencies=dependencies.operations
    )


@router.get("/api/projects/{project_id}/memory/stats")
async def get_project_memory_stats(
    request: Request,
    project_id: str,
    *,
    dependencies: CitationsDependencies = Depends(get_citations_dependencies),
) -> dict[str, Any]:
    """Get memory statistics for a project (all memories scoped to this project)."""
    await dependencies.require_project_member(request, dependencies.store, project_id)
    return await citations.get_project_memory_stats(
        project_id, dependencies=dependencies.operations
    )


@router.get("/api/jobs/{job_id}/memories")
async def list_job_memories(
    request: Request,
    job_id: str,
    memory_type: str | None = Query(default=None),
    source: str | None = Query(default=None),
    search: str | None = Query(default=None),
    sort_by: str = Query(default="created_at"),
    sort_order: str = Query(default="desc"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    *,
    dependencies: CitationsDependencies = Depends(get_citations_dependencies),
) -> dict[str, Any]:
    """List individual memories for a job with optional filters and pagination."""
    await dependencies.require_job_access(request, dependencies.store, job_id)
    return await citations.list_job_memories(
        job_id,
        memory_type=memory_type,
        source=source,
        search=search,
        sort_by=sort_by,
        sort_order=sort_order,
        limit=limit,
        offset=offset,
        dependencies=dependencies.operations,
    )


@router.get("/api/jobs/{job_id}/sources/search")
async def search_job_sources(
    request: Request,
    job_id: str,
    query: str = Query(..., description="Search query"),
    mode: str = Query(
        default="keyword", description="Search mode: keyword, semantic, hybrid"
    ),
    source_type: str | None = Query(default=None),
    tags: str | None = Query(
        default=None, description="Comma-separated tags (AND logic)"
    ),
    top_k: int = Query(default=10, ge=1, le=50),
    *,
    dependencies: CitationsDependencies = Depends(get_citations_dependencies),
) -> dict[str, Any]:
    """Search a job's source library using keyword search.

    Falls back to SQL keyword search. Semantic/hybrid modes require
    the CitationEngine with pgvector.
    """
    await dependencies.require_job_access(request, dependencies.store, job_id)
    return await citations.search_job_sources(
        job_id,
        query=query,
        mode=mode,
        source_type=source_type,
        tags=tags,
        top_k=top_k,
        dependencies=dependencies.operations,
    )
