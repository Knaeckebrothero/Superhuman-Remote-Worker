"""FastAPI backend for the Debug Cockpit.

Run with:
    uvicorn cockpit.api.main:app --reload --port 8085

Or from cockpit/api directory:
    uvicorn main:app --reload --port 8085
"""

import json
from contextlib import asynccontextmanager

from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv())
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from services.mongodb import FilterCategory, mongodb_service
from services.postgres import ALLOWED_TABLES, postgres_service
from services.workspace import workspace_service
from graph_routes import router as graph_router


class CustomJSONEncoder(json.JSONEncoder):
    """JSON encoder that handles PostgreSQL types."""

    def default(self, obj: Any) -> Any:
        if isinstance(obj, UUID):
            return str(obj)
        if isinstance(obj, (datetime, date)):
            return obj.isoformat()
        if isinstance(obj, Decimal):
            return float(obj)
        return super().default(obj)


class CustomJSONResponse(JSONResponse):
    """JSON response that uses custom encoder."""

    def render(self, content: Any) -> bytes:
        return json.dumps(
            content,
            cls=CustomJSONEncoder,
            ensure_ascii=False,
            allow_nan=False,
            indent=None,
            separators=(",", ":"),
        ).encode("utf-8")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    await postgres_service.connect()
    await mongodb_service.connect()
    yield
    await mongodb_service.disconnect()
    await postgres_service.disconnect()


app = FastAPI(
    title="Debug Cockpit API",
    description="Backend API for the Graph-RAG Debug Cockpit",
    version="0.1.0",
    lifespan=lifespan,
    default_response_class=CustomJSONResponse,
)

# CORS for Angular frontend (dev server on 4200, production/SSR on 4000)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:4200",
        "http://127.0.0.1:4200",
        "http://localhost:4000",
        "http://127.0.0.1:4000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(graph_router)


@app.get("/api/tables")
async def list_tables() -> list[dict[str, Any]]:
    """List available tables with row counts."""
    return await postgres_service.get_tables()


@app.get("/api/tables/{table_name}")
async def get_table_data(
    table_name: str,
    page: int = Query(default=1, ge=-1),
    page_size: int = Query(default=50, ge=1, le=500, alias="pageSize"),
) -> dict[str, Any]:
    """Get paginated table data. Use page=-1 to request the last page."""
    if table_name not in ALLOWED_TABLES:
        raise HTTPException(status_code=404, detail=f"Table '{table_name}' not found")

    try:
        return await postgres_service.get_table_data(table_name, page, page_size)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/tables/{table_name}/schema")
async def get_table_schema(table_name: str) -> list[dict[str, Any]]:
    """Get column definitions for a table."""
    if table_name not in ALLOWED_TABLES:
        raise HTTPException(status_code=404, detail=f"Table '{table_name}' not found")

    try:
        return await postgres_service.get_table_schema(table_name)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/health")
async def health_check() -> dict[str, str]:
    """Health check endpoint."""
    return {"status": "ok"}


@app.get("/api/workspace/status")
async def workspace_status() -> dict[str, Any]:
    """Get workspace configuration status for debugging.

    Returns:
        Dict with workspace path, availability, and sample job directories
    """
    import os

    base_path = workspace_service.base_path
    is_available = workspace_service.is_available

    # List job directories if available
    job_dirs = []
    if is_available:
        try:
            job_dirs = [
                d.name for d in base_path.iterdir()
                if d.is_dir() and d.name.startswith("job_")
            ][:10]  # Limit to 10 for display
        except Exception:
            pass

    return {
        "configured_path": str(base_path),
        "resolved_path": str(base_path.resolve()) if base_path.exists() else None,
        "is_available": is_available,
        "env_workspace_path": os.environ.get("WORKSPACE_PATH"),
        "job_directories": job_dirs,
        "job_count": len(job_dirs) if is_available else 0,
    }


@app.get("/api/jobs")
async def list_jobs(
    status: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict[str, Any]]:
    """List jobs with optional status filter.

    Returns jobs enriched with audit_count from MongoDB if available.
    """
    try:
        jobs = await postgres_service.get_jobs(status=status, limit=limit)

        # Enrich with audit counts if MongoDB is available
        if mongodb_service.is_available:
            for job in jobs:
                job_id = str(job["id"])
                job["audit_count"] = await mongodb_service.get_audit_count(job_id)
        else:
            for job in jobs:
                job["audit_count"] = None

        return jobs
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str) -> dict[str, Any]:
    """Get a single job by ID."""
    try:
        job = await postgres_service.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

        # Enrich with audit count if MongoDB is available
        if mongodb_service.is_available:
            job["audit_count"] = await mongodb_service.get_audit_count(job_id)
        else:
            job["audit_count"] = None

        return job
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/jobs/{job_id}/audit")
async def get_job_audit(
    job_id: str,
    page: int = Query(default=1, ge=-1),
    page_size: int = Query(default=50, ge=1, le=200, alias="pageSize"),
    filter: FilterCategory = Query(default="all"),
) -> dict[str, Any]:
    """Get paginated audit entries for a job from MongoDB.

    Query params:
        page: Page number (1-indexed). Use -1 to request the last page.
        pageSize: Number of entries per page (max 200)
        filter: Filter category - all, messages, tools, or errors
    """
    if not mongodb_service.is_available:
        return {
            "entries": [],
            "total": 0,
            "page": page,
            "pageSize": page_size,
            "hasMore": False,
            "error": "MongoDB not available",
        }

    try:
        return await mongodb_service.get_job_audit(
            job_id=job_id,
            page=page,
            page_size=page_size,
            filter_category=filter,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/requests/{doc_id}")
async def get_request(doc_id: str) -> dict[str, Any]:
    """Get a single LLM request by MongoDB document ID.

    Args:
        doc_id: MongoDB ObjectId as string (24 hex characters)

    Returns:
        Full LLM request document with messages and response
    """
    if not mongodb_service.is_available:
        raise HTTPException(
            status_code=503,
            detail="MongoDB not available",
        )

    try:
        request = await mongodb_service.get_request(doc_id)
        if request is None:
            raise HTTPException(
                status_code=404,
                detail=f"Request '{doc_id}' not found",
            )
        return request
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/jobs/{job_id}/audit/page-for-timestamp")
async def get_page_for_timestamp(
    job_id: str,
    timestamp: str = Query(..., description="ISO timestamp to locate"),
    page_size: int = Query(default=50, ge=1, le=200, alias="pageSize"),
    filter: FilterCategory = Query(default="all"),
) -> dict[str, int]:
    """Find which page contains the audit entry at a given timestamp.

    Returns:
        Dict with 'page' (1-indexed) and 'index' (position within that page)
    """
    if not mongodb_service.is_available:
        return {"page": 1, "index": 0}

    try:
        return await mongodb_service.get_page_for_timestamp(
            job_id=job_id,
            timestamp=timestamp,
            page_size=page_size,
            filter_category=filter,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/jobs/{job_id}/audit/timerange")
async def get_audit_time_range(job_id: str) -> dict[str, str] | None:
    """Get first and last timestamps for job audit entries.

    Returns:
        Dict with 'start' and 'end' ISO timestamps, or null if no entries/MongoDB unavailable
    """
    if not mongodb_service.is_available:
        return None

    try:
        return await mongodb_service.get_audit_time_range(job_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/jobs/{job_id}/chat")
async def get_job_chat_history(
    job_id: str,
    page: int = Query(default=1, ge=-1),
    page_size: int = Query(default=50, ge=1, le=200, alias="pageSize"),
) -> dict[str, Any]:
    """Get paginated chat history for a job.

    Returns a clean sequential view of conversation turns without duplicates.
    Each entry contains the input message(s) that triggered an LLM response
    and the response itself.

    Query params:
        page: Page number (1-indexed). Use -1 to request the last page.
        pageSize: Number of entries per page (max 200)
    """
    if not mongodb_service.is_available:
        return {
            "entries": [],
            "total": 0,
            "page": page,
            "pageSize": page_size,
            "hasMore": False,
            "error": "MongoDB not available",
        }

    try:
        return await mongodb_service.get_chat_history(
            job_id=job_id,
            page=page,
            page_size=page_size,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


# =============================================================================
# Workspace / Todo Endpoints
# =============================================================================


@app.get("/api/jobs/{job_id}/todos")
async def get_job_todos(job_id: str) -> dict[str, Any]:
    """Get all todos for a job (current + archives).

    Returns:
        Dict with:
        - job_id: Job UUID
        - current: Current todos from todos.yaml (if exists)
        - archives: List of archived todo files
        - has_workspace: Whether workspace directory exists
    """
    return workspace_service.get_all_todos(job_id)


@app.get("/api/jobs/{job_id}/todos/current")
async def get_current_todos(job_id: str) -> dict[str, Any]:
    """Get current active todos from todos.yaml.

    Returns:
        Dict with todos list and metadata, or 404 if not found
    """
    result = workspace_service.get_current_todos(job_id)
    if result is None:
        raise HTTPException(
            status_code=404,
            detail=f"No current todos found for job '{job_id}'"
        )
    return result


@app.get("/api/jobs/{job_id}/todos/archives")
async def list_todo_archives(job_id: str) -> list[dict[str, Any]]:
    """List all archived todo files for a job.

    Returns:
        List of archive metadata (filename, phase_name, timestamp)
    """
    return workspace_service.list_archived_todos(job_id)


@app.get("/api/jobs/{job_id}/todos/archives/{filename}")
async def get_archived_todos(job_id: str, filename: str) -> dict[str, Any]:
    """Get parsed content of an archived todo file.

    Args:
        job_id: Job UUID
        filename: Archive filename (e.g., "todos_phase1_20260124_183618.md")

    Returns:
        Dict with parsed todos, summary, and metadata
    """
    result = workspace_service.get_archived_todos(job_id, filename)
    if result is None:
        raise HTTPException(
            status_code=404,
            detail=f"Archive '{filename}' not found for job '{job_id}'"
        )
    return result


# =============================================================================
# Bulk Fetch Endpoints for Client-Side Caching
# =============================================================================


@app.get("/api/jobs/{job_id}/audit/bulk")
async def get_job_audit_bulk(
    job_id: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=5000, ge=1, le=5000),
) -> dict[str, Any]:
    """Get bulk audit entries for caching in IndexedDB.

    Uses offset/limit instead of page/pageSize for efficient bulk fetching.
    Returns up to 5000 entries per request.

    Query params:
        offset: Number of entries to skip (default 0)
        limit: Maximum entries to return (max 5000)
    """
    if not mongodb_service.is_available:
        return {
            "entries": [],
            "total": 0,
            "offset": offset,
            "limit": limit,
            "hasMore": False,
            "error": "MongoDB not available",
        }

    try:
        return await mongodb_service.get_job_audit_bulk(
            job_id=job_id,
            offset=offset,
            limit=limit,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/jobs/{job_id}/chat/bulk")
async def get_job_chat_bulk(
    job_id: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=5000, ge=1, le=5000),
) -> dict[str, Any]:
    """Get bulk chat history entries for caching in IndexedDB.

    Uses offset/limit for efficient bulk fetching.
    Returns up to 5000 entries per request.

    Query params:
        offset: Number of entries to skip (default 0)
        limit: Maximum entries to return (max 5000)
    """
    if not mongodb_service.is_available:
        return {
            "entries": [],
            "total": 0,
            "offset": offset,
            "limit": limit,
            "hasMore": False,
            "error": "MongoDB not available",
        }

    try:
        return await mongodb_service.get_chat_history_bulk(
            job_id=job_id,
            offset=offset,
            limit=limit,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/jobs/{job_id}/graph/bulk")
async def get_job_graph_bulk(
    job_id: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=5000, ge=1, le=5000),
) -> dict[str, Any]:
    """Get bulk graph deltas (execute_cypher_query tool calls) for caching.

    Returns raw graph operation data without computed snapshots.
    Use /api/graph/changes/{job_id} for full graph timeline with snapshots.

    Query params:
        offset: Number of deltas to skip (default 0)
        limit: Maximum deltas to return (max 5000)
    """
    if not mongodb_service.is_available:
        return {
            "deltas": [],
            "total": 0,
            "offset": offset,
            "limit": limit,
            "hasMore": False,
            "error": "MongoDB not available",
        }

    try:
        return await mongodb_service.get_graph_deltas_bulk(
            job_id=job_id,
            offset=offset,
            limit=limit,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/jobs/{job_id}/version")
async def get_job_version(job_id: str) -> dict[str, Any] | None:
    """Get job data version info for cache invalidation.

    Returns counts and timestamps that can be compared to cached values
    to determine if the cache needs to be refreshed.

    Returns:
        Dict with version, auditEntryCount, chatEntryCount, graphDeltaCount, lastUpdate
        Returns null if job has no audit data or MongoDB unavailable
    """
    if not mongodb_service.is_available:
        return None

    try:
        return await mongodb_service.get_job_version(job_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
