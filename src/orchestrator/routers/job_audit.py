"""HTTP adapters for job audit with per-app dependencies."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from orchestrator.database.audit_store import FilterCategory
from orchestrator.security.access import require_job_access
from orchestrator.security.auth import require_approved_user
from orchestrator.services import job_audit

router = APIRouter()


@dataclass(frozen=True)
class JobAuditDependencies:
    store: Any
    audit_reader: job_audit.JobAuditReader
    require_admin: Callable[..., Awaitable[Any]]
    require_approved_user: Callable[..., Awaitable[Any]] = require_approved_user
    require_job_access: Callable[..., Awaitable[Any]] = require_job_access


def get_job_audit_dependencies(request: Request) -> JobAuditDependencies:
    return request.app.state.job_audit_dependencies_factory()


@router.get("/api/jobs/{job_id}/audit")
async def get_job_audit(
    request: Request,
    job_id: str,
    page: int = Query(default=1, ge=-1),
    page_size: int = Query(default=50, ge=1, le=200, alias="pageSize"),
    offset: Optional[int] = Query(default=None, ge=0),
    limit: Optional[int] = Query(default=None, ge=1, le=200),
    order: Literal["asc", "desc"] = Query(default="asc"),
    filter: FilterCategory = Query(default="all"),
    lean: bool = Query(default=False),
    *,
    dependencies: JobAuditDependencies = Depends(get_job_audit_dependencies),
) -> dict[str, Any]:
    """Get paginated audit entries for a job from the audit store.

    Two pagination styles are supported; use whichever you prefer:
        - offset/limit (REST-style): ?offset=50&limit=50
        - page/pageSize (legacy):    ?page=2&pageSize=50
    If both are provided, offset/limit wins. The response echoes both styles.

    Query params:
        offset: Entries to skip (overrides page if set)
        limit: Max entries to return, max 200 (overrides pageSize if set)
        page: 1-indexed page number; -1 = last page
        pageSize: Entries per page, max 200
        order: asc (oldest first, default) or desc (newest first)
        filter: all, messages, tools, or errors
    """
    await dependencies.require_job_access(request, dependencies.store, job_id)
    return await job_audit.get_job_audit(
        job_id=job_id,
        page=page,
        page_size=page_size,
        offset=offset,
        limit=limit,
        order=order,
        filter=filter,
        lean=lean,
        audit_reader=dependencies.audit_reader,
    )


@router.get("/api/jobs/{job_id}/audit/step/{step_id}")
async def get_audit_step(
    request: Request,
    job_id: str,
    step_id: int,
    *,
    dependencies: JobAuditDependencies = Depends(get_job_audit_dependencies),
) -> dict[str, Any]:
    """Full detail for a single audit step (heavy payload + metadata).

    The lean list projection (``/audit?lean=true``) omits per-row arguments,
    tracebacks, state, and metadata; the debug UI fetches them on demand here when
    a row is expanded. The distinct ``/step/`` segment avoids colliding with the
    ``/audit/timerange`` route.
    """
    await dependencies.require_job_access(request, dependencies.store, job_id)
    return await job_audit.get_audit_step(
        job_id=job_id, step_id=step_id, audit_reader=dependencies.audit_reader
    )


@router.get("/api/requests/{doc_id}")
async def get_request(
    request: Request,
    doc_id: str,
    *,
    dependencies: JobAuditDependencies = Depends(get_job_audit_dependencies),
) -> dict[str, Any]:
    """Get a single LLM request by its audit-store request ID.

    Gated by the caller's access to the request's underlying job — admins
    pass; otherwise the embedded `job_id` is run through `require_job_access`.
    Requests without a `job_id` (legacy) are admin-only.
    """
    if not dependencies.audit_reader.is_available:
        raise HTTPException(status_code=503, detail="Audit store not available")

    try:
        llm_doc = await job_audit.read_request_document(
            doc_id=doc_id, audit_reader=dependencies.audit_reader
        )
        if llm_doc is None:
            # The store must be queried first; auth still precedes disclosure.
            await dependencies.require_approved_user(request, dependencies.store)
            raise HTTPException(status_code=404, detail=f"Request '{doc_id}' not found")
        job_id = llm_doc.get("job_id")
        if job_id:
            await dependencies.require_job_access(
                request, dependencies.store, str(job_id)
            )
        else:
            await dependencies.require_admin(request)
        return llm_doc
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/api/jobs/{job_id}/audit/timerange")
async def get_audit_time_range(
    request: Request,
    job_id: str,
    *,
    dependencies: JobAuditDependencies = Depends(get_job_audit_dependencies),
) -> dict[str, str] | None:
    """Get first and last timestamps for job audit entries.

    Returns:
        Dict with 'start' and 'end' ISO timestamps, or null if no entries / audit store unavailable
    """
    await dependencies.require_job_access(request, dependencies.store, job_id)
    return await job_audit.get_audit_time_range(
        job_id=job_id, audit_reader=dependencies.audit_reader
    )


@router.get("/api/jobs/{job_id}/chat")
async def get_job_chat_history(
    request: Request,
    job_id: str,
    page: int = Query(default=1, ge=-1),
    page_size: int = Query(default=50, ge=1, le=200, alias="pageSize"),
    offset: Optional[int] = Query(default=None, ge=0),
    limit: Optional[int] = Query(default=None, ge=1, le=200),
    lean: bool = Query(default=False),
    *,
    dependencies: JobAuditDependencies = Depends(get_job_audit_dependencies),
) -> dict[str, Any]:
    """Get paginated chat history for a job.

    Returns a clean sequential view of conversation turns without duplicates.
    Each entry contains the input message(s) that triggered an LLM response
    and the response itself.

    Two pagination styles are supported (mirrors ``/audit``):
        - offset/limit (REST-style): ?offset=50&limit=50
        - page/pageSize (legacy):    ?page=2&pageSize=50
    If both are provided, offset/limit wins. The response echoes both styles.

    Query params:
        offset: Entries to skip (overrides page if set)
        limit: Max entries to return, max 200 (overrides pageSize if set)
        page: Page number (1-indexed). Use -1 to request the last page.
        pageSize: Number of entries per page (max 200)
        lean: Strip full message bodies (previews + truncated markers only);
            hydrate single turns via ``/chat/entry/{entry_id}``.
    """
    await dependencies.require_job_access(request, dependencies.store, job_id)
    return await job_audit.get_job_chat_history(
        job_id=job_id,
        page=page,
        page_size=page_size,
        offset=offset,
        limit=limit,
        lean=lean,
        audit_reader=dependencies.audit_reader,
    )


@router.get("/api/jobs/{job_id}/chat/entry/{entry_id}")
async def get_job_chat_entry(
    request: Request,
    job_id: str,
    entry_id: int,
    *,
    dependencies: JobAuditDependencies = Depends(get_job_audit_dependencies),
) -> dict[str, Any]:
    """Full detail for a single chat turn (complete inputs/response bodies).

    The lean listing (``/chat?lean=true``) carries previews only; the debug
    chat panel hydrates a turn here when the user expands a message or tool
    result. The distinct ``/entry/`` segment mirrors ``/audit/step/{id}``.
    """
    await dependencies.require_job_access(request, dependencies.store, job_id)
    return await job_audit.get_job_chat_entry(
        job_id=job_id, entry_id=entry_id, audit_reader=dependencies.audit_reader
    )


@router.get("/api/jobs/{job_id}/version")
async def get_job_version(
    request: Request,
    job_id: str,
    *,
    dependencies: JobAuditDependencies = Depends(get_job_audit_dependencies),
) -> dict[str, Any] | None:
    """Get job data version info for cache invalidation.

    Returns counts and timestamps that can be compared to cached values
    to determine if the cache needs to be refreshed.

    Returns:
        Dict with version, auditEntryCount, chatEntryCount, graphDeltaCount, lastUpdate
        Returns null if job has no audit data or the audit store is unavailable
    """
    await dependencies.require_job_access(request, dependencies.store, job_id)
    return await job_audit.get_job_version(
        job_id=job_id, audit_reader=dependencies.audit_reader
    )
