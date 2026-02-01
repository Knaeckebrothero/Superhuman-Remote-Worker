"""FastAPI application for Universal Agent.

Provides HTTP endpoints for health checks, agent status, and orchestrator
integration. Jobs are received from the orchestrator via /job/start endpoint.
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException, BackgroundTasks

from ..agent import UniversalAgent
from ..core.loader import resolve_config_path
from .models import (
    HealthResponse,
    HealthStatus,
    ReadyResponse,
    AgentStatusResponse,
    ErrorResponse,
    MetricsResponse,
    JobStartRequest,
    JobStartResponse,
    JobCancelByOrchestratorRequest,
    JobResumeRequest,
)
from .orchestrator_client import OrchestratorClient, create_orchestrator_client_from_env

logger = logging.getLogger(__name__)

# Global state
_agent: Optional[UniversalAgent] = None
_shutdown_requested = False
_config_path: Optional[str] = None

# Orchestrator integration state
_orchestrator_client: Optional[OrchestratorClient] = None
_heartbeat_task: Optional[asyncio.Task] = None
_current_job_id: Optional[str] = None
_current_job_task: Optional[asyncio.Task] = None


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
    Agents receive jobs from orchestrator (ORCHESTRATOR_URL defaults to localhost:8085).
    """
    global _agent, _shutdown_requested
    global _orchestrator_client, _heartbeat_task

    # Startup
    logger.info("Starting Universal Agent application...")

    # Get config path from environment or global setting
    config_path = _config_path or os.getenv("AGENT_CONFIG", "creator")
    resolved_path = resolve_config_path(config_path)

    logger.info(f"Loading agent configuration from: {resolved_path}")

    # Create and initialize agent
    _agent = UniversalAgent.from_config(resolved_path)
    await _agent.initialize()

    # Register with orchestrator and start heartbeat
    _orchestrator_client = create_orchestrator_client_from_env(_agent.config.agent_id)
    logger.info("Registering with orchestrator...")
    await _orchestrator_client.connect()

    if await _orchestrator_client.register():
        # Start heartbeat loop
        _heartbeat_task = asyncio.create_task(
            _orchestrator_client.run_heartbeat_loop(
                get_status=_get_agent_status_for_heartbeat,
                get_job_id=_get_current_job_id,
                get_metrics=_get_agent_metrics,
            )
        )
        logger.info("Orchestrator heartbeat loop started")
    else:
        logger.error("Failed to register with orchestrator - agent will not receive jobs")
        await _orchestrator_client.close()
        _orchestrator_client = None

    yield

    # Shutdown
    logger.info("Shutting down Universal Agent application...")
    _shutdown_requested = True

    # Stop orchestrator heartbeat and deregister
    if _orchestrator_client:
        logger.info("Stopping orchestrator heartbeat and deregistering...")
        _orchestrator_client.stop_heartbeat()

        if _heartbeat_task:
            _heartbeat_task.cancel()
            try:
                await _heartbeat_task
            except asyncio.CancelledError:
                pass

        await _orchestrator_client.deregister()
        await _orchestrator_client.close()

    # Cancel any running job task
    if _current_job_task and not _current_job_task.done():
        _current_job_task.cancel()
        try:
            await _current_job_task
        except asyncio.CancelledError:
            pass

    if _agent:
        await _agent.shutdown()

    logger.info("Universal Agent application shutdown complete")


def _get_agent_status_for_heartbeat() -> str:
    """Get current agent status for heartbeat reporting."""
    if _agent is None:
        return "booting"

    if _current_job_id is not None:
        return "working"

    status = _agent.get_status()
    if not status.get("initialized"):
        return "booting"

    return "ready"


def _get_current_job_id() -> Optional[str]:
    """Get current job ID for heartbeat reporting."""
    return _current_job_id


def _get_agent_metrics() -> Optional[Dict[str, Any]]:
    """Get agent metrics for heartbeat reporting."""
    try:
        import psutil

        process = psutil.Process()
        return {
            "memory_mb": process.memory_info().rss / (1024 * 1024),
            "cpu_percent": process.cpu_percent(),
        }
    except ImportError:
        # psutil not installed
        return None
    except Exception as e:
        logger.debug(f"Failed to collect metrics: {e}")
        return None


async def _process_orchestrator_job(
    job_id: str,
    prompt: str,
    document_path: Optional[str] = None,
    document_dir: Optional[str] = None,
    context: Optional[Dict[str, Any]] = None,
    instructions: Optional[str] = None,
) -> None:
    """Process a job assigned by the orchestrator.

    This runs in the background after accepting a job from the orchestrator.
    """
    global _current_job_id

    if _agent is None:
        logger.error("Cannot process job - agent not initialized")
        return

    try:
        logger.info(f"Starting orchestrator job {job_id}")

        # Build metadata
        metadata: Dict[str, Any] = {"prompt": prompt}
        if document_path:
            metadata["document_path"] = document_path
        if document_dir:
            metadata["document_dir"] = document_dir
        if context:
            metadata["context"] = context
        if instructions:
            metadata["instructions"] = instructions

        # Process the job
        result = await _agent.process_job(job_id, metadata)

        logger.info(f"Orchestrator job {job_id} completed: {result.get('should_stop')}")

    except asyncio.CancelledError:
        logger.info(f"Orchestrator job {job_id} was cancelled")
        raise
    except Exception as e:
        logger.error(f"Orchestrator job {job_id} failed: {e}", exc_info=True)
    finally:
        _current_job_id = None


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
            try:
                success_count = await _agent.postgres_conn.fetchval(
                    "SELECT COUNT(*) FROM jobs WHERE status = 'complete'"
                )
                failed_count = await _agent.postgres_conn.fetchval(
                    "SELECT COUNT(*) FROM jobs WHERE status = 'failed'"
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

    # =========================================================================
    # Orchestrator Integration Endpoints
    # =========================================================================

    @app.post(
        "/job/start",
        response_model=JobStartResponse,
        status_code=202,
        tags=["Orchestrator"],
        responses={
            409: {"model": ErrorResponse, "description": "Agent is busy"},
            503: {"model": ErrorResponse, "description": "Agent not initialized"},
        },
    )
    async def start_job_from_orchestrator(
        request: JobStartRequest,
        background_tasks: BackgroundTasks,
    ) -> JobStartResponse:
        """Receive and start a job from the orchestrator.

        This endpoint is called by the orchestrator to assign a job to this agent.
        The job is processed in the background and the endpoint returns immediately
        with a 202 Accepted status.
        """
        global _current_job_id, _current_job_task

        if _agent is None:
            raise HTTPException(status_code=503, detail="Agent not initialized")

        # Check if already processing a job
        if _current_job_id is not None:
            raise HTTPException(
                status_code=409,
                detail=f"Agent is busy processing job {_current_job_id}",
            )

        # Accept the job
        _current_job_id = request.job_id

        # Start processing in background
        _current_job_task = asyncio.create_task(
            _process_orchestrator_job(
                job_id=request.job_id,
                prompt=request.prompt,
                document_path=request.document_path,
                document_dir=request.document_dir,
                context=request.context,
                instructions=request.instructions,
            )
        )

        logger.info(f"Accepted job {request.job_id} from orchestrator")

        return JobStartResponse(
            job_id=request.job_id,
            status="accepted",
            message="Job processing started",
        )

    @app.post(
        "/job/cancel",
        tags=["Orchestrator"],
        responses={
            404: {"model": ErrorResponse, "description": "No job running"},
        },
    )
    async def cancel_current_job(
        request: JobCancelByOrchestratorRequest,
    ) -> Dict[str, Any]:
        """Cancel the currently running job.

        This endpoint is called by the orchestrator to gracefully cancel
        the current job processing.
        """
        global _current_job_id, _current_job_task

        if _current_job_id is None:
            raise HTTPException(
                status_code=404,
                detail="No job currently running",
            )

        job_id = _current_job_id
        reason = request.reason or "Cancelled by orchestrator"

        # Cancel the job task
        if _current_job_task and not _current_job_task.done():
            _current_job_task.cancel()
            try:
                await _current_job_task
            except asyncio.CancelledError:
                pass

        _current_job_id = None
        _current_job_task = None

        logger.info(f"Cancelled job {job_id}: {reason}")

        return {
            "job_id": job_id,
            "status": "cancelled",
            "reason": reason,
        }

    @app.post(
        "/job/resume",
        response_model=JobStartResponse,
        status_code=202,
        tags=["Orchestrator"],
        responses={
            409: {"model": ErrorResponse, "description": "Agent is busy"},
            503: {"model": ErrorResponse, "description": "Agent not initialized"},
        },
    )
    async def resume_job(
        request: JobResumeRequest,
        background_tasks: BackgroundTasks,
    ) -> JobStartResponse:
        """Resume a job from checkpoint.

        This endpoint resumes a previously started job from its last checkpoint.
        Optional feedback can be injected before resuming.
        """
        global _current_job_id, _current_job_task

        if _agent is None:
            raise HTTPException(status_code=503, detail="Agent not initialized")

        # Check if already processing a job
        if _current_job_id is not None:
            raise HTTPException(
                status_code=409,
                detail=f"Agent is busy processing job {_current_job_id}",
            )

        # Accept the resume request
        _current_job_id = request.job_id

        # Build metadata for resume
        metadata: Dict[str, Any] = {"resume": True}
        if request.feedback:
            metadata["feedback"] = request.feedback

        # Start processing in background
        async def _resume_job():
            global _current_job_id
            try:
                result = await _agent.process_job(request.job_id, metadata)
                logger.info(f"Resumed job {request.job_id} completed: {result.get('should_stop')}")
            except asyncio.CancelledError:
                logger.info(f"Resumed job {request.job_id} was cancelled")
                raise
            except Exception as e:
                logger.error(f"Resumed job {request.job_id} failed: {e}", exc_info=True)
            finally:
                _current_job_id = None

        _current_job_task = asyncio.create_task(_resume_job())

        logger.info(f"Accepted resume request for job {request.job_id}")

        return JobStartResponse(
            job_id=request.job_id,
            status="accepted",
            message="Job resume started",
        )

    @app.get("/job/current", tags=["Orchestrator"])
    async def get_current_job() -> Dict[str, Any]:
        """Get information about the currently running job."""
        return {
            "job_id": _current_job_id,
            "is_busy": _current_job_id is not None,
        }

    return app


# Default app instance (uses environment config)
app = create_app()
