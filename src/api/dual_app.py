"""Dual-mode FastAPI application for Universal Agent.

Merges worker (job dispatch) and persistent (interactive session) routes
into a single application with a state machine:

Pod-per-task mode (default, for K8s):
    IDLE  ──/job/start──>  WORKING  ──job done──>  EXIT
    IDLE  ──/session/attach──>  SESSION  ──detach──>  EXIT

Loop mode (AGENT_LOOP=1, enable with --loop):
    IDLE  ──/job/start──>  WORKING  ──job done──>  IDLE
    IDLE  ──/session/attach──>  SESSION  ──detach──>  IDLE

Start with: python agent.py --mode dual --port 8001
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime
from enum import Enum
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import JSONResponse

from .models import (
    ErrorResponse,
    HealthResponse,
    HealthStatus,
    JobCancelByOrchestratorRequest,
    JobResumeRequest,
    JobStartRequest,
    JobStartResponse,
    ReadyResponse,
)
from ._session_auth import validate_session_token as _validate_session_token
from .orchestrator_client import OrchestratorClient, create_orchestrator_client_from_env
from ..agent import UniversalAgent
from ..core.loader import resolve_config_path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pod state machine
# ---------------------------------------------------------------------------


class PodState(str, Enum):
    IDLE = "idle"
    WORKING = "working"
    SESSION = "session"


_pod_state: PodState = PodState.IDLE
_state_lock = asyncio.Lock()

# Shared singleton layer
_agent: Optional[UniversalAgent] = None
_config_path: Optional[str] = None
_orchestrator_client: Optional[OrchestratorClient] = None
_heartbeat_task: Optional[asyncio.Task] = None
_shutdown_requested = False
_started_at: Optional[datetime] = None
_pending_exit_task: Optional[asyncio.Task] = None

# Worker-mode state (imported from app.py at runtime to avoid circular deps)
_current_job_id: Optional[str] = None
_current_job_task: Optional[asyncio.Task] = None
_stop_requested: asyncio.Event = asyncio.Event()
_stop_reason: Optional[str] = None
_stop_completed: asyncio.Event = asyncio.Event()


def _request_stop(reason: str) -> None:
    global _stop_reason
    if _stop_reason == "cancel" and reason == "pause":
        return
    _stop_reason = reason
    _stop_completed.clear()
    _stop_requested.set()


def _clear_stop() -> None:
    global _stop_reason
    _stop_reason = None
    _stop_requested.clear()
    _stop_completed.clear()


# ---------------------------------------------------------------------------
# Metrics helpers (shared by both modes)
# ---------------------------------------------------------------------------


def _get_agent_metrics() -> Optional[Dict[str, Any]]:
    try:
        import psutil

        proc = psutil.Process()
        return {
            "memory_mb": round(proc.memory_info().rss / 1_048_576, 1),
            "cpu_percent": proc.cpu_percent(interval=0),
        }
    except Exception:
        return None


def _get_heartbeat_status() -> str:
    if _shutdown_requested:
        return "draining"
    if _agent is None:
        return "booting"
    if _pod_state == PodState.WORKING:
        return "working"
    if _pod_state == PodState.SESSION:
        return "session"
    status = _agent.get_status()
    if not status.get("initialized"):
        return "booting"
    return "ready"


def _get_current_job_id() -> Optional[str]:
    return _current_job_id


# Drain intent flag — set the first time the orchestrator's heartbeat
# response carries ``intents.should_drain=true``. Idle workers exit
# immediately; busy workers expose the flag to the graph so it can
# freeze with ``freeze_data.type="version_upgrade"`` at the next phase
# boundary (Continue-as-New).
_drain_intent_received: bool = False
_drain_intent_handled: bool = False


def is_drain_requested() -> bool:
    """Public helper for the worker graph to check drain intent.

    The graph uses this at phase boundaries to decide whether to freeze
    with ``version_upgrade`` instead of continuing into the next phase.
    """
    return _drain_intent_received


async def _handle_heartbeat_intents(response: Dict[str, Any]) -> None:
    """Heartbeat-response callback: react to orchestrator-set intents.

    Idle workers (``ready`` status, no job) exit immediately to free
    the slot for a fresh-version pod. Busy workers just record the
    intent — the graph reacts at its next safe boundary.
    """
    global _drain_intent_received, _drain_intent_handled
    intents = response.get("intents") or {}
    if not isinstance(intents, dict) or not intents.get("should_drain"):
        return
    _drain_intent_received = True
    if _drain_intent_handled:
        return
    if _current_job_id is None and _pod_state == PodState.IDLE:
        _drain_intent_handled = True
        logger.info(
            "Drain intent received (reason=%s) — idle worker exiting",
            intents.get("drain_reason", "unspecified"),
        )
        # Best-effort drain heartbeat so the orchestrator sees us go.
        try:
            await _orchestrator_client.heartbeat(
                status="draining",
                job_id=None,
                metrics=_get_agent_metrics(),
            )
        except Exception:
            pass
        os._exit(0)
    elif not _drain_intent_handled:
        # Busy — log once. The graph picks this up via is_drain_requested().
        _drain_intent_handled = True
        logger.info(
            "Drain intent received (reason=%s) — will freeze at next phase boundary",
            intents.get("drain_reason", "unspecified"),
        )


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    global \
        _agent, \
        _shutdown_requested, \
        _orchestrator_client, \
        _heartbeat_task, \
        _started_at

    _started_at = datetime.now()
    logger.info("Starting dual-mode agent application...")

    config_path = _config_path or os.getenv("AGENT_CONFIG", "default")
    resolved_path, deployment_dir = resolve_config_path(config_path)
    logger.info(f"Loading agent configuration from: {resolved_path}")

    _agent = UniversalAgent.from_config(config_path)
    await _agent.initialize()

    # Register with orchestrator
    _orchestrator_client = create_orchestrator_client_from_env(_agent.config.agent_id)
    logger.info("Registering with orchestrator...")
    await _orchestrator_client.connect()

    if await _orchestrator_client.register(agent_mode="dual"):
        logger.info("Registered with orchestrator (dual mode)")
    else:
        logger.warning("Initial registration failed — will keep retrying in background")

    _heartbeat_task = asyncio.create_task(
        _orchestrator_client.run_heartbeat_loop(
            get_status=_get_heartbeat_status,
            get_job_id=_get_current_job_id,
            get_metrics=_get_agent_metrics,
            on_response=_handle_heartbeat_intents,
        )
    )

    yield

    # --- Shutdown ---
    logger.info("Shutting down dual-mode agent...")
    _shutdown_requested = True

    # Drain: send immediate draining heartbeat
    if _orchestrator_client and _orchestrator_client.agent_id:
        try:
            await _orchestrator_client.heartbeat(
                status="draining",
                job_id=_current_job_id,
                metrics=_get_agent_metrics(),
            )
        except Exception:
            pass

    # If a job is running, cooperative stop
    releasing_job_id = None
    if _current_job_task and not _current_job_task.done():
        releasing_job_id = _current_job_id
        logger.info("Requesting cooperative stop for graceful shutdown...")
        _request_stop("pause")
        try:
            await asyncio.wait_for(_stop_completed.wait(), timeout=120.0)
            logger.info("Job stopped cooperatively during shutdown")
        except asyncio.TimeoutError:
            logger.warning("Cooperative stop timed out — hard cancelling")
            _current_job_task.cancel()
            try:
                await _current_job_task
            except asyncio.CancelledError:
                pass

    if releasing_job_id and _orchestrator_client:
        try:
            await _orchestrator_client.report_pause(releasing_job_id)
        except Exception:
            logger.warning(
                f"Could not notify orchestrator to pause job {releasing_job_id}"
            )

    # Detach any active session
    if _pod_state == PodState.SESSION:
        try:
            from .persistent_app import _detach_session

            await _detach_session()
        except Exception as e:
            logger.warning(f"Session detach during shutdown failed: {e}")

    # Stop heartbeat and deregister
    if _orchestrator_client:
        _orchestrator_client.stop_heartbeat()
        if _heartbeat_task:
            _heartbeat_task.cancel()
            try:
                await _heartbeat_task
            except asyncio.CancelledError:
                pass
        await _orchestrator_client.deregister()
        await _orchestrator_client.close()

    if _agent:
        await _agent.shutdown()

    logger.info("Dual-mode agent shutdown complete")


# ---------------------------------------------------------------------------
# Pod exit helper
# ---------------------------------------------------------------------------


def _schedule_exit(delay: float = 1.0) -> None:
    """Schedule process exit after a short delay (allows response to be sent)."""
    global _pending_exit_task

    # Cancel any previously scheduled exit
    if _pending_exit_task and not _pending_exit_task.done():
        _pending_exit_task.cancel()

    async def _exit():
        await asyncio.sleep(delay)
        logger.info("Pod task complete — exiting process")
        # Use os._exit to bypass uvicorn's signal handling
        os._exit(0)

    _pending_exit_task = asyncio.create_task(_exit())


def _should_loop() -> bool:
    """Check if agent should loop back to IDLE instead of exiting."""
    return os.environ.get("AGENT_LOOP", "").strip() == "1"


async def _reset_to_idle(source: str, *, skip_session_cleanup: bool = False) -> None:
    """Clean up current task state and return to IDLE for next task.

    The state flip to IDLE and the ready-heartbeat are in a try/finally so
    they run even if the inline cleanup (session detach, file-handler tear-
    down) raises. Without that guarantee a partial failure leaves the agent
    reporting 'working'/'session' forever — exactly the zombie pattern we
    saw in dev (PR 1's orchestrator-side sweep is the safety net for this).

    The ready-heartbeat retries 3x with backoff. If all attempts fail the
    orchestrator's stuck-working/session sweep catches it within 60s.
    """
    global _pod_state, _current_job_id, _current_job_task

    logger.info(f"Resetting to IDLE after: {source}")

    try:
        _current_job_id = None
        _current_job_task = None
        _clear_stop()

        if _pod_state == PodState.SESSION and not skip_session_cleanup:
            try:
                from .persistent_app import _detach_session

                await _detach_session()
            except Exception as e:
                logger.warning(f"Session cleanup during reset failed: {e}")

        # Remove per-job file handlers from root logger
        try:
            root_logger = logging.getLogger()
            for handler in root_logger.handlers[:]:
                if isinstance(handler, logging.FileHandler) and "job_" in getattr(
                    handler, "baseFilename", ""
                ):
                    handler.close()
                    root_logger.removeHandler(handler)
        except Exception as e:
            logger.warning(f"File handler cleanup during reset failed: {e}")
    finally:
        async with _state_lock:
            _pod_state = PodState.IDLE

        if _orchestrator_client and _orchestrator_client.agent_id:
            last_err: Optional[Exception] = None
            for attempt in range(3):
                try:
                    await _orchestrator_client.heartbeat(
                        status="ready", job_id=None, metrics=_get_agent_metrics()
                    )
                    last_err = None
                    break
                except Exception as e:
                    last_err = e
                    if attempt < 2:
                        await asyncio.sleep(0.5 * (attempt + 1))
            if last_err is not None:
                logger.warning(
                    f"Failed to send ready heartbeat after 3 attempts: {last_err}"
                )

    logger.info("Agent returned to IDLE — ready for next task")


async def _complete_stop(reset_source: str) -> None:
    """Finish a cooperative stop (cancel/pause): reset to IDLE, then signal the
    waiting cancel/pause handler.

    Ordering is load-bearing. ``_reset_to_idle()`` calls ``_clear_stop()``,
    which clears ``_stop_completed``; the cancel/pause handler is blocked on
    that event, so it must be ``set()`` *after* the reset or the handler hangs
    to its 120s timeout. Without the reset, ``_pod_state`` stays ``WORKING``
    with no job — the zombie pattern (see
    docs/done/worker_pod_state_zombie_on_cancel.md).
    """
    await _reset_to_idle(reset_source)
    _stop_completed.set()


# ---------------------------------------------------------------------------
# Job processing (adapted from app.py)
# ---------------------------------------------------------------------------


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
    config_name: Optional[str] = None,
    config_override: Optional[Dict[str, Any]] = None,
    git_remote_url: Optional[str] = None,
    datasources: Optional[list] = None,
    repositories: Optional[list] = None,
    branch_name: Optional[str] = None,
    project_id: Optional[str] = None,
) -> None:
    """Process a job assigned by the orchestrator, then exit."""
    global _current_job_id, _pod_state

    if _agent is None:
        logger.error("Cannot process job — agent not initialized")
        return

    try:
        from ..core.workspace import get_logs_path

        # Set up per-job file logging
        logs_dir = get_logs_path()
        log_file = logs_dir / f"job_{job_id}.log"
        level_name = os.getenv("LOG_LEVEL", "INFO").upper()
        level = getattr(logging, level_name, logging.INFO)

        file_handler = logging.FileHandler(log_file, mode="a")
        file_handler.setLevel(level)
        file_handler.setFormatter(
            logging.Formatter(
                "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        logging.getLogger().addHandler(file_handler)

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
        if config_name and config_name != "default":
            metadata["config_name"] = config_name
        if config_override:
            metadata["config_override"] = config_override
        if git_remote_url:
            metadata["git_remote_url"] = git_remote_url
        if datasources:
            metadata["datasources"] = datasources
        if repositories:
            metadata["repositories"] = repositories
        if branch_name:
            metadata["branch_name"] = branch_name
        if project_id:
            metadata["project_id"] = project_id

        _clear_stop()

        # Inject orchestrator client so delegation tools can reach the API
        _agent._orchestrator_client = _orchestrator_client

        # Process with streaming
        final_state = None
        last_iteration = "?"
        streaming_gen = await _agent.process_job(job_id, metadata, stream=True)
        async for state in streaming_gen:
            final_state = state
            if isinstance(state, dict):
                iteration = state.get("iteration")
                if iteration is not None:
                    last_iteration = iteration
                logger.info(f"[Iteration {last_iteration}] job={job_id}")

            if _stop_requested.is_set():
                logger.info(f"Stop requested ({_stop_reason}) for job {job_id}")
                break

        # Handle cooperative stop vs normal completion
        if _stop_requested.is_set():
            reason = _stop_reason
            logger.info(f"Job {job_id} stopped gracefully (reason={reason})")
            # Reset to IDLE so the pod doesn't strand _pod_state=WORKING with no
            # job (zombie). Don't exit — cancel/pause means the orchestrator may
            # reassign/resume. See worker_pod_state_zombie_on_cancel.md.
            await _complete_stop(f"job {reason}")
            return

        result = final_state or {}
        logger.info(f"Job {job_id} completed: {result.get('should_stop')}")

        _current_job_id = None

        # Report completion
        if _orchestrator_client:
            try:
                if _orchestrator_client.agent_id:
                    await _orchestrator_client.heartbeat(
                        status="ready", job_id=None, metrics=_get_agent_metrics()
                    )
                await _orchestrator_client.report_completion(job_id, result)
            except Exception as e:
                logger.error(f"Failed to report completion for job {job_id}: {e}")

        # Always reset state — _reset_to_idle pushes a final 'ready' heartbeat
        # so the DB matches reality even in non-loop mode where _schedule_exit
        # would otherwise os._exit(0) before lifespan cleanup runs.
        await _reset_to_idle("job completion")
        if not _should_loop():
            _schedule_exit(delay=2.0)

    except asyncio.CancelledError:
        logger.info(f"Job {job_id} was cancelled")
        raise
    except Exception as e:
        logger.error(f"Job {job_id} failed: {e}", exc_info=True)
        if _orchestrator_client:
            try:
                await _orchestrator_client.report_completion(
                    job_id, {"error": {"message": str(e)}}
                )
            except Exception:
                logger.error(f"Failed to report error for job {job_id}")
        await _reset_to_idle("job error")
        if not _should_loop():
            _schedule_exit(delay=2.0)
    finally:
        if _current_job_id == job_id:
            _current_job_id = None


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_dual_app(config_path: Optional[str] = None) -> FastAPI:
    """Create the dual-mode FastAPI application.

    Merges worker and persistent routes with state-machine guards.
    """
    global _config_path
    if config_path:
        _config_path = config_path

    app = FastAPI(
        title="Universal Agent API (Dual Mode)",
        description="Dual-mode agent: accepts jobs or interactive sessions",
        version="2.0.0",
        lifespan=lifespan,
    )

    # ===================================================================
    # Health endpoints
    # ===================================================================

    @app.get("/health", response_model=HealthResponse, tags=["Health"])
    async def health_check() -> HealthResponse:
        if _agent is None:
            return HealthResponse(
                status=HealthStatus.UNHEALTHY,
                agent_id="unknown",
                agent_name="Unknown",
                uptime_seconds=0,
                checks={"initialized": False},
            )
        status = _agent.get_status()
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
                "postgres": status["connections"].get("postgres", False),
            },
        )

    @app.get("/ready", response_model=ReadyResponse, tags=["Health"])
    async def readiness_check() -> ReadyResponse:
        if _shutdown_requested:
            return ReadyResponse(
                ready=False,
                message="Agent is draining (shutting down)",
                connections={},
            )
        if _agent is None:
            return ReadyResponse(
                ready=False, message="Agent not initialized", connections={}
            )
        status = _agent.get_status()
        base_ready = status["initialized"] and status["connections"]["postgres"]

        # When in SESSION state, the readiness gate is the three-way check
        # in persistent_app._session_ready() — shared with /session/status
        # and handle_persistent_websocket so probe and WS gate stay aligned.
        if _pod_state == PodState.SESSION:
            import src.api.persistent_app as pa

            session_ready = pa._session_ready()
            return ReadyResponse(
                ready=session_ready,
                message="Session ready" if session_ready else "Session initializing",
                connections=status["connections"],
            )

        return ReadyResponse(
            ready=base_ready,
            message="Ready to accept work" if base_ready else "Not ready",
            connections=status["connections"],
        )

    @app.get("/status", tags=["Health"])
    async def agent_status() -> Dict[str, Any]:
        return {
            "mode": "dual",
            "pod_state": _pod_state.value,
            "loop_mode": _should_loop(),
            "current_job_id": _current_job_id,
            "config": _config_path,
            "uptime_seconds": (datetime.now() - _started_at).total_seconds()
            if _started_at
            else 0,
        }

    # ===================================================================
    # System monitoring (from app.py)
    # ===================================================================

    @app.get("/system/info", tags=["Monitoring"])
    async def system_info() -> Dict[str, Any]:
        try:
            from .app import _collect_system_info

            return _collect_system_info()
        except ImportError:
            raise HTTPException(501, "psutil not installed")

    @app.get("/system/shell-state", tags=["Monitoring"])
    async def shell_state() -> Dict[str, Any]:
        if _agent is None:
            return {"tabs": [], "message": "Agent not initialized"}
        shell_manager = getattr(_agent, "_shell_manager", None)
        if shell_manager is None:
            return {"tabs": [], "message": "No active shell sessions"}
        try:
            tab_list = shell_manager.list_tabs()
            tabs = []
            for tab_meta in tab_list:
                name = tab_meta.get("name", "unknown")
                try:
                    read_result = shell_manager.read_with_offset(name, lines=30)
                    recent_output = (
                        read_result.get("output", "")
                        if isinstance(read_result, dict)
                        else str(read_result)
                    )
                    total_lines = (
                        read_result.get("total_lines", 0)
                        if isinstance(read_result, dict)
                        else 0
                    )
                except Exception:
                    recent_output = ""
                    total_lines = 0
                tabs.append(
                    {
                        "name": name,
                        "type": tab_meta.get("type", "unknown"),
                        "created_at": tab_meta.get("created_at", ""),
                        "total_lines": total_lines,
                        "recent_output": recent_output,
                    }
                )
            return {"tabs": tabs}
        except Exception as e:
            return {"tabs": [], "message": f"Error: {str(e)}"}

    # ===================================================================
    # Worker routes (job dispatch)
    # ===================================================================

    @app.post(
        "/job/start",
        response_model=JobStartResponse,
        status_code=202,
        tags=["Worker"],
        responses={
            409: {"model": ErrorResponse, "description": "Pod is busy"},
            503: {"model": ErrorResponse, "description": "Agent not initialized"},
        },
    )
    async def start_job(request: JobStartRequest) -> JobStartResponse:
        global _pod_state, _current_job_id, _current_job_task

        if _agent is None:
            raise HTTPException(503, "Agent not initialized")
        if _shutdown_requested:
            raise HTTPException(503, "Agent is shutting down")

        async with _state_lock:
            if _pod_state != PodState.IDLE:
                raise HTTPException(
                    409,
                    f"Pod is in {_pod_state.value} state, cannot accept job",
                )
            _pod_state = PodState.WORKING
            _current_job_id = request.job_id

        _clear_stop()

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
                config_name=request.config_name,
                config_override=request.config_override,
                git_remote_url=request.git_remote_url,
                datasources=request.datasources,
                repositories=request.repositories,
                branch_name=request.branch_name,
                project_id=request.project_id,
            )
        )

        logger.info(f"Accepted job {request.job_id} (dual mode)")
        return JobStartResponse(
            job_id=request.job_id,
            status="accepted",
            message="Job processing started",
        )

    @app.post("/job/cancel", tags=["Worker"])
    async def cancel_job(request: JobCancelByOrchestratorRequest) -> Dict[str, Any]:
        global _current_job_id, _current_job_task

        if _pod_state != PodState.WORKING or _current_job_id is None:
            raise HTTPException(404, "No job currently running")

        job_id = _current_job_id
        _request_stop("cancel")

        logger.info(f"Cancel requested for job {job_id}")
        try:
            await asyncio.wait_for(_stop_completed.wait(), timeout=120.0)
            return {
                "job_id": job_id,
                "status": "cancelled",
                "reason": request.reason or "Cancelled by orchestrator",
                "graceful": True,
            }
        except asyncio.TimeoutError:
            if _current_job_task and not _current_job_task.done():
                _current_job_task.cancel()
                try:
                    await _current_job_task
                except asyncio.CancelledError:
                    pass
            # The hard-killed task re-raises CancelledError without resetting
            # _pod_state, so reset here or the pod strands at WORKING (zombie).
            # _reset_to_idle also nulls _current_job_id/_task and clears stop
            # state. See worker_pod_state_zombie_on_cancel.md.
            await _reset_to_idle("cancel hard-kill")
            return {
                "job_id": job_id,
                "status": "cancelled",
                "reason": f"{request.reason or 'Cancelled'} (hard-killed)",
                "graceful": False,
            }

    @app.post("/job/pause", tags=["Worker"])
    async def pause_job() -> Dict[str, Any]:
        if _pod_state != PodState.WORKING or _current_job_id is None:
            raise HTTPException(404, "No job currently running")

        job_id = _current_job_id
        _request_stop("pause")

        logger.info(f"Pause requested for job {job_id}")
        try:
            await asyncio.wait_for(_stop_completed.wait(), timeout=120.0)
        except asyncio.TimeoutError:
            raise HTTPException(
                408,
                f"Pause timed out after 120s. Job {job_id} will pause after current node.",
            )
        return {"job_id": job_id, "status": "paused"}

    @app.post(
        "/job/resume",
        response_model=JobStartResponse,
        status_code=202,
        tags=["Worker"],
    )
    async def resume_job(request: JobResumeRequest) -> JobStartResponse:
        global _pod_state, _current_job_id, _current_job_task

        if _agent is None:
            raise HTTPException(503, "Agent not initialized")
        if _shutdown_requested:
            raise HTTPException(503, "Agent is shutting down")

        async with _state_lock:
            if _pod_state != PodState.IDLE:
                raise HTTPException(
                    409,
                    f"Pod is in {_pod_state.value} state, cannot accept resume",
                )
            _pod_state = PodState.WORKING
            _current_job_id = request.job_id

        _clear_stop()

        async def _do_resume():
            global _current_job_id, _pod_state

            try:
                from ..core.workspace import get_logs_path

                logs_dir = get_logs_path()
                log_file = logs_dir / f"job_{request.job_id}.log"
                file_handler = logging.FileHandler(log_file, mode="a")
                file_handler.setLevel(logging.INFO)
                file_handler.setFormatter(
                    logging.Formatter(
                        "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S",
                    )
                )
                logging.getLogger().addHandler(file_handler)

                resume_metadata = {}
                if request.config_upload_id:
                    resume_metadata["config_upload_id"] = request.config_upload_id
                if request.config_override:
                    resume_metadata["config_override"] = request.config_override
                if request.datasources:
                    resume_metadata["datasources"] = request.datasources
                if request.project_id:
                    resume_metadata["project_id"] = request.project_id

                _agent._orchestrator_client = _orchestrator_client
                final_state = None
                streaming_gen = await _agent.process_job(
                    request.job_id,
                    metadata=resume_metadata if resume_metadata else None,
                    resume=True,
                    feedback=request.feedback,
                    original_config_name=request.config_name,
                    previous_status=request.previous_status,
                    stream=True,
                )
                async for state in streaming_gen:
                    final_state = state
                    if _stop_requested.is_set():
                        break

                if _stop_requested.is_set():
                    reason = _stop_reason
                    logger.info(
                        f"Resume of job {request.job_id} stopped gracefully "
                        f"(reason={reason})"
                    )
                    await _complete_stop(f"job resume {reason}")
                    return

                result = final_state or {}
                _current_job_id = None

                if _orchestrator_client:
                    try:
                        if _orchestrator_client.agent_id:
                            await _orchestrator_client.heartbeat(
                                status="ready",
                                job_id=None,
                                metrics=_get_agent_metrics(),
                            )
                        await _orchestrator_client.report_completion(
                            request.job_id, result
                        )
                    except Exception as e:
                        logger.error(f"Failed to report completion: {e}")

                await _reset_to_idle("job resume completion")
                if not _should_loop():
                    _schedule_exit(delay=2.0)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Resume job {request.job_id} failed: {e}", exc_info=True)
                if _orchestrator_client:
                    try:
                        await _orchestrator_client.report_completion(
                            request.job_id, {"error": {"message": str(e)}}
                        )
                    except Exception:
                        pass
                await _reset_to_idle("job resume error")
                if not _should_loop():
                    _schedule_exit(delay=2.0)
            finally:
                if _current_job_id == request.job_id:
                    _current_job_id = None

        _current_job_task = asyncio.create_task(_do_resume())
        return JobStartResponse(
            job_id=request.job_id,
            status="accepted",
            message="Job resume started",
        )

    @app.get("/job/current", tags=["Worker"])
    async def get_current_job() -> Dict[str, Any]:
        return {
            "job_id": _current_job_id,
            "is_busy": _pod_state == PodState.WORKING,
        }

    # ===================================================================
    # Session routes (persistent/interactive)
    # ===================================================================

    @app.post("/session/attach", tags=["Session"])
    async def session_attach(request: dict = {}) -> JSONResponse:
        global _pod_state

        thread_id = request.get("thread_id")
        if not thread_id:
            return JSONResponse({"error": "thread_id is required"}, status_code=400)

        async with _state_lock:
            if _pod_state != PodState.IDLE:
                return JSONResponse(
                    {"error": f"Pod is in {_pod_state.value} state"},
                    status_code=409,
                )
            _pod_state = PodState.SESSION

        # Respond immediately — heavy setup (workspace polling, session init)
        # runs in the background. The /ready endpoint will report readiness
        # once the session is fully set up.
        async def _setup_session():
            # Without this, `_pod_state = PodState.IDLE` below creates a
            # closure-local variable instead of resetting the module global,
            # leaving the agent permanently stuck reporting `session` status.
            global _pod_state
            try:
                import src.api.persistent_app as pa

                pa._agent = _agent
                pa._orchestrator_client = _orchestrator_client
                pa._config_path = _config_path
                pa._started_at = _started_at

                await pa._attach_session(
                    thread_id=thread_id,
                    config_override=request.get("config_override"),
                    project_ids=request.get("project_ids"),
                    datasources=request.get("datasources"),
                )
                logger.info(f"Session setup complete for thread {thread_id}")
            except Exception:
                logger.exception(f"Session setup failed for thread {thread_id}")
                import src.api.persistent_app as pa

                # _attach_session sets pa._thread_id before it can fail;
                # _detach_session short-circuits when _session is None and
                # never clears _thread_id, so we have to do it explicitly.
                pa._thread_id = None
                async with _state_lock:
                    _pod_state = PodState.IDLE
                if _orchestrator_client and _orchestrator_client.agent_id:
                    try:
                        await _orchestrator_client.heartbeat(
                            status="ready",
                            job_id=None,
                            metrics=_get_agent_metrics(),
                        )
                    except Exception as e:
                        logger.warning(
                            f"Failed to send ready heartbeat after attach failure: {e}"
                        )
                if _orchestrator_client:
                    try:
                        await _orchestrator_client.release_thread_agent(thread_id)
                    except Exception as e:
                        logger.warning(
                            f"Failed to release thread→agent binding for {thread_id}: {e}"
                        )

        asyncio.create_task(_setup_session())

        return JSONResponse(
            {
                "status": "attaching",
                "thread_id": thread_id,
            }
        )

    @app.get("/session/status", tags=["Session"])
    async def session_status() -> JSONResponse:
        """Check if session is fully set up and ready for WebSocket.

        Delegates to ``persistent_app._session_ready()`` so /ready (SESSION
        branch), this endpoint, and ``handle_persistent_websocket`` share one
        definition of "ready for a WS" and can't drift.
        """
        import src.api.persistent_app as pa

        if _pod_state != PodState.SESSION:
            return JSONResponse({"ready": False, "state": _pod_state.value})

        return JSONResponse(
            {
                "ready": pa._session_ready(),
                "thread_id": pa._thread_id,
                "state": "session",
            }
        )

    @app.post("/session/detach", tags=["Session"])
    async def session_detach() -> JSONResponse:
        global _pod_state

        if _pod_state != PodState.SESSION:
            return JSONResponse({"status": "not_in_session"}, status_code=404)

        try:
            import src.api.persistent_app as pa

            thread_id = pa._thread_id
            await pa._detach_session()

            if _should_loop():
                await _reset_to_idle("session detach", skip_session_cleanup=True)
            else:
                _schedule_exit(delay=2.0)

            return JSONResponse(
                {
                    "status": "detached",
                    "thread_id": thread_id,
                }
            )
        except Exception as e:
            logger.exception("Failed to detach session")
            return JSONResponse({"error": str(e)}, status_code=500)

    # --- Persistent-session REST endpoints (orchestrator-driven turns) ---
    #
    # Mirror of /api/{input,interrupt,approve} in persistent_app.py. The
    # orchestrator forwards from POST /api/persistent/threads/{id}/{input,
    # interrupt,approve} to whichever agent is attached to the thread, which
    # in cluster usage is a dual-mode pod. Without these routes the agent
    # returns 404 and the cockpit's turn never lands. See task #136 /
    # docs/issues/persistent_session_dual_mode_phase1_gap.md for the parallel
    # WS-handler gap this duplication caused.

    @app.post("/api/input", tags=["Session"])
    async def api_input(request: Request):
        if _pod_state != PodState.SESSION:
            return JSONResponse(
                {"error": "Pod is not in session mode"}, status_code=404
            )
        import src.api.persistent_app as pa

        return await pa.handle_api_input(request)

    @app.post("/api/interrupt", tags=["Session"])
    async def api_interrupt():
        if _pod_state != PodState.SESSION:
            return JSONResponse(
                {"error": "Pod is not in session mode"}, status_code=404
            )
        import src.api.persistent_app as pa

        return await pa.handle_api_interrupt()

    @app.post("/api/approve", tags=["Session"])
    async def api_approve(request: Request):
        if _pod_state != PodState.SESSION:
            return JSONResponse(
                {"error": "Pod is not in session mode"}, status_code=404
            )
        import src.api.persistent_app as pa

        return await pa.handle_api_approve(request)

    async def _do_ws_chat(ws: WebSocket) -> None:
        """Shared WS handler for both /ws/chat and /p/{thread_id}/ws.

        The two routes exist so the agent answers both the legacy direct path
        (/ws/chat, used by local dev with websocat and any cluster-internal
        callers) and the external path that the per-session Ingress forwards
        through Traefik (/p/<thread_id>/ws — cockpit's new direct-WS path).
        """
        global _pending_exit_task

        # Validate the session JWT carried as ?t={token}.
        if not await _validate_session_token(ws):
            return

        # Cancel any pending exit from a previous WS disconnect (e.g. page
        # refresh). Vestigial under the Phase-1 keystone — the WS-disconnect
        # path no longer schedules exit — but kept as a no-op safety net for
        # other paths (drain intent, shutdown) that still arm _pending_exit_task.
        if _pending_exit_task and not _pending_exit_task.done():
            _pending_exit_task.cancel()
            _pending_exit_task = None
            logger.info("Cancelled pending exit — new WebSocket connecting")

        if _pod_state != PodState.SESSION:
            await ws.accept()
            await ws.send_json(
                {
                    "method": "error",
                    "params": {"message": "Pod is not in session mode"},
                }
            )
            await ws.close(code=4403, reason="Not in session mode")
            return

        import src.api.persistent_app as pa

        if not pa._session or not pa._session.llm_with_tools:
            await ws.accept()
            await ws.send_json(
                {
                    "method": "error",
                    "params": {"message": "Session not ready"},
                }
            )
            await ws.close(code=4503, reason="Session not ready")
            return

        # Delegate to the shared module-level handler. Same implementation
        # the pure-persistent route uses — Phase-1 keystone, subscriber
        # model, Phase-5 status flips all included. See
        # docs/issues/persistent_session_dual_mode_phase1_gap.md for why
        # this delegation matters.
        await pa.handle_persistent_websocket(ws)

    @app.websocket("/ws/chat")
    async def ws_chat(ws: WebSocket):
        await _do_ws_chat(ws)

    @app.websocket("/p/{thread_id}/ws")
    async def ws_session(ws: WebSocket, thread_id: str):
        # thread_id is enforced by _validate_session_token against
        # SESSION_BOUND_THREAD_ID + the JWT's tid claim — the path param is
        # only here so the Ingress's /p/<tid>/ws path matches a FastAPI route.
        await _do_ws_chat(ws)

    return app
