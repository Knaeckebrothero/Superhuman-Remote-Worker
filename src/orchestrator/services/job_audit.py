"""Read the optional audit tier after the caller completes job authorization.

Unavailable-list, nullable-metadata and unavailable-detail contracts differ
intentionally. Request-document authorization stays with the HTTP adapter,
which first loads the document to learn its job (or its legacy admin scope).
"""

from typing import Any, Literal, Protocol

from fastapi import HTTPException

from orchestrator.database.audit_store import FilterCategory


class JobAuditReader(Protocol):
    @property
    def is_available(self) -> bool: ...
    async def get_job_audit(
        self,
        job_id: str,
        page: int = 1,
        page_size: int = 50,
        filter_category: FilterCategory = "all",
        offset: int | None = None,
        limit: int | None = None,
        order: Literal["asc", "desc"] = "asc",
        lean: bool = False,
    ) -> dict[str, Any]: ...
    async def get_audit_step(
        self, job_id: str, step_id: int
    ) -> dict[str, Any] | None: ...
    async def get_request(self, doc_id: str) -> dict[str, Any] | None: ...
    async def get_audit_time_range(self, job_id: str) -> dict[str, str] | None: ...
    async def get_chat_history(
        self,
        job_id: str,
        page: int = 1,
        page_size: int = 50,
        offset: int | None = None,
        limit: int | None = None,
        lean: bool = False,
    ) -> dict[str, Any]: ...
    async def get_chat_entry(
        self, job_id: str, entry_id: int
    ) -> dict[str, Any] | None: ...
    async def get_job_version(self, job_id: str) -> dict[str, Any] | None: ...


async def get_job_audit(
    *,
    job_id: str,
    page: int,
    page_size: int,
    offset: int | None,
    limit: int | None,
    order: Literal["asc", "desc"],
    filter: FilterCategory,
    lean: bool,
    audit_reader: JobAuditReader,
) -> dict[str, Any]:
    """Preserve the reader's existing result and outage contract."""
    effective_size = limit if limit is not None else page_size
    if not audit_reader.is_available:
        return {
            "entries": [],
            "total": 0,
            "page": page,
            "pageSize": effective_size,
            "offset": offset if offset is not None else 0,
            "limit": effective_size,
            "hasMore": False,
            "error": "Audit store not available",
        }

    try:
        return await audit_reader.get_job_audit(
            job_id=job_id,
            page=page,
            page_size=page_size,
            filter_category=filter,
            offset=offset,
            limit=limit,
            order=order,
            lean=lean,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


async def get_audit_step(
    *, job_id: str, step_id: int, audit_reader: JobAuditReader
) -> dict[str, Any]:
    """Preserve the reader's existing result and outage contract."""
    if not audit_reader.is_available:
        raise HTTPException(status_code=503, detail="Audit store not available")
    try:
        doc = await audit_reader.get_audit_step(job_id, step_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    if doc is None:
        raise HTTPException(status_code=404, detail=f"Audit step '{step_id}' not found")
    return doc


async def get_audit_time_range(
    *, job_id: str, audit_reader: JobAuditReader
) -> dict[str, str] | None:
    """Preserve the reader's existing result and outage contract."""
    if not audit_reader.is_available:
        return None

    try:
        return await audit_reader.get_audit_time_range(job_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


async def get_job_chat_history(
    *,
    job_id: str,
    page: int,
    page_size: int,
    offset: int | None,
    limit: int | None,
    lean: bool,
    audit_reader: JobAuditReader,
) -> dict[str, Any]:
    """Preserve the reader's existing result and outage contract."""
    effective_size = limit if limit is not None else page_size
    if not audit_reader.is_available:
        return {
            "entries": [],
            "total": 0,
            "page": page,
            "pageSize": effective_size,
            "offset": offset if offset is not None else 0,
            "limit": effective_size,
            "hasMore": False,
            "error": "Audit store not available",
        }

    try:
        return await audit_reader.get_chat_history(
            job_id=job_id,
            page=page,
            page_size=page_size,
            offset=offset,
            limit=limit,
            lean=lean,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


async def get_job_chat_entry(
    *, job_id: str, entry_id: int, audit_reader: JobAuditReader
) -> dict[str, Any]:
    """Preserve the reader's existing result and outage contract."""
    if not audit_reader.is_available:
        raise HTTPException(status_code=503, detail="Audit store not available")
    try:
        doc = await audit_reader.get_chat_entry(job_id, entry_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    if doc is None:
        raise HTTPException(
            status_code=404, detail=f"Chat entry '{entry_id}' not found"
        )
    return doc


async def get_job_version(
    *, job_id: str, audit_reader: JobAuditReader
) -> dict[str, Any] | None:
    """Preserve the reader's existing result and outage contract."""
    if not audit_reader.is_available:
        return None

    try:
        return await audit_reader.get_job_version(job_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


async def read_request_document(
    *, doc_id: str, audit_reader: JobAuditReader
) -> dict[str, Any] | None:
    """Read after availability, before HTTP auth of either a document or absence.

    The adapter owns this route's existing exception boundary around both the
    read and the subsequent authorization; availability is checked outside it.
    """
    return await audit_reader.get_request(doc_id)
