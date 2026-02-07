"""FastAPI backend for the Debug Cockpit.

Run with:
    uvicorn orchestrator.main:app --reload --port 8085

Or from orchestrator directory:
    uvicorn main:app --reload --port 8085
"""

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv())
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

import yaml
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from pydantic import BaseModel, Field

from database import PostgresDB, MongoDB, ALLOWED_TABLES, FilterCategory
from services.workspace import workspace_service
from services.gitea import GiteaClient
from graph_routes import router as graph_router, set_mongodb
from uploads import router as uploads_router

logger = logging.getLogger(__name__)

# =============================================================================
# Database Instances (singleton pattern)
# =============================================================================

postgres_db = PostgresDB()
mongodb = MongoDB()
gitea_client = GiteaClient()


# =============================================================================
# Background Tasks
# =============================================================================

# Flag to signal shutdown to background tasks
_shutdown_event: asyncio.Event | None = None


async def stale_agent_detector(shutdown_event: asyncio.Event) -> None:
    """Background task that marks agents as offline if no heartbeat received.

    Runs every 60 seconds and marks agents as offline if they haven't sent
    a heartbeat in the last 3 minutes.
    """
    logger.info("Stale agent detector started")
    while not shutdown_event.is_set():
        try:
            count = await postgres_db.mark_stale_agents_offline(timeout_minutes=3)
            if count > 0:
                logger.info(f"Marked {count} agent(s) as offline due to missed heartbeats")
        except Exception as e:
            logger.error(f"Error in stale agent detector: {e}")

        # Wait 60 seconds or until shutdown
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=60.0)
            break  # Shutdown signaled
        except asyncio.TimeoutError:
            pass  # Continue loop

    logger.info("Stale agent detector stopped")


# =============================================================================
# Pydantic Models for Agent Orchestration
# =============================================================================


class AgentRegistration(BaseModel):
    """Request body for agent registration."""

    config_name: str = Field(..., description="Agent configuration name")
    pod_ip: str = Field(..., description="Agent IP address for receiving commands")
    hostname: str | None = Field(None, description="Pod/host name")
    pod_port: int = Field(8001, description="Agent API port")
    pid: int | None = Field(None, description="Process ID")


class AgentRegistrationResponse(BaseModel):
    """Response from agent registration."""

    agent_id: str
    heartbeat_interval_seconds: int


class AgentHeartbeat(BaseModel):
    """Request body for agent heartbeat."""

    status: str = Field(
        ...,
        description="Agent status",
        pattern="^(booting|ready|working|completed|failed)$",
    )
    current_job_id: str | None = Field(None, description="Current job UUID if working")
    metrics: dict[str, Any] | None = Field(
        None,
        description="Optional metrics (memory_mb, cpu_percent, tokens_processed)",
    )


# =============================================================================
# Pydantic Models for Job Management
# =============================================================================


class JobCreate(BaseModel):
    """Request body for creating a new job."""

    description: str = Field(..., description="Job description - what the agent should accomplish")
    upload_id: str | None = Field(None, description="Upload ID for document files (from /api/uploads)")
    config_upload_id: str | None = Field(None, description="Upload ID for config YAML override")
    instructions_upload_id: str | None = Field(None, description="Upload ID for instructions markdown")
    document_path: str | None = Field(None, description="Path to a document (deprecated, use upload_id)")
    document_dir: str | None = Field(None, description="Directory containing documents (deprecated)")
    config_name: str = Field("default", description="Agent configuration name")
    config_override: dict[str, Any] | None = Field(None, description="Per-job configuration overrides")
    context: dict[str, Any] | None = Field(None, description="Optional context dictionary")
    instructions: str | None = Field(None, description="Additional inline instructions for the agent")


class JobStartRequest(BaseModel):
    """Request sent to agent to start a job."""

    job_id: str
    description: str
    upload_id: str | None = None
    config_upload_id: str | None = None
    instructions_upload_id: str | None = None
    document_path: str | None = None
    document_dir: str | None = None
    config_name: str = "default"
    config_override: dict[str, Any] | None = None
    context: dict[str, Any] | None = None
    instructions: str | None = None
    git_remote_url: str | None = None


class CustomJSONEncoder(json.JSONEncoder):
    """JSON encoder that handles PostgreSQL types."""

    def default(self, obj: Any) -> Any:
        if isinstance(obj, UUID):
            return str(obj)
        if isinstance(obj, datetime):
            # Ensure timestamps include UTC indicator for proper browser parsing
            if obj.tzinfo is None:
                # Naive datetime - assume UTC and add Z suffix
                return obj.isoformat() + "Z"
            else:
                # Timezone-aware - convert to UTC and use Z suffix
                utc_dt = obj.astimezone(timezone.utc)
                return utc_dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        if isinstance(obj, date):
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
    global _shutdown_event

    # Connect to databases
    await postgres_db.connect()
    await mongodb.connect()

    # Share MongoDB instance with graph_routes
    set_mongodb(mongodb)

    # Initialize Gitea workspace delivery (graceful if unavailable)
    await gitea_client.ensure_initialized()

    # Start background tasks
    _shutdown_event = asyncio.Event()
    stale_detector_task = asyncio.create_task(stale_agent_detector(_shutdown_event))

    yield

    # Signal shutdown to background tasks
    _shutdown_event.set()
    await stale_detector_task

    # Cleanup clients
    await gitea_client.close()

    # Disconnect from databases
    await mongodb.disconnect()
    await postgres_db.disconnect()


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
app.include_router(uploads_router)


@app.get("/api/tables")
async def list_tables() -> list[dict[str, Any]]:
    """List available tables with row counts."""
    return await postgres_db.get_tables()


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
        return await postgres_db.get_table_data(table_name, page, page_size)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/tables/{table_name}/schema")
async def get_table_schema(table_name: str) -> list[dict[str, Any]]:
    """Get column definitions for a table."""
    if table_name not in ALLOWED_TABLES:
        raise HTTPException(status_code=404, detail=f"Table '{table_name}' not found")

    try:
        return await postgres_db.get_table_schema(table_name)
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
        jobs = await postgres_db.get_jobs(status=status, limit=limit)

        # Enrich with audit counts if MongoDB is available
        if mongodb.is_available:
            for job in jobs:
                job_id = str(job["id"])
                job["audit_count"] = await mongodb.get_audit_count(job_id)
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
        job = await postgres_db.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

        # Enrich with audit count if MongoDB is available
        if mongodb.is_available:
            job["audit_count"] = await mongodb.get_audit_count(job_id)
        else:
            job["audit_count"] = None

        return job
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/api/jobs")
async def create_job(job: JobCreate) -> dict[str, Any]:
    """Create a new job.

    Creates a job with status 'created'. The job must be assigned to an agent
    to start processing.
    """
    try:
        # Merge upload IDs into context
        context = dict(job.context) if job.context else {}
        if job.upload_id:
            context["upload_id"] = job.upload_id
        if job.config_upload_id:
            context["config_upload_id"] = job.config_upload_id
        if job.instructions_upload_id:
            context["instructions_upload_id"] = job.instructions_upload_id

        result = await postgres_db.create_job(
            description=job.description,
            document_path=job.document_path,
            document_dir=job.document_dir,
            config_name=job.config_name,
            config_override=job.config_override,
            context=context if context else None,
        )

        # Create Gitea repo for workspace delivery
        if gitea_client.is_initialized:
            job_id_str = str(result["id"])
            git_remote_url = await gitea_client.create_repo(f"job-{job_id_str}")
            if git_remote_url:
                ctx = dict(context) if context else {}
                ctx["git_remote_url"] = git_remote_url
                await postgres_db.update_job_context(job_id_str, ctx)

        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.delete("/api/jobs/{job_id}")
async def delete_job(job_id: str) -> dict[str, str]:
    """Delete a job and its requirements."""
    try:
        success = await postgres_db.delete_job(job_id)
        if not success:
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
        return {"status": "deleted"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.put("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: str) -> dict[str, str]:
    """Cancel a running job.

    If the job is assigned to an agent, this will also send a cancel request
    to the agent pod.
    """
    try:
        # First get the job to check if it's assigned to an agent
        job = await postgres_db.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

        # TODO: If job is assigned to an agent, send cancel request to agent pod
        # For now, just update the database status

        success = await postgres_db.cancel_job(job_id)
        if not success:
            raise HTTPException(
                status_code=400,
                detail="Job cannot be cancelled (already completed or cancelled)",
            )
        return {"status": "cancelled"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


class JobResumeRequest(BaseModel):
    """Request body for resuming a failed job."""

    feedback: str | None = Field(None, description="Optional feedback to inject before resuming")
    agent_id: str | None = Field(None, description="Override agent ID if original is offline")


@app.post("/api/jobs/{job_id}/resume")
async def resume_job(job_id: str, request: JobResumeRequest | None = None) -> dict[str, str]:
    """Resume a failed job from its checkpoint.

    This endpoint:
    1. Validates the job exists and is in 'failed' status
    2. Gets the assigned agent (or uses override agent_id from request)
    3. Validates the agent is ready or completed (not offline/working)
    4. Sends a resume request to the agent's pod
    5. Updates job and agent status on success

    Returns:
        Status message indicating resume result
    """
    import httpx

    if request is None:
        request = JobResumeRequest()

    try:
        # Get job details
        job = await postgres_db.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

        # Allow resuming jobs in any status except completed/cancelled
        # This handles cases where agents disappear without marking jobs as failed
        if job["status"] in ("completed", "cancelled"):
            raise HTTPException(
                status_code=400,
                detail=f"Job cannot be resumed (status: {job['status']}).",
            )

        # Determine which agent to use
        # Convert to string since DB returns asyncpg UUID objects
        assigned_agent_id = job.get("assigned_agent_id")
        agent_id = request.agent_id or (str(assigned_agent_id) if assigned_agent_id else None)
        agent = None

        # Try to get the specified/assigned agent
        if agent_id:
            agent = await postgres_db.get_agent(agent_id)

        # If no agent or agent is offline/unavailable, find a ready one
        if not agent or agent["status"] in ("offline", "failed"):
            ready_agents = await postgres_db.list_agents(status="ready", limit=1)
            if not ready_agents:
                raise HTTPException(
                    status_code=400,
                    detail="No ready agents available to resume job.",
                )
            agent = ready_agents[0]
            agent_id = str(agent["id"])
            logger.info(f"Auto-selected agent {agent_id} for job resume")

        if agent["status"] not in ("ready", "completed"):
            raise HTTPException(
                status_code=400,
                detail=f"Agent is not ready (status: {agent['status']})",
            )

        if not agent.get("pod_ip"):
            raise HTTPException(
                status_code=400,
                detail="Agent has no pod IP configured",
            )

        # Build resume request payload
        # Include full config info so agent can restore the original job configuration
        job_config_name = job.get("config_name", "default")

        # Handle context - might be dict or JSON string depending on DB driver
        job_context = job.get("context") or {}
        if isinstance(job_context, str):
            import json
            try:
                job_context = json.loads(job_context)
            except json.JSONDecodeError:
                job_context = {}

        # Same for config_override
        config_override = job.get("config_override")
        if isinstance(config_override, str):
            import json
            try:
                config_override = json.loads(config_override)
            except json.JSONDecodeError:
                config_override = None

        resume_payload = {
            "job_id": job_id,
            "config_name": job_config_name,
            "config_upload_id": job_context.get("config_upload_id") if job_context else None,
            "config_override": config_override,
        }
        if request and request.feedback:
            resume_payload["feedback"] = request.feedback

        # Send request to agent pod
        agent_url = f"http://{agent['pod_ip']}:{agent['pod_port']}/job/resume"

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                agent_url,
                json=resume_payload,
            )

        if response.status_code not in (200, 202):
            raise HTTPException(
                status_code=502,
                detail=f"Agent rejected resume request: {response.text}",
            )

        # Update job status and assign to agent (if using override)
        await postgres_db.update_job_status(
            job_id=job_id,
            status="processing",
            assigned_agent_id=agent_id,
        )

        # Update agent status via heartbeat simulation
        await postgres_db.heartbeat(
            agent_id=agent_id,
            status="working",
            current_job_id=job_id,
        )

        return {"status": "resumed", "job_id": job_id, "agent_id": str(agent_id)}

    except HTTPException:
        raise
    except httpx.RequestError as e:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to connect to agent: {str(e)}. Agent may be offline.",
        ) from e
    except Exception as e:
        logger.exception(f"Failed to resume job {job_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/jobs/{job_id}/requirements")
async def get_job_requirements(
    job_id: str,
    status: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """Get requirements for a job with optional filtering."""
    try:
        return await postgres_db.get_requirements(
            job_id=job_id,
            status=status,
            limit=limit,
            offset=offset,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/jobs/{job_id}/progress")
async def get_job_progress(job_id: str) -> dict[str, Any]:
    """Get detailed progress information for a job including ETA."""
    try:
        progress = await postgres_db.get_job_progress(job_id)
        if not progress:
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
        return progress
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
    if not mongodb.is_available:
        return {
            "entries": [],
            "total": 0,
            "page": page,
            "pageSize": page_size,
            "hasMore": False,
            "error": "MongoDB not available",
        }

    try:
        return await mongodb.get_job_audit(
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
    if not mongodb.is_available:
        raise HTTPException(
            status_code=503,
            detail="MongoDB not available",
        )

    try:
        request = await mongodb.get_request(doc_id)
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


@app.get("/api/jobs/{job_id}/audit/timerange")
async def get_audit_time_range(job_id: str) -> dict[str, str] | None:
    """Get first and last timestamps for job audit entries.

    Returns:
        Dict with 'start' and 'end' ISO timestamps, or null if no entries/MongoDB unavailable
    """
    if not mongodb.is_available:
        return None

    try:
        return await mongodb.get_audit_time_range(job_id)
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
    if not mongodb.is_available:
        return {
            "entries": [],
            "total": 0,
            "page": page,
            "pageSize": page_size,
            "hasMore": False,
            "error": "MongoDB not available",
        }

    try:
        return await mongodb.get_chat_history(
            job_id=job_id,
            page=page,
            page_size=page_size,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


# =============================================================================
# Workspace / Todo Endpoints
# =============================================================================


@app.get("/api/jobs/{job_id}/workspace")
async def get_job_workspace(job_id: str) -> dict[str, Any]:
    """Get workspace overview for a job.

    Returns:
        Dict with workspace files, workspace.md/plan.md content (truncated),
        current todos, and archive count.
    """
    return workspace_service.get_workspace_overview(job_id)


@app.get("/api/jobs/{job_id}/workspace/{filename}")
async def get_workspace_file(job_id: str, filename: str) -> dict[str, str]:
    """Get content of a specific workspace file.

    Args:
        job_id: Job UUID
        filename: File name (workspace.md, plan.md, or todos.yaml)

    Returns:
        Dict with file content
    """
    content = workspace_service.get_workspace_file(job_id, filename)
    if content is None:
        raise HTTPException(
            status_code=404,
            detail=f"File '{filename}' not found in workspace for job '{job_id}'",
        )
    return {"filename": filename, "content": content}


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
    if not mongodb.is_available:
        return {
            "entries": [],
            "total": 0,
            "offset": offset,
            "limit": limit,
            "hasMore": False,
            "error": "MongoDB not available",
        }

    try:
        return await mongodb.get_job_audit_bulk(
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
    if not mongodb.is_available:
        return {
            "entries": [],
            "total": 0,
            "offset": offset,
            "limit": limit,
            "hasMore": False,
            "error": "MongoDB not available",
        }

    try:
        return await mongodb.get_chat_history_bulk(
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
    if not mongodb.is_available:
        return {
            "deltas": [],
            "total": 0,
            "offset": offset,
            "limit": limit,
            "hasMore": False,
            "error": "MongoDB not available",
        }

    try:
        return await mongodb.get_graph_deltas_bulk(
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
    if not mongodb.is_available:
        return None

    try:
        return await mongodb.get_job_version(job_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


# =============================================================================
# Job Assignment Endpoints
# =============================================================================


@app.post("/api/jobs/{job_id}/assign/{agent_id}")
async def assign_job_to_agent(job_id: str, agent_id: str) -> dict[str, str]:
    """Assign a job to an agent.

    This endpoint:
    1. Validates the job exists and is in 'created' status
    2. Validates the agent exists and is in 'ready' status
    3. Sends a JobStartRequest to the agent's pod
    4. Updates job and agent status on success

    Returns:
        Status message indicating assignment result
    """
    import httpx

    try:
        # Get job details
        job = await postgres_db.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

        if job["status"] not in ("created", "failed"):
            raise HTTPException(
                status_code=400,
                detail=f"Job cannot be assigned (status: {job['status']})",
            )

        # Get agent details
        agent = await postgres_db.get_agent(agent_id)
        if not agent:
            raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")

        if agent["status"] != "ready":
            raise HTTPException(
                status_code=400,
                detail=f"Agent is not ready (status: {agent['status']})",
            )

        if not agent.get("pod_ip"):
            raise HTTPException(
                status_code=400,
                detail="Agent has no pod IP configured",
            )

        # Extract upload IDs from context if present
        job_context = job.get("context") or {}
        if isinstance(job_context, str):
            import json as json_module
            job_context = json_module.loads(job_context)
        upload_id = job_context.get("upload_id")
        config_upload_id = job_context.get("config_upload_id")
        instructions_upload_id = job_context.get("instructions_upload_id")
        git_remote_url = job_context.get("git_remote_url")

        # Parse config_override if stored as string
        config_override = job.get("config_override")
        if isinstance(config_override, str):
            import json as json_module
            config_override = json_module.loads(config_override)

        # Build job start request - use job's config, not agent's
        job_start = JobStartRequest(
            job_id=job_id,
            description=job["description"],
            upload_id=upload_id,
            config_upload_id=config_upload_id,
            instructions_upload_id=instructions_upload_id,
            document_path=job.get("document_path"),
            config_name=job.get("config_name", "default"),
            config_override=config_override,
            git_remote_url=git_remote_url,
        )

        # Send request to agent pod
        agent_url = f"http://{agent['pod_ip']}:{agent['pod_port']}/job/start"

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                agent_url,
                json=job_start.model_dump(exclude_none=True),
            )

        if response.status_code not in (200, 202):
            raise HTTPException(
                status_code=502,
                detail=f"Agent rejected job: {response.text}",
            )

        # Update job status and assign to agent
        await postgres_db.update_job_status(
            job_id=job_id,
            status="processing",
            creator_status="pending",
            assigned_agent_id=agent_id,
        )

        # Update agent status via heartbeat simulation
        await postgres_db.heartbeat(
            agent_id=agent_id,
            status="working",
            current_job_id=job_id,
        )

        return {"status": "assigned", "agent_id": agent_id, "job_id": job_id}

    except HTTPException:
        raise
    except httpx.RequestError as e:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to connect to agent: {str(e)}",
        ) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


# =============================================================================
# Statistics Endpoints
# =============================================================================


@app.get("/api/stats/jobs")
async def get_job_statistics() -> dict[str, int]:
    """Get overall job statistics."""
    try:
        return await postgres_db.get_job_statistics()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/stats/daily")
async def get_daily_statistics(
    days: int = Query(default=7, ge=1, le=90),
) -> list[dict[str, Any]]:
    """Get daily job statistics for the past N days."""
    try:
        return await postgres_db.get_daily_statistics(days=days)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/stats/agents")
async def get_agent_statistics() -> dict[str, Any]:
    """Get agent workforce summary."""
    try:
        agents = await postgres_db.list_agents(limit=500)

        # Count by status
        status_counts = {
            "total": len(agents),
            "booting": 0,
            "ready": 0,
            "working": 0,
            "completed": 0,
            "failed": 0,
            "offline": 0,
        }

        for agent in agents:
            status = agent.get("status", "unknown")
            if status in status_counts:
                status_counts[status] += 1

        return status_counts
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/stats/stuck")
async def get_stuck_jobs(
    threshold_minutes: int = Query(default=60, ge=1, le=1440),
) -> list[dict[str, Any]]:
    """Get jobs that appear to be stuck.

    A job is considered stuck if it's in 'processing' status but hasn't
    been updated within the threshold period.
    """
    try:
        return await postgres_db.detect_stuck_jobs(threshold_minutes=threshold_minutes)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


# =============================================================================
# Agent Orchestration Endpoints
# =============================================================================


@app.post("/api/agents/register", response_model=AgentRegistrationResponse)
async def register_agent(registration: AgentRegistration) -> AgentRegistrationResponse:
    """Register a new agent or update existing one.

    When an agent starts up, it calls this endpoint to register itself.
    If an agent with the same hostname exists, its pod_ip is updated.

    Returns:
        AgentRegistrationResponse with agent_id and heartbeat_interval_seconds
    """
    try:
        result = await postgres_db.register_agent(
            config_name=registration.config_name,
            pod_ip=registration.pod_ip,
            hostname=registration.hostname,
            pod_port=registration.pod_port,
            pid=registration.pid,
        )
        return AgentRegistrationResponse(**result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/api/agents/{agent_id}/heartbeat")
async def agent_heartbeat(agent_id: str, heartbeat: AgentHeartbeat) -> dict[str, str]:
    """Update agent heartbeat and status.

    Agents call this every 60 seconds to report their status.
    The orchestrator uses this to track agent health and current job state.
    """
    try:
        success = await postgres_db.heartbeat(
            agent_id=agent_id,
            status=heartbeat.status,
            current_job_id=heartbeat.current_job_id,
            metrics=heartbeat.metrics,
        )
        if not success:
            raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
        return {"status": "ok"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/agents")
async def list_agents(
    status: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict[str, Any]]:
    """List all registered agents.

    Args:
        status: Optional status filter (booting, ready, working, completed, failed, offline)
        limit: Maximum agents to return
    """
    try:
        return await postgres_db.list_agents(status=status, limit=limit)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/agents/{agent_id}")
async def get_agent(agent_id: str) -> dict[str, Any]:
    """Get agent details by ID."""
    try:
        agent = await postgres_db.get_agent(agent_id)
        if not agent:
            raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
        return agent
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.delete("/api/agents/{agent_id}")
async def delete_agent(agent_id: str) -> dict[str, str]:
    """Deregister an agent.

    Called when an agent shuts down gracefully.
    """
    try:
        success = await postgres_db.delete_agent(agent_id)
        if not success:
            raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")
        return {"status": "deleted"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


# =============================================================================
# Expert Discovery
# =============================================================================


class ExpertInfo(BaseModel):
    """Expert configuration metadata for discovery."""

    id: str
    display_name: str
    description: str
    icon: str = "psychology"
    color: str = "#cba6f7"
    tags: list[str] = []


def _get_config_dir() -> Path:
    """Resolve the config directory path."""
    config_dir_env = os.environ.get("CONFIG_DIR")
    if config_dir_env:
        return Path(config_dir_env)
    # Orchestrator runs from orchestrator/ or project root
    candidates = [
        Path(__file__).parent.parent / "config",  # from orchestrator/
        Path("/app/config"),  # in container
    ]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return candidates[0]


def _scan_experts() -> list[ExpertInfo]:
    """Scan config/experts/ for expert configurations."""
    config_dir = _get_config_dir()
    experts_dir = config_dir / "experts"
    experts: list[ExpertInfo] = []

    # Add synthetic defaults entry
    experts.append(
        ExpertInfo(
            id="defaults",
            display_name="Generalist Agent",
            description="General-purpose agent with balanced capabilities for requirement extraction, validation, and compliance checking.",
            icon="psychology",
            color="#cba6f7",
            tags=["general", "requirements", "compliance"],
        )
    )

    if not experts_dir.is_dir():
        return experts

    for entry in sorted(experts_dir.iterdir()):
        config_path = entry / "config.yaml"
        if not entry.is_dir() or not config_path.exists():
            continue

        try:
            with open(config_path) as f:
                data = yaml.safe_load(f) or {}

            description = data.get("description", "").strip()

            # Summarize tools if no description
            if not description:
                tools = data.get("tools", {})
                tool_categories = [k for k in tools if tools[k]]
                description = f"Agent with {', '.join(tool_categories)} tools." if tool_categories else "Custom agent configuration."

            experts.append(
                ExpertInfo(
                    id=entry.name,
                    display_name=data.get("display_name", entry.name.replace("_", " ").title()),
                    description=description,
                    icon=data.get("icon", "psychology"),
                    color=data.get("color", "#cba6f7"),
                    tags=data.get("tags", []),
                )
            )
        except Exception as e:
            logger.warning(f"Failed to parse expert config {config_path}: {e}")

    return experts


# Cache experts at startup
_experts_cache: list[ExpertInfo] | None = None


@app.get("/api/experts")
async def list_experts() -> list[dict[str, Any]]:
    """List available expert configurations.

    Scans config/experts/ for expert configs and returns metadata
    for expert selection in the cockpit UI.
    """
    global _experts_cache
    if _experts_cache is None:
        _experts_cache = _scan_experts()
    return [e.model_dump() for e in _experts_cache]


@app.post("/api/experts/reload")
async def reload_experts() -> dict[str, Any]:
    """Force reload of expert configurations cache."""
    global _experts_cache
    _experts_cache = _scan_experts()
    return {"status": "reloaded", "count": len(_experts_cache)}
