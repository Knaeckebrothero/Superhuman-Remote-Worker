"""FastAPI application for the Creator Agent.

This module provides HTTP endpoints for health checking, status monitoring,
job submission, and graceful shutdown of the Creator Agent container.

Endpoints:
- GET /health - Basic health check
- GET /ready - Readiness probe (checks database connection)
- GET /status - Detailed agent status
- POST /shutdown - Request graceful shutdown
- POST /jobs - Submit a new job for processing
- GET /jobs/{job_id} - Get job status
- GET /jobs/{job_id}/requirements - Get requirements created by job
"""

import os
import asyncio
import base64
import logging
import tempfile
import uuid
from datetime import datetime
from contextlib import asynccontextmanager
from typing import Optional, Dict, Any, List

from fastapi import FastAPI, HTTPException, BackgroundTasks
from pydantic import BaseModel

from src.core.postgres_utils import (
    create_postgres_connection,
    PostgresConnection,
    create_job,
    get_job,
    update_job_status,
    count_requirements_by_status,
)
from src.agents.creator.creator_agent import CreatorAgent, create_creator_agent
from src.agents.creator.models import (
    JobSubmitRequest,
    JobSubmitResponse,
    JobStatusResponse,
    RequirementSummary,
    JobRequirementsResponse,
)

logger = logging.getLogger(__name__)

# Global state
_agent: Optional[CreatorAgent] = None
_postgres_conn: Optional[PostgresConnection] = None
_start_time: datetime = datetime.utcnow()
_shutdown_requested: bool = False
_agent_task: Optional[asyncio.Task] = None

# Metrics
_jobs_processed: int = 0
_requirements_created: int = 0
_last_activity: Optional[datetime] = None

# Manual job tracking - tracks jobs submitted via API
_manual_jobs: Dict[str, Dict[str, Any]] = {}  # job_id -> {status, phase, iteration, ...}


class HealthResponse(BaseModel):
    """Health check response."""
    status: str
    agent: str = "creator"
    uptime_seconds: float


class ReadyResponse(BaseModel):
    """Readiness check response."""
    ready: bool
    database: str
    message: Optional[str] = None


class StatusResponse(BaseModel):
    """Detailed status response."""
    agent: str = "creator"
    state: str
    current_job_id: Optional[str]
    last_activity: Optional[str]
    shutdown_requested: bool
    metrics: dict


class ShutdownResponse(BaseModel):
    """Shutdown response."""
    status: str
    message: str


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler - startup and shutdown."""
    global _agent, _postgres_conn, _agent_task, _start_time

    logger.info("Creator Agent application starting...")
    _start_time = datetime.utcnow()

    # Initialize PostgreSQL connection
    _postgres_conn = create_postgres_connection()
    await _postgres_conn.connect()
    logger.info("PostgreSQL connection established")

    # Create agent
    _agent = create_creator_agent(postgres_conn=_postgres_conn)
    logger.info("Creator Agent initialized")

    # Start agent polling in background
    _agent_task = asyncio.create_task(_run_agent_loop())
    logger.info("Creator Agent polling loop started")

    yield

    # Shutdown
    logger.info("Creator Agent application shutting down...")

    global _shutdown_requested
    _shutdown_requested = True

    # Cancel agent task
    if _agent_task and not _agent_task.done():
        _agent_task.cancel()
        try:
            await _agent_task
        except asyncio.CancelledError:
            pass

    # Shutdown agent
    if _agent:
        await _agent.shutdown()

    # Close database connection
    if _postgres_conn:
        await _postgres_conn.disconnect()

    logger.info("Creator Agent application stopped")


async def _run_agent_loop():
    """Run the agent polling loop."""
    global _jobs_processed, _requirements_created, _last_activity

    while not _shutdown_requested:
        try:
            if _agent:
                # Process one job at a time
                job = await _agent._poll_for_job()
                if job:
                    _last_activity = datetime.utcnow()
                    job_id = str(job["id"])
                    logger.info(f"Processing job {job_id}")

                    result = await _agent.process_job(job_id)

                    _jobs_processed += 1
                    _requirements_created += len(result.get("requirements_created", []))
                    _last_activity = datetime.utcnow()
                else:
                    # No jobs, wait before polling again
                    await asyncio.sleep(_agent.config.get("polling_interval_seconds", 30))

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error in agent loop: {e}")
            await asyncio.sleep(10)  # Wait before retrying


# Create FastAPI app
app = FastAPI(
    title="Creator Agent",
    description="Graph-RAG Creator Agent for document processing and requirement extraction",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health", response_model=HealthResponse)
async def health():
    """Basic health check endpoint.

    Returns 200 if the application is running.
    Used by container orchestrators for liveness probes.
    """
    uptime = (datetime.utcnow() - _start_time).total_seconds()
    return HealthResponse(
        status="healthy",
        uptime_seconds=uptime,
    )


@app.get("/ready", response_model=ReadyResponse)
async def ready():
    """Readiness check endpoint.

    Returns 200 if the agent is ready to process jobs.
    Checks database connectivity.
    """
    if _shutdown_requested:
        return ReadyResponse(
            ready=False,
            database="disconnected",
            message="Shutdown in progress",
        )

    # Check database connection
    try:
        if _postgres_conn and _postgres_conn.is_connected:
            await _postgres_conn.fetchrow("SELECT 1")
            return ReadyResponse(
                ready=True,
                database="connected",
            )
        else:
            return ReadyResponse(
                ready=False,
                database="disconnected",
                message="Database connection not established",
            )
    except Exception as e:
        return ReadyResponse(
            ready=False,
            database="error",
            message=str(e),
        )


@app.get("/status", response_model=StatusResponse)
async def status():
    """Detailed agent status endpoint.

    Returns current agent state, metrics, and configuration.
    """
    uptime = (datetime.utcnow() - _start_time).total_seconds()
    uptime_hours = uptime / 3600

    # Determine state
    if _shutdown_requested:
        state = "shutting_down"
    elif _agent and _agent.current_job_id:
        state = "processing"
    else:
        state = "idle"

    return StatusResponse(
        state=state,
        current_job_id=_agent.current_job_id if _agent else None,
        last_activity=_last_activity.isoformat() if _last_activity else None,
        shutdown_requested=_shutdown_requested,
        metrics={
            "jobs_processed": _jobs_processed,
            "requirements_created": _requirements_created,
            "uptime_hours": round(uptime_hours, 2),
        },
    )


@app.post("/shutdown", response_model=ShutdownResponse)
async def shutdown():
    """Request graceful shutdown of the agent.

    The agent will complete any current job before shutting down.
    """
    global _shutdown_requested

    if _shutdown_requested:
        return ShutdownResponse(
            status="already_requested",
            message="Shutdown already in progress",
        )

    _shutdown_requested = True
    logger.info("Shutdown requested via API")

    return ShutdownResponse(
        status="shutdown_initiated",
        message="Agent will shutdown after completing current job",
    )


@app.get("/metrics")
async def metrics():
    """Prometheus-compatible metrics endpoint."""
    uptime = (datetime.utcnow() - _start_time).total_seconds()

    lines = [
        "# HELP creator_agent_uptime_seconds Agent uptime in seconds",
        "# TYPE creator_agent_uptime_seconds gauge",
        f"creator_agent_uptime_seconds {uptime:.2f}",
        "",
        "# HELP creator_agent_jobs_processed_total Total jobs processed",
        "# TYPE creator_agent_jobs_processed_total counter",
        f"creator_agent_jobs_processed_total {_jobs_processed}",
        "",
        "# HELP creator_agent_requirements_created_total Total requirements created",
        "# TYPE creator_agent_requirements_created_total counter",
        f"creator_agent_requirements_created_total {_requirements_created}",
        "",
        "# HELP creator_agent_ready Agent readiness status",
        "# TYPE creator_agent_ready gauge",
        f"creator_agent_ready {1 if not _shutdown_requested else 0}",
    ]

    return "\n".join(lines)


# =============================================================================
# Job Submission and Status Endpoints
# =============================================================================


async def _process_manual_job(job_id: str, max_iterations: int):
    """Background task to process a manually submitted job."""
    global _jobs_processed, _requirements_created, _last_activity

    try:
        _manual_jobs[job_id]["status"] = "processing"
        _manual_jobs[job_id]["started_at"] = datetime.utcnow()
        _last_activity = datetime.utcnow()

        if _agent:
            result = await _agent.process_job(job_id, max_iterations=max_iterations)

            _manual_jobs[job_id]["status"] = "completed"
            _manual_jobs[job_id]["completed_at"] = datetime.utcnow()
            _manual_jobs[job_id]["requirements_created"] = len(
                result.get("requirements_created", [])
            )
            _manual_jobs[job_id]["result"] = result

            _jobs_processed += 1
            _requirements_created += len(result.get("requirements_created", []))
            _last_activity = datetime.utcnow()

            logger.info(f"Manual job {job_id} completed with {_manual_jobs[job_id]['requirements_created']} requirements")
        else:
            _manual_jobs[job_id]["status"] = "failed"
            _manual_jobs[job_id]["error"] = {"message": "Agent not initialized"}

    except Exception as e:
        logger.error(f"Error processing manual job {job_id}: {e}")
        _manual_jobs[job_id]["status"] = "failed"
        _manual_jobs[job_id]["error"] = {"message": str(e)}
        _manual_jobs[job_id]["completed_at"] = datetime.utcnow()


@app.post("/jobs", response_model=JobSubmitResponse)
async def submit_job(request: JobSubmitRequest, background_tasks: BackgroundTasks):
    """Submit a new job for processing.

    Creates a job in the database and starts processing in the background.
    Use GET /jobs/{job_id} to poll for status.
    """
    if not _postgres_conn or not _postgres_conn.is_connected:
        raise HTTPException(status_code=503, detail="Database not connected")

    if _shutdown_requested:
        raise HTTPException(status_code=503, detail="Agent is shutting down")

    try:
        # Handle document content if provided
        document_path = None
        if request.document_content and request.document_filename:
            # Decode base64 and save to temp file
            document_bytes = base64.b64decode(request.document_content)
            suffix = os.path.splitext(request.document_filename)[1] or ".pdf"
            with tempfile.NamedTemporaryFile(
                delete=False, suffix=suffix, dir="/tmp"
            ) as f:
                f.write(document_bytes)
                document_path = f.name
            logger.info(f"Saved uploaded document to {document_path}")

        # Create job in database
        job_uuid = await create_job(
            conn=_postgres_conn,
            prompt=request.prompt,
            document_path=document_path,
            context=request.context,
        )
        job_id = str(job_uuid)

        # Track the manual job
        _manual_jobs[job_id] = {
            "status": "created",
            "phase": None,
            "iteration": 0,
            "max_iterations": request.max_iterations,
            "requirements_created": 0,
            "started_at": None,
            "completed_at": None,
            "error": None,
            "result": None,
        }

        # Start processing in background
        background_tasks.add_task(_process_manual_job, job_id, request.max_iterations)

        logger.info(f"Manual job {job_id} submitted for processing")

        return JobSubmitResponse(
            job_id=job_id,
            status="created",
            message="Job submitted for processing. Poll GET /jobs/{job_id} for status.",
        )

    except Exception as e:
        logger.error(f"Error submitting job: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/jobs/{job_id}", response_model=JobStatusResponse)
async def get_job_status(job_id: str):
    """Get the current status of a job.

    Returns processing phase, iteration count, and completion status.
    """
    if not _postgres_conn or not _postgres_conn.is_connected:
        raise HTTPException(status_code=503, detail="Database not connected")

    try:
        job_uuid = uuid.UUID(job_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid job ID format")

    # Get job from database
    job = await get_job(_postgres_conn, job_uuid)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    # Get requirement counts
    req_counts = await count_requirements_by_status(_postgres_conn, job_uuid)
    total_requirements = sum(req_counts.values())

    # Get manual job tracking info if available
    manual_info = _manual_jobs.get(job_id, {})

    # Determine current phase from agent if processing
    current_phase = manual_info.get("phase")
    if _agent and _agent.current_job_id == job_id:
        current_phase = getattr(_agent, "_current_phase", None)

    # Calculate progress
    iteration = manual_info.get("iteration", 0)
    max_iterations = manual_info.get("max_iterations", 50)
    progress = min((iteration / max_iterations) * 100, 100) if max_iterations > 0 else 0

    # Map database status to response status
    db_status = job.get("status", "created")
    creator_status = job.get("creator_status", "pending")

    return JobStatusResponse(
        job_id=job_id,
        status=db_status,
        creator_status=creator_status,
        current_phase=current_phase,
        iteration=iteration,
        max_iterations=max_iterations,
        requirements_created=total_requirements,
        progress_percent=progress,
        started_at=manual_info.get("started_at") or job.get("created_at"),
        completed_at=manual_info.get("completed_at") or job.get("completed_at"),
        error=manual_info.get("error"),
    )


@app.get("/jobs/{job_id}/requirements", response_model=JobRequirementsResponse)
async def get_job_requirements(job_id: str):
    """Get all requirements created by a job."""
    if not _postgres_conn or not _postgres_conn.is_connected:
        raise HTTPException(status_code=503, detail="Database not connected")

    try:
        job_uuid = uuid.UUID(job_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid job ID format")

    # Verify job exists
    job = await get_job(_postgres_conn, job_uuid)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    # Query requirements for this job
    rows = await _postgres_conn.fetch(
        """
        SELECT
            id, name, text, type, priority, confidence,
            gobd_relevant, gdpr_relevant,
            mentioned_objects, mentioned_messages,
            created_at
        FROM requirement_cache
        WHERE job_id = $1
        ORDER BY created_at ASC
        """,
        job_uuid,
    )

    requirements = [
        RequirementSummary(
            id=str(row["id"]),
            name=row.get("name"),
            text=row["text"],
            type=row.get("type"),
            priority=row.get("priority"),
            confidence=row.get("confidence", 0.0),
            gobd_relevant=row.get("gobd_relevant", False),
            gdpr_relevant=row.get("gdpr_relevant", False),
            mentioned_objects=row.get("mentioned_objects") or [],
            mentioned_messages=row.get("mentioned_messages") or [],
            created_at=row.get("created_at"),
        )
        for row in rows
    ]

    return JobRequirementsResponse(
        job_id=job_id,
        total=len(requirements),
        requirements=requirements,
    )


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("CREATOR_PORT", "8001"))
    uvicorn.run(
        "src.agents.creator.app:app",
        host="0.0.0.0",
        port=port,
        log_level="info",
    )
