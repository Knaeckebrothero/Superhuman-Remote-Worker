"""FastAPI application for Universal Agent.

Provides HTTP endpoints for health checks, agent status, and orchestrator
integration. Jobs are received from the orchestrator via /job/start endpoint.
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException, BackgroundTasks

from ..agent import UniversalAgent
from ..core.loader import resolve_config_path
from ..core.workspace import get_logs_path
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
    resolved_path, deployment_dir = resolve_config_path(config_path)

    logger.info(f"Loading agent configuration from: {resolved_path}")

    # Create and initialize agent - pass original config_path, not resolved tuple
    _agent = UniversalAgent.from_config(config_path)
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


class _FlushingFileHandler(logging.FileHandler):
    """File handler that flushes after every emit for crash safety."""

    def emit(self, record):
        super().emit(record)
        self.flush()


def _setup_job_file_logging(job_id: str) -> Path:
    """Set up file logging for a specific job in server mode.

    Creates a log file in the logs directory. Uses FlushingFileHandler
    to ensure logs are written immediately for crash safety.

    Args:
        job_id: The job identifier

    Returns:
        Path to the log file
    """
    logs_dir = get_logs_path()  # Creates workspace/logs/ if needed
    log_file = logs_dir / f"job_{job_id}.log"

    # Get level from env var
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    # Create flushing file handler for crash safety
    file_handler = _FlushingFileHandler(log_file, mode='a')
    file_handler.setLevel(level)
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))

    # Add to root logger
    root_logger = logging.getLogger()
    root_logger.addHandler(file_handler)

    logger.info(f"Job log file: {log_file}")

    return log_file


def _cleanup_job_file_handler(job_id: str) -> None:
    """Remove job-specific file handler from root logger.

    Args:
        job_id: The job identifier
    """
    root = logging.getLogger()
    for handler in root.handlers[:]:
        if isinstance(handler, logging.FileHandler):
            if f"job_{job_id}.log" in str(handler.baseFilename):
                handler.close()
                root.removeHandler(handler)


async def _process_orchestrator_job(
    job_id: str,
    description: str,
    upload_id: Optional[str] = None,
    config_upload_id: Optional[str] = None,
    instructions_upload_id: Optional[str] = None,
    document_path: Optional[str] = None,
    document_dir: Optional[str] = None,
    context: Optional[Dict[str, Any]] = None,
    instructions: Optional[str] = None,
    config_override: Optional[Dict[str, Any]] = None,
    git_remote_url: Optional[str] = None,
    datasources: Optional[list] = None,
) -> None:
    """Process a job assigned by the orchestrator.

    This runs in the background after accepting a job from the orchestrator.
    Uses streaming mode with iteration logging and per-job file logging.
    """
    global _current_job_id

    if _agent is None:
        logger.error("Cannot process job - agent not initialized")
        return

    # Set up per-job file logging for crash safety
    _setup_job_file_logging(job_id)

    try:
        logger.info(f"Starting orchestrator job {job_id}")

        # Build metadata
        metadata: Dict[str, Any] = {"description": description}
        if upload_id:
            metadata["upload_id"] = upload_id
        if config_upload_id:
            metadata["config_upload_id"] = config_upload_id
        if instructions_upload_id:
            metadata["instructions_upload_id"] = instructions_upload_id
        if document_path:
            metadata["document_path"] = document_path
        if document_dir:
            metadata["document_dir"] = document_dir
        if context:
            metadata.update(context)
        if instructions:
            metadata["instructions"] = instructions
        if config_override:
            metadata["config_override"] = config_override
        if git_remote_url:
            metadata["git_remote_url"] = git_remote_url
        if datasources:
            metadata["datasources"] = datasources

        # Process the job with streaming for iteration logging
        final_state = None
        streaming_gen = await _agent.process_job(job_id, metadata, stream=True)
        async for state in streaming_gen:
            final_state = state
            if isinstance(state, dict):
                iteration = state.get("iteration", "?")
                has_error = state.get("error") is not None
                logger.info(f"[Iteration {iteration}] job={job_id} error={has_error}")

        result = final_state or {}
        logger.info(f"Orchestrator job {job_id} completed: {result.get('should_stop')}")

    except asyncio.CancelledError:
        logger.info(f"Orchestrator job {job_id} was cancelled")
        raise
    except Exception as e:
        logger.error(f"Orchestrator job {job_id} failed: {e}", exc_info=True)
    finally:
        _current_job_id = None
        _cleanup_job_file_handler(job_id)


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
                description=request.description,
                upload_id=request.upload_id,
                config_upload_id=request.config_upload_id,
                instructions_upload_id=request.instructions_upload_id,
                document_path=request.document_path,
                document_dir=request.document_dir,
                context=request.context,
                instructions=request.instructions,
                config_override=request.config_override,
                git_remote_url=request.git_remote_url,
                datasources=request.datasources,
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
        """Resume a job from last completed phase snapshot.

        This endpoint resumes a previously started job from its last phase snapshot.
        Optional feedback can be injected before resuming.
        """
        global _current_job_id, _current_job_task

        if _agent is None:
            raise HTTPException(status_code=503, detail="Agent not initialized")

        # Log config mismatch as warning (don't reject - checkpoint discovery handles it)
        if request.config_name and request.config_name != _agent.config.agent_id:
            logger.warning(
                f"Config mismatch: job has config '{request.config_name}' but this agent is '{_agent.config.agent_id}'. "
                f"Will attempt to discover correct checkpoint."
            )

        # Check if already processing a job
        if _current_job_id is not None:
            raise HTTPException(
                status_code=409,
                detail=f"Agent is busy processing job {_current_job_id}",
            )

        # Accept the resume request
        _current_job_id = request.job_id

        # Capture for closure
        feedback = request.feedback
        config_name = request.config_name

        # Build metadata with config info for resume
        resume_metadata = {}
        if request.config_upload_id:
            resume_metadata["config_upload_id"] = request.config_upload_id
        if request.config_override:
            resume_metadata["config_override"] = request.config_override
        if request.datasources:
            resume_metadata["datasources"] = request.datasources

        # Start processing in background
        async def _resume_job():
            global _current_job_id
            try:
                result = await _agent.process_job(
                    request.job_id,
                    metadata=resume_metadata if resume_metadata else None,
                    resume=True,
                    feedback=feedback,
                    original_config_name=config_name,
                )
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
