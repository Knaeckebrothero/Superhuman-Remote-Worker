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

# Cooperative stop mechanism (checked between graph iterations)
# Used by both pause and cancel — _stop_reason discriminates the action.
_stop_requested: asyncio.Event = asyncio.Event()  # Signals streaming loop to break
_stop_reason: Optional[str] = None  # "pause" or "cancel"
_stop_completed: asyncio.Event = asyncio.Event()  # Signals waiting endpoint that stop finished


def _request_stop(reason: str) -> None:
    """Request cooperative stop. Cancel overrides a pending pause (higher severity)."""
    global _stop_reason
    if _stop_reason == "cancel" and reason == "pause":
        return  # Don't downgrade cancel to pause
    _stop_reason = reason
    _stop_completed.clear()
    _stop_requested.set()


def _clear_stop() -> None:
    """Reset all stop state for a new job."""
    global _stop_reason
    _stop_reason = None
    _stop_requested.clear()
    _stop_completed.clear()


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

    # Attempt initial registration (non-fatal if it fails)
    if await _orchestrator_client.register():
        logger.info("Registered with orchestrator")
    else:
        logger.warning("Initial registration failed - will keep retrying in background")

    # Always start the heartbeat loop (handles registration retries too)
    _heartbeat_task = asyncio.create_task(
        _orchestrator_client.run_heartbeat_loop(
            get_status=_get_agent_status_for_heartbeat,
            get_job_id=_get_current_job_id,
            get_metrics=_get_agent_metrics,
        )
    )
    logger.info("Orchestrator heartbeat loop started")

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
        listening = [
            c for c in psutil.net_connections(kind="inet")
            if c.status == "LISTEN"
        ]
        return {
            "memory_mb": process.memory_info().rss / (1024 * 1024),
            "cpu_percent": process.cpu_percent(),
            "listening_ports": len(listening),
            "process_count": len(psutil.pids()),
        }
    except ImportError:
        # psutil not installed
        return None
    except Exception as e:
        logger.debug(f"Failed to collect metrics: {e}")
        return None


def _collect_system_info() -> Dict[str, Any]:
    """Collect comprehensive system information for the /system/info endpoint."""
    import psutil

    # CPU
    cpu_info = {
        "percent": psutil.cpu_percent(interval=0.1),
        "cores": psutil.cpu_count(),
    }

    # Memory
    mem = psutil.virtual_memory()
    memory_info = {
        "total_mb": round(mem.total / (1024 * 1024)),
        "used_mb": round(mem.used / (1024 * 1024)),
        "percent": mem.percent,
    }

    # Disk
    disk = psutil.disk_usage("/")
    disk_info = {
        "total_gb": round(disk.total / (1024 ** 3), 1),
        "used_gb": round(disk.used / (1024 ** 3), 1),
        "percent": disk.percent,
    }

    # Listening ports
    listening_ports = []
    try:
        for c in psutil.net_connections(kind="inet"):
            if c.status == "LISTEN":
                listening_ports.append({
                    "port": c.laddr.port,
                    "address": c.laddr.ip,
                    "pid": c.pid,
                })
    except (psutil.AccessDenied, PermissionError):
        pass

    # Top 20 processes by memory
    processes = []
    try:
        for proc in sorted(
            psutil.process_iter(["pid", "name", "cmdline", "memory_info", "cpu_percent"]),
            key=lambda p: (p.info.get("memory_info") or type("", (), {"rss": 0})).rss,
            reverse=True,
        )[:20]:
            info = proc.info
            mem_info = info.get("memory_info")
            cmdline = info.get("cmdline") or []
            processes.append({
                "pid": info["pid"],
                "name": info.get("name", ""),
                "cmd": " ".join(cmdline[:5]) if cmdline else "",
                "memory_mb": round(mem_info.rss / (1024 * 1024), 1) if mem_info else 0,
                "cpu_percent": info.get("cpu_percent", 0),
            })
    except (psutil.AccessDenied, PermissionError):
        pass

    # Established TCP connections (limit 50)
    network_connections = []
    try:
        for c in psutil.net_connections(kind="inet"):
            if c.status == "ESTABLISHED" and len(network_connections) < 50:
                network_connections.append({
                    "local": f"{c.laddr.ip}:{c.laddr.port}" if c.laddr else "",
                    "remote": f"{c.raddr.ip}:{c.raddr.port}" if c.raddr else "",
                    "pid": c.pid,
                })
    except (psutil.AccessDenied, PermissionError):
        pass

    # Agent info
    agent_info: Dict[str, Any] = {"agent_id": None, "current_job": None}
    if _agent:
        agent_info["agent_id"] = _agent.config.agent_id
    agent_info["current_job"] = _current_job_id

    return {
        "cpu": cpu_info,
        "memory": memory_info,
        "disk": disk_info,
        "listening_ports": listening_ports,
        "processes": processes,
        "network_connections": network_connections,
        "agent": agent_info,
    }


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


def _get_verification_config() -> Dict[str, Any]:
    """Get verification config from config.extra (same pattern as shell/claude_code)."""
    if _agent is None:
        return {}
    config = _agent.config
    if hasattr(config, "extra") and isinstance(config.extra, dict):
        vc = config.extra.get("verification")
        if isinstance(vc, dict):
            return vc
    return {}


def _is_verification_enabled() -> bool:
    """Check if verification is enabled in the current agent config."""
    return bool(_get_verification_config().get("enabled", False))


def _is_job_completion_freeze(row) -> bool:
    """Check if a job's freeze_data indicates a job completion (not phase boundary).

    Args:
        row: Database row with a 'freeze_data' column, or None.
    """
    if not row or not row.get("freeze_data"):
        return False
    try:
        import json as _json
        fd = row["freeze_data"]
        freeze_data = _json.loads(fd) if isinstance(fd, str) else fd
        freeze_type = freeze_data.get("freeze_type")
        return (
            freeze_type == "job_complete"
            or freeze_data.get("status") == "job_completed"
        )
    except Exception:
        return False


async def _maybe_trigger_verification(
    job_id: str,
    result: Dict[str, Any],
    context: Optional[Dict[str, Any]] = None,
    description: Optional[str] = None,
) -> None:
    """Check if a verification job should be created or resumed for a completed job.

    Called after a job enters reviewing status. Either creates a new critic job
    (first round) or resumes an existing waiting critic (subsequent rounds).

    Guards:
    1. Agent config has verification.enabled = true
    2. The job has no parent_job_id (prevents recursive sub-job spawning)
    3. The job froze with freeze_type = "job_complete" (not phase_boundary)

    Args:
        job_id: The completed job's UUID
        result: Final graph state (should_stop, goal_achieved, etc.)
        context: Job context dict (from job creation). Queried from DB if None.
        description: Original job description. Queried from DB if None.
    """
    logger.info(f"Checking verification trigger for job {job_id} (should_stop={result.get('should_stop')}, error={result.get('error') is not None})")

    # Guard: only trigger on jobs that stopped
    if not result.get("should_stop", False):
        return
    if result.get("error"):
        return

    # Guard: check agent config
    if not _is_verification_enabled():
        logger.warning(
            f"Verification not enabled for job {job_id} — "
            f"agent={_agent is not None}, "
            f"has_extra={hasattr(_agent.config, 'extra') and isinstance(getattr(_agent.config, 'extra', None), dict) if _agent else False}, "
            f"extra_keys={sorted(_agent.config.extra.keys()) if _agent and hasattr(_agent.config, 'extra') and isinstance(_agent.config.extra, dict) else 'N/A'}, "
            f"verification={_agent.config.extra.get('verification') if _agent and hasattr(_agent.config, 'extra') and isinstance(_agent.config.extra, dict) else 'N/A'}"
        )
        return
    verification_config = _get_verification_config()
    logger.info(f"Verification enabled for job {job_id}: {verification_config}")

    # Read freeze_data (and optionally description/context) from DB
    try:
        import json as _json

        freeze_data = None
        project_id = None
        if _agent.postgres_conn:
            row = await _agent.postgres_conn.fetchrow(
                "SELECT freeze_data, project_id, description, context, parent_job_id FROM jobs WHERE id = $1::uuid",
                job_id,
            )

            # Guard: prevent recursive verification — no sub-jobs for sub-jobs
            if row and row.get("parent_job_id") is not None:
                logger.debug(
                    f"Skipping verification for job {job_id} — it is a sub-job "
                    f"(parent_job_id={row['parent_job_id']})"
                )
                return
            if row and row.get("freeze_data"):
                fd = row["freeze_data"]
                freeze_data = _json.loads(fd) if isinstance(fd, str) else fd
                project_id = str(row["project_id"]) if row.get("project_id") else None
                # Fill in description/context from DB if not provided by caller
                if description is None:
                    description = row.get("description", "")
                if context is None:
                    ctx = row.get("context")
                    if ctx:
                        context = _json.loads(ctx) if isinstance(ctx, str) else ctx

        if not freeze_data:
            logger.warning(
                f"No freeze_data found for job {job_id} — cannot trigger verification"
            )
            return

        # Guard: only trigger on job completion, not phase_boundary freezes.
        # Two formats exist depending on autonomy level:
        #   - review/partial: freeze_type="job_complete"
        #   - full: status="job_completed" (no freeze_type field)
        freeze_type = freeze_data.get("freeze_type")
        is_job_completion = (
            freeze_type == "job_complete"
            or freeze_data.get("status") == "job_completed"
        )
        if freeze_type == "phase_boundary" or not is_job_completion:
            logger.debug(
                f"Skipping verification for job {job_id} — "
                f"not a job completion event (freeze_type='{freeze_type}', "
                f"status='{freeze_data.get('status')}')"
            )
            return

        # Check for an existing waiting critic job (subsequent rounds)
        critic_row = await _agent.postgres_conn.fetchrow(
            "SELECT id, status, context FROM jobs WHERE parent_job_id = $1::uuid AND status = 'waiting'",
            job_id,
        )

        if critic_row:
            # Subsequent round: resume existing critic
            critic_id = str(critic_row["id"])
            critic_ctx_raw = critic_row.get("context")
            critic_context = (
                _json.loads(critic_ctx_raw) if isinstance(critic_ctx_raw, str) and critic_ctx_raw
                else (critic_ctx_raw or {})
            )
            new_round = critic_context.get("verification_round", 0) + 1

            # Update critic context with new round + updated deliverables
            critic_context["verification_round"] = new_round
            critic_context["deliverables"] = freeze_data.get("deliverables", [])
            critic_context["summary"] = freeze_data.get("summary", "")
            critic_context["confidence"] = freeze_data.get("confidence", 0)

            await _agent.postgres_conn.execute(
                "UPDATE jobs SET context = $1::jsonb WHERE id = $2::uuid",
                _json.dumps(critic_context), critic_id,
            )

            logger.info(
                f"Resuming existing critic {critic_id} for job {job_id} "
                f"(round {new_round})"
            )

            await _orchestrator_client.resume_job(
                critic_id,
                feedback=(
                    f"Target job addressed your feedback (round {new_round}). "
                    f"Review the updated deliverables and either approve or return with new feedback."
                ),
            )
        else:
            # First round: create new critic job
            critic_config = verification_config.get("critic_config", "critic")
            max_rounds = verification_config.get("max_rounds", 3)
            freeze_data["critic_config"] = critic_config
            config_name = getattr(_agent.config, "agent_id", "unknown")

            logger.info(
                f"Triggering verification for job {job_id} "
                f"(critic_config={critic_config}, max_rounds={max_rounds})"
            )

            create_result = await _orchestrator_client.create_verification_job(
                job_id=job_id,
                description=description,
                freeze_data=freeze_data,
                config_name=config_name,
                project_id=project_id,
                max_rounds=max_rounds,
            )

            if create_result:
                critic_job_id = create_result.get("id", "unknown")
                logger.info(
                    f"Verification job {critic_job_id} created for job {job_id}"
                )
            else:
                logger.error(f"Failed to create verification job for {job_id}")

    except Exception as e:
        # Verification trigger failures should never crash the agent
        logger.error(
            f"Error triggering verification for job {job_id}: {e}",
            exc_info=True,
        )


async def _update_job_status_from_result(job_id: str, result: Dict[str, Any]) -> None:
    """Update job status in PostgreSQL based on graph execution result.

    Determines the appropriate status from the final state:
    - error present → 'failed'
    - should_stop=True, goal_achieved=True, verification enabled → 'reviewing'
    - should_stop=True, goal_achieved=True, verification disabled → no override
      (graph.py already set 'completed' for full autonomy)
    - should_stop=True, goal_achieved=False, critic job → no override
      (handle_transition already set the correct status from freeze_data)
    - should_stop=True, goal_achieved=False → 'pending_review' (frozen for review)
    - Otherwise → leave as 'processing'
    """
    if _agent is None or _agent.postgres_conn is None:
        logger.warning(f"Cannot update job {job_id} status: no database connection")
        return

    try:

        error = result.get("error")
        should_stop = result.get("should_stop", False)
        goal_achieved = result.get("goal_achieved", False)

        if error:
            error_msg = error.get("message", str(error)) if isinstance(error, dict) else str(error)
            await _agent.postgres_conn.jobs.update_status(
                job_id, status="failed", error_message=error_msg
            )
            logger.info(f"Updated job {job_id} status to 'failed'")
        elif should_stop:
            # Check if this is a critic job (has parent_job_id).
            # If so, handle_transition already set the correct status
            # (e.g., 'waiting' for returned verdicts) — don't override.
            is_critic = False
            try:
                row = await _agent.postgres_conn.fetchrow(
                    "SELECT parent_job_id, freeze_data FROM jobs WHERE id = $1::uuid",
                    job_id,
                )
                if row and row.get("parent_job_id") is not None:
                    is_critic = True
            except Exception:
                row = None

            if is_critic:
                logger.info(
                    f"Job {job_id} is a critic job — "
                    f"skipping status override (handle_transition set it)"
                )
            elif goal_achieved or _is_job_completion_freeze(row):
                # Job completion (any autonomy level). If verification is
                # enabled, override to 'reviewing' so the critic handles it.
                logger.debug(
                    f"Job {job_id} completion check: goal_achieved={goal_achieved}, "
                    f"is_job_completion_freeze={_is_job_completion_freeze(row)}, "
                    f"verification_enabled={_is_verification_enabled()}"
                )
                if _is_verification_enabled():
                    await _agent.postgres_conn.jobs.update_status(
                        job_id, status="reviewing"
                    )
                    logger.info(
                        f"Updated job {job_id} status to 'reviewing' "
                        f"(verification enabled)"
                    )
                elif goal_achieved:
                    logger.info(
                        f"Job {job_id} auto-completed (full autonomy, no verification)"
                    )
                else:
                    # Non-full autonomy, no verification — keep pending_review
                    await _agent.postgres_conn.jobs.update_status(
                        job_id, status="pending_review"
                    )
                    logger.info(f"Updated job {job_id} status to 'pending_review'")
            else:
                # Phase boundary freeze or other non-completion stop
                await _agent.postgres_conn.jobs.update_status(
                    job_id, status="pending_review"
                )
                logger.info(
                    f"Updated job {job_id} status to 'pending_review' "
                    f"(not job completion: goal_achieved={goal_achieved}, "
                    f"freeze_check={_is_job_completion_freeze(row)}, "
                    f"has_freeze_data={bool(row.get('freeze_data') if row else False)})"
                )
        else:
            logger.warning(
                f"Job {job_id} ended without should_stop or error — "
                f"leaving status unchanged (goal_achieved={result.get('goal_achieved')})"
            )
    except Exception as e:
        logger.error(f"Failed to update job {job_id} status: {e}")


async def _set_target_to_autonomy_status(target_job_id: str) -> None:
    """Set a target job's status based on its autonomy level.

    Reads the target job's resolved_config to determine autonomy:
    - full → completed
    - all others → pending_review

    Args:
        target_job_id: UUID of the target job
    """
    if _agent is None or _agent.postgres_conn is None:
        return

    try:
        import json as _json

        row = await _agent.postgres_conn.fetchrow(
            "SELECT resolved_config FROM jobs WHERE id = $1::uuid",
            target_job_id,
        )

        autonomy = "review"  # default
        if row and row.get("resolved_config"):
            rc = row["resolved_config"]
            resolved = _json.loads(rc) if isinstance(rc, str) else rc
            autonomy = resolved.get("autonomy", "review")

        if autonomy == "full":
            new_status = "completed"
            await _agent.postgres_conn.execute(
                "UPDATE jobs SET status = $1, completed_at = NOW() WHERE id = $2::uuid",
                new_status, target_job_id,
            )
        else:
            new_status = "pending_review"
            await _agent.postgres_conn.jobs.update_status(
                target_job_id, status=new_status
            )

        logger.info(
            f"Set target job {target_job_id} to '{new_status}' "
            f"(autonomy={autonomy})"
        )

    except Exception as e:
        logger.error(f"Failed to set target job {target_job_id} autonomy status: {e}")


async def _handle_critic_verdict(job_id: str, result: Dict[str, Any]) -> None:
    """Handle the deferred verdict from a critic job after it completes.

    Called after _update_job_status_from_result in _process_orchestrator_job.
    Only applies to critic jobs (those with parent_job_id).

    For approved verdicts: set target to autonomy-dictated status.
    For returned verdicts: resume target with feedback (or fail if round limit reached).

    Args:
        job_id: The critic job's UUID
        result: Final graph state
    """
    if _agent is None or _agent.postgres_conn is None:
        return

    try:
        import json as _json

        row = await _agent.postgres_conn.fetchrow(
            "SELECT parent_job_id, freeze_data, context FROM jobs WHERE id = $1::uuid",
            job_id,
        )
        if not row or not row.get("parent_job_id"):
            return  # Not a subjob

        # Skip scholar jobs — they are not critics
        ctx = row.get("context")
        if ctx:
            ctx_dict = _json.loads(ctx) if isinstance(ctx, str) else ctx
            if isinstance(ctx_dict, dict) and ctx_dict.get("scholar_target"):
                logger.debug(f"Job {job_id} is a scholar — skipping critic verdict handling")
                return

        target_job_id = str(row["parent_job_id"])

        # Parse freeze_data for verdict
        fd = row.get("freeze_data")
        if not fd:
            logger.debug(f"No freeze_data for critic job {job_id} — no verdict to process")
            return
        freeze_data = _json.loads(fd) if isinstance(fd, str) else fd
        verdict = freeze_data.get("verdict")

        if not verdict:
            # Critic completed without using approve_job/return_job_with_feedback.
            # Treat as implicit approval so the target job doesn't get stuck.
            logger.warning(
                f"Critic job {job_id} completed without verdict — "
                f"treating as implicit approval for target {target_job_id}"
            )
            verdict = "approved"

        # Parse critic context for round tracking
        ctx = row.get("context")
        critic_context = _json.loads(ctx) if isinstance(ctx, str) and ctx else (ctx or {})

        if verdict == "approved":
            logger.info(f"Critic {job_id} approved target {target_job_id}")
            await _set_target_to_autonomy_status(target_job_id)

        elif verdict == "returned":
            current_round = critic_context.get("verification_round", 0)
            max_rounds = critic_context.get("max_verification_rounds", 3)

            if current_round >= max_rounds:
                # Round limit reached — auto-accept
                logger.warning(
                    f"Critic {job_id} returned feedback but round limit reached "
                    f"({current_round}/{max_rounds}). Auto-accepting target {target_job_id}."
                )
                await _agent.postgres_conn.jobs.update_status(
                    job_id,
                    status="failed",
                    error_message=f"Verification limit reached ({max_rounds} rounds)",
                )
                await _set_target_to_autonomy_status(target_job_id)
            else:
                # Resume target with feedback
                feedback = freeze_data.get("feedback", "")
                logger.info(
                    f"Critic {job_id} returned feedback for target {target_job_id} "
                    f"(round {current_round}/{max_rounds})"
                )
                await _orchestrator_client.resume_job(target_job_id, feedback=feedback)

        else:
            logger.warning(f"Unknown verdict '{verdict}' for critic job {job_id}")

    except Exception as e:
        logger.error(
            f"Error handling critic verdict for job {job_id}: {e}",
            exc_info=True,
        )


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

        # Reset stop flags for this job
        _clear_stop()

        # Process the job with streaming for iteration logging
        final_state = None
        last_iteration = "?"
        streaming_gen = await _agent.process_job(job_id, metadata, stream=True)
        async for state in streaming_gen:
            final_state = state
            if isinstance(state, dict):
                iteration = state.get("iteration")
                if iteration is not None:
                    last_iteration = iteration
                has_error = state.get("error") is not None
                logger.info(f"[Iteration {last_iteration}] job={job_id} error={has_error}")

            # Cooperative stop check: exit after the current node completes
            if _stop_requested.is_set():
                logger.info(f"Stop requested ({_stop_reason}) for job {job_id} — stopping after current node")
                break

        # Handle cooperative stop (pause or cancel) vs normal completion
        if _stop_requested.is_set():
            reason = _stop_reason
            new_status = "cancelled" if reason == "cancel" else "paused"
            _clear_stop()
            logger.info(f"Job {job_id} stopped gracefully (reason={reason}, status={new_status})")
            try:
                if _agent and _agent.postgres_conn:
                    await _agent.postgres_conn.execute(
                        "UPDATE jobs SET status = $2, assigned_agent_id = NULL WHERE id = $1::uuid",
                        job_id,
                        new_status,
                    )
            except Exception as e:
                logger.error(f"Failed to update job {job_id} to {new_status}: {e}")
            _current_job_id = None
            _stop_completed.set()  # Signal the waiting endpoint
            _cleanup_job_file_handler(job_id)
            return

        result = final_state or {}
        logger.info(f"Orchestrator job {job_id} completed: {result.get('should_stop')}")

        # Mark agent as available BEFORE post-completion handlers.
        # Critic verdict handling calls orchestrator resume, which checks
        # agent status — if _current_job_id is still set, the agent reports
        # "working" and the resume is rejected (race condition).
        _current_job_id = None
        if _orchestrator_client and _orchestrator_client.agent_id:
            await _orchestrator_client.heartbeat(
                status="ready", job_id=None,
                metrics=_get_agent_metrics(),
            )

        # Report completion to orchestrator — it handles status, verification,
        # critic verdicts, curation, and dispatch.
        orchestrator_handled = False
        if _orchestrator_client:
            try:
                orchestrator_handled = await _orchestrator_client.report_completion(
                    job_id, result
                )
            except Exception as e:
                logger.warning(f"Orchestrator completion report failed, falling back: {e}")

        if not orchestrator_handled:
            # Legacy fallback — handle post-completion locally
            logger.info(f"Using legacy post-completion handling for job {job_id}")
            await _update_job_status_from_result(job_id, result)

            # Squash-merge subjob branch into parent (if this is a subjob)
            if _agent and _agent.postgres_conn and _orchestrator_client:
                try:
                    row = await _agent.postgres_conn.fetchrow(
                        "SELECT parent_job_id FROM jobs WHERE id = $1::uuid", job_id
                    )
                    if row and row.get("parent_job_id"):
                        await _orchestrator_client.trigger_subjob_merge(job_id)
                except Exception as e:
                    logger.error(f"Failed to trigger subjob merge for {job_id}: {e}")

            # Handle deferred critic verdicts (approve/return target jobs)
            await _handle_critic_verdict(job_id, result)

            # Check if we should spawn a verification (critic) job
            await _maybe_trigger_verification(job_id, result, context, description)

    except asyncio.CancelledError:
        logger.info(f"Orchestrator job {job_id} was cancelled")
        raise
    except Exception as e:
        logger.error(f"Orchestrator job {job_id} failed: {e}", exc_info=True)
        # Report error to orchestrator, or fall back to local handling
        error_result = {"error": {"message": str(e)}}
        error_handled = False
        if _orchestrator_client:
            try:
                error_handled = await _orchestrator_client.report_completion(
                    job_id, error_result
                )
            except Exception:
                pass
        if not error_handled:
            await _update_job_status_from_result(job_id, error_result)
    finally:
        # Only clear if this job still owns the slot — a new job may
        # have been dispatched while post-completion handlers were running.
        if _current_job_id == job_id:
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
                "postgres": status["connections"].get("postgres", False),
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
    # System Monitoring
    # =========================================================================

    @app.get("/system/info", tags=["Monitoring"])
    async def system_info() -> Dict[str, Any]:
        """Get comprehensive system information for container monitoring.

        Returns CPU, memory, disk usage, listening ports, top processes,
        established network connections, and agent state.
        """
        try:
            return _collect_system_info()
        except ImportError:
            raise HTTPException(
                status_code=501,
                detail="psutil not installed — system monitoring unavailable",
            )
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @app.get("/system/shell-state", tags=["Monitoring"])
    async def shell_state() -> Dict[str, Any]:
        """Get current shell tab state including recent output.

        Returns the list of open terminal tabs with their type and
        recent output lines. Useful for inspecting what the agent is
        doing in its shell sessions.
        """
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
                    recent_output = read_result.get("output", "") if isinstance(read_result, dict) else str(read_result)
                    total_lines = read_result.get("total_lines", 0) if isinstance(read_result, dict) else 0
                except Exception:
                    recent_output = ""
                    total_lines = 0

                tabs.append({
                    "name": name,
                    "type": tab_meta.get("type", "unknown"),
                    "created_at": tab_meta.get("created_at", ""),
                    "total_lines": total_lines,
                    "recent_output": recent_output,
                })

            return {"tabs": tabs}
        except Exception as e:
            logger.error(f"Failed to get shell state: {e}")
            return {"tabs": [], "message": f"Error: {str(e)}"}

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

        # Accept the job — reset stop state
        _current_job_id = request.job_id
        _clear_stop()

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
                config_name=request.config_name,
                config_override=request.config_override,
                git_remote_url=request.git_remote_url,
                datasources=request.datasources,
                repositories=request.repositories,
                branch_name=request.branch_name,
                project_id=request.project_id,
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
            408: {"model": ErrorResponse, "description": "Cancel timed out (hard-killed)"},
        },
    )
    async def cancel_current_job(
        request: JobCancelByOrchestratorRequest,
    ) -> Dict[str, Any]:
        """Cancel the currently running job (cooperative with hard-kill fallback).

        Sets a cooperative flag checked between graph node executions.
        The agent finishes its current node, LangGraph saves the checkpoint,
        then processing stops. If the agent doesn't stop within 120 seconds,
        falls back to a hard cancel (task.cancel()).
        """
        global _current_job_id, _current_job_task

        if _current_job_id is None:
            raise HTTPException(
                status_code=404,
                detail="No job currently running",
            )

        job_id = _current_job_id
        reason = request.reason or "Cancelled by orchestrator"

        # Signal the streaming loop to stop after the current node
        _request_stop("cancel")

        logger.info(f"Cancel requested for job {job_id} — waiting for graceful stop")

        # Wait for cooperative stop (with timeout)
        try:
            await asyncio.wait_for(_stop_completed.wait(), timeout=120.0)
            logger.info(f"Job {job_id} cancelled gracefully: {reason}")
            return {
                "job_id": job_id,
                "status": "cancelled",
                "reason": reason,
                "graceful": True,
            }
        except asyncio.TimeoutError:
            # Cooperative stop failed — fall back to hard kill
            logger.warning(f"Graceful cancel timed out for job {job_id} — hard killing")

            if _current_job_task and not _current_job_task.done():
                _current_job_task.cancel()
                try:
                    await _current_job_task
                except asyncio.CancelledError:
                    pass

            _current_job_id = None
            _current_job_task = None
            _clear_stop()

            return {
                "job_id": job_id,
                "status": "cancelled",
                "reason": f"{reason} (hard-killed after timeout)",
                "graceful": False,
            }

    @app.post(
        "/job/pause",
        tags=["Orchestrator"],
        responses={
            404: {"model": ErrorResponse, "description": "No job running"},
            408: {"model": ErrorResponse, "description": "Pause timed out"},
        },
    )
    async def pause_current_job() -> Dict[str, Any]:
        """Gracefully pause the currently running job.

        Sets a cooperative flag that is checked between graph node executions.
        The agent finishes its current node, LangGraph saves the checkpoint,
        then processing stops and the agent becomes available.

        Waits up to 120 seconds for the job to actually pause.
        """
        if _current_job_id is None:
            raise HTTPException(
                status_code=404,
                detail="No job currently running",
            )

        job_id = _current_job_id

        # Signal the streaming loop to stop after the current node
        _request_stop("pause")

        logger.info(f"Pause requested for job {job_id} — waiting for graceful stop")

        # Wait for the job to actually pause (with timeout)
        try:
            await asyncio.wait_for(_stop_completed.wait(), timeout=120.0)
        except asyncio.TimeoutError:
            # The flag persists, so the job will still pause after the current node
            # finishes — but we can't wait any longer
            logger.warning(f"Pause timed out for job {job_id} — flag still set, will pause after current node")
            raise HTTPException(
                status_code=408,
                detail=f"Pause timed out after 120s. Job {job_id} will pause after current node completes.",
            )

        return {
            "job_id": job_id,
            "status": "paused",
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
        previous_status = request.previous_status

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
            _setup_job_file_logging(request.job_id)
            _clear_stop()
            try:
                # Use streaming for cooperative stop support (pause/cancel)
                final_state = None
                streaming_gen = await _agent.process_job(
                    request.job_id,
                    metadata=resume_metadata if resume_metadata else None,
                    resume=True,
                    feedback=feedback,
                    original_config_name=config_name,
                    previous_status=previous_status,
                    stream=True,
                )
                last_iteration = "?"
                async for state in streaming_gen:
                    final_state = state
                    if isinstance(state, dict):
                        iteration = state.get("iteration")
                        if iteration is not None:
                            last_iteration = iteration
                        logger.info(f"[Resume iteration {last_iteration}] job={request.job_id}")

                    # Cooperative stop check
                    if _stop_requested.is_set():
                        logger.info(f"Stop requested ({_stop_reason}) for resumed job {request.job_id}")
                        break

                # Handle cooperative stop vs normal completion
                if _stop_requested.is_set():
                    reason = _stop_reason
                    new_status = "cancelled" if reason == "cancel" else "paused"
                    _clear_stop()
                    logger.info(f"Resumed job {request.job_id} stopped gracefully (status={new_status})")
                    try:
                        if _agent and _agent.postgres_conn:
                            await _agent.postgres_conn.execute(
                                "UPDATE jobs SET status = $2, assigned_agent_id = NULL WHERE id = $1::uuid",
                                request.job_id,
                                new_status,
                            )
                    except Exception as e:
                        logger.error(f"Failed to update resumed job {request.job_id} to {new_status}: {e}")
                    _current_job_id = None
                    _stop_completed.set()
                    _cleanup_job_file_handler(request.job_id)
                    return

                result = final_state or {}
                logger.info(f"Resumed job {request.job_id} completed: {result.get('should_stop')}")

                # Mark agent as available BEFORE post-completion handlers
                # (same fix as _process_orchestrator_job — see comment there)
                _current_job_id = None
                if _orchestrator_client and _orchestrator_client.agent_id:
                    await _orchestrator_client.heartbeat(
                        status="ready", job_id=None,
                        metrics=_get_agent_metrics(),
                    )

                # Report completion to orchestrator
                orchestrator_handled = False
                if _orchestrator_client:
                    try:
                        orchestrator_handled = await _orchestrator_client.report_completion(
                            request.job_id, result
                        )
                    except Exception as e:
                        logger.warning(f"Orchestrator completion report failed, falling back: {e}")

                if not orchestrator_handled:
                    # Legacy fallback
                    logger.info(f"Using legacy post-completion handling for resumed job {request.job_id}")
                    await _update_job_status_from_result(request.job_id, result or {})
                    await _handle_critic_verdict(request.job_id, result or {})
                    await _maybe_trigger_verification(request.job_id, result or {})
            except asyncio.CancelledError:
                logger.info(f"Resumed job {request.job_id} was cancelled")
                raise
            except Exception as e:
                logger.error(f"Resumed job {request.job_id} failed: {e}", exc_info=True)
                error_result = {"error": {"message": str(e)}}
                error_handled = False
                if _orchestrator_client:
                    try:
                        error_handled = await _orchestrator_client.report_completion(
                            request.job_id, error_result
                        )
                    except Exception:
                        pass
                if not error_handled:
                    await _update_job_status_from_result(request.job_id, error_result)
            finally:
                # Only clear if this job still owns the slot — a new job may
                # have been dispatched while post-completion handlers were running.
                if _current_job_id == request.job_id:
                    _current_job_id = None
                _cleanup_job_file_handler(request.job_id)

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
