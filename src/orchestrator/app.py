"""FastAPI application for the Orchestrator.

This module provides HTTP endpoints for job management, status monitoring,
and agent coordination.

Endpoints:
- GET /health - Basic health check
- GET /ready - Readiness probe (checks database connection)
- GET /status - Orchestrator status and agent health
- POST /jobs - Create a new job
- GET /jobs - List jobs
- GET /jobs/{job_id} - Get job status
- GET /jobs/{job_id}/report - Get job report
- POST /jobs/{job_id}/cancel - Cancel a job
- GET /agents - Get agent health status
"""

import os
import uuid
import logging
from datetime import datetime
from contextlib import asynccontextmanager
from typing import Optional, List

from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from src.core.postgres_utils import create_postgres_connection, PostgresConnection
from src.orchestrator.job_manager import JobManager, create_job_manager
from src.orchestrator.monitor import Monitor, create_monitor, JobCompletionStatus
from src.orchestrator.reporter import Reporter, create_reporter

logger = logging.getLogger(__name__)

# Global state
_postgres_conn: Optional[PostgresConnection] = None
_job_manager: Optional[JobManager] = None
_monitor: Optional[Monitor] = None
_reporter: Optional[Reporter] = None
_start_time: datetime = datetime.utcnow()


# Pydantic models
class HealthResponse(BaseModel):
    status: str
    component: str = "orchestrator"
    uptime_seconds: float


class ReadyResponse(BaseModel):
    ready: bool
    database: str
    message: Optional[str] = None


class JobCreateRequest(BaseModel):
    prompt: str
    context: Optional[dict] = None


class JobCreateResponse(BaseModel):
    job_id: str
    status: str
    message: str


class JobStatusResponse(BaseModel):
    job_id: str
    status: str
    creator_status: str
    validator_status: str
    progress_percent: float
    requirement_counts: dict
    created_at: str
    error_message: Optional[str] = None


class JobListResponse(BaseModel):
    jobs: List[dict]
    total: int
    offset: int
    limit: int


class AgentHealthResponse(BaseModel):
    creator: dict
    validator: dict


class CancelResponse(BaseModel):
    success: bool
    message: str


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler - startup and shutdown."""
    global _postgres_conn, _job_manager, _monitor, _reporter, _start_time

    logger.info("Orchestrator application starting...")
    _start_time = datetime.utcnow()

    # Initialize PostgreSQL connection
    _postgres_conn = create_postgres_connection()
    await _postgres_conn.connect()
    logger.info("PostgreSQL connection established")

    # Initialize components
    _job_manager = create_job_manager(_postgres_conn)
    _monitor = create_monitor(_postgres_conn)
    _reporter = create_reporter(_postgres_conn)
    logger.info("Orchestrator components initialized")

    yield

    # Shutdown
    logger.info("Orchestrator application shutting down...")

    if _postgres_conn:
        await _postgres_conn.disconnect()

    logger.info("Orchestrator application stopped")


# Create FastAPI app
app = FastAPI(
    title="Graph-RAG Orchestrator",
    description="Orchestrator API for job management and agent coordination",
    version="1.0.0",
    lifespan=lifespan,
)


# Health endpoints
@app.get("/health", response_model=HealthResponse)
async def health():
    """Basic health check endpoint."""
    uptime = (datetime.utcnow() - _start_time).total_seconds()
    return HealthResponse(
        status="healthy",
        uptime_seconds=uptime,
    )


@app.get("/ready", response_model=ReadyResponse)
async def ready():
    """Readiness check endpoint."""
    try:
        if _postgres_conn and _postgres_conn.is_connected:
            await _postgres_conn.fetchrow("SELECT 1")
            return ReadyResponse(ready=True, database="connected")
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


@app.get("/status")
async def status():
    """Orchestrator status with overview."""
    uptime = (datetime.utcnow() - _start_time).total_seconds()

    # Get job counts
    jobs = await _job_manager.list_jobs(limit=1000)
    status_counts = {}
    for job in jobs:
        s = job.get("status", "unknown")
        status_counts[s] = status_counts.get(s, 0) + 1

    # Check for stuck jobs
    stuck_jobs = await _monitor.detect_stuck_jobs() if _monitor else []

    return {
        "component": "orchestrator",
        "status": "healthy",
        "uptime_seconds": uptime,
        "jobs": {
            "total": len(jobs),
            "by_status": status_counts,
            "stuck": len(stuck_jobs),
        },
    }


# Job management endpoints
@app.post("/jobs", response_model=JobCreateResponse)
async def create_job(
    prompt: str = Form(...),
    context: Optional[str] = Form(None),
    document: Optional[UploadFile] = File(None),
):
    """Create a new job.

    Args:
        prompt: User prompt describing the task
        context: Optional JSON context string
        document: Optional document file
    """
    if not _job_manager:
        raise HTTPException(status_code=503, detail="Job manager not initialized")

    # Parse context
    import json
    ctx = None
    if context:
        try:
            ctx = json.loads(context)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid context JSON")

    # Handle document upload
    doc_path = None
    if document:
        # Save to temp location
        import tempfile
        import shutil

        suffix = os.path.splitext(document.filename)[1] if document.filename else ".pdf"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            shutil.copyfileobj(document.file, tmp)
            doc_path = tmp.name

    try:
        job_id = await _job_manager.create_new_job(
            prompt=prompt,
            document_path=doc_path,
            context=ctx,
        )

        return JobCreateResponse(
            job_id=str(job_id),
            status="created",
            message="Job created successfully",
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except FileNotFoundError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/jobs", response_model=JobListResponse)
async def list_jobs(
    status: Optional[str] = Query(None, description="Filter by status"),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    """List jobs with optional filtering."""
    if not _job_manager:
        raise HTTPException(status_code=503, detail="Job manager not initialized")

    jobs = await _job_manager.list_jobs(status=status, limit=limit, offset=offset)

    # Convert datetimes to strings
    for job in jobs:
        for key, value in job.items():
            if isinstance(value, datetime):
                job[key] = value.isoformat()

    return JobListResponse(
        jobs=jobs,
        total=len(jobs),
        offset=offset,
        limit=limit,
    )


@app.get("/jobs/{job_id}", response_model=JobStatusResponse)
async def get_job_status(job_id: str):
    """Get status for a specific job."""
    if not _job_manager:
        raise HTTPException(status_code=503, detail="Job manager not initialized")

    try:
        uid = uuid.UUID(job_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid job ID format")

    status = await _job_manager.get_job_status(uid)
    if not status:
        raise HTTPException(status_code=404, detail="Job not found")

    return JobStatusResponse(
        job_id=str(status["id"]),
        status=status["status"],
        creator_status=status["creator_status"],
        validator_status=status["validator_status"],
        progress_percent=status.get("progress_percent", 0),
        requirement_counts=status.get("requirement_counts", {}),
        created_at=status["created_at"].isoformat()
            if isinstance(status["created_at"], datetime) else str(status["created_at"]),
        error_message=status.get("error_message"),
    )


@app.get("/jobs/{job_id}/progress")
async def get_job_progress(job_id: str):
    """Get detailed progress for a job."""
    if not _monitor:
        raise HTTPException(status_code=503, detail="Monitor not initialized")

    try:
        uid = uuid.UUID(job_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid job ID format")

    progress = await _monitor.get_job_progress(uid)
    if "error" in progress:
        raise HTTPException(status_code=404, detail=progress["error"])

    return progress


@app.get("/jobs/{job_id}/report", response_class=PlainTextResponse)
async def get_job_report(job_id: str, format: str = Query("text", enum=["text", "json"])):
    """Get report for a completed job."""
    if not _reporter:
        raise HTTPException(status_code=503, detail="Reporter not initialized")

    try:
        uid = uuid.UUID(job_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid job ID format")

    if format == "json":
        report = await _reporter.generate_json_report(uid)
        if not report:
            raise HTTPException(status_code=404, detail="Job not found")
        import json
        return PlainTextResponse(
            json.dumps(report, indent=2, default=str),
            media_type="application/json",
        )
    else:
        report = await _reporter.generate_text_report(uid)
        if not report:
            raise HTTPException(status_code=404, detail="Job not found")
        return PlainTextResponse(report)


@app.post("/jobs/{job_id}/cancel", response_model=CancelResponse)
async def cancel_job(job_id: str):
    """Cancel a job."""
    if not _job_manager:
        raise HTTPException(status_code=503, detail="Job manager not initialized")

    try:
        uid = uuid.UUID(job_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid job ID format")

    success = await _job_manager.cancel_job(uid)

    if success:
        return CancelResponse(success=True, message="Job cancelled")
    else:
        return CancelResponse(success=False, message="Job not found or already completed")


# Agent management endpoints
@app.get("/agents", response_model=AgentHealthResponse)
async def get_agent_health():
    """Get health status of all agents."""
    if not _monitor:
        raise HTTPException(status_code=503, detail="Monitor not initialized")

    creator_url = os.getenv("CREATOR_AGENT_URL", "http://creator:8001")
    validator_url = os.getenv("VALIDATOR_AGENT_URL", "http://validator:8002")

    creator_health = await _monitor.check_agent_health("creator", f"{creator_url}/health")
    validator_health = await _monitor.check_agent_health("validator", f"{validator_url}/health")

    return AgentHealthResponse(
        creator={
            "healthy": creator_health.healthy,
            "message": creator_health.message,
            "current_job_id": str(creator_health.current_job_id) if creator_health.current_job_id else None,
        },
        validator={
            "healthy": validator_health.healthy,
            "message": validator_health.message,
            "current_job_id": str(validator_health.current_job_id) if validator_health.current_job_id else None,
        },
    )


@app.get("/agents/stuck")
async def get_stuck_jobs():
    """Get list of stuck jobs."""
    if not _monitor:
        raise HTTPException(status_code=503, detail="Monitor not initialized")

    stuck_jobs = await _monitor.detect_stuck_jobs()

    return {
        "stuck_jobs": [
            {
                "job_id": str(job.job_id),
                "stuck_component": job.stuck_component,
                "stuck_since": job.stuck_since.isoformat(),
                "pending_requirements": job.pending_requirements,
                "reason": job.reason,
            }
            for job in stuck_jobs
        ],
        "total": len(stuck_jobs),
    }


@app.get("/metrics")
async def metrics():
    """Prometheus-compatible metrics endpoint."""
    uptime = (datetime.utcnow() - _start_time).total_seconds()

    # Get job counts
    jobs = await _job_manager.list_jobs(limit=1000) if _job_manager else []
    status_counts = {}
    for job in jobs:
        s = job.get("status", "unknown")
        status_counts[s] = status_counts.get(s, 0) + 1

    lines = [
        "# HELP orchestrator_uptime_seconds Orchestrator uptime in seconds",
        "# TYPE orchestrator_uptime_seconds gauge",
        f"orchestrator_uptime_seconds {uptime:.2f}",
        "",
        "# HELP orchestrator_jobs_total Total jobs by status",
        "# TYPE orchestrator_jobs_total gauge",
    ]

    for s, count in status_counts.items():
        lines.append(f'orchestrator_jobs_total{{status="{s}"}} {count}')

    lines.extend([
        "",
        "# HELP orchestrator_ready Orchestrator readiness status",
        "# TYPE orchestrator_ready gauge",
        f"orchestrator_ready {1 if _postgres_conn and _postgres_conn.is_connected else 0}",
    ])

    return PlainTextResponse("\n".join(lines))


@app.get("/stats")
async def get_statistics(days: int = Query(7, ge=1, le=30)):
    """Get daily statistics."""
    if not _reporter:
        raise HTTPException(status_code=503, detail="Reporter not initialized")

    stats = await _reporter.get_daily_statistics(days)
    return {"days": days, "statistics": stats}


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("ORCHESTRATOR_PORT", "8000"))
    uvicorn.run(
        "src.orchestrator.app:app",
        host="0.0.0.0",
        port=port,
        log_level="info",
    )
