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
from fastapi import Body, FastAPI, HTTPException, Query, Request, Response  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import JSONResponse, StreamingResponse  # noqa: E402

from pydantic import BaseModel, Field  # noqa: E402

from database import PostgresDB, MongoDB, ALLOWED_TABLES, FilterCategory  # noqa: E402
from security.auth import (  # noqa: E402
    create_session,
    validate_session,
    delete_session,
    cleanup_expired_sessions,
)
from security.csrf import validate_csrf_token  # noqa: E402
from services.workspace import workspace_service  # noqa: E402
from services.gitea import GiteaClient  # noqa: E402
from services.builder_tools import (  # noqa: E402
    BUILDER_TOOLS,
    SERVER_SIDE_TOOLS,
    WORKSPACE_EDIT_TOOLS,
    build_message_context,
    build_summarization_prompt,
    get_builder_api_key,
    get_builder_base_url,
    get_builder_model,
    get_builder_provider,
)
from services.builder_search import tavily_search  # noqa: E402
from services.builder_prompt import build_system_prompt  # noqa: E402
from services.builder_dispatch import execute_server_tool as _dispatch_server_tool  # noqa: E402
import httpx  # noqa: E402
from graph_routes import router as graph_router, set_mongodb  # noqa: E402
from uploads import router as uploads_router  # noqa: E402

logger = logging.getLogger(__name__)

# =============================================================================
# Database Instances (singleton pattern)
# =============================================================================

postgres_db = PostgresDB()
mongodb = MongoDB()
gitea_client = GiteaClient()


async def resolve_job_repo(job_id: str) -> tuple[str, str | None]:
    """Resolve the Gitea repo name and branch for a job.

    Project jobs use a shared project jobs repo + per-job branch.
    Non-project jobs use a dedicated per-job repo (job-{id}).

    Returns:
        (repo_name, job_branch) where job_branch is None for non-project jobs.
    """
    job = await postgres_db.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

    if job.get("project_id"):
        repos = await postgres_db.get_project_repositories(
            str(job["project_id"]), role="jobs"
        )
        if repos:
            return repos[0]["name"], job.get("branch_name")

    return f"job-{job_id}", None


# =============================================================================
# Background Tasks
# =============================================================================

# Flag to signal shutdown to background tasks
_shutdown_event: asyncio.Event | None = None

# Auto-assignment toggle (env var, default true)
AUTO_ASSIGN_ENABLED = os.environ.get("AUTO_ASSIGN_ENABLED", "true").lower() in ("true", "1", "yes")

# Dispatcher lock prevents concurrent dispatch (double-assignment)
_dispatch_lock = asyncio.Lock()

# Track jobs with pending pause requests (prevent re-preemption)
_pause_pending_job_ids: set[str] = set()


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
# Job Auto-Assignment Dispatcher
# =============================================================================


async def _dispatch_job_to_agent(job: dict, agent: dict) -> bool:
    """Start a new job on an agent. Returns True on success.

    Extracted from assign_job_to_agent() endpoint. Handles datasource resolution,
    config overrides, HTTP POST to agent pod, and status updates.
    """
    import httpx

    job_id = str(job["id"])
    agent_id = str(agent["id"])

    if not agent.get("pod_ip"):
        logger.warning(f"Agent {agent_id} has no pod IP — skipping dispatch")
        return False

    try:
        # Extract upload IDs from context if present
        job_context = job.get("context") or {}
        if isinstance(job_context, str):
            job_context = json.loads(job_context)
        upload_id = job_context.get("upload_id")
        config_upload_id = job_context.get("config_upload_id")
        instructions_upload_id = job_context.get("instructions_upload_id")
        instructions = job_context.get("instructions")
        git_remote_url = job_context.get("git_remote_url")

        # Parse config_override if stored as string
        config_override = job.get("config_override")
        if isinstance(config_override, str):
            config_override = json.loads(config_override)

        # Build remaining context (fields not extracted as dedicated params)
        extracted_keys = {"upload_id", "config_upload_id", "instructions_upload_id", "instructions", "git_remote_url"}
        remaining_context = {k: v for k, v in job_context.items() if k not in extracted_keys}

        # Resolve project repositories if this is a project job
        repositories_payload = None
        if job.get("project_id"):
            try:
                repos = await postgres_db.get_project_repositories(str(job["project_id"]))
                repositories_payload = [
                    {
                        "id": str(r["id"]),
                        "name": r["name"],
                        "role": r["role"],
                        "repo_url": r.get("repo_url"),
                        "read_only": r["read_only"],
                        "branch": r.get("branch", "main"),
                        "clone_path": r.get("clone_path"),
                        "credentials": r.get("credentials"),
                    }
                    for r in repos
                ]
            except Exception as e:
                logger.warning(f"Dispatch: failed to resolve project repos for job {job_id}: {e}")

            # Derive git_remote_url from jobs repo if not already set
            if repositories_payload and not git_remote_url:
                jobs_repo = next((r for r in repositories_payload if r["role"] == "jobs"), None)
                if jobs_repo and jobs_repo.get("repo_url"):
                    git_remote_url = jobs_repo["repo_url"]

        # Resolve datasources for this job (job > project > global)
        resolved_ds = await postgres_db.resolve_datasources_for_job(
            job_id, project_id=str(job["project_id"]) if job.get("project_id") else None
        )
        datasources_payload = _build_datasources_payload(resolved_ds)

        # Apply datasource-driven tool override (inject/strip db tool categories)
        if resolved_ds:
            config_override = _build_datasource_tool_override(resolved_ds, config_override)

        # Build job start request
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
            repositories=repositories_payload,
            branch_name=job.get("branch_name"),
            project_id=str(job["project_id"]) if job.get("project_id") else None,
        )

        # Send to agent pod
        agent_url = f"http://{agent['pod_ip']}:{agent['pod_port']}/job/start"
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                agent_url,
                json=job_start.model_dump(exclude_none=True),
            )

        if response.status_code not in (200, 202):
            logger.warning(f"Dispatch: agent {agent_id} rejected job {job_id}: {response.text}")
            return False

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

        logger.info(f"Dispatch: assigned job {job_id} (priority={job.get('priority', '?')}) to agent {agent_id}")
        return True

    except Exception as e:
        logger.error(f"Dispatch: failed to assign job {job_id} to agent {agent_id}: {e}")
        return False


async def _resume_job_on_agent(job: dict, agent: dict) -> bool:
    """Resume a paused job on an agent. Returns True on success."""
    import httpx

    job_id = str(job["id"])
    agent_id = str(agent["id"])

    if not agent.get("pod_ip"):
        logger.warning(f"Agent {agent_id} has no pod IP — skipping resume dispatch")
        return False

    try:
        # Re-resolve datasources in case they changed
        resolved_ds = await postgres_db.resolve_datasources_for_job(
            job_id, project_id=str(job["project_id"]) if job.get("project_id") else None
        )
        datasources_payload = _build_datasources_payload(resolved_ds)

        config_override = job.get("config_override")
        if isinstance(config_override, str):
            config_override = json.loads(config_override)
        if resolved_ds:
            config_override = _build_datasource_tool_override(resolved_ds, config_override)

        resume_payload = {
            "job_id": job_id,
            "config_name": job.get("config_name", "default"),
            "config_override": config_override,
            "datasources": datasources_payload,
            "previous_status": job.get("status"),
        }

        agent_url = f"http://{agent['pod_ip']}:{agent['pod_port']}/job/resume"
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                agent_url,
                json={k: v for k, v in resume_payload.items() if v is not None},
            )

        if response.status_code not in (200, 202):
            logger.warning(f"Dispatch: agent {agent_id} rejected resume for job {job_id}: {response.text}")
            return False

        # Update job status and assign to agent
        await postgres_db.update_job_status(
            job_id=job_id,
            status="processing",
            assigned_agent_id=agent_id,
        )

        await postgres_db.heartbeat(
            agent_id=agent_id,
            status="working",
            current_job_id=job_id,
        )

        logger.info(f"Dispatch: resumed job {job_id} (priority={job.get('priority', '?')}) on agent {agent_id}")
        return True

    except Exception as e:
        logger.error(f"Dispatch: failed to resume job {job_id} on agent {agent_id}: {e}")
        return False


async def _initiate_pause(job: dict) -> None:
    """Request graceful pause of a running job. Non-blocking (fire-and-forget).

    The agent will finish its current node, save checkpoint, and become available.
    The actual dispatch of the high-priority job happens on the next dispatcher cycle.
    """
    import httpx

    job_id = str(job["id"])
    agent_id = str(job.get("assigned_agent_id", ""))

    if not job.get("pod_ip"):
        logger.warning(f"Preempt: no pod IP for job {job_id} agent — cannot pause")
        return

    try:
        agent_url = f"http://{job['pod_ip']}:{job['pod_port']}/job/pause"
        async with httpx.AsyncClient(timeout=130.0) as client:
            response = await client.post(agent_url)

        if response.status_code in (200, 408):
            # 200 = paused, 408 = timed out but flag set (will pause after current node)
            logger.info(f"Preempt: pause request sent for job {job_id} on agent {agent_id}")
            # DB update handled by agent + orchestrator fallback
            await postgres_db.pause_job(job_id)
        else:
            logger.warning(f"Preempt: agent returned {response.status_code} for pause of job {job_id}")

    except Exception as e:
        logger.warning(f"Preempt: failed to pause job {job_id}: {e}")
    finally:
        _pause_pending_job_ids.discard(job_id)


async def _try_dispatch_pending_jobs() -> None:
    """Core dispatcher: match pending jobs to available agents.

    Phase 1: Direct assignment (free agents → highest priority pending jobs)
    Phase 2: Preemption (remaining high-priority jobs → lowest-priority running jobs)
    """
    if not AUTO_ASSIGN_ENABLED:
        return

    async with _dispatch_lock:
        try:
            # Get pending jobs (created + paused, priority ordered)
            pending_jobs = await postgres_db.get_dispatchable_jobs(limit=50)
            if not pending_jobs:
                return

            # Get available agents (ready, cooldown passed)
            available_agents = await postgres_db.get_available_agents(limit=50)

            # Phase 1: Direct assignment
            matched_job_ids = set()
            matched_agent_ids = set()

            agents_iter = iter(available_agents)
            for job in pending_jobs:
                agent = next(agents_iter, None)
                if agent is None:
                    break  # No more free agents

                job_id = str(job["id"])
                if job["status"] == "paused":
                    success = await _resume_job_on_agent(job, agent)
                else:
                    success = await _dispatch_job_to_agent(job, agent)

                if success:
                    matched_job_ids.add(job_id)
                    matched_agent_ids.add(str(agent["id"]))

            # Phase 2: Preemption (non-blocking)
            remaining = [j for j in pending_jobs if str(j["id"]) not in matched_job_ids]
            if not remaining:
                return

            candidates = await postgres_db.get_preemption_candidates()
            if not candidates:
                return

            for pending_job in remaining:
                pending_priority = pending_job.get("priority", 5)
                pending_job_id = str(pending_job["id"])

                # Find lowest-priority running job that can be preempted
                for candidate in candidates:
                    candidate_id = str(candidate["id"])
                    candidate_priority = candidate.get("priority", 5)

                    # Only preempt if strictly higher priority
                    if pending_priority <= candidate_priority:
                        continue

                    # Skip if already being paused
                    if candidate_id in _pause_pending_job_ids:
                        continue

                    # Skip if already matched (agent taken)
                    if str(candidate.get("assigned_agent_id", "")) in matched_agent_ids:
                        continue

                    # Initiate preemption (fire-and-forget)
                    _pause_pending_job_ids.add(candidate_id)
                    asyncio.create_task(_initiate_pause(candidate))
                    logger.info(
                        f"Preempt: pausing job {candidate_id} (priority={candidate_priority}) "
                        f"for pending job {pending_job_id} (priority={pending_priority})"
                    )
                    # Remove this candidate so it's not preempted again in this cycle
                    candidates.remove(candidate)
                    break  # One preemption per pending job per cycle

        except Exception as e:
            logger.error(f"Dispatcher error: {e}", exc_info=True)


async def auto_assign_dispatcher(shutdown_event: asyncio.Event) -> None:
    """Background task that periodically dispatches pending jobs to available agents.

    Runs every 30 seconds as a catch-all. Event-driven triggers (job creation,
    agent heartbeat) also call _try_dispatch_pending_jobs() for faster response.
    """
    logger.info("Auto-assign dispatcher started (enabled=%s)", AUTO_ASSIGN_ENABLED)
    while not shutdown_event.is_set():
        try:
            await _try_dispatch_pending_jobs()
        except Exception as e:
            logger.error(f"Error in auto-assign dispatcher: {e}")

        # Wait 30 seconds or until shutdown
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=30.0)
            break
        except asyncio.TimeoutError:
            pass

    logger.info("Auto-assign dispatcher stopped")


def _trigger_dispatch() -> None:
    """Fire-and-forget trigger for the dispatcher. Safe to call from any endpoint."""
    if AUTO_ASSIGN_ENABLED:
        asyncio.create_task(_try_dispatch_pending_jobs())


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
    kickoff_message: str | None = Field(None, description="Opening message to the agent (task brief)")
    datasource_ids: list[str] | None = Field(None, description="Global datasource IDs to clone as job-scoped")
    builder_session_id: str | None = Field(None, description="Builder session ID to link to this job")
    user_id: str | None = Field(None, description="User UUID who created this job")
    project_id: str | None = Field(None, description="Project UUID to associate this job with")
    parent_job_id: str | None = Field(None, description="Parent job UUID for verification/follow-up jobs")
    priority: int = Field(5, ge=0, le=10, description="Job priority (0=low, 5=normal, 10=high)")


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
    repositories: list[dict[str, Any]] | None = Field(
        default=None,
        description="Project repositories for workspace setup",
    )
    branch_name: str | None = Field(
        default=None,
        description="Git branch name for this job's workspace",
    )
    project_id: str | None = Field(
        default=None,
        description="Project ID for datasource resolution",
    )


class BuilderSessionCreate(BaseModel):
    """Request body for creating a builder session."""

    expert_id: str | None = Field(None, description="Expert used as starting point")
    user_id: str | None = Field(None, description="User UUID who created this session")


class UserCreate(BaseModel):
    """Request body for creating a user."""

    display_name: str = Field(..., description="Display name")
    avatar_color: str = Field("#89b4fa", description="Hex color for avatar")
    email: str | None = Field(None, description="Email address")


class UserUpdate(BaseModel):
    """Request body for updating a user."""

    display_name: str | None = None
    avatar_color: str | None = None
    email: str | None = None


class LoginRequest(BaseModel):
    """Request body for email login."""

    email: str = Field(..., description="Email address")


class ProjectCreate(BaseModel):
    """Request body for creating a project."""

    name: str = Field(..., description="Project name")
    description: str | None = Field(None, description="Project description")
    goal: str | None = Field(None, description="Project goal statement")
    default_config_name: str | None = Field(None, description="Default agent config for new jobs")
    default_config_override: dict[str, Any] | None = Field(None, description="Default config overrides")
    user_id: str = Field(..., description="Owner user UUID")


class ProjectUpdate(BaseModel):
    """Request body for updating a project."""

    name: str | None = None
    description: str | None = None
    goal: str | None = None
    status: str | None = None
    default_config_name: str | None = None
    default_config_override: dict[str, Any] | None = None


class ProjectMemberAdd(BaseModel):
    """Request body for adding a project member."""

    user_id: str = Field(..., description="User UUID to add")
    role: str = Field("editor", description="Member role: owner, editor, viewer")


class ProjectMemberUpdate(BaseModel):
    """Request body for updating a project member's role."""

    role: str = Field(..., description="New role: owner, editor, viewer")


class ProjectRepositoryCreate(BaseModel):
    """Request body for attaching a repository to a project."""

    name: str = Field(..., description="Repository display name")
    description: str | None = Field(None, description="Repository description")
    repo_url: str | None = Field(None, description="Repository URL (external repos)")
    role: str = Field("source", description="Repository role: jobs, source, reference")
    read_only: bool = Field(False, description="Whether this repo is read-only")
    branch: str = Field("main", description="Default branch")
    clone_path: str | None = Field(None, description="Local clone path")
    create_managed: bool = Field(False, description="Create a managed Gitea repo")


class ProjectRepositoryUpdate(BaseModel):
    """Request body for updating a project repository."""

    name: str | None = None
    description: str | None = None
    read_only: bool | None = None
    branch: str | None = None
    clone_path: str | None = None


class MergeRequest(BaseModel):
    """Request body for merging a job's branch."""

    merge_strategy: str = Field(
        default="merge",
        description="Merge method: merge, rebase, or squash",
    )
    delete_branch: bool = Field(
        default=False,
        description="Delete the branch after successful merge",
    )


class PromoteRequest(BaseModel):
    """Request body for promoting a job into a dedicated project."""

    name: str = Field(..., description="Name for the new project")
    description: str | None = Field(None, description="Project description")
    goal: str | None = Field(None, description="Project goal")
    user_id: str = Field(..., description="User UUID who owns the new project")


class BuilderMessageRequest(BaseModel):
    """Request body for sending a message to the builder."""

    message: str = Field(..., description="User's message text")
    model: str | None = Field(None, description="Builder model override")
    instructions: str | None = Field(None, description="Current instructions content")
    config: dict[str, Any] | None = Field(None, description="Current config override")
    description: str | None = Field(None, description="Current job description")
    active_job_id: str | None = Field(None, description="Active job context for inspection tools")


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
    session_cleanup_task = asyncio.create_task(cleanup_expired_sessions(postgres_db, _shutdown_event))
    dispatcher_task = asyncio.create_task(auto_assign_dispatcher(_shutdown_event))

    yield

    # Signal shutdown to background tasks
    _shutdown_event.set()
    await stale_detector_task
    await session_cleanup_task
    await dispatcher_task

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

# CSRF middleware — validates X-CSRF-Token header on mutating requests
@app.middleware("http")
async def csrf_middleware(request: Request, call_next):
    """Validate CSRF token on mutating requests (double-submit cookie pattern)."""
    is_valid = await validate_csrf_token(request)
    if not is_valid:
        return JSONResponse(status_code=403, content={"detail": "CSRF validation failed"})
    return await call_next(request)


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
    user_id: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict[str, Any]]:
    """List jobs with optional status and user filter.

    Returns jobs enriched with audit_count from MongoDB if available.
    """
    try:
        jobs = await postgres_db.get_jobs(status=status, user_id=user_id, limit=limit)

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
        if job.kickoff_message:
            context["kickoff_message"] = job.kickoff_message

        # Resolve project_id: use provided, or fall back to user's default
        project_id = job.project_id
        if not project_id and job.user_id:
            try:
                user = await postgres_db.get_user(job.user_id)
                if user and user.get("default_project_id"):
                    project_id = str(user["default_project_id"])
            except Exception as e:
                logger.warning(f"Failed to resolve default project for user {job.user_id}: {e}")

        result = await postgres_db.create_job(
            description=job.description,
            document_path=job.document_path,
            document_dir=job.document_dir,
            config_name=job.config_name,
            config_override=job.config_override,
            context=context if context else None,
            user_id=job.user_id,
            project_id=project_id,
            parent_job_id=job.parent_job_id,
            priority=job.priority,
        )

        # Create Gitea repo/branch for workspace delivery
        job_id_str = str(result["id"])
        if gitea_client.is_initialized:
            if project_id and job.parent_job_id:
                # Subjob of a project job: branch from parent's branch
                repos = await postgres_db.get_project_repositories(project_id, role="jobs")
                if repos:
                    jobs_repo = repos[0]
                    from_branch = "main"
                    parent = await postgres_db.get_job(job.parent_job_id)
                    if parent and parent.get("branch_name"):
                        from_branch = parent["branch_name"]
                    else:
                        logger.warning(
                            f"Parent job {job.parent_job_id} has no branch_name, "
                            f"branching subjob from 'main'"
                        )
                    branch_name = f"job/{job_id_str[:8]}"
                    await gitea_client.create_branch(
                        jobs_repo["name"], branch_name, from_branch=from_branch
                    )
                    ctx = dict(context) if context else {}
                    ctx["git_remote_url"] = jobs_repo["repo_url"]
                    await postgres_db.update_job_context(job_id_str, ctx)
                    async with postgres_db.acquire() as conn:
                        await conn.execute(
                            "UPDATE jobs SET branch_name = $1 WHERE id = $2",
                            branch_name, result["id"],
                        )
                    result["branch_name"] = branch_name
            elif project_id:
                # Root project job via /api/jobs: branch from main
                repos = await postgres_db.get_project_repositories(project_id, role="jobs")
                if repos:
                    jobs_repo = repos[0]
                    branch_name = f"job/{job_id_str[:8]}"
                    await gitea_client.create_branch(jobs_repo["name"], branch_name)
                    ctx = dict(context) if context else {}
                    ctx["git_remote_url"] = jobs_repo["repo_url"]
                    await postgres_db.update_job_context(job_id_str, ctx)
                    async with postgres_db.acquire() as conn:
                        await conn.execute(
                            "UPDATE jobs SET branch_name = $1 WHERE id = $2",
                            branch_name, result["id"],
                        )
                    result["branch_name"] = branch_name
            else:
                # Non-project job: standalone repo
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

        # Trigger auto-assignment dispatcher (fire-and-forget)
        _trigger_dispatch()

        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.delete("/api/jobs/{job_id}")
async def delete_job(job_id: str) -> dict[str, str]:
    """Delete a job and its requirements."""
    try:
        # Look up the job before deletion for branch cleanup
        job = await postgres_db.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

        # Clean up Gitea branch for project jobs
        if job.get("project_id") and job.get("branch_name") and gitea_client.is_initialized:
            repos = await postgres_db.get_project_repositories(
                str(job["project_id"]), role="jobs"
            )
            if repos:
                await gitea_client.delete_branch(repos[0]["name"], job["branch_name"])

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
    import httpx

    try:
        # First get the job to check if it's assigned to an agent
        job = await postgres_db.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

        # If job is assigned to an agent, send cancel request to agent pod
        assigned_agent_id = job.get("assigned_agent_id")
        if assigned_agent_id:
            agent = await postgres_db.get_agent(str(assigned_agent_id))
            if agent and agent.get("pod_ip") and agent["status"] not in ("offline",):
                agent_url = f"http://{agent['pod_ip']}:{agent['pod_port']}/job/cancel"
                try:
                    # 130s timeout: agent tries cooperative stop for up to 120s,
                    # then falls back to hard kill. We wait slightly longer.
                    async with httpx.AsyncClient(timeout=130.0) as client:
                        response = await client.post(
                            agent_url,
                            json={"reason": "Cancelled via cockpit"},
                        )
                        if response.status_code == 200:
                            resp_data = response.json()
                            if resp_data.get("graceful", True):
                                logger.info(f"Agent confirmed graceful cancel for job {job_id}")
                            else:
                                logger.warning(f"Agent hard-killed job {job_id} after cooperative timeout")
                        elif response.status_code == 408:
                            logger.warning(f"Agent cancel timed out for job {job_id} — may still stop after current node")
                        else:
                            logger.warning(
                                f"Agent cancel returned {response.status_code}: {response.text}"
                            )
                except Exception as e:
                    # Agent might be unreachable — still cancel in DB
                    logger.warning(f"Could not reach agent to cancel job {job_id}: {e}")

        success = await postgres_db.cancel_job(job_id)
        if not success:
            raise HTTPException(
                status_code=400,
                detail="Job cannot be cancelled (already completed or cancelled)",
            )
        # Agent is being freed — trigger dispatcher for queued jobs
        _trigger_dispatch()

        return {"status": "cancelled"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.put("/api/jobs/{job_id}/pause")
async def pause_job(job_id: str) -> dict[str, str]:
    """Pause a running job.

    If the job is assigned to an agent, sends a graceful pause request
    to the agent pod. The agent finishes its current graph node, saves
    the checkpoint, and becomes available for new work.

    The paused job re-enters the dispatch queue and will be auto-resumed
    when an agent becomes available.
    """
    import httpx

    try:
        job = await postgres_db.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

        if job["status"] != "processing":
            raise HTTPException(
                status_code=400,
                detail=f"Job cannot be paused (status: {job['status']})",
            )

        # Send pause request to agent pod
        assigned_agent_id = job.get("assigned_agent_id")
        if assigned_agent_id:
            agent = await postgres_db.get_agent(str(assigned_agent_id))
            if agent and agent.get("pod_ip") and agent["status"] not in ("offline",):
                agent_url = f"http://{agent['pod_ip']}:{agent['pod_port']}/job/pause"
                try:
                    async with httpx.AsyncClient(timeout=130.0) as client:
                        response = await client.post(agent_url)
                        if response.status_code == 200:
                            logger.info(f"Agent confirmed pause for job {job_id}")
                        elif response.status_code == 408:
                            # Pause timed out but flag is set — agent will pause after current node
                            logger.warning(f"Pause timed out for job {job_id} — will pause after current node")
                        else:
                            logger.warning(f"Agent pause returned {response.status_code}: {response.text}")
                except httpx.TimeoutException:
                    logger.warning(f"Timeout sending pause to agent for job {job_id}")
                except Exception as e:
                    logger.warning(f"Could not reach agent to pause job {job_id}: {e}")

        # Update DB — the agent also does this, but we ensure it here as fallback
        success = await postgres_db.pause_job(job_id)
        if not success:
            raise HTTPException(
                status_code=400,
                detail="Job cannot be paused (status may have changed)",
            )
        return {"status": "paused", "job_id": job_id}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


class JobResumeRequest(BaseModel):
    """Request body for resuming a failed or paused job."""

    feedback: str | None = Field(None, description="Optional feedback to inject before resuming")
    agent_id: str | None = Field(None, description="Override agent ID if original is offline")


@app.post("/api/jobs/{job_id}/resume")
async def resume_job(job_id: str, request: JobResumeRequest | None = None) -> dict[str, str]:
    """Resume a failed or paused job from its checkpoint.

    This endpoint:
    1. Validates the job exists and is not 'completed'
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

        # Allow resuming jobs in any status except completed
        # This handles cancelled jobs (user wants to retry) and cases where
        # agents disappear without marking jobs as failed
        if job["status"] == "completed":
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
            "previous_status": job["status"],
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

        if job["status"] not in ("pending_review", "reviewing"):
            raise HTTPException(
                status_code=400,
                detail=f"Job cannot be approved (status: {job['status']}). "
                       f"Only jobs in 'pending_review' or 'reviewing' status can be approved.",
            )

        # 2. Read freeze data — DB first, Gitea fallback, local fallback
        frozen_data = None

        # Primary: read freeze_data from DB
        if job.get("freeze_data"):
            frozen_data = job["freeze_data"]
            if isinstance(frozen_data, str):
                frozen_data = json.loads(frozen_data)

        # Fallback: Gitea (backward compat for pre-migration jobs)
        if frozen_data is None and gitea_client.is_initialized:
            # Resolve correct repo name (project jobs use shared repo)
            if job.get("project_id"):
                repos = await postgres_db.get_project_repositories(
                    str(job["project_id"]), role="jobs"
                )
                repo_name = repos[0]["name"] if repos else f"job-{job_id}"
                job_branch = job.get("branch_name")
            else:
                repo_name = f"job-{job_id}"
                job_branch = None
            frozen_data = await gitea_client.get_file(
                repo_name, "output/job_frozen.json", ref=job_branch
            )

        # Fallback: local workspace
        if frozen_data is None:
            workspace_path = workspace_service.base_path / f"job_{job_id}" / "output" / "job_frozen.json"
            if workspace_path.exists():
                frozen_data = json.loads(workspace_path.read_text())
            else:
                raise HTTPException(
                    status_code=404,
                    detail=f"No freeze data found for job '{job_id}' "
                           f"(checked DB, Gitea repo, and local workspace)",
                )

        # 3. Determine freeze type (backward compat: missing = job_complete)
        freeze_type = frozen_data.get("freeze_type", "job_complete")

        if freeze_type == "phase_boundary":
            # Phase boundary freeze: approve to continue execution (not complete)
            # Remove job_frozen.json from local workspace
            local_frozen = workspace_service.base_path / f"job_{job_id}" / "output" / "job_frozen.json"
            if local_frozen.exists():
                local_frozen.unlink()

            # Update DB: status → processing, clear freeze_data
            async with postgres_db.acquire() as conn:
                await conn.execute(
                    "UPDATE jobs SET status = 'processing', freeze_data = NULL, "
                    "updated_at = CURRENT_TIMESTAMP WHERE id = $1::uuid",
                    job_id,
                )

            logger.info(f"Job {job_id} phase boundary approved (resume execution)")

            return {
                "status": "approved_continue",
                "job_id": job_id,
                "freeze_type": freeze_type,
                "phase_type": frozen_data.get("phase_type"),
                "phase_number": frozen_data.get("phase_number"),
            }

        # job_complete freeze (or backward compat): mark as truly completed
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

        # 5. Update DB: status → completed, clear freeze_data, set completed_at
        async with postgres_db.acquire() as conn:
            await conn.execute(
                "UPDATE jobs SET status = 'completed', freeze_data = NULL, "
                "completed_at = CURRENT_TIMESTAMP, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = $1::uuid",
                job_id,
            )

        logger.info(f"Job {job_id} approved (gitea={wrote_to_gitea})")

        # Agent is freed after completion — trigger dispatcher
        _trigger_dispatch()

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
        frozen_data = None

        # Primary: read freeze_data from DB
        job = await postgres_db.get_job(job_id)
        if job and job.get("freeze_data"):
            frozen_data = job["freeze_data"]
            if isinstance(frozen_data, str):
                frozen_data = json.loads(frozen_data)

        # Fallback: Gitea (backward compat for pre-migration jobs)
        if frozen_data is None and gitea_client.is_initialized:
            if job and job.get("project_id"):
                repos = await postgres_db.get_project_repositories(
                    str(job["project_id"]), role="jobs"
                )
                repo_name = repos[0]["name"] if repos else f"job-{job_id}"
                job_branch = job.get("branch_name") if job else None
            else:
                repo_name = f"job-{job_id}"
                job_branch = None
            frozen_data = await gitea_client.get_file(
                repo_name, "output/job_frozen.json", ref=job_branch
            )

        # Fallback: local workspace
        if frozen_data is None:
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
    ref: str | None = Query(default=None, description="Branch, tag, or commit SHA"),
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

    repo_name, job_branch = await resolve_job_repo(job_id)
    contents = await gitea_client.list_contents(repo_name, path, ref=ref or job_branch)

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
    ref: str | None = Query(default=None, description="Branch, tag, or commit SHA"),
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

    repo_name, job_branch = await resolve_job_repo(job_id)
    content = await gitea_client.get_file_content(repo_name, path, ref=ref or job_branch)

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


@app.get("/api/jobs/{job_id}/repo/commits")
async def list_repo_commits(
    job_id: str,
    sha: str = Query(default="main", description="Branch, tag, or commit SHA to list from"),
    since_ref: str | None = Query(default=None, description="Only show commits after this ref"),
    page: int = Query(default=1, ge=1, description="Page number"),
    limit: int = Query(default=20, ge=1, le=100, description="Max commits per page"),
) -> dict[str, Any]:
    """List git commits for a job's repository.

    If since_ref is provided, returns only commits between since_ref and sha
    using git compare. Otherwise lists commits from sha.

    Returns:
        Dict with commits list and total count
    """
    if not gitea_client.is_initialized:
        raise HTTPException(status_code=503, detail="Gitea not available")

    repo_name, job_branch = await resolve_job_repo(job_id)
    effective_sha = sha if sha != "main" else (job_branch or sha)

    if since_ref:
        # Use compare to get commits between two refs
        compare = await gitea_client.get_compare(repo_name, since_ref, effective_sha)
        if compare is None:
            raise HTTPException(
                status_code=404,
                detail=f"Could not compare {since_ref}...{effective_sha} in repo for job '{job_id}'",
            )
        return compare
    else:
        commits = await gitea_client.get_commits(repo_name, sha=effective_sha, page=page, limit=limit)
        if commits is None:
            raise HTTPException(
                status_code=404,
                detail=f"No commits found in repo for job '{job_id}'",
            )
        return {"total_commits": len(commits), "commits": commits}


@app.get("/api/jobs/{job_id}/repo/diff")
async def get_repo_diff(
    job_id: str,
    base: str = Query(..., description="Base ref (commit SHA, tag, or branch)"),
    head: str = Query(default="HEAD", description="Head ref"),
) -> dict[str, str]:
    """Get unified diff between two refs in a job's repository.

    Returns:
        Dict with base, head, and diff text
    """
    if not gitea_client.is_initialized:
        raise HTTPException(status_code=503, detail="Gitea not available")

    repo_name, _job_branch = await resolve_job_repo(job_id)
    diff_text = await gitea_client.get_diff(repo_name, base, head)

    if diff_text is None:
        raise HTTPException(
            status_code=404,
            detail=f"Could not get diff {base}...{head} in repo for job '{job_id}'",
        )

    return {"base": base, "head": head, "diff": diff_text}


@app.get("/api/jobs/{job_id}/repo/tags")
async def list_repo_tags(job_id: str, all_jobs: bool = False) -> list[dict[str, Any]]:
    """List tags in a job's repository.

    By default, only returns tags for the specified job (namespaced by
    job short ID prefix). Set all_jobs=True to return all tags in the repo.

    Returns:
        List of tags with name, sha, and message
    """
    if not gitea_client.is_initialized:
        raise HTTPException(status_code=503, detail="Gitea not available")

    repo_name, _job_branch = await resolve_job_repo(job_id)
    tags = await gitea_client.get_tags(repo_name)

    if tags is None:
        raise HTTPException(
            status_code=404,
            detail=f"No tags found in repo for job '{job_id}'",
        )

    # Filter to this job's tags unless all_jobs requested
    if not all_jobs:
        short_id = job_id[:8]
        tags = [t for t in tags if t["name"].startswith(f"{short_id}-")]

    return tags


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


@app.get("/api/jobs/{job_id}/workspace/{path:path}")
async def get_workspace_file(job_id: str, path: str) -> dict[str, str]:
    """Get content of a workspace file by relative path.

    Supports any file within the job workspace, including subdirectories
    (e.g., "archive/phase_1_retrospective.md"). Path is sandboxed.

    Args:
        job_id: Job UUID
        path: Relative path within the workspace

    Returns:
        Dict with path and file content
    """
    content = workspace_service.get_workspace_file(job_id, path)
    if content is None:
        raise HTTPException(
            status_code=404,
            detail=f"File '{path}' not found in workspace for job '{job_id}'",
        )
    return {"path": path, "content": content}


@app.put("/api/jobs/{job_id}/workspace/{path:path}")
async def write_workspace_file(job_id: str, path: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Write content to a workspace file.

    Sandboxed — directory traversal is blocked, and certain paths
    (todos.yaml, .git/, tools/) are not editable.

    Args:
        job_id: Job UUID
        path: Relative path within the workspace
        body: {"content": "...", "commit_message": "..."}
    """
    content = body.get("content")
    if content is None:
        raise HTTPException(status_code=400, detail="Missing 'content' in request body")

    blocked = workspace_service.is_path_blocked(path)
    if blocked:
        raise HTTPException(status_code=403, detail=blocked)

    result = workspace_service.write_workspace_file(
        job_id=job_id,
        path=path,
        content=content,
        commit_message=body.get("commit_message"),
    )
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


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
    """Manually assign a job to an agent.

    Validates job and agent status, then delegates to the shared dispatch helper.
    Accepts jobs in 'created', 'failed', or 'paused' status.
    """
    try:
        job = await postgres_db.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

        if job["status"] not in ("created", "failed", "paused"):
            raise HTTPException(
                status_code=400,
                detail=f"Job cannot be assigned (status: {job['status']})",
            )

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

        # Use resume path for paused jobs, start path for new/failed
        if job["status"] == "paused":
            success = await _resume_job_on_agent(job, agent)
        else:
            success = await _dispatch_job_to_agent(job, agent)

        if not success:
            raise HTTPException(
                status_code=502,
                detail="Failed to dispatch job to agent",
            )

        return {"status": "assigned", "agent_id": agent_id, "job_id": job_id}

    except HTTPException:
        raise
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
# Citation & Source Library Endpoints
# =============================================================================


@app.get("/api/sources")
async def list_sources(
    job_id: str | None = Query(default=None, description="Filter by job ID"),
    type: str | None = Query(default=None, description="Filter by source type"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """List sources, optionally filtered by job and/or type.

    When job_id is provided, returns sources linked to that job via job_sources.
    When omitted, returns all sources across jobs.
    """
    try:
        async with postgres_db.acquire() as conn:
            conditions = []
            params: list[Any] = []
            idx = 1

            if job_id:
                conditions.append(f"s.id IN (SELECT source_id FROM job_sources WHERE job_id = ${idx}::uuid)")
                params.append(job_id)
                idx += 1
            if type:
                conditions.append(f"s.type::text = ${idx}")
                params.append(type)
                idx += 1

            where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

            # Count total
            count_row = await conn.fetchrow(
                f"SELECT COUNT(*) as total FROM sources s {where}", *params
            )
            total = count_row["total"] if count_row else 0

            # Fetch sources
            params.append(limit)
            params.append(offset)
            rows = await conn.fetch(
                f"""SELECT s.id, s.type::text as type, s.identifier, s.name,
                       s.version, s.content_hash,
                       LEFT(s.content, 200) as content_preview,
                       s.metadata, s.created_at
                FROM sources s {where}
                ORDER BY s.created_at DESC
                LIMIT ${idx} OFFSET ${idx + 1}""",
                *params,
            )

            sources = [dict(r) for r in rows]

            # If querying across jobs, include job IDs for each source
            if not job_id:
                for src in sources:
                    job_rows = await conn.fetch(
                        "SELECT job_id FROM job_sources WHERE source_id = $1",
                        src["id"],
                    )
                    src["job_ids"] = [str(r["job_id"]) for r in job_rows]

            return {"sources": sources, "total": total}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/sources/{source_id}")
async def get_source_detail(
    source_id: int,
    content_limit: int = Query(default=2000, ge=0, le=100000),
) -> dict[str, Any]:
    """Get full detail for a single source."""
    try:
        async with postgres_db.acquire() as conn:
            if content_limit > 0:
                row = await conn.fetchrow(
                    """SELECT id, type::text as type, identifier, name, version,
                          LEFT(content, $2) as content, content_hash, metadata, created_at,
                          LENGTH(content) as full_content_length
                    FROM sources WHERE id = $1""",
                    source_id, content_limit,
                )
            else:
                row = await conn.fetchrow(
                    """SELECT id, type::text as type, identifier, name, version,
                          content, content_hash, metadata, created_at,
                          LENGTH(content) as full_content_length
                    FROM sources WHERE id = $1""",
                    source_id,
                )

            if not row:
                raise HTTPException(status_code=404, detail=f"Source {source_id} not found")

            result = dict(row)
            result["content_truncated"] = (
                content_limit > 0 and result.get("full_content_length", 0) > content_limit
            )

            # Include linked job IDs
            job_rows = await conn.fetch(
                "SELECT job_id FROM job_sources WHERE source_id = $1", source_id
            )
            result["job_ids"] = [str(r["job_id"]) for r in job_rows]

            return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/jobs/{job_id}/citations")
async def list_job_citations(
    job_id: str,
    source_id: int | None = Query(default=None),
    status: str | None = Query(default=None, description="Filter by verification_status"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """List citations for a job with optional filters."""
    try:
        async with postgres_db.acquire() as conn:
            conditions = ["c.job_id = $1::uuid"]
            params: list[Any] = [job_id]
            idx = 2

            if source_id is not None:
                conditions.append(f"c.source_id = ${idx}")
                params.append(source_id)
                idx += 1
            if status:
                conditions.append(f"c.verification_status::text = ${idx}")
                params.append(status)
                idx += 1

            where = "WHERE " + " AND ".join(conditions)

            count_row = await conn.fetchrow(
                f"SELECT COUNT(*) as total FROM citations c {where}", *params
            )
            total = count_row["total"] if count_row else 0

            params.append(limit)
            params.append(offset)
            rows = await conn.fetch(
                f"""SELECT c.id, LEFT(c.claim, 200) as claim, c.source_id,
                       s.name as source_name, s.type::text as source_type,
                       c.verification_status::text as verification_status,
                       c.confidence::text as confidence,
                       c.extraction_method::text as extraction_method,
                       c.similarity_score, c.created_at
                FROM citations c
                JOIN sources s ON c.source_id = s.id
                {where}
                ORDER BY c.created_at DESC
                LIMIT ${idx} OFFSET ${idx + 1}""",
                *params,
            )

            return {"citations": [dict(r) for r in rows], "total": total}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/citations/{citation_id}")
async def get_citation_detail(citation_id: int) -> dict[str, Any]:
    """Get full citation record with source info and verification details."""
    try:
        async with postgres_db.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT c.id, c.job_id, c.claim, c.verbatim_quote, c.quote_context,
                      c.quote_language, c.relevance_reasoning,
                      c.confidence::text as confidence,
                      c.extraction_method::text as extraction_method,
                      c.source_id, s.name as source_name, s.type::text as source_type,
                      s.identifier as source_identifier,
                      c.locator, c.verification_status::text as verification_status,
                      c.verification_notes, c.similarity_score, c.matched_location,
                      c.created_at, c.created_by
                FROM citations c
                JOIN sources s ON c.source_id = s.id
                WHERE c.id = $1""",
                citation_id,
            )

            if not row:
                raise HTTPException(status_code=404, detail=f"Citation {citation_id} not found")

            return dict(row)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/jobs/{job_id}/sources/{source_id}/annotations")
async def get_source_annotations(
    job_id: str,
    source_id: int,
    type: str | None = Query(default=None, description="Filter by annotation_type"),
) -> list[dict[str, Any]]:
    """Get annotations for a source within a job."""
    try:
        async with postgres_db.acquire() as conn:
            if type:
                rows = await conn.fetch(
                    """SELECT id, annotation_type, content, page_reference, created_at, created_by
                    FROM source_annotations
                    WHERE source_id = $1 AND job_id = $2::uuid AND annotation_type = $3
                    ORDER BY created_at""",
                    source_id, job_id, type,
                )
            else:
                rows = await conn.fetch(
                    """SELECT id, annotation_type, content, page_reference, created_at, created_by
                    FROM source_annotations
                    WHERE source_id = $1 AND job_id = $2::uuid
                    ORDER BY created_at""",
                    source_id, job_id,
                )

            return [dict(r) for r in rows]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/jobs/{job_id}/sources/{source_id}/tags")
async def get_source_tags(job_id: str, source_id: int) -> list[str]:
    """Get tags for a source within a job."""
    try:
        async with postgres_db.acquire() as conn:
            rows = await conn.fetch(
                "SELECT tag FROM source_tags WHERE source_id = $1 AND job_id = $2::uuid ORDER BY tag",
                source_id, job_id,
            )
            return [r["tag"] for r in rows]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/jobs/{job_id}/citations/stats")
async def get_citation_stats(job_id: str) -> dict[str, Any]:
    """Get citation statistics for a job."""
    try:
        async with postgres_db.acquire() as conn:
            # Sources by type
            source_rows = await conn.fetch(
                """SELECT s.type::text as type, COUNT(*) as count
                FROM sources s
                JOIN job_sources js ON s.id = js.source_id
                WHERE js.job_id = $1::uuid
                GROUP BY s.type""",
                job_id,
            )
            sources_by_type = {r["type"]: r["count"] for r in source_rows}
            total_sources = sum(sources_by_type.values())

            # Citations by verification status
            status_rows = await conn.fetch(
                """SELECT verification_status::text as status, COUNT(*) as count
                FROM citations WHERE job_id = $1::uuid
                GROUP BY verification_status""",
                job_id,
            )
            by_status = {r["status"]: r["count"] for r in status_rows}

            # Citations by confidence
            conf_rows = await conn.fetch(
                """SELECT confidence::text as confidence, COUNT(*) as count
                FROM citations WHERE job_id = $1::uuid
                GROUP BY confidence""",
                job_id,
            )
            by_confidence = {r["confidence"]: r["count"] for r in conf_rows}

            # Citations by extraction method
            method_rows = await conn.fetch(
                """SELECT extraction_method::text as method, COUNT(*) as count
                FROM citations WHERE job_id = $1::uuid
                GROUP BY extraction_method""",
                job_id,
            )
            by_method = {r["method"]: r["count"] for r in method_rows}

            total_citations = sum(by_status.values())

            return {
                "job_id": job_id,
                "total_sources": total_sources,
                "sources_by_type": sources_by_type,
                "total_citations": total_citations,
                "citations_by_verification_status": by_status,
                "citations_by_confidence": by_confidence,
                "citations_by_extraction_method": by_method,
            }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/jobs/{job_id}/sources/search")
async def search_job_sources(
    job_id: str,
    query: str = Query(..., description="Search query"),
    mode: str = Query(default="keyword", description="Search mode: keyword, semantic, hybrid"),
    source_type: str | None = Query(default=None),
    tags: str | None = Query(default=None, description="Comma-separated tags (AND logic)"),
    top_k: int = Query(default=10, ge=1, le=50),
) -> dict[str, Any]:
    """Search a job's source library using keyword search.

    Falls back to SQL keyword search. Semantic/hybrid modes require
    the CitationEngine with pgvector.
    """
    try:
        async with postgres_db.acquire() as conn:
            # Build conditions for source filtering
            conditions = ["js.job_id = $1::uuid"]
            params: list[Any] = [job_id]
            idx = 2

            if source_type:
                conditions.append(f"s.type::text = ${idx}")
                params.append(source_type)
                idx += 1

            # Tag filtering: find sources that have ALL specified tags
            if tags:
                tag_list = [t.strip() for t in tags.split(",") if t.strip()]
                for tag in tag_list:
                    conditions.append(
                        f"EXISTS (SELECT 1 FROM source_tags st "
                        f"WHERE st.source_id = s.id AND st.job_id = $1::uuid AND st.tag = ${idx})"
                    )
                    params.append(tag)
                    idx += 1

            where = "WHERE " + " AND ".join(conditions)

            # Keyword search using PostgreSQL full-text search
            params.append(query)
            query_param_idx = idx
            idx += 1
            params.append(top_k)

            rows = await conn.fetch(
                f"""SELECT s.id, s.name, s.type::text as type, s.identifier,
                       ts_rank(to_tsvector('simple', s.content),
                               plainto_tsquery('simple', ${query_param_idx})) as rank,
                       ts_headline('simple', s.content,
                                   plainto_tsquery('simple', ${query_param_idx}),
                                   'MaxFragments=2,MaxWords=60,MinWords=20') as snippet
                FROM sources s
                JOIN job_sources js ON s.id = js.source_id
                {where}
                  AND to_tsvector('simple', s.content) @@ plainto_tsquery('simple', ${query_param_idx})
                ORDER BY rank DESC
                LIMIT ${idx}""",
                *params,
            )

            results = []
            for r in rows:
                rank = float(r["rank"]) if r["rank"] else 0.0
                if rank > 0.1:
                    evidence = "HIGH"
                elif rank > 0.01:
                    evidence = "MEDIUM"
                else:
                    evidence = "LOW"

                results.append({
                    "source_id": r["id"],
                    "source_name": r["name"],
                    "source_type": r["type"],
                    "identifier": r["identifier"],
                    "evidence_label": evidence,
                    "rank": rank,
                    "snippet": r["snippet"],
                })

            return {
                "job_id": job_id,
                "query": query,
                "mode": "keyword",
                "results": results,
                "total": len(results),
            }
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
        result = await postgres_db.heartbeat(
            agent_id=agent_id,
            status=heartbeat.status,
            current_job_id=heartbeat.current_job_id,
            metrics=heartbeat.metrics,
        )
        if result is None:
            raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")

        # If agent transitioned to ready, trigger the dispatcher
        # (will be wired up in the dispatcher task)
        prev_status = result.get("previous_status")
        if prev_status and prev_status != heartbeat.status and heartbeat.status == "ready":
            logger.info(f"Agent {agent_id} transitioned {prev_status} → ready")
            _trigger_dispatch()

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


@app.get("/api/agents/{agent_id}/system-info")
async def get_agent_system_info(agent_id: str) -> dict[str, Any]:
    """Proxy system info request to an agent's /system/info endpoint.

    Returns CPU, memory, disk, processes, listening ports, and network
    connections from the agent's container.
    """
    import httpx

    try:
        agent = await postgres_db.get_agent(agent_id)
        if not agent:
            raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")

        if agent["status"] == "offline":
            raise HTTPException(status_code=400, detail="Agent is offline")

        pod_ip = agent.get("pod_ip")
        if not pod_ip:
            raise HTTPException(status_code=400, detail="Agent has no pod IP configured")

        pod_port = agent.get("pod_port", 8001)
        agent_url = f"http://{pod_ip}:{pod_port}/system/info"

        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(agent_url)

        if response.status_code != 200:
            raise HTTPException(
                status_code=502,
                detail=f"Agent returned {response.status_code}: {response.text}",
            )

        return response.json()

    except HTTPException:
        raise
    except httpx.RequestError as e:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to connect to agent: {str(e)}",
        ) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/jobs/{job_id}/logs")
async def get_job_logs(
    job_id: str,
    lines: int = Query(default=100, ge=1, le=1000),
    grep: str | None = Query(default=None),
    level: str | None = Query(default=None),
) -> dict[str, Any]:
    """Read the tail of a job's log file with optional filtering.

    Args:
        job_id: Job UUID
        lines: Number of tail lines to return (1-1000, default 100)
        grep: Case-insensitive substring filter
        level: Log level filter (DEBUG, INFO, WARNING, ERROR)
    """
    import re

    # Validate job_id format
    try:
        UUID(job_id)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid job_id format: {job_id}")

    log_path = workspace_service.base_path / "logs" / f"job_{job_id}.log"
    if not log_path.exists():
        raise HTTPException(status_code=404, detail=f"Log file not found for job {job_id}")

    try:
        all_lines = log_path.read_text().splitlines()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read log file: {e}")

    filtered = False

    # Level filter: match lines starting with timestamp pattern followed by level
    if level:
        level_upper = level.upper()
        if level_upper not in ("DEBUG", "INFO", "WARNING", "ERROR"):
            raise HTTPException(status_code=400, detail=f"Invalid level: {level}. Must be DEBUG, INFO, WARNING, or ERROR")
        pattern = re.compile(rf"^\d{{4}}-\d{{2}}-\d{{2}}\s+\d{{2}}:\d{{2}}:\d{{2}}\s+-\s+\S+\s+-\s+{level_upper}\s+-")
        all_lines = [l for l in all_lines if pattern.match(l)]
        filtered = True

    # Grep filter
    if grep:
        grep_lower = grep.lower()
        all_lines = [l for l in all_lines if grep_lower in l.lower()]
        filtered = True

    total_lines = len(all_lines)

    # Tail N lines
    tail_lines = all_lines[-lines:]

    return {
        "job_id": job_id,
        "lines": tail_lines,
        "total_lines": total_lines,
        "filtered": filtered,
        "log_path": str(log_path),
    }


@app.get("/api/jobs/{job_id}/llm-requests")
async def get_job_llm_requests(
    job_id: str,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """List LLM requests for a job with summary fields.

    Returns model, timestamp, token usage, tool call names, and iteration
    for each request. Use the _id with GET /api/requests/{doc_id} to get
    the full request/response.
    """
    if not mongodb.is_available:
        raise HTTPException(status_code=503, detail="MongoDB not available")

    try:
        UUID(job_id)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid job_id format: {job_id}")

    try:
        data = await mongodb.list_llm_requests(job_id, limit=limit, offset=offset)
        return data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/jobs/{job_id}/shell-state")
async def get_job_shell_state(job_id: str) -> dict[str, Any]:
    """Proxy shell state request to the agent processing a job.

    Resolves job -> assigned agent -> pod IP, then proxies to
    the agent's GET /system/shell-state endpoint.
    """
    import httpx as _httpx

    try:
        job = await postgres_db.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

        if job.get("status") != "processing":
            raise HTTPException(
                status_code=400,
                detail=f"Job is not processing (status: {job.get('status')})"
            )

        assigned_agent_id = job.get("assigned_agent_id")
        if not assigned_agent_id:
            raise HTTPException(status_code=400, detail="Job has no assigned agent")

        agent = await postgres_db.get_agent(str(assigned_agent_id))
        if not agent:
            raise HTTPException(status_code=404, detail="Assigned agent not found")

        pod_ip = agent.get("pod_ip")
        if not pod_ip:
            raise HTTPException(status_code=400, detail="Agent has no pod IP configured")

        pod_port = agent.get("pod_port", 8001)
        agent_url = f"http://{pod_ip}:{pod_port}/system/shell-state"

        async with _httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(agent_url)

        if response.status_code != 200:
            raise HTTPException(
                status_code=502,
                detail=f"Agent returned {response.status_code}: {response.text}",
            )

        return response.json()

    except HTTPException:
        raise
    except _httpx.RequestError as e:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to connect to agent: {str(e)}",
        ) from e
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
# Project Expert Endpoints
# =============================================================================


async def _get_project_jobs_repo(project_id: str) -> str | None:
    """Get the jobs repo name for a project. Returns None if not found."""
    repos = await postgres_db.get_project_repositories(project_id, role="jobs")
    if not repos:
        return None
    return repos[0].get("name")


@app.get("/api/projects/{project_id}/experts")
async def list_project_experts(project_id: str) -> list[dict[str, Any]]:
    """List expert configurations from a project's jobs repo.

    Scans the experts/ directory in the project's Gitea jobs repo and returns
    metadata for each expert configuration found.
    """
    if not gitea_client.is_initialized:
        return []

    repo_name = await _get_project_jobs_repo(project_id)
    if not repo_name:
        return []

    try:
        entries = await gitea_client.list_contents(repo_name, "experts")
    except Exception:
        return []

    if not entries:
        return []

    experts: list[dict[str, Any]] = []
    for entry in entries:
        if entry.get("type") != "dir":
            continue
        name = entry.get("name", "")
        try:
            content = await gitea_client.get_file_content(
                repo_name, f"experts/{name}/config.yaml"
            )
            if not content:
                continue
            data = yaml.safe_load(content) or {}

            description = data.get("description", "").strip()
            if not description:
                tools = data.get("tools", {})
                tool_categories = [k for k in tools if tools[k]]
                description = (
                    f"Agent with {', '.join(tool_categories)} tools."
                    if tool_categories
                    else "Custom agent configuration."
                )

            experts.append(
                ExpertInfo(
                    id=name,
                    display_name=data.get(
                        "display_name", name.replace("_", " ").title()
                    ),
                    description=description,
                    icon=data.get("icon", "psychology"),
                    color=data.get("color", "#cba6f7"),
                    tags=data.get("tags", []),
                ).model_dump()
            )
        except Exception as e:
            logger.warning(f"Failed to parse project expert {name}: {e}")

    return experts


@app.get("/api/projects/{project_id}/experts/{expert_name}")
async def get_project_expert(
    project_id: str, expert_name: str
) -> dict[str, Any]:
    """Get full detail for a project expert including merged config and instructions."""
    if not gitea_client.is_initialized:
        raise HTTPException(status_code=503, detail="Gitea not available")

    repo_name = await _get_project_jobs_repo(project_id)
    if not repo_name:
        raise HTTPException(status_code=404, detail="No jobs repo for project")

    # Read config
    config_content = await gitea_client.get_file_content(
        repo_name, f"experts/{expert_name}/config.yaml"
    )
    if not config_content:
        raise HTTPException(
            status_code=404, detail=f"Expert not found: {expert_name}"
        )

    try:
        expert_data = yaml.safe_load(config_content) or {}
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Invalid YAML: {e}"
        ) from e

    # Build info
    description = expert_data.get("description", "").strip()
    if not description:
        tools = expert_data.get("tools", {})
        tool_categories = [k for k in tools if tools[k]]
        description = (
            f"Agent with {', '.join(tool_categories)} tools."
            if tool_categories
            else "Custom agent configuration."
        )

    info = ExpertInfo(
        id=expert_name,
        display_name=expert_data.get(
            "display_name", expert_name.replace("_", " ").title()
        ),
        description=description,
        icon=expert_data.get("icon", "psychology"),
        color=expert_data.get("color", "#cba6f7"),
        tags=expert_data.get("tags", []),
    )

    # Merge with defaults
    config_dir = _get_config_dir()
    defaults_path = config_dir / "defaults.yaml"
    if defaults_path.exists():
        with open(defaults_path) as f:
            defaults = yaml.safe_load(f) or {}
    else:
        defaults = {}

    expert_data_clean = dict(expert_data)
    expert_data_clean.pop("$extends", None)
    merged = _deep_merge(defaults, expert_data_clean)
    for key in ("$extends", "connections"):
        merged.pop(key, None)

    # Read instructions
    instructions_content = await gitea_client.get_file_content(
        repo_name, f"experts/{expert_name}/instructions.md"
    )

    return {
        **info.model_dump(),
        "config": merged,
        "instructions": instructions_content,
    }


# =============================================================================
# Auth Endpoints
# =============================================================================


@app.post("/api/auth/login")
async def auth_login(body: LoginRequest, response: Response) -> dict[str, Any]:
    """Email-based login. Finds or creates user, issues session cookie."""
    email = body.email.strip().lower()
    if not email:
        raise HTTPException(status_code=400, detail="Email is required")

    # Find existing user by email
    user = await postgres_db.get_user_by_email(email)

    if not user:
        # Create new user from email
        display_name = email.split("@")[0].title()
        user = await postgres_db.create_user(
            display_name=display_name,
            email=email,
        )
        logger.info(f"Created new user via login: {email}")

    # Create session
    session_key, csrf_token = await create_session(
        postgres_db,
        user_id=str(user["id"]),
        user_email=email,
    )

    # Session timeout from env or default 24h
    session_timeout_hours = int(os.getenv("SESSION_TIMEOUT_HOURS", "24"))
    max_age = session_timeout_hours * 3600

    # Set httpOnly session cookie
    response.set_cookie(
        key="session",
        value=session_key,
        max_age=max_age,
        httponly=True,
        secure=False,  # False for localhost dev
        samesite="lax",
        path="/",
    )

    # Set CSRF token cookie (readable by JS for double-submit pattern)
    response.set_cookie(
        key="csrf_token",
        value=csrf_token,
        max_age=max_age,
        httponly=False,
        secure=False,
        samesite="lax",
        path="/",
    )

    return {
        "user": {
            "id": str(user["id"]),
            "display_name": user["display_name"],
            "avatar_color": user["avatar_color"],
            "email": user.get("email"),
            "created_at": user["created_at"],
        },
        "message": "Login successful",
    }


@app.post("/api/auth/logout")
async def auth_logout(request: Request, response: Response) -> dict[str, str]:
    """Logout: invalidate session and clear cookies."""
    session_key = request.cookies.get("session")
    if session_key:
        await delete_session(postgres_db, session_key)

    response.delete_cookie(key="session", path="/", httponly=True, samesite="lax")
    response.delete_cookie(key="csrf_token", path="/", httponly=False, samesite="lax")
    return {"message": "Logged out successfully"}


@app.get("/api/auth/me")
async def auth_me(request: Request, response: Response) -> dict[str, Any]:
    """Get current authenticated user from session cookie."""
    session_key = request.cookies.get("session")
    if not session_key:
        raise HTTPException(status_code=401, detail="Not authenticated")

    session_info = await validate_session(postgres_db, session_key)
    if not session_info:
        raise HTTPException(status_code=401, detail="Invalid or expired session")

    # Fetch full user record
    user = await postgres_db.get_user(session_info["user_id"])
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    # Ensure CSRF cookie is set
    csrf_cookie = request.cookies.get("csrf_token")
    csrf_token = session_info.get("csrf_token")
    if csrf_token and not csrf_cookie:
        expires_in = session_info.get("expires_in", 86400)
        response.set_cookie(
            key="csrf_token",
            value=csrf_token,
            max_age=expires_in,
            httponly=False,
            secure=False,
            samesite="lax",
            path="/",
        )

    return {
        "user": {
            "id": str(user["id"]),
            "display_name": user["display_name"],
            "avatar_color": user["avatar_color"],
            "email": user.get("email"),
            "created_at": user["created_at"],
        },
    }


# =============================================================================
# User Endpoints
# =============================================================================


@app.get("/api/users")
async def list_users() -> list[dict[str, Any]]:
    """List all users."""
    try:
        return await postgres_db.list_users()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/users/{user_id}")
async def get_user(user_id: str) -> dict[str, Any]:
    """Get a single user by ID."""
    user = await postgres_db.get_user(user_id)
    if not user:
        raise HTTPException(status_code=404, detail=f"User '{user_id}' not found")
    return user


@app.post("/api/users")
async def create_user(body: UserCreate) -> dict[str, Any]:
    """Create a new user with a default project."""
    try:
        user = await postgres_db.create_user(
            display_name=body.display_name,
            avatar_color=body.avatar_color,
            email=body.email,
        )

        # Create default project for the new user
        try:
            project = await postgres_db.create_default_project_for_user(
                str(user["id"]), body.display_name
            )
            # Create Gitea jobs repo for the project
            if gitea_client.is_initialized and project:
                repo_name = f"project-{str(project['id'])[:8]}-jobs"
                repo_url = await gitea_client.create_repo(repo_name)
                if repo_url:
                    await postgres_db.add_project_repository(
                        project_id=str(project["id"]),
                        name=repo_name,
                        repo_url=repo_url,
                        role="jobs",
                        is_managed=True,
                    )
        except Exception as e:
            logger.warning(f"Failed to create default project for user {user['id']}: {e}")

        # Re-fetch user to include default_project_id
        return await postgres_db.get_user(str(user["id"])) or user
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.put("/api/users/{user_id}")
async def update_user(user_id: str, body: UserUpdate) -> dict[str, str]:
    """Update a user."""
    success = await postgres_db.update_user(
        user_id=user_id,
        display_name=body.display_name,
        avatar_color=body.avatar_color,
        email=body.email,
    )
    if not success:
        raise HTTPException(status_code=404, detail=f"User '{user_id}' not found")
    return {"status": "updated"}


@app.delete("/api/users/{user_id}")
async def delete_user(user_id: str) -> dict[str, str]:
    """Delete a user."""
    success = await postgres_db.delete_user(user_id)
    if not success:
        raise HTTPException(status_code=404, detail=f"User '{user_id}' not found")
    return {"status": "deleted"}


# =============================================================================
# Project Endpoints
# =============================================================================


@app.post("/api/projects")
async def create_project(body: ProjectCreate) -> dict[str, Any]:
    """Create a new project with the requesting user as owner."""
    try:
        project = await postgres_db.create_project(
            name=body.name,
            description=body.description,
            goal=body.goal,
            default_config_name=body.default_config_name,
            default_config_override=body.default_config_override,
        )

        # Add creator as owner
        await postgres_db.add_project_member(
            project_id=str(project["id"]),
            user_id=body.user_id,
            role="owner",
        )

        # Create Gitea jobs repo
        if gitea_client.is_initialized:
            repo_name = f"project-{str(project['id'])[:8]}-jobs"
            repo_url = await gitea_client.create_repo(repo_name)
            if repo_url:
                await postgres_db.add_project_repository(
                    project_id=str(project["id"]),
                    name=repo_name,
                    repo_url=repo_url,
                    role="jobs",
                    is_managed=True,
                )

        return project
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/projects")
async def list_projects(
    user_id: str | None = Query(default=None),
) -> list[dict[str, Any]]:
    """List projects. If user_id provided, returns projects the user is a member of."""
    try:
        if user_id:
            return await postgres_db.get_projects_for_user(user_id)
        # Without user_id, return all projects (admin view)
        async with postgres_db.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM projects WHERE status != 'deleted' ORDER BY updated_at DESC LIMIT 100"
            )
            return [dict(r) for r in rows]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/projects/{project_id}")
async def get_project(project_id: str) -> dict[str, Any]:
    """Get a single project by ID."""
    project = await postgres_db.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")
    return project


@app.patch("/api/projects/{project_id}")
async def update_project(project_id: str, body: ProjectUpdate) -> dict[str, str]:
    """Update a project."""
    kwargs = {k: v for k, v in body.model_dump().items() if v is not None}
    if not kwargs:
        raise HTTPException(status_code=400, detail="No fields to update")
    success = await postgres_db.update_project(project_id, **kwargs)
    if not success:
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")
    return {"status": "updated"}


@app.delete("/api/projects/{project_id}")
async def delete_project(project_id: str) -> dict[str, str]:
    """Delete a project. Cannot delete default projects."""
    project = await postgres_db.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")
    if project.get("is_default"):
        raise HTTPException(status_code=400, detail="Cannot delete a default project")

    # Clean up managed repos
    repos = await postgres_db.get_project_repositories(project_id)
    for repo in repos:
        if repo.get("is_managed") and gitea_client.is_initialized:
            await gitea_client.delete_repo(repo["name"])

    success = await postgres_db.delete_project(project_id)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to delete project")
    return {"status": "deleted"}


@app.get("/api/projects/{project_id}/members")
async def list_project_members(project_id: str) -> list[dict[str, Any]]:
    """List members of a project with user info."""
    project = await postgres_db.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")
    return await postgres_db.get_project_members(project_id)


@app.post("/api/projects/{project_id}/members")
async def add_project_member(project_id: str, body: ProjectMemberAdd) -> dict[str, Any]:
    """Add a member to a project."""
    project = await postgres_db.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")
    try:
        return await postgres_db.add_project_member(
            project_id=project_id,
            user_id=body.user_id,
            role=body.role,
        )
    except Exception as e:
        if "duplicate key" in str(e).lower() or "unique" in str(e).lower():
            raise HTTPException(status_code=409, detail="User is already a member")
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.patch("/api/projects/{project_id}/members/{user_id}")
async def update_project_member(
    project_id: str, user_id: str, body: ProjectMemberUpdate
) -> dict[str, str]:
    """Update a member's role in a project."""
    success = await postgres_db.update_project_member_role(
        project_id=project_id, user_id=user_id, role=body.role
    )
    if not success:
        raise HTTPException(status_code=404, detail="Member not found")
    return {"status": "updated"}


@app.delete("/api/projects/{project_id}/members/{user_id}")
async def remove_project_member(project_id: str, user_id: str) -> dict[str, str]:
    """Remove a member from a project. Cannot remove the last owner."""
    # Check if this is the last owner
    role = await postgres_db.get_user_role_in_project(project_id, user_id)
    if role == "owner":
        members = await postgres_db.get_project_members(project_id)
        owner_count = sum(1 for m in members if m.get("role") == "owner")
        if owner_count <= 1:
            raise HTTPException(
                status_code=400, detail="Cannot remove the last owner of a project"
            )

    success = await postgres_db.remove_project_member(project_id, user_id)
    if not success:
        raise HTTPException(status_code=404, detail="Member not found")
    return {"status": "removed"}


@app.get("/api/projects/{project_id}/repositories")
async def list_project_repositories(
    project_id: str,
    role: str | None = Query(default=None),
) -> list[dict[str, Any]]:
    """List repositories attached to a project."""
    project = await postgres_db.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")
    return await postgres_db.get_project_repositories(project_id, role=role)


@app.post("/api/projects/{project_id}/repositories")
async def add_project_repository(
    project_id: str, body: ProjectRepositoryCreate
) -> dict[str, Any]:
    """Attach a repository to a project. Optionally creates a managed Gitea repo."""
    project = await postgres_db.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")

    repo_url = body.repo_url
    is_managed = False

    # Create a managed Gitea repo if requested
    if body.create_managed and gitea_client.is_initialized:
        repo_url = await gitea_client.create_repo(body.name)
        if not repo_url:
            raise HTTPException(status_code=502, detail="Failed to create Gitea repository")
        is_managed = True

    try:
        return await postgres_db.add_project_repository(
            project_id=project_id,
            name=body.name,
            repo_url=repo_url,
            role=body.role,
            description=body.description,
            read_only=body.read_only,
            is_managed=is_managed,
            branch=body.branch,
            clone_path=body.clone_path,
        )
    except Exception as e:
        # If we created a managed repo but DB insert failed, clean up
        if is_managed and repo_url:
            await gitea_client.delete_repo(body.name)
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.patch("/api/projects/{project_id}/repositories/{repo_id}")
async def update_project_repository(
    project_id: str, repo_id: str, body: ProjectRepositoryUpdate
) -> dict[str, str]:
    """Update a project repository."""
    kwargs = {k: v for k, v in body.model_dump().items() if v is not None}
    if not kwargs:
        raise HTTPException(status_code=400, detail="No fields to update")
    success = await postgres_db.update_project_repository(repo_id, **kwargs)
    if not success:
        raise HTTPException(status_code=404, detail="Repository not found")
    return {"status": "updated"}


@app.delete("/api/projects/{project_id}/repositories/{repo_id}")
async def remove_project_repository(project_id: str, repo_id: str) -> dict[str, str]:
    """Remove a repository from a project. Cannot remove the jobs repo."""
    repo = await postgres_db.get_project_repository(repo_id)
    if not repo:
        raise HTTPException(status_code=404, detail="Repository not found")
    if repo.get("role") == "jobs":
        raise HTTPException(status_code=400, detail="Cannot remove the jobs repository")

    removed = await postgres_db.remove_project_repository(repo_id)
    if not removed:
        raise HTTPException(status_code=500, detail="Failed to remove repository")

    # Clean up managed Gitea repo
    if removed.get("is_managed") and gitea_client.is_initialized:
        await gitea_client.delete_repo(removed["name"])

    return {"status": "removed"}


@app.post("/api/projects/{project_id}/jobs")
async def create_project_job(project_id: str, job: JobCreate) -> dict[str, Any]:
    """Create a job within a project context.

    Automatically sets project_id and creates a Gitea branch for the job.
    """
    project = await postgres_db.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")

    try:
        # Build context
        context = dict(job.context) if job.context else {}
        if job.upload_id:
            context["upload_id"] = job.upload_id
        if job.config_upload_id:
            context["config_upload_id"] = job.config_upload_id
        if job.instructions_upload_id:
            context["instructions_upload_id"] = job.instructions_upload_id
        if job.instructions:
            context["instructions"] = job.instructions
        if job.kickoff_message:
            context["kickoff_message"] = job.kickoff_message

        # Use project's default config if not specified
        config_name = job.config_name
        if config_name == "default" and project.get("default_config_name"):
            config_name = project["default_config_name"]

        result = await postgres_db.create_job(
            description=job.description,
            document_path=job.document_path,
            document_dir=job.document_dir,
            config_name=config_name,
            config_override=job.config_override or project.get("default_config_override"),
            context=context if context else None,
            user_id=job.user_id,
            project_id=project_id,
            parent_job_id=job.parent_job_id,
            priority=job.priority,
        )

        job_id_str = str(result["id"])

        # Create branch in the jobs repo and set git_remote_url
        branch_name = f"job/{job_id_str[:8]}"
        repos = await postgres_db.get_project_repositories(project_id, role="jobs")

        if gitea_client.is_initialized and repos:
            jobs_repo = repos[0]
            from_branch = "main"
            if job.parent_job_id:
                parent = await postgres_db.get_job(job.parent_job_id)
                if parent and parent.get("branch_name"):
                    from_branch = parent["branch_name"]
                else:
                    logger.warning(
                        f"Parent job {job.parent_job_id} has no branch_name, "
                        f"branching subjob from 'main'"
                    )
            await gitea_client.create_branch(
                jobs_repo["name"], branch_name, from_branch=from_branch
            )

            # Use the project's jobs repo URL directly (no separate job repo needed)
            ctx = dict(context) if context else {}
            ctx["git_remote_url"] = jobs_repo["repo_url"]
            await postgres_db.update_job_context(job_id_str, ctx)

        # Update job with branch name
        async with postgres_db.acquire() as conn:
            await conn.execute(
                "UPDATE jobs SET branch_name = $1 WHERE id = $2",
                branch_name, result["id"],
            )

        result["branch_name"] = branch_name

        # Clone selected global datasources as job-scoped
        if job.datasource_ids:
            for ds_id in job.datasource_ids:
                try:
                    ds = await postgres_db.get_datasource(ds_id)
                    if ds and ds.get("job_id") is None:
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
                            job_id=job_id_str,
                        )
                except Exception as e:
                    logger.warning(f"Failed to clone datasource {ds_id}: {e}")

        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/projects/{project_id}/jobs")
async def list_project_jobs(
    project_id: str,
    status: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict[str, Any]]:
    """List jobs belonging to a project."""
    project = await postgres_db.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")

    try:
        async with postgres_db.acquire() as conn:
            query = "SELECT * FROM job_summary WHERE project_id = $1"
            params: list = [project_id]
            if status:
                query += " AND status = $2"
                params.append(status)
            query += " ORDER BY created_at DESC LIMIT $" + str(len(params) + 1)
            params.append(limit)
            rows = await conn.fetch(query, *params)

        jobs = [dict(r) for r in rows]

        # Enrich with audit counts
        if mongodb.is_available:
            for job in jobs:
                job["audit_count"] = await mongodb.get_audit_count(str(job["id"]))
        else:
            for job in jobs:
                job["audit_count"] = None

        return jobs
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/api/projects/{project_id}/jobs/{job_id}/merge")
async def merge_project_job(
    project_id: str,
    job_id: str,
    request: MergeRequest | None = None,
) -> dict[str, Any]:
    """Merge a job's branch into the project main branch via Gitea PR."""
    if request is None:
        request = MergeRequest()

    if not gitea_client.is_initialized:
        raise HTTPException(
            status_code=503,
            detail="Gitea is not initialized — merge requires Gitea",
        )

    try:
        # Validate job
        job = await postgres_db.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

        if str(job.get("project_id", "")) != project_id:
            raise HTTPException(
                status_code=400,
                detail=f"Job '{job_id}' does not belong to project '{project_id}'",
            )

        if job["status"] != "completed":
            raise HTTPException(
                status_code=400,
                detail=f"Job must be completed to merge (status: {job['status']})",
            )

        if job.get("merge_status") == "merged":
            raise HTTPException(
                status_code=409,
                detail="Job branch has already been merged",
            )

        branch_name = job.get("branch_name")
        if not branch_name:
            raise HTTPException(
                status_code=400,
                detail="Job has no branch_name — cannot merge",
            )

        # Get the jobs repo
        repos = await postgres_db.get_project_repositories(project_id, role="jobs")
        if not repos:
            raise HTTPException(
                status_code=400,
                detail="Project has no jobs repository configured",
            )
        repo_name = repos[0]["name"]

        # Create PR
        pr_title = f"Merge job {job_id[:8]}: {(job.get('description') or 'No description')[:60]}"
        pr_body = (
            f"**Job ID:** `{job_id}`\n"
            f"**Branch:** `{branch_name}`\n"
            f"**Config:** {job.get('config_name', 'default')}\n"
            f"**Merge strategy:** {request.merge_strategy}"
        )

        pr = await gitea_client.create_pr(
            repo_name,
            title=pr_title,
            head=branch_name,
            base="main",
            body=pr_body,
        )

        if pr is None:
            # PR creation failed — likely no diff or branch not found
            await postgres_db.update_job_merge_status(job_id, merge_status="skipped")
            return {
                "status": "skipped",
                "job_id": job_id,
                "reason": "PR creation failed — branch may have no changes or not exist",
            }

        # Merge the PR
        merged = await gitea_client.merge_pr(
            repo_name,
            pr["number"],
            merge_strategy=request.merge_strategy,
            delete_branch_after_merge=request.delete_branch,
        )

        if not merged:
            # Merge failed — likely conflict
            await postgres_db.update_job_merge_status(job_id, merge_status="conflict")
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "Merge conflict — resolve via Gitea PR UI then retry or skip",
                    "pr_number": pr["number"],
                    "pr_url": pr.get("url", ""),
                },
            )

        # Success
        await postgres_db.update_job_merge_status(job_id, merge_status="merged")

        # If delete_branch was not handled by Gitea merge, do it explicitly
        branch_deleted = request.delete_branch

        logger.info(
            f"Merged job {job_id[:8]} branch '{branch_name}' into main "
            f"(PR #{pr['number']}, strategy: {request.merge_strategy})"
        )

        return {
            "status": "merged",
            "job_id": job_id,
            "pr_number": pr["number"],
            "pr_url": pr.get("url", ""),
            "branch_deleted": branch_deleted,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Merge failed for job {job_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/api/projects/{project_id}/jobs/{job_id}/skip-merge")
async def skip_merge_project_job(
    project_id: str,
    job_id: str,
) -> dict[str, Any]:
    """Mark a job's merge as skipped (exploratory/research jobs)."""
    try:
        job = await postgres_db.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

        if str(job.get("project_id", "")) != project_id:
            raise HTTPException(
                status_code=400,
                detail=f"Job '{job_id}' does not belong to project '{project_id}'",
            )

        if job["status"] != "completed":
            raise HTTPException(
                status_code=400,
                detail=f"Job must be completed to skip merge (status: {job['status']})",
            )

        await postgres_db.update_job_merge_status(job_id, merge_status="skipped")

        return {"status": "skipped", "job_id": job_id}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/api/jobs/{job_id}/promote")
async def promote_job(job_id: str, request: PromoteRequest) -> dict[str, Any]:
    """Promote a default-project job into a dedicated project.

    Creates a new project, seeds its jobs repo from the job's branch content
    (preserving git history), and moves the job to the new project.
    """
    import shutil
    import subprocess
    import tempfile

    try:
        # Validate job
        job = await postgres_db.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

        if job["status"] != "completed":
            raise HTTPException(
                status_code=400,
                detail=f"Job must be completed to promote (status: {job['status']})",
            )

        # Verify job is in a default project
        old_project_id = str(job["project_id"]) if job.get("project_id") else None
        if not old_project_id:
            raise HTTPException(
                status_code=400,
                detail="Job has no project_id — cannot determine source project",
            )

        old_project = await postgres_db.get_project(old_project_id)
        if not old_project:
            raise HTTPException(
                status_code=400,
                detail=f"Source project '{old_project_id}' not found",
            )

        if not old_project.get("is_default"):
            raise HTTPException(
                status_code=400,
                detail="Job can only be promoted from a default project",
            )

        # Create new project
        new_project = await postgres_db.create_project(
            name=request.name,
            description=request.description,
            goal=request.goal,
        )
        new_project_id = str(new_project["id"])

        # Add user as owner
        await postgres_db.add_project_member(
            project_id=new_project_id,
            user_id=request.user_id,
            role="owner",
        )

        # Create Gitea jobs repo for the new project
        new_repo_name = f"project-{new_project_id[:8]}-jobs"
        new_repo_url = None

        if gitea_client.is_initialized:
            new_repo_url = await gitea_client.create_repo(new_repo_name)

        if new_repo_url:
            await postgres_db.add_project_repository(
                project_id=new_project_id,
                name=new_repo_name,
                repo_url=new_repo_url,
                role="jobs",
                is_managed=True,
            )

            # Seed the new repo from the old job's branch content
            branch_name = job.get("branch_name")
            old_repos = await postgres_db.get_project_repositories(
                old_project_id, role="jobs"
            )

            if old_repos and branch_name:
                old_repo_url = old_repos[0]["repo_url"]
                tmp_dir = None
                try:
                    tmp_dir = tempfile.mkdtemp(prefix="srw-promote-")
                    clone_path = os.path.join(tmp_dir, "repo")

                    # Clone old jobs repo at the job branch
                    result = subprocess.run(
                        ["git", "clone", "--branch", branch_name, old_repo_url, clone_path],
                        capture_output=True,
                        text=True,
                        timeout=120,
                    )
                    if result.returncode != 0:
                        logger.warning(
                            f"Promote: clone failed for branch '{branch_name}': "
                            f"{result.stderr[:200]}"
                        )
                        # Fall back: clone default branch
                        result = subprocess.run(
                            ["git", "clone", old_repo_url, clone_path],
                            capture_output=True,
                            text=True,
                            timeout=120,
                        )
                        if result.returncode != 0:
                            logger.error(f"Promote: clone fallback also failed: {result.stderr[:200]}")

                    if os.path.isdir(clone_path):
                        # Add new repo as remote, push as main
                        subprocess.run(
                            ["git", "remote", "add", "new-origin", new_repo_url],
                            cwd=clone_path,
                            capture_output=True,
                            text=True,
                            timeout=30,
                        )
                        # Rename current branch to main for the new repo
                        subprocess.run(
                            ["git", "checkout", "-B", "main"],
                            cwd=clone_path,
                            capture_output=True,
                            text=True,
                            timeout=30,
                        )
                        push_result = subprocess.run(
                            ["git", "push", "-u", "new-origin", "main"],
                            cwd=clone_path,
                            capture_output=True,
                            text=True,
                            timeout=120,
                        )
                        if push_result.returncode == 0:
                            logger.info(
                                f"Promote: seeded new repo '{new_repo_name}' from "
                                f"branch '{branch_name}'"
                            )
                        else:
                            logger.warning(
                                f"Promote: push to new repo failed: "
                                f"{push_result.stderr[:200]}"
                            )
                finally:
                    if tmp_dir and os.path.exists(tmp_dir):
                        shutil.rmtree(tmp_dir, ignore_errors=True)

        # Move job to new project
        async with postgres_db.acquire() as conn:
            await conn.execute(
                "UPDATE jobs SET project_id = $1, updated_at = CURRENT_TIMESTAMP WHERE id = $2",
                UUID(new_project_id),
                UUID(job_id),
            )

        logger.info(
            f"Promoted job {job_id[:8]} to project '{request.name}' ({new_project_id[:8]})"
        )

        return {
            "status": "promoted",
            "project_id": new_project_id,
            "project_name": request.name,
            "job_id": job_id,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Promote failed for job {job_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


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
            user_id=body.user_id,
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


def _build_workspace_proposal(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Build a workspace edit proposal for the frontend, or return an error.

    Validates path, reads current content, and packages the proposal data.
    """
    from services.workspace import WorkspaceService

    job_id = args.get("job_id", "")
    path = args.get("path", "")

    blocked = WorkspaceService.is_path_blocked(path)
    if blocked:
        return {"error": blocked}

    current_content = workspace_service.get_workspace_file(job_id, path)

    if tool_name == "write_workspace_file":
        return {
            "tool": tool_name,
            "job_id": job_id,
            "path": path,
            "operation": "write",
            "content": args.get("content", ""),
            "current_content": current_content,
        }
    elif tool_name == "edit_workspace_file":
        old_text = args.get("old_text", "")
        new_text = args.get("new_text", "")
        if current_content is None:
            return {"error": f"File '{path}' not found in workspace for job '{job_id}'"}
        if old_text not in current_content:
            return {"error": f"old_text not found in '{path}'. The file may have changed."}
        return {
            "tool": tool_name,
            "job_id": job_id,
            "path": path,
            "operation": "edit",
            "old_text": old_text,
            "new_text": new_text,
            "current_content": current_content,
        }
    return {"error": f"Unknown workspace edit tool: {tool_name}"}


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
        active_job_id=body.active_job_id,
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
            model = body.model or get_builder_model()
            provider = _detect_provider(model)
            api_key = _get_api_key_for_provider(provider)
            use_responses_api = model in RESPONSES_API_MODELS

            for iteration in range(MAX_ITERATIONS):
                yield f"event: step\ndata: {json.dumps({'type': 'thought', 'title': 'Analyzing request...' if iteration == 0 else 'Processing tool results...'})}\n\n"
                turn_text = ""
                turn_tool_calls = []  # {"name", "args", "id"} dicts
                error_occurred = False

                # Select streaming function based on provider and API type
                if provider == "anthropic":
                    stream_fn = _stream_anthropic(system_prompt, loop_messages, model, api_key)
                elif use_responses_api:
                    input_items = _chat_messages_to_responses_input(loop_messages)
                    stream_fn = _stream_openai_responses(system_prompt, input_items, model, api_key)
                else:
                    stream_fn = _stream_openai(system_prompt, loop_messages, model, api_key)

                async for evt_type, evt_data in stream_fn:
                    if evt_type == "token":
                        turn_text += evt_data["text"]
                        yield f"event: token\ndata: {json.dumps(evt_data)}\n\n"
                    elif evt_type == "tool_call":
                        turn_tool_calls.append(evt_data)
                        if evt_data["name"] in SERVER_SIDE_TOOLS:
                            yield f"event: tool_executing\ndata: {json.dumps({'tool': evt_data['name'], 'args': evt_data['args']})}\n\n"
                        elif evt_data["name"] in WORKSPACE_EDIT_TOOLS:
                            proposal = _build_workspace_proposal(evt_data["name"], evt_data["args"])
                            if not proposal.get("error"):
                                yield f"event: workspace_proposal\ndata: {json.dumps(proposal)}\n\n"
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
                            result, full_content = await _execute_server_tool(tc["name"], tc["args"])
                            evt_data: dict[str, Any] = {"tool": tc["name"], "summary": result[:200]}
                            if full_content is not None:
                                evt_data["content"] = full_content
                            yield f"event: tool_result\ndata: {json.dumps(evt_data)}\n\n"
                        elif tc["name"] in WORKSPACE_EDIT_TOOLS:
                            proposal = _build_workspace_proposal(tc["name"], tc["args"])
                            if proposal.get("error"):
                                result = f"Error: {proposal['error']}"
                            else:
                                result = f"Proposed edit to {tc['args'].get('path', 'file')}. The user will review and approve or dismiss."
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
                            result, full_content = await _execute_server_tool(tc["name"], tc["args"])
                            evt_data_oai: dict[str, Any] = {"tool": tc["name"], "summary": result[:200]}
                            if full_content is not None:
                                evt_data_oai["content"] = full_content
                            yield f"event: tool_result\ndata: {json.dumps(evt_data_oai)}\n\n"
                        elif tc["name"] in WORKSPACE_EDIT_TOOLS:
                            proposal = _build_workspace_proposal(tc["name"], tc["args"])
                            if proposal.get("error"):
                                result = f"Error: {proposal['error']}"
                            else:
                                result = f"Proposed edit to {tc['args'].get('path', 'file')}. The user will review and approve or dismiss."
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


async def _execute_server_tool(tool_name: str, args: dict) -> tuple[str, str | None]:
    """Execute a server-side builder tool via the shared dispatch module."""
    return await _dispatch_server_tool(
        tool_name, args, tavily_search_fn=tavily_search,
    )


# =============================================================================
# Responses API support (GPT-5.2 Pro and future models)
# =============================================================================

RESPONSES_API_MODELS = {"gpt-5.2-pro"}


def _detect_provider(model: str) -> str:
    """Detect LLM provider from model name."""
    if model.startswith("claude-"):
        return "anthropic"
    return "openai"


def _get_api_key_for_provider(provider: str) -> str | None:
    """Get API key for the given provider, with explicit override support."""
    explicit = os.getenv("BUILDER_API_KEY")
    if explicit:
        return explicit
    if provider == "anthropic":
        return os.getenv("ANTHROPIC_API_KEY")
    return os.getenv("OPENAI_API_KEY")


def _chat_messages_to_responses_input(
    messages: list[dict],
) -> list[dict]:
    """Convert Chat Completions format messages to Responses API input items.

    - {role: user, content: ...} → kept as-is
    - {role: assistant, content: ..., tool_calls: [...]} → text item + function_call items
    - {role: tool, tool_call_id: ..., content: ...} → function_call_output items
    - {role: system, ...} → skipped (goes to `instructions` param)
    """
    items: list[dict] = []
    for msg in messages:
        role = msg.get("role", "")
        if role == "system":
            continue
        elif role == "user":
            content = msg.get("content", "")
            if isinstance(content, str):
                items.append({"role": "user", "content": content})
            elif isinstance(content, list):
                # Anthropic-style tool_result blocks from multi-turn — skip for Responses API
                # These are handled separately
                items.append({"role": "user", "content": str(content)})
        elif role == "assistant":
            content = msg.get("content")
            if content:
                items.append({"type": "message", "role": "assistant", "content": content})
            tool_calls = msg.get("tool_calls", [])
            for tc in tool_calls:
                func = tc.get("function", {})
                items.append({
                    "type": "function_call",
                    "call_id": tc.get("id", ""),
                    "name": func.get("name", ""),
                    "arguments": func.get("arguments", "{}"),
                })
        elif role == "tool":
            items.append({
                "type": "function_call_output",
                "call_id": msg.get("tool_call_id", ""),
                "output": msg.get("content", ""),
            })
    return items


async def _stream_openai_responses(
    system_prompt: str,
    input_items: list[dict],
    model: str,
    api_key: str | None = None,
):
    """Stream from OpenAI Responses API, yielding structured events.

    Uses client.responses.create() instead of chat.completions.create().
    System prompt goes in `instructions` parameter.

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
        api_key=api_key or get_builder_api_key(),
        base_url=get_builder_base_url(),
    )

    # Convert BUILDER_TOOLS to Responses API function tool format
    response_tools = []
    for tool in BUILDER_TOOLS:
        func = tool["function"]
        response_tools.append({
            "type": "function",
            "name": func["name"],
            "description": func["description"],
            "parameters": func["parameters"],
        })

    try:
        stream = await client.responses.create(
            model=model,
            instructions=system_prompt,
            input=input_items,
            tools=response_tools,
            reasoning={"effort": "high"},
            stream=True,
        )

        # Track function call assembly
        function_calls: dict[str, dict] = {}  # call_id -> {name, arguments}

        async for event in stream:
            event_type = event.type

            # Text deltas
            if event_type == "response.output_text.delta":
                yield ("token", {"text": event.delta})

            # Function call starts — capture name
            elif event_type == "response.output_item.added":
                item = event.item
                if hasattr(item, "type") and item.type == "function_call":
                    call_id = getattr(item, "call_id", "") or ""
                    name = getattr(item, "name", "") or ""
                    function_calls[call_id] = {"name": name, "arguments": ""}

            # Function call argument deltas
            elif event_type == "response.function_call_arguments.delta":
                call_id = getattr(event, "call_id", "") or getattr(event, "item_id", "")
                if call_id in function_calls:
                    function_calls[call_id]["arguments"] += event.delta

            # Function call done
            elif event_type == "response.function_call_arguments.done":
                call_id = getattr(event, "call_id", "") or getattr(event, "item_id", "")
                if call_id in function_calls:
                    tc = function_calls[call_id]
                    try:
                        args = json.loads(tc["arguments"]) if tc["arguments"] else {}
                        yield ("tool_call", {"name": tc["name"], "args": args, "id": call_id})
                    except json.JSONDecodeError:
                        logger.warning(f"Failed to parse Responses API tool args: {tc['arguments'][:100]}")

            # Output item done — emit function calls if not already emitted
            elif event_type == "response.output_item.done":
                item = event.item
                if hasattr(item, "type") and item.type == "function_call":
                    call_id = getattr(item, "call_id", "") or ""
                    if call_id in function_calls:
                        # Already emitted via arguments.done
                        pass

    except Exception as e:
        yield ("error", {"message": str(e)})


async def _stream_openai(
    system_prompt: str,
    context_messages: list[dict],
    model: str,
    api_key: str | None = None,
):
    """Stream from OpenAI Chat Completions API, yielding structured events.

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
        api_key=api_key or get_builder_api_key(),
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
    api_key: str | None = None,
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

    client = AsyncAnthropic(api_key=api_key or get_builder_api_key())

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
