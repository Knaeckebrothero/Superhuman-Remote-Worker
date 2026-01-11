"""FastAPI application for Universal Agent.

Provides HTTP endpoints for job submission, status queries,
health checks, and agent management.
"""

import asyncio
import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException, Query, BackgroundTasks
from fastapi.responses import JSONResponse

from ..agent import UniversalAgent
from ..core.loader import resolve_config_path
from .models import (
    JobSubmitRequest,
    JobSubmitResponse,
    JobStatusResponse,
    JobCancelRequest,
    JobListResponse,
    HealthResponse,
    HealthStatus,
    ReadyResponse,
    AgentStatusResponse,
    ErrorResponse,
    MetricsResponse,
    JobStatus,
)

logger = logging.getLogger(__name__)

# Global state
_agent: Optional[UniversalAgent] = None
_agent_task: Optional[asyncio.Task] = None
_shutdown_requested = False
_config_path: Optional[str] = None


def set_config_path(path: str) -> None:
    """Set the configuration path for the agent.

    Call this before creating the app to specify which config to use.
    """
    global _config_path
    _config_path = path


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager.

    Handles startup and shutdown of the agent and its components.
    """
    global _agent, _agent_task, _shutdown_requested

    # Startup
    logger.info("Starting Universal Agent application...")

    # Get config path from environment or global setting
    config_path = _config_path or os.getenv("AGENT_CONFIG", "creator")
    resolved_path = resolve_config_path(config_path)

    logger.info(f"Loading agent configuration from: {resolved_path}")

    # Create and initialize agent
    _agent = UniversalAgent.from_config(resolved_path)
    await _agent.initialize()

    # Start polling loop if enabled
    if _agent.config.polling.enabled:
        _agent_task = asyncio.create_task(_run_agent_loop())
        logger.info("Agent polling loop started")

    yield

    # Shutdown
    logger.info("Shutting down Universal Agent application...")
    _shutdown_requested = True

    if _agent_task:
        _agent_task.cancel()
        try:
            await _agent_task
        except asyncio.CancelledError:
            pass

    if _agent:
        await _agent.shutdown()

    logger.info("Universal Agent application shutdown complete")


async def _run_agent_loop():
    """Run the agent's polling loop."""
    global _shutdown_requested

    while not _shutdown_requested:
        try:
            await _agent.start_polling()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Agent loop error: {e}", exc_info=True)
            await asyncio.sleep(5)


def create_app(config_path: Optional[str] = None) -> FastAPI:
    """Create the FastAPI application.

    Args:
        config_path: Path to agent config file (or name like "creator")

    Returns:
        Configured FastAPI application
    """
    if config_path:
        set_config_path(config_path)

    app = FastAPI(
        title="Universal Agent API",
        description="REST API for the Universal Agent",
        version="1.0.0",
        lifespan=lifespan,
    )

    # Health endpoints

    @app.get("/health", response_model=HealthResponse, tags=["Health"])
    async def health_check() -> HealthResponse:
        """Liveness probe - check if the service is running."""
        if _agent is None:
            return HealthResponse(
                status=HealthStatus.UNHEALTHY,
                agent_id="unknown",
                agent_name="Unknown",
                uptime_seconds=0,
                checks={"initialized": False},
            )

        status = _agent.get_status()

        # Determine health status
        health = HealthStatus.HEALTHY
        if not status["initialized"]:
            health = HealthStatus.UNHEALTHY
        elif not status["connections"]["postgres"]:
            health = HealthStatus.DEGRADED

        return HealthResponse(
            status=health,
            agent_id=status["agent_id"],
            agent_name=status["display_name"],
            uptime_seconds=status["uptime_seconds"],
            checks={
                "initialized": status["initialized"],
                "postgres": status["connections"]["postgres"],
                "neo4j": status["connections"]["neo4j"],
            },
        )

    @app.get("/ready", response_model=ReadyResponse, tags=["Health"])
    async def readiness_check() -> ReadyResponse:
        """Readiness probe - check if the service can accept requests."""
        if _agent is None:
            return ReadyResponse(
                ready=False,
                message="Agent not initialized",
                connections={},
            )

        status = _agent.get_status()

        # Ready if initialized and has required connections
        ready = (
            status["initialized"]
            and status["connections"]["postgres"]
        )

        return ReadyResponse(
            ready=ready,
            message="Ready to accept jobs" if ready else "Not ready",
            connections=status["connections"],
        )

    @app.get("/status", response_model=AgentStatusResponse, tags=["Health"])
    async def agent_status() -> AgentStatusResponse:
        """Get detailed agent status."""
        if _agent is None:
            raise HTTPException(status_code=503, detail="Agent not initialized")

        status = _agent.get_status()
        return AgentStatusResponse(**status)

    # Job management endpoints

    @app.post(
        "/jobs",
        response_model=JobSubmitResponse,
        tags=["Jobs"],
        responses={400: {"model": ErrorResponse}},
    )
    async def submit_job(
        request: JobSubmitRequest,
        background_tasks: BackgroundTasks,
    ) -> JobSubmitResponse:
        """Submit a new job for processing."""
        if _agent is None:
            raise HTTPException(status_code=503, detail="Agent not initialized")

        # Generate job ID
        job_id = str(uuid.uuid4())
        created_at = datetime.utcnow()

        # Build metadata from request
        metadata: Dict[str, Any] = {}
        if request.document_path:
            metadata["document_path"] = request.document_path
        if request.prompt:
            metadata["prompt"] = request.prompt
        if request.requirement_id:
            metadata["requirement_id"] = request.requirement_id
        if request.metadata:
            metadata.update(request.metadata)

        # Add job to processing queue
        background_tasks.add_task(
            _process_job_background,
            job_id,
            metadata,
        )

        return JobSubmitResponse(
            job_id=job_id,
            status=JobStatus.PENDING,
            created_at=created_at,
            message="Job submitted successfully",
        )

    @app.get(
        "/jobs/{job_id}",
        response_model=JobStatusResponse,
        tags=["Jobs"],
        responses={404: {"model": ErrorResponse}},
    )
    async def get_job_status(job_id: str) -> JobStatusResponse:
        """Get status of a specific job."""
        if _agent is None:
            raise HTTPException(status_code=503, detail="Agent not initialized")

        # Query job from database
        if _agent.postgres_conn:
            table = _agent.config.polling.table
            query = f"SELECT * FROM {table} WHERE id = $1::uuid"
            try:
                row = await _agent.postgres_conn.fetchrow(query, job_id)
                if row:
                    job = dict(row)
                    status_field = _agent.config.polling.status_field
                    return JobStatusResponse(
                        job_id=str(job["id"]),
                        status=JobStatus(job.get(status_field, "pending")),
                        created_at=job.get("created_at", datetime.utcnow()),
                        updated_at=job.get("updated_at"),
                        iteration=job.get("iteration", 0),
                        error=job.get("error"),
                        result=job.get("result"),
                    )
            except Exception as e:
                logger.error(f"Error querying job: {e}")

        raise HTTPException(
            status_code=404,
            detail=f"Job {job_id} not found",
        )

    @app.get("/jobs", response_model=JobListResponse, tags=["Jobs"])
    async def list_jobs(
        status: Optional[str] = Query(None, description="Filter by status"),
        page: int = Query(1, ge=1, description="Page number"),
        page_size: int = Query(20, ge=1, le=100, description="Items per page"),
    ) -> JobListResponse:
        """List jobs with optional filtering."""
        if _agent is None:
            raise HTTPException(status_code=503, detail="Agent not initialized")

        jobs = []
        total = 0

        if _agent.postgres_conn:
            table = _agent.config.polling.table
            status_field = _agent.config.polling.status_field
            offset = (page - 1) * page_size

            # Build query
            if status:
                query = f"""
                    SELECT * FROM {table}
                    WHERE {status_field} = $1
                    ORDER BY created_at DESC
                    LIMIT $2 OFFSET $3
                """
                count_query = f"""
                    SELECT COUNT(*) FROM {table}
                    WHERE {status_field} = $1
                """
                rows = await _agent.postgres_conn.fetch(
                    query, status, page_size, offset
                )
                total = await _agent.postgres_conn.fetchval(count_query, status)
            else:
                query = f"""
                    SELECT * FROM {table}
                    ORDER BY created_at DESC
                    LIMIT $1 OFFSET $2
                """
                count_query = f"SELECT COUNT(*) FROM {table}"
                rows = await _agent.postgres_conn.fetch(query, page_size, offset)
                total = await _agent.postgres_conn.fetchval(count_query)

            for row in rows:
                job = dict(row)
                jobs.append(
                    JobStatusResponse(
                        job_id=str(job["id"]),
                        status=JobStatus(job.get(status_field, "pending")),
                        created_at=job.get("created_at", datetime.utcnow()),
                        updated_at=job.get("updated_at"),
                        iteration=job.get("iteration", 0),
                    )
                )

        return JobListResponse(
            jobs=jobs,
            total=total or 0,
            page=page,
            page_size=page_size,
        )

    @app.post(
        "/jobs/{job_id}/cancel",
        response_model=JobStatusResponse,
        tags=["Jobs"],
        responses={404: {"model": ErrorResponse}},
    )
    async def cancel_job(
        job_id: str,
        request: JobCancelRequest,
    ) -> JobStatusResponse:
        """Cancel a running job."""
        if _agent is None:
            raise HTTPException(status_code=503, detail="Agent not initialized")

        # Update job status to cancelled
        if _agent.postgres_conn:
            table = _agent.config.polling.table
            status_field = _agent.config.polling.status_field

            query = f"""
                UPDATE {table}
                SET {status_field} = 'cancelled',
                    error = $1,
                    updated_at = NOW()
                WHERE id = $2::uuid
                RETURNING *
            """

            try:
                row = await _agent.postgres_conn.fetchrow(
                    query,
                    {"reason": request.reason or "User cancelled"},
                    job_id,
                )

                if row:
                    job = dict(row)
                    return JobStatusResponse(
                        job_id=str(job["id"]),
                        status=JobStatus.CANCELLED,
                        created_at=job.get("created_at", datetime.utcnow()),
                        updated_at=job.get("updated_at"),
                        error={"reason": request.reason or "User cancelled"},
                    )
            except Exception as e:
                logger.error(f"Error cancelling job: {e}")

        raise HTTPException(
            status_code=404,
            detail=f"Job {job_id} not found",
        )

    # Metrics endpoint

    @app.get("/metrics", response_model=MetricsResponse, tags=["Monitoring"])
    async def get_metrics() -> MetricsResponse:
        """Get agent metrics for monitoring."""
        if _agent is None:
            raise HTTPException(status_code=503, detail="Agent not initialized")

        status = _agent.get_status()

        # Get job statistics from database
        jobs_success = 0
        jobs_failed = 0

        if _agent.postgres_conn:
            table = _agent.config.polling.table
            status_field = _agent.config.polling.status_field

            try:
                success_count = await _agent.postgres_conn.fetchval(
                    f"SELECT COUNT(*) FROM {table} WHERE {status_field} = 'complete'"
                )
                failed_count = await _agent.postgres_conn.fetchval(
                    f"SELECT COUNT(*) FROM {table} WHERE {status_field} = 'failed'"
                )
                jobs_success = success_count or 0
                jobs_failed = failed_count or 0
            except Exception as e:
                logger.warning(f"Error fetching metrics: {e}")

        return MetricsResponse(
            agent_id=status["agent_id"],
            timestamp=datetime.utcnow(),
            jobs_total=status["jobs_processed"],
            jobs_success=jobs_success,
            jobs_failed=jobs_failed,
            average_duration_seconds=None,  # TODO: Calculate from job history
            current_iterations=0,  # Would need to track in agent
            uptime_seconds=status["uptime_seconds"],
        )

    return app


async def _process_job_background(job_id: str, metadata: Dict[str, Any]) -> None:
    """Process a job in the background."""
    if _agent is None:
        logger.error("Cannot process job - agent not initialized")
        return

    try:
        result = await _agent.process_job(job_id, metadata)
        logger.info(f"Job {job_id} completed: {result.get('should_stop')}")
    except Exception as e:
        logger.error(f"Background job {job_id} failed: {e}", exc_info=True)


# Default app instance (uses environment config)
app = create_app()
