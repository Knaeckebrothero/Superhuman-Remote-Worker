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
from datetime import date, datetime, timezone  # noqa: E402
from decimal import Decimal  # noqa: E402
from typing import Any  # noqa: E402
from uuid import UUID  # noqa: E402

import asyncpg  # noqa: E402
import yaml  # noqa: E402
from fastapi import FastAPI, HTTPException, Query  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import JSONResponse, StreamingResponse  # noqa: E402

from pydantic import BaseModel, Field  # noqa: E402

from database import PostgresDB, MongoDB, ALLOWED_TABLES, FilterCategory  # noqa: E402
from services.workspace import workspace_service  # noqa: E402
from services.gitea import GiteaClient  # noqa: E402
from services.builder_tools import (  # noqa: E402
    BUILDER_TOOLS,
    SERVER_SIDE_TOOLS,
    build_message_context,
    build_summarization_prompt,
    get_builder_api_key,
    get_builder_base_url,
    get_builder_model,
    get_builder_provider,
)
from services.builder_search import tavily_search  # noqa: E402
from services.builder_prompt import build_system_prompt  # noqa: E402
from graph_routes import router as graph_router, set_mongodb  # noqa: E402
from uploads import router as uploads_router  # noqa: E402

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


class DatasourceCreate(BaseModel):
    """Request body for creating a datasource."""

    name: str = Field(..., description="User-provided label")
    type: str = Field(..., description="Datasource type: postgresql, neo4j, mongodb")
    connection_url: str = Field(..., description="Full connection string")
    description: str | None = Field(None, description="What this datasource contains")
    credentials: dict[str, Any] | None = Field(None, description="Additional auth details")
    read_only: bool = Field(True, description="Whether the agent is allowed to write")
    job_id: str | None = Field(None, description="Job UUID (null for global)")


class DatasourceUpdate(BaseModel):
    """Request body for updating a datasource."""

    name: str | None = Field(None, description="New label")
    description: str | None = Field(None, description="New description")
    connection_url: str | None = Field(None, description="New connection string")
    credentials: dict[str, Any] | None = Field(None, description="New auth details")
    read_only: bool | None = Field(None, description="New read_only flag")


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
    datasource_ids: list[str] | None = Field(None, description="Global datasource IDs to clone as job-scoped")
    builder_session_id: str | None = Field(None, description="Builder session ID to link to this job")


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
    datasources: list[dict[str, Any]] | None = None


class BuilderSessionCreate(BaseModel):
    """Request body for creating a builder session."""

    expert_id: str | None = Field(None, description="Expert used as starting point")


class BuilderMessageRequest(BaseModel):
    """Request body for sending a message to the builder."""

    message: str = Field(..., description="User's message text")
    instructions: str | None = Field(None, description="Current instructions content")
    config: dict[str, Any] | None = Field(None, description="Current config override")
    description: str | None = Field(None, description="Current job description")


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
        if job.instructions:
            context["instructions"] = job.instructions

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

        # Clone selected global datasources as job-scoped
        if job.datasource_ids:
            new_job_id = str(result["id"])
            for ds_id in job.datasource_ids:
                try:
                    ds = await postgres_db.get_datasource(ds_id)
                    if ds and ds.get("job_id") is None:
                        # Parse credentials if stored as string
                        creds = ds.get("credentials") or {}
                        if isinstance(creds, str):
                            try:
                                creds = json.loads(creds)
                            except (json.JSONDecodeError, ValueError):
                                creds = {}

                        await postgres_db.create_datasource(
                            name=ds["name"],
                            ds_type=ds["type"],
                            connection_url=ds["connection_url"],
                            description=ds.get("description"),
                            credentials=creds if creds else None,
                            read_only=ds.get("read_only", True),
                            job_id=new_job_id,
                        )
                    else:
                        logger.warning(
                            f"Skipping datasource {ds_id}: "
                            f"{'not found' if not ds else 'not global (already job-scoped)'}"
                        )
                except Exception as e:
                    logger.warning(f"Failed to clone datasource {ds_id} for job {new_job_id}: {e}")

        # Link builder session to job (if provided)
        if job.builder_session_id:
            try:
                await postgres_db.update_builder_session_job(
                    session_id=job.builder_session_id,
                    job_id=str(result["id"]),
                )
            except Exception as e:
                logger.warning(f"Failed to link builder session {job.builder_session_id}: {e}")

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

        # Resolve datasources for this job (job-specific > global fallback)
        resolved_ds = await postgres_db.resolve_datasources_for_job(job_id)
        datasources_payload = _build_datasources_payload(resolved_ds)

        # Apply datasource-driven tool override (inject/strip db tool categories)
        if resolved_ds:
            config_override = _build_datasource_tool_override(resolved_ds, config_override)

        resume_payload = {
            "job_id": job_id,
            "config_name": job_config_name,
            "config_upload_id": job_context.get("config_upload_id") if job_context else None,
            "config_override": config_override,
            "datasources": datasources_payload,
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


class JobApproveRequest(BaseModel):
    """Request body for approving a frozen job."""

    notes: str | None = Field(None, description="Optional reviewer notes")


@app.post("/api/jobs/{job_id}/approve")
async def approve_job(job_id: str, request: JobApproveRequest | None = None) -> dict[str, Any]:
    """Approve a frozen job, marking it as completed.

    This endpoint mirrors the logic from agent.py:approve_frozen_job but runs
    entirely on the orchestrator side — no agent pod needs to be running.

    Steps:
    1. Validates job exists and is in 'pending_review' status
    2. Reads job_frozen.json from the Gitea repo
    3. Writes job_completion.json to the Gitea repo
    4. Removes job_frozen.json from the Gitea repo
    5. Updates DB status to 'completed' with completed_at timestamp
    """
    if request is None:
        request = JobApproveRequest()

    try:
        # 1. Validate job exists and is in pending_review
        job = await postgres_db.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

        if job["status"] != "pending_review":
            raise HTTPException(
                status_code=400,
                detail=f"Job cannot be approved (status: {job['status']}). "
                       f"Only jobs in 'pending_review' status can be approved.",
            )

        # 2. Read job_frozen.json from Gitea
        repo_name = f"job-{job_id}"
        frozen_data = None

        if gitea_client.is_initialized:
            frozen_data = await gitea_client.get_file(repo_name, "output/job_frozen.json")

        if frozen_data is None:
            # Fallback: try local workspace filesystem
            workspace_path = workspace_service.base_path / f"job_{job_id}" / "output" / "job_frozen.json"
            if workspace_path.exists():
                frozen_data = json.loads(workspace_path.read_text())
            else:
                raise HTTPException(
                    status_code=404,
                    detail=f"No job_frozen.json found for job '{job_id}' "
                           f"(checked Gitea repo and local workspace)",
                )

        # 3. Build completion data
        completion_data = {
            **frozen_data,
            "status": "job_completed",
            "approved_at": datetime.now(timezone.utc).isoformat(),
            "approved_by": "human_operator",
        }
        if request.notes:
            completion_data["reviewer_notes"] = request.notes

        completion_json = json.dumps(completion_data, indent=2, ensure_ascii=False)

        # 4. Write job_completion.json and remove job_frozen.json
        wrote_to_gitea = False
        if gitea_client.is_initialized:
            wrote_completion = await gitea_client.create_or_update_file(
                repo_name,
                "output/job_completion.json",
                completion_json,
                "Approve job: write job_completion.json",
            )
            if wrote_completion:
                await gitea_client.delete_file(
                    repo_name,
                    "output/job_frozen.json",
                    "Approve job: remove job_frozen.json",
                )
                wrote_to_gitea = True

        # Also write to local workspace if it exists
        local_output = workspace_service.base_path / f"job_{job_id}" / "output"
        if local_output.exists():
            completion_path = local_output / "job_completion.json"
            completion_path.write_text(completion_json)
            frozen_path = local_output / "job_frozen.json"
            if frozen_path.exists():
                frozen_path.unlink()

        # 5. Update DB: status → completed, set completed_at
        async with postgres_db.acquire() as conn:
            await conn.execute(
                "UPDATE jobs SET status = 'completed', completed_at = CURRENT_TIMESTAMP, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = $1::uuid",
                job_id,
            )

        logger.info(f"Job {job_id} approved (gitea={wrote_to_gitea})")

        return {
            "status": "approved",
            "job_id": job_id,
            "summary": completion_data.get("summary", ""),
            "deliverables": completion_data.get("deliverables", []),
            "approved_at": completion_data["approved_at"],
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Failed to approve job {job_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/jobs/{job_id}/frozen")
async def get_frozen_job_data(job_id: str) -> dict[str, Any]:
    """Get the frozen job data (job_frozen.json) for a pending_review job.

    Tries Gitea first, falls back to local workspace.

    Returns:
        Contents of job_frozen.json (summary, deliverables, confidence, notes, etc.)
    """
    try:
        # Try Gitea first
        repo_name = f"job-{job_id}"
        frozen_data = None

        if gitea_client.is_initialized:
            frozen_data = await gitea_client.get_file(repo_name, "output/job_frozen.json")

        if frozen_data is None:
            # Fallback: local workspace
            workspace_path = workspace_service.base_path / f"job_{job_id}" / "output" / "job_frozen.json"
            if workspace_path.exists():
                frozen_data = json.loads(workspace_path.read_text())

        if frozen_data is None:
            raise HTTPException(
                status_code=404,
                detail=f"No frozen job data found for job '{job_id}'",
            )

        return frozen_data

    except HTTPException:
        raise
    except Exception as e:
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
# Workspace Browser Endpoints (Gitea proxy)
# =============================================================================


@app.get("/api/jobs/{job_id}/repo/contents")
async def list_repo_contents(
    job_id: str,
    path: str = Query(default="", description="Directory path within the repo"),
) -> list[dict[str, Any]]:
    """List directory contents of a job's Gitea repository.

    Proxies the Gitea contents API so the cockpit doesn't need Gitea credentials.

    Returns:
        List of entries, each with: name, path, type ("file"|"dir"), size
    """
    if not gitea_client.is_initialized:
        raise HTTPException(
            status_code=503,
            detail="Gitea not available",
        )

    repo_name = f"job-{job_id}"
    contents = await gitea_client.list_contents(repo_name, path)

    if contents is None:
        raise HTTPException(
            status_code=404,
            detail=f"Path '{path or '/'}' not found in repo for job '{job_id}'",
        )

    return contents


@app.get("/api/jobs/{job_id}/repo/file")
async def get_repo_file(
    job_id: str,
    path: str = Query(..., description="File path within the repo"),
) -> dict[str, Any]:
    """Get file content from a job's Gitea repository.

    Returns:
        Dict with path, content (text), and size
    """
    if not gitea_client.is_initialized:
        raise HTTPException(
            status_code=503,
            detail="Gitea not available",
        )

    repo_name = f"job-{job_id}"
    content = await gitea_client.get_file_content(repo_name, path)

    if content is None:
        raise HTTPException(
            status_code=404,
            detail=f"File '{path}' not found in repo for job '{job_id}'",
        )

    return {
        "path": path,
        "content": content,
        "size": len(content),
    }


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


def _build_datasource_tool_override(
    datasources: list[dict[str, Any]], config_override: dict[str, Any] | None
) -> dict[str, Any]:
    """Inject/strip database tool categories based on attached datasources.

    For each known datasource type, if a datasource is attached, the corresponding
    tool category is injected. If not attached, the category is set to an empty list.
    This ensures the agent only has database tools for databases that are actually
    connected.

    Args:
        datasources: List of resolved datasource dicts (from resolve_datasources_for_job)
        config_override: Existing config override dict (may be None)

    Returns:
        Updated config override dict with tool categories adjusted
    """
    override = dict(config_override or {})
    tools_override = dict(override.get("tools", {}))

    # Datasource type -> tool category + tool names
    DS_TOOL_MAP = {
        "neo4j": {
            "category": "graph",
            "read": ["execute_cypher_query", "get_database_schema"],
            "write": ["execute_cypher_query", "get_database_schema"],
        },
        "postgresql": {
            "category": "sql",
            "read": ["sql_query", "sql_schema"],
            "write": ["sql_query", "sql_schema", "sql_execute"],
        },
        "mongodb": {
            "category": "mongodb",
            "read": ["mongo_query", "mongo_aggregate", "mongo_schema"],
            "write": ["mongo_query", "mongo_aggregate", "mongo_schema", "mongo_insert", "mongo_update"],
        },
    }

    attached_types = {ds["type"] for ds in datasources}

    for ds_type, tool_info in DS_TOOL_MAP.items():
        category = tool_info["category"]
        if ds_type in attached_types:
            # Find the datasource to check read_only
            ds = next(d for d in datasources if d["type"] == ds_type)
            tools = tool_info["write"] if not ds.get("read_only", True) else tool_info["read"]
            tools_override[category] = tools
        else:
            # No datasource attached — strip the category
            tools_override[category] = []

    override["tools"] = tools_override
    return override


def _build_datasources_payload(resolved_ds: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    """Build the datasources payload for sending to the agent.

    Strips internal fields (id, job_id, created_at, updated_at) that the
    agent doesn't need.

    Args:
        resolved_ds: List of resolved datasource dicts from the database

    Returns:
        List of datasource dicts for the agent, or None if empty
    """
    if not resolved_ds:
        return None

    payload = []
    for ds in resolved_ds:
        creds = ds.get("credentials") or {}
        if isinstance(creds, str):
            import json as json_module
            try:
                creds = json_module.loads(creds)
            except (json.JSONDecodeError, ValueError):
                creds = {}

        payload.append({
            "type": ds["type"],
            "name": ds["name"],
            "description": ds.get("description"),
            "connection_url": ds["connection_url"],
            "credentials": creds,
            "read_only": ds.get("read_only", True),
        })

    return payload or None


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
        instructions = job_context.get("instructions")
        git_remote_url = job_context.get("git_remote_url")

        # Parse config_override if stored as string
        config_override = job.get("config_override")
        if isinstance(config_override, str):
            import json as json_module
            config_override = json_module.loads(config_override)

        # Build remaining context (fields not extracted as dedicated params)
        extracted_keys = {"upload_id", "config_upload_id", "instructions_upload_id", "instructions", "git_remote_url"}
        remaining_context = {k: v for k, v in job_context.items() if k not in extracted_keys}

        # Resolve datasources for this job (job-specific > global fallback)
        resolved_ds = await postgres_db.resolve_datasources_for_job(job_id)
        datasources_payload = _build_datasources_payload(resolved_ds)

        # Apply datasource-driven tool override (inject/strip db tool categories)
        if resolved_ds:
            config_override = _build_datasource_tool_override(resolved_ds, config_override)

        # Build job start request - use job's config, not agent's
        job_start = JobStartRequest(
            job_id=job_id,
            description=job["description"],
            upload_id=upload_id,
            config_upload_id=config_upload_id,
            instructions_upload_id=instructions_upload_id,
            instructions=instructions,
            document_path=job.get("document_path"),
            config_name=job.get("config_name", "default"),
            config_override=config_override,
            git_remote_url=git_remote_url,
            context=remaining_context if remaining_context else None,
            datasources=datasources_payload,
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
# Datasource Endpoints
# =============================================================================


@app.get("/api/datasources")
async def list_datasources(
    job_id: str | None = Query(default=None, description="Filter by job ID (use 'global' for global-only)"),
    type: str | None = Query(default=None, description="Filter by type (postgresql, neo4j, mongodb)"),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict[str, Any]]:
    """List datasources with optional filters."""
    try:
        return await postgres_db.list_datasources(job_id=job_id, ds_type=type, limit=limit)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/datasources/{datasource_id}")
async def get_datasource(datasource_id: str) -> dict[str, Any]:
    """Get a single datasource by ID."""
    try:
        ds = await postgres_db.get_datasource(datasource_id)
        if not ds:
            raise HTTPException(status_code=404, detail=f"Datasource '{datasource_id}' not found")
        return ds
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/api/datasources")
async def create_datasource(body: DatasourceCreate) -> dict[str, Any]:
    """Create a new datasource.

    Use job_id=null for global datasources (available to all jobs).
    """
    valid_types = {"postgresql", "neo4j", "mongodb"}
    if body.type not in valid_types:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid type '{body.type}'. Must be one of: {', '.join(sorted(valid_types))}",
        )

    try:
        return await postgres_db.create_datasource(
            name=body.name,
            ds_type=body.type,
            connection_url=body.connection_url,
            description=body.description,
            credentials=body.credentials,
            read_only=body.read_only,
            job_id=body.job_id,
        )
    except Exception as e:
        error_msg = str(e)
        if "unique" in error_msg.lower() or "duplicate" in error_msg.lower():
            scope = f"job '{body.job_id}'" if body.job_id else "global scope"
            raise HTTPException(
                status_code=409,
                detail=f"A '{body.type}' datasource already exists for {scope}",
            ) from e
        raise HTTPException(status_code=500, detail=error_msg) from e


@app.put("/api/datasources/{datasource_id}")
async def update_datasource(datasource_id: str, body: DatasourceUpdate) -> dict[str, str]:
    """Update a datasource."""
    try:
        success = await postgres_db.update_datasource(
            datasource_id=datasource_id,
            name=body.name,
            description=body.description,
            connection_url=body.connection_url,
            credentials=body.credentials,
            read_only=body.read_only,
        )
        if not success:
            raise HTTPException(status_code=404, detail=f"Datasource '{datasource_id}' not found")
        return {"status": "updated"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.delete("/api/datasources/{datasource_id}")
async def delete_datasource(datasource_id: str) -> dict[str, str]:
    """Delete a datasource."""
    try:
        success = await postgres_db.delete_datasource(datasource_id)
        if not success:
            raise HTTPException(status_code=404, detail=f"Datasource '{datasource_id}' not found")
        return {"status": "deleted"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/jobs/{job_id}/datasources")
async def get_job_datasources(job_id: str) -> list[dict[str, Any]]:
    """Get resolved datasources for a job.

    Returns one datasource per type, with job-specific taking precedence
    over global datasources.
    """
    try:
        return await postgres_db.resolve_datasources_for_job(job_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/api/datasources/{datasource_id}/test")
async def test_datasource(datasource_id: str) -> dict[str, Any]:
    """Test connectivity to a datasource.

    Attempts to connect using the stored connection details and returns
    the result. Does not modify any data.
    """
    try:
        ds = await postgres_db.get_datasource(datasource_id)
        if not ds:
            raise HTTPException(status_code=404, detail=f"Datasource '{datasource_id}' not found")

        ds_type = ds["type"]
        url = ds["connection_url"]
        creds = ds.get("credentials") or {}
        if isinstance(creds, str):
            creds = json.loads(creds)

        if ds_type == "postgresql":
            try:
                conn = await asyncpg.connect(url, timeout=10)
                version = await conn.fetchval("SELECT version()")
                await conn.close()
                return {"status": "ok", "message": f"Connected: {version[:80]}"}
            except Exception as e:
                return {"status": "error", "message": str(e)}

        elif ds_type == "neo4j":
            try:
                from neo4j import GraphDatabase
                username = creds.get("username", "neo4j")
                password = creds.get("password", "")
                driver = GraphDatabase.driver(url, auth=(username, password))
                driver.verify_connectivity()
                driver.close()
                return {"status": "ok", "message": "Connected to Neo4j"}
            except Exception as e:
                return {"status": "error", "message": str(e)}

        elif ds_type == "mongodb":
            try:
                from pymongo import MongoClient
                client = MongoClient(url, serverSelectionTimeoutMS=5000)
                client.server_info()
                client.close()
                return {"status": "ok", "message": "Connected to MongoDB"}
            except Exception as e:
                return {"status": "error", "message": str(e)}

        else:
            return {"status": "error", "message": f"Unknown datasource type: {ds_type}"}

    except HTTPException:
        raise
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


def _deep_merge(base: dict, override: dict) -> dict:
    """Deep merge two dictionaries (objects merge, arrays replace, None clears)."""
    result = base.copy()
    for key, value in override.items():
        if value is None:
            result.pop(key, None)
        elif isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _load_expert_detail(expert_id: str) -> dict[str, Any]:
    """Load full expert detail: merged config + instructions content."""
    config_dir = _get_config_dir()

    # Load defaults
    defaults_path = config_dir / "defaults.yaml"
    if defaults_path.exists():
        with open(defaults_path) as f:
            defaults = yaml.safe_load(f) or {}
    else:
        defaults = {}

    # Load expert config
    if expert_id == "defaults":
        merged = dict(defaults)
        expert_config_dir = config_dir
    else:
        expert_dir = config_dir / "experts" / expert_id
        config_path = expert_dir / "config.yaml"
        if not expert_dir.is_dir() or not config_path.exists():
            return {}
        with open(config_path) as f:
            expert_data = yaml.safe_load(f) or {}
        # Remove meta keys before merge
        expert_data.pop("$extends", None)
        merged = _deep_merge(defaults, expert_data)
        expert_config_dir = expert_dir

    # Load instructions content
    instructions_content = None
    # Check for expert-specific instructions.md first
    instr_path = expert_config_dir / "instructions.md"
    if expert_id != "defaults" and instr_path.exists():
        instructions_content = instr_path.read_text(encoding="utf-8")
    else:
        # Fall back to template referenced in config
        template_name = merged.get("workspace", {}).get("instructions_template", "instructions.md")
        template_path = config_dir / "prompts" / template_name
        if template_path.exists():
            instructions_content = template_path.read_text(encoding="utf-8")

    # Remove internal/sensitive keys from merged config
    for key in ("$extends", "connections"):
        merged.pop(key, None)

    return {
        "config": merged,
        "instructions": instructions_content,
    }


@app.get("/api/experts/{expert_id}")
async def get_expert(expert_id: str) -> dict[str, Any]:
    """Get full expert detail including merged config and instructions content.

    Returns the expert's configuration (merged with defaults) and the raw
    instructions.md content, enabling the cockpit to pre-populate the job
    creation form.
    """
    # Verify expert exists
    global _experts_cache
    if _experts_cache is None:
        _experts_cache = _scan_experts()

    expert_info = next((e for e in _experts_cache if e.id == expert_id), None)
    if not expert_info:
        raise HTTPException(status_code=404, detail=f"Expert not found: {expert_id}")

    detail = _load_expert_detail(expert_id)
    if not detail:
        raise HTTPException(status_code=404, detail=f"Expert config not found: {expert_id}")

    return {
        **expert_info.model_dump(),
        **detail,
    }


# =============================================================================
# Builder Session Endpoints
# =============================================================================


@app.post("/api/builder/sessions")
async def create_builder_session(body: BuilderSessionCreate) -> dict[str, Any]:
    """Create a new builder chat session.

    Called when the user sends their first message in the builder chat.
    The session is not linked to a job yet (that happens on job submission).
    """
    try:
        session = await postgres_db.create_builder_session(
            expert_id=body.expert_id,
        )
        return session
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/builder/sessions/{session_id}")
async def get_builder_session(session_id: str) -> dict[str, Any]:
    """Get builder session details."""
    session = await postgres_db.get_builder_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


@app.get("/api/builder/sessions/{session_id}/messages")
async def get_builder_messages(session_id: str) -> list[dict[str, Any]]:
    """Get all messages for a builder session."""
    session = await postgres_db.get_builder_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return await postgres_db.get_builder_messages(session_id)


@app.post("/api/builder/sessions/{session_id}/message")
async def send_builder_message(
    session_id: str,
    body: BuilderMessageRequest,
) -> StreamingResponse:
    """Send a message to the builder AI and stream the response via SSE.

    The request includes the current artifact state (instructions, config,
    description) which is injected into the system prompt fresh each turn.

    Returns an SSE stream with events:
    - token: streamed text chunks
    - tool_call: artifact mutations
    - done: stream complete with usage info
    - error: error information
    """
    # Verify session exists
    session = await postgres_db.get_builder_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    # Store user message
    await postgres_db.create_builder_message(
        session_id=session_id,
        role="user",
        content=body.message,
    )

    # Build context
    messages = await postgres_db.get_builder_messages(session_id)
    system_prompt = build_system_prompt(
        instructions_content=body.instructions,
        config_settings=body.config,
        description=body.description,
    )

    # Build conversation context (with potential summarization)
    context_messages, needs_summarization = build_message_context(
        messages=messages,
        summary=session.get("summary"),
    )

    async def event_stream():
        """Generate SSE events from LLM streaming response with agentic tool loop.

        Server-side tools (like web_search) are executed between LLM calls.
        Artifact mutation tools are forwarded to the frontend via SSE.
        """
        MAX_ITERATIONS = 50
        loop_messages = list(context_messages)
        final_text = ""
        final_tool_calls = []

        try:
            provider = get_builder_provider()
            model = get_builder_model()

            for iteration in range(MAX_ITERATIONS):
                yield f"event: step\ndata: {json.dumps({'type': 'thought', 'title': 'Analyzing request...' if iteration == 0 else 'Processing tool results...'})}\n\n"
                turn_text = ""
                turn_tool_calls = []  # {"name", "args", "id"} dicts
                error_occurred = False

                if provider == "anthropic":
                    async for evt_type, evt_data in _stream_anthropic(
                        system_prompt, loop_messages, model
                    ):
                        if evt_type == "token":
                            turn_text += evt_data["text"]
                            yield f"event: token\ndata: {json.dumps(evt_data)}\n\n"
                        elif evt_type == "tool_call":
                            turn_tool_calls.append(evt_data)
                            if evt_data["name"] in SERVER_SIDE_TOOLS:
                                yield f"event: tool_executing\ndata: {json.dumps({'tool': evt_data['name'], 'args': evt_data['args']})}\n\n"
                            else:
                                yield f"event: tool_call\ndata: {json.dumps({'tool': evt_data['name'], 'args': evt_data['args']})}\n\n"
                                final_tool_calls.append({"tool": evt_data["name"], "args": evt_data["args"]})
                        elif evt_type == "error":
                            yield f"event: error\ndata: {json.dumps({'message': evt_data['message']})}\n\n"
                            error_occurred = True
                else:
                    async for evt_type, evt_data in _stream_openai(
                        system_prompt, loop_messages, model
                    ):
                        if evt_type == "token":
                            turn_text += evt_data["text"]
                            yield f"event: token\ndata: {json.dumps(evt_data)}\n\n"
                        elif evt_type == "tool_call":
                            turn_tool_calls.append(evt_data)
                            if evt_data["name"] in SERVER_SIDE_TOOLS:
                                yield f"event: tool_executing\ndata: {json.dumps({'tool': evt_data['name'], 'args': evt_data['args']})}\n\n"
                            else:
                                yield f"event: tool_call\ndata: {json.dumps({'tool': evt_data['name'], 'args': evt_data['args']})}\n\n"
                                final_tool_calls.append({"tool": evt_data["name"], "args": evt_data["args"]})
                        elif evt_type == "error":
                            yield f"event: error\ndata: {json.dumps({'message': evt_data['message']})}\n\n"
                            error_occurred = True

                final_text += turn_text

                if error_occurred:
                    break

                # If no tool calls at all, the LLM is done (pure text response)
                if not turn_tool_calls:
                    break

                # Build assistant message and tool results for next iteration
                if provider == "anthropic":
                    # Anthropic format: assistant content blocks + user tool_result blocks
                    assistant_content = []
                    if turn_text:
                        assistant_content.append({"type": "text", "text": turn_text})
                    for tc in turn_tool_calls:
                        assistant_content.append({
                            "type": "tool_use",
                            "id": tc["id"],
                            "name": tc["name"],
                            "input": tc["args"],
                        })
                    loop_messages.append({"role": "assistant", "content": assistant_content})

                    # Execute server-side tools and build tool_result blocks
                    tool_results = []
                    for tc in turn_tool_calls:
                        if tc["name"] in SERVER_SIDE_TOOLS:
                            result = await _execute_server_tool(tc["name"], tc["args"])
                            yield f"event: tool_result\ndata: {json.dumps({'tool': tc['name'], 'summary': result[:200]})}\n\n"
                        else:
                            result = "OK"
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": tc["id"],
                            "content": result,
                        })
                    loop_messages.append({"role": "user", "content": tool_results})
                else:
                    # OpenAI format: assistant message with tool_calls + tool role messages
                    openai_tool_calls = []
                    for tc in turn_tool_calls:
                        openai_tool_calls.append({
                            "id": tc["id"],
                            "type": "function",
                            "function": {"name": tc["name"], "arguments": json.dumps(tc["args"])},
                        })
                    assistant_msg: dict[str, Any] = {"role": "assistant", "tool_calls": openai_tool_calls}
                    if turn_text:
                        assistant_msg["content"] = turn_text
                    loop_messages.append(assistant_msg)

                    # Execute server-side tools and append tool results
                    for tc in turn_tool_calls:
                        if tc["name"] in SERVER_SIDE_TOOLS:
                            result = await _execute_server_tool(tc["name"], tc["args"])
                            yield f"event: tool_result\ndata: {json.dumps({'tool': tc['name'], 'summary': result[:200]})}\n\n"
                        else:
                            result = "OK"
                        loop_messages.append({
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "content": result,
                        })

            # Store assistant message
            await postgres_db.create_builder_message(
                session_id=session_id,
                role="assistant",
                content=final_text if final_text else None,
                tool_calls=final_tool_calls if final_tool_calls else None,
            )

            # Handle auto-summarization if needed
            if needs_summarization:
                try:
                    await _summarize_builder_session(session_id, messages)
                except Exception as e:
                    logger.warning(f"Builder auto-summarization failed: {e}")

            # Send done event
            yield f"event: done\ndata: {json.dumps({'usage': {}})}\n\n"

        except Exception as e:
            logger.error(f"Builder stream error: {e}", exc_info=True)
            yield f"event: error\ndata: {json.dumps({'message': str(e)})}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def _execute_server_tool(tool_name: str, args: dict) -> str:
    """Execute a server-side builder tool and return the result string."""
    if tool_name == "web_search":
        return await tavily_search(
            query=args.get("query", ""),
            max_results=args.get("max_results", 5),
        )
    return f"Error: Unknown server tool: {tool_name}"


async def _stream_openai(
    system_prompt: str,
    context_messages: list[dict],
    model: str,
):
    """Stream from OpenAI-compatible API, yielding structured events.

    Yields tuples of (event_type, event_data):
    - ("token", {"text": str})
    - ("tool_call", {"name": str, "args": dict, "id": str})
    - ("error", {"message": str})
    """
    try:
        from openai import AsyncOpenAI
    except ImportError:
        yield ("error", {"message": "openai package not installed"})
        return

    client = AsyncOpenAI(
        api_key=get_builder_api_key(),
        base_url=get_builder_base_url(),
    )

    llm_messages = [{"role": "system", "content": system_prompt}]
    llm_messages.extend(context_messages)

    try:
        stream = await client.chat.completions.create(
            model=model,
            messages=llm_messages,
            tools=BUILDER_TOOLS,
            stream=True,
        )

        # Track tool call assembly across chunks
        tool_call_buffers: dict[int, dict] = {}

        async for chunk in stream:
            delta = chunk.choices[0].delta if chunk.choices else None
            if not delta:
                continue

            # Text content
            if delta.content:
                yield ("token", {"text": delta.content})

            # Tool calls (streamed incrementally)
            if delta.tool_calls:
                for tc in delta.tool_calls:
                    idx = tc.index
                    if idx not in tool_call_buffers:
                        tool_call_buffers[idx] = {"id": "", "name": "", "arguments": ""}
                    if tc.id:
                        tool_call_buffers[idx]["id"] = tc.id
                    if tc.function and tc.function.name:
                        tool_call_buffers[idx]["name"] = tc.function.name
                    if tc.function and tc.function.arguments:
                        tool_call_buffers[idx]["arguments"] += tc.function.arguments

        # Emit completed tool calls
        for _idx, tc_buf in sorted(tool_call_buffers.items()):
            try:
                args = json.loads(tc_buf["arguments"])
                yield ("tool_call", {"name": tc_buf["name"], "args": args, "id": tc_buf["id"]})
            except json.JSONDecodeError:
                logger.warning(f"Failed to parse tool call args: {tc_buf['arguments'][:100]}")

    except Exception as e:
        yield ("error", {"message": str(e)})


async def _stream_anthropic(
    system_prompt: str,
    context_messages: list[dict],
    model: str,
):
    """Stream from Anthropic API, yielding structured events.

    Yields tuples of (event_type, event_data):
    - ("token", {"text": str})
    - ("tool_call", {"name": str, "args": dict, "id": str})
    - ("error", {"message": str})
    """
    try:
        from anthropic import AsyncAnthropic
    except ImportError:
        yield ("error", {"message": "anthropic package not installed"})
        return

    client = AsyncAnthropic(api_key=get_builder_api_key())

    # Convert OpenAI tool format to Anthropic format
    anthropic_tools = []
    for tool in BUILDER_TOOLS:
        func = tool["function"]
        anthropic_tools.append({
            "name": func["name"],
            "description": func["description"],
            "input_schema": func["parameters"],
        })

    # Separate system messages from conversation messages
    filtered_messages = [m for m in context_messages if m.get("role") != "system"]
    extra_system = "\n".join(
        m["content"] for m in context_messages
        if m.get("role") == "system" and isinstance(m.get("content"), str)
    )
    full_system = system_prompt
    if extra_system:
        full_system += "\n\n" + extra_system

    try:
        async with client.messages.stream(
            model=model,
            system=full_system,
            messages=filtered_messages,
            tools=anthropic_tools,
            max_tokens=4096,
        ) as stream:
            current_tool_id = ""
            current_tool_name = ""
            current_tool_args = ""

            async for event in stream:
                if event.type == "content_block_start":
                    if hasattr(event.content_block, "type"):
                        if event.content_block.type == "tool_use":
                            current_tool_id = event.content_block.id
                            current_tool_name = event.content_block.name
                            current_tool_args = ""
                elif event.type == "content_block_delta":
                    if hasattr(event.delta, "text"):
                        yield ("token", {"text": event.delta.text})
                    elif hasattr(event.delta, "partial_json"):
                        current_tool_args += event.delta.partial_json
                elif event.type == "content_block_stop":
                    if current_tool_name:
                        try:
                            args = json.loads(current_tool_args) if current_tool_args else {}
                            yield ("tool_call", {"name": current_tool_name, "args": args, "id": current_tool_id})
                        except json.JSONDecodeError:
                            logger.warning(f"Failed to parse Anthropic tool args: {current_tool_args[:100]}")
                        current_tool_id = ""
                        current_tool_name = ""
                        current_tool_args = ""

    except Exception as e:
        yield ("error", {"message": str(e)})


async def _summarize_builder_session(
    session_id: str,
    messages: list[dict[str, Any]],
) -> None:
    """Summarize older builder messages to compress context.

    Uses the same builder model for summarization.
    """
    from services.builder_tools import build_summarization_prompt

    # Only summarize if we have enough messages
    if len(messages) < 6:
        return

    # Summarize all but the last 4 messages
    to_summarize = messages[:-4]
    summary_prompt = build_summarization_prompt(to_summarize)

    provider = get_builder_provider()
    model = get_builder_model()

    summary_text = ""

    if provider == "anthropic":
        try:
            from anthropic import AsyncAnthropic
            client = AsyncAnthropic(api_key=get_builder_api_key())
            response = await client.messages.create(
                model=model,
                system=summary_prompt[0]["content"],
                messages=[{"role": "user", "content": summary_prompt[1]["content"]}],
                max_tokens=1024,
            )
            summary_text = response.content[0].text
        except Exception as e:
            logger.warning(f"Anthropic summarization failed: {e}")
            return
    else:
        try:
            from openai import AsyncOpenAI
            client = AsyncOpenAI(
                api_key=get_builder_api_key(),
                base_url=get_builder_base_url(),
            )
            response = await client.chat.completions.create(
                model=model,
                messages=summary_prompt,
                max_tokens=1024,
            )
            summary_text = response.choices[0].message.content or ""
        except Exception as e:
            logger.warning(f"OpenAI summarization failed: {e}")
            return

    if summary_text:
        await postgres_db.update_builder_session_summary(session_id, summary_text)
