"""FastAPI application for the Creator Agent.

This module provides HTTP endpoints for health checking, status monitoring,
and graceful shutdown of the Creator Agent container.

Endpoints:
- GET /health - Basic health check
- GET /ready - Readiness probe (checks database connection)
- GET /status - Detailed agent status
- POST /shutdown - Request graceful shutdown
"""

import os
import asyncio
import logging
from datetime import datetime
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from src.core.postgres_utils import create_postgres_connection, PostgresConnection
from src.agents.creator.creator_agent import CreatorAgent, create_creator_agent

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


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("CREATOR_PORT", "8001"))
    uvicorn.run(
        "src.agents.creator.app:app",
        host="0.0.0.0",
        port=port,
        log_level="info",
    )
