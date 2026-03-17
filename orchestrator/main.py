"""FastAPI backend for the Debug Cockpit.

Run with:
    uvicorn orchestrator.main:app --reload --port 8085

Or from orchestrator directory:
    uvicorn main:app --reload --port 8085
"""

import asyncio
import hashlib
import json
import logging
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv())

# Configure application-level logging (Uvicorn only configures its own loggers)
# When DEBUG, only app loggers get DEBUG; third-party stays at INFO.
# Set DEBUG_ALL=1 to include third-party debug output.
_log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
if _log_level == "DEBUG" and not os.environ.get("DEBUG_ALL"):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # App namespaces (covers both `uvicorn orchestrator.main:app` and `uvicorn main:app`)
    for _ns in ("orchestrator", "main", "database", "security", "services",
                "uploads", "mcp", "graph_routes", "workspace"):
        logging.getLogger(_ns).setLevel(logging.DEBUG)
else:
    logging.basicConfig(
        level=_log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

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
from security.auth import get_current_user, cleanup_expired_tokens  # noqa: E402
from services.workspace import workspace_service  # noqa: E402
from services.gitea import GiteaClient  # noqa: E402
from services.keycloak_admin import KeycloakGroupSync  # noqa: E402
from services.builder_tools import (  # noqa: E402
    BUILDER_TOOLS,
    SERVER_SIDE_TOOLS,
    WORKSPACE_EDIT_TOOLS,
    build_message_context,
    build_summarization_prompt,
    get_builder_api_key,
    get_builder_base_url,
    get_builder_model,
    is_auth_or_quota_error,
    rotate_builder_key,
)
from services.builder_search import tavily_search  # noqa: E402
from services.builder_prompt import build_system_prompt  # noqa: E402
from services.builder_config import resolve_builder_settings  # noqa: E402
from services.builder_dispatch import execute_server_tool as _dispatch_server_tool  # noqa: E402
from services.nats_bridge import nats_bridge  # noqa: E402
from services.vm_provisioner import vm_provisioner  # noqa: E402
from services.snapshot_service import snapshot_service  # noqa: E402
from services.ide_session import ide_session_service  # noqa: E402
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
keycloak_groups = KeycloakGroupSync()

# Vector DB — separate pgvector instance for citations, memories + knowledge_index.
_vector_url = os.getenv("VECTOR_DB_URL")
if not _vector_url:
    raise RuntimeError("VECTOR_DB_URL environment variable is required")
vector_db = PostgresDB(connection_string=_vector_url)


async def resolve_job_repo(job_id: str) -> tuple[str, str | None]:
    """Resolve the Gitea repo name and branch for a job.

    Per-job repo model: root jobs own a repo (stored in repo_name column),
    subjobs work on branches within their root job's repo.

    Falls back to legacy project-jobs-repo resolution for jobs created before
    the per-job repo migration.

    Returns:
        (repo_name, job_branch) where job_branch is None for root jobs.
    """
    job = await postgres_db.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

    # New model: repo_name stored directly on the job
    if job.get("repo_name"):
        return job["repo_name"], job.get("branch_name")

    # Subjob without repo_name: traverse to root job
    if job.get("parent_job_id"):
        parent = await postgres_db.get_job(str(job["parent_job_id"]))
        if parent and parent.get("repo_name"):
            return parent["repo_name"], job.get("branch_name")

    # Legacy fallback: project jobs repo (pre-migration jobs)
    if job.get("project_id"):
        repos = await postgres_db.get_project_repositories(
            str(job["project_id"]), role="jobs"
        )
        if repos:
            return repos[0]["name"], job.get("branch_name")

    # Non-project legacy jobs: repo named job-{full-uuid}
    return f"job-{job_id}", None


# Files to delete from subjob branch before squash merge (job-scoped working files).
SUBJOB_CLEANUP_FILES = [
    "workspace.md",
    "plan.md",
    "todos.yaml",
    "instructions.md",
    "task_brief.md",
    "output/job_frozen.json",
    "output/job_completion.json",
]

SUBJOB_CLEANUP_DIRS = [
    "archive",
    "tools",
    "documents",
    "reference",
]


def _deep_merge_dicts(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Deep merge two dicts. Override wins for scalars/lists; dicts merge recursively."""
    result = base.copy()
    for key, value in override.items():
        if value is None:
            result.pop(key, None)
        elif isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge_dicts(result[key], value)
        else:
            result[key] = value
    return result


async def _squash_merge_subjob(job_id: str) -> dict[str, Any] | None:
    """Squash-merge a completed subjob's branch into its parent's branch.

    Pre-merge cleanup: deletes job-scoped files from the subjob branch
    before creating the PR, so the parent's workspace.md / plan.md are
    not overwritten.

    Returns:
        Merge result dict, or None if merge was skipped/not applicable.
    """
    job = await postgres_db.get_job(job_id)
    if not job or not job.get("parent_job_id"):
        return None

    if not job.get("branch_name") or not job.get("repo_name"):
        logger.debug(f"Subjob {job_id} has no branch/repo — skipping squash merge")
        return None

    if not gitea_client.is_initialized:
        logger.warning(f"Gitea not initialized — cannot squash-merge subjob {job_id}")
        return None

    repo_name = job["repo_name"]
    subjob_branch = job["branch_name"]
    short_id = str(job_id)[:8]

    # Determine the base branch (parent's branch, or main if parent is root)
    parent = await postgres_db.get_job(str(job["parent_job_id"]))
    base_branch = (parent.get("branch_name") if parent else None) or "main"

    # Pre-merge cleanup: delete job-scoped files from subjob branch
    for file_path in SUBJOB_CLEANUP_FILES:
        await gitea_client.delete_file(
            repo_name, file_path,
            f"Pre-merge cleanup: remove {file_path}",
            branch=subjob_branch,
        )

    # Delete job-scoped directories (list contents then delete each file)
    for dir_path in SUBJOB_CLEANUP_DIRS:
        entries = await gitea_client.list_contents(repo_name, dir_path, ref=subjob_branch)
        if entries:
            for entry in entries:
                if entry.get("type") == "file":
                    await gitea_client.delete_file(
                        repo_name, entry["path"],
                        f"Pre-merge cleanup: remove {entry['path']}",
                        branch=subjob_branch,
                    )

    # Create PR for squash merge
    config_name = job.get("config_name", "subjob")
    pr_title = f"Subjob {short_id}/{config_name}: {(job.get('description') or 'completed')[:60]}"
    pr = await gitea_client.create_pr(
        repo_name,
        title=pr_title,
        head=subjob_branch,
        base=base_branch,
        body=f"Squash merge subjob `{job_id}` (`{config_name}`) into parent branch.",
    )

    if pr is None:
        logger.info(
            f"Subjob {short_id} PR creation returned None — "
            f"branch may have no changes vs {base_branch}"
        )
        await postgres_db.update_job_merge_status(job_id, merge_status="skipped")
        return {"status": "skipped", "reason": "no changes"}

    # Squash merge
    merged = await gitea_client.merge_pr(
        repo_name,
        pr["number"],
        merge_strategy="squash",
        delete_branch_after_merge=True,
    )

    if not merged:
        logger.warning(f"Squash merge failed for subjob {short_id} (PR #{pr['number']})")
        await postgres_db.update_job_merge_status(job_id, merge_status="conflict")
        return {"status": "conflict", "pr_number": pr["number"]}

    await postgres_db.update_job_merge_status(job_id, merge_status="merged")
    logger.info(
        f"Squash-merged subjob {short_id}/{config_name} into {base_branch} "
        f"(PR #{pr['number']})"
    )

    return {
        "status": "merged",
        "pr_number": pr["number"],
        "base_branch": base_branch,
    }


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


async def sudo_expiration_sweeper(shutdown_event: asyncio.Event) -> None:
    """Background task that denies expired sudo approval requests.

    Runs every 15 seconds. For each expired request, publishes a denial
    to the stored NATS reply subject so the daemon unblocks.
    """
    from services.sudo_gate import sudo_gate  # noqa: E402

    logger.info("Sudo expiration sweeper started")
    while not shutdown_event.is_set():
        try:
            await sudo_gate.sweep_expired()
        except Exception as e:
            logger.error("Error in sudo expiration sweeper: %s", e)

        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=15.0)
            break
        except asyncio.TimeoutError:
            pass

    logger.info("Sudo expiration sweeper stopped")


async def ide_session_ttl_sweeper(shutdown_event: asyncio.Event) -> None:
    """Background task that expires IDE sessions past their TTL.

    Runs every 60 seconds. Checks active/idle sessions for:
    - Max lifetime exceeded (default: 4 hours)
    - Idle timeout exceeded (default: 30 minutes, only for 'idle' status)
    """
    logger.info("IDE session TTL sweeper started")
    while not shutdown_event.is_set():
        try:
            expired = await ide_session_service.check_ttl_all()
            if expired:
                logger.info("IDE session sweeper: expired %d sessions", expired)
        except Exception as e:
            logger.error("Error in IDE session TTL sweeper: %s", e)

        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=60.0)
            break
        except asyncio.TimeoutError:
            pass

    logger.info("IDE session TTL sweeper stopped")


async def snapshot_gc_sweeper(shutdown_event: asyncio.Event) -> None:
    """Background task that runs snapshot garbage collection daily.

    Applies retention policies, soft-deletes expired snapshots, and
    purges items past the 7-day grace period.
    """
    logger.info("Snapshot GC sweeper started")
    gc_interval = 24 * 3600  # 24 hours

    while not shutdown_event.is_set():
        try:
            if snapshot_service.is_available:
                stats = await snapshot_service.run_gc()
                if stats.get("soft_deleted") or stats.get("purged"):
                    logger.info("Snapshot GC: %s", stats)
        except Exception as e:
            logger.error("Error in snapshot GC sweeper: %s", e)

        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=gc_interval)
            break
        except asyncio.TimeoutError:
            pass

    logger.info("Snapshot GC sweeper stopped")


# =============================================================================
# Job Auto-Assignment Dispatcher
# =============================================================================


async def _dispatch_job_to_agent(job: dict, agent: dict) -> bool:
    """Start a new job on an agent. Returns True on success.

    Extracted from assign_job_to_agent() endpoint. Handles datasource resolution,
    config overrides, HTTP POST to agent pod, and status updates.
    """

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

        # Inject VM workspace config if job has a ready VM
        vm_ctx = _get_vm_context(job)
        if vm_ctx.get("status") == "ready" and vm_ctx.get("ssh_host"):
            config_override = config_override or {}
            ws = config_override.setdefault("workspace", {})
            ws["backend"] = "remote"
            remote = ws.setdefault("remote", {})
            remote.setdefault("host", vm_ctx["ssh_host"])
            remote.setdefault("port", vm_ctx.get("ssh_port", 22))
            remote.setdefault("username", "agent-host")
            remote.setdefault("key_path", "/run/secrets/vm-ssh-key")
            remote.setdefault("workspace_path", "/home/agent-host/workspace")
            logger.info(
                f"Dispatch: injected VM workspace config for job {job_id} "
                f"(host={vm_ctx['ssh_host']}:{vm_ctx.get('ssh_port', 22)})"
            )

        # Resolve user/project API keys (user > project > env var fallback)
        resolved_keys = await postgres_db.resolve_api_keys_for_job(
            user_id=str(job["user_id"]) if job.get("user_id") else None,
            project_id=str(job["project_id"]) if job.get("project_id") else None,
        )
        if resolved_keys:
            config_override = config_override or {}
            # Detect main LLM provider and inject key
            llm_provider = _detect_llm_provider_for_dispatch(job, config_override)
            if llm_provider and llm_provider in resolved_keys:
                config_override.setdefault("llm", {})["api_key"] = resolved_keys[llm_provider]
            # Inject non-LLM tool keys as env_keys
            _ENV_KEY_MAP = {"tavily": "TAVILY_API_KEY", "vision": "VISION_API_KEY"}
            env_keys = {_ENV_KEY_MAP[p]: resolved_keys[p] for p in ("tavily", "vision") if p in resolved_keys}
            if env_keys:
                config_override.setdefault("env_keys", {}).update(env_keys)
            logger.info(f"Dispatch: injected API keys for providers: {list(resolved_keys.keys())}")

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

        # Inject VM workspace config if job has a ready VM
        vm_ctx = _get_vm_context(job)
        if vm_ctx.get("status") == "ready" and vm_ctx.get("ssh_host"):
            config_override = config_override or {}
            ws = config_override.setdefault("workspace", {})
            ws["backend"] = "remote"
            remote = ws.setdefault("remote", {})
            remote.setdefault("host", vm_ctx["ssh_host"])
            remote.setdefault("port", vm_ctx.get("ssh_port", 22))
            remote.setdefault("username", "agent-host")
            remote.setdefault("key_path", "/run/secrets/vm-ssh-key")
            remote.setdefault("workspace_path", "/home/agent-host/workspace")
            logger.info(
                f"Resume dispatch: injected VM workspace config for job {job_id} "
                f"(host={vm_ctx['ssh_host']}:{vm_ctx.get('ssh_port', 22)})"
            )

        # Extract queued feedback (stored by resume endpoint when no agent was available)
        job_context = job.get("context") or {}
        if isinstance(job_context, str):
            job_context = json.loads(job_context)
        queued_feedback = job_context.pop("queued_feedback", None)

        resume_payload = {
            "job_id": job_id,
            "config_name": job.get("config_name", "default"),
            "config_override": config_override,
            "datasources": datasources_payload,
            "previous_status": job.get("status"),
        }
        if queued_feedback:
            resume_payload["feedback"] = queued_feedback
            # Clean up queued_feedback from context so it's not re-injected
            async with postgres_db.acquire() as conn:
                await conn.execute(
                    "UPDATE jobs SET context = $1::jsonb WHERE id = $2::uuid",
                    json.dumps(job_context), job_id,
                )

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


def _job_needs_vm(job: dict) -> bool:
    """Check if a job requires a VM workspace (from config_override or context)."""
    # Explicit VM request in context
    ctx = job.get("context") or {}
    if isinstance(ctx, str):
        try:
            ctx = json.loads(ctx)
        except (json.JSONDecodeError, TypeError):
            ctx = {}
    vm_ctx = ctx.get("vm", {})
    if vm_ctx.get("requested"):
        return True
    # Config override specifies remote workspace
    co = job.get("config_override") or {}
    if isinstance(co, str):
        try:
            co = json.loads(co)
        except (json.JSONDecodeError, TypeError):
            co = {}
    return co.get("workspace", {}).get("backend") == "remote"


def _get_vm_context(job: dict) -> dict:
    """Extract the vm sub-dict from job context."""
    ctx = job.get("context") or {}
    if isinstance(ctx, str):
        try:
            ctx = json.loads(ctx)
        except (json.JSONDecodeError, TypeError):
            ctx = {}
    return ctx.get("vm", {})


def _detect_llm_provider_for_dispatch(job: dict, config_override: dict | None) -> str | None:
    """Detect the LLM provider for a job from its config override or config name.

    Uses the same prefix-matching logic as src/core/loader.py:_detect_provider().
    """
    # Check config_override for explicit provider or model
    if config_override:
        llm = config_override.get("llm", {})
        if llm.get("provider"):
            return llm["provider"].lower()
        model = llm.get("model")
        if model:
            model_lower = model.lower()
            if model_lower.startswith("openrouter/"):
                return "openrouter"
            if model_lower.startswith("groq/"):
                return "groq"
            if model_lower.startswith("claude"):
                return "anthropic"
            if model_lower.startswith("gemini"):
                return "google"
            return "openai"

    # Fall back to config_name heuristic (most configs use openai-compatible default)
    config_name = job.get("config_name", "default")
    if config_name and "anthropic" in config_name.lower():
        return "anthropic"
    return "openai"


async def _try_dispatch_pending_jobs() -> None:
    """Core dispatcher: match pending jobs to available agents.

    Phase 1: Direct assignment (free agents → highest priority pending jobs)
    Phase 2: Preemption (remaining high-priority jobs → lowest-priority running jobs)

    VM-aware: jobs needing a VM are auto-provisioned and held until the VM
    registers as ready. Jobs with a ready VM get workspace config injected
    into config_override before dispatch.
    """
    if not AUTO_ASSIGN_ENABLED:
        return

    async with _dispatch_lock:
        try:
            # Get pending jobs (created + paused, priority ordered)
            pending_jobs = await postgres_db.get_dispatchable_jobs(limit=50)
            if not pending_jobs:
                return

            # Pre-filter: auto-provision VMs for jobs that need one
            dispatchable_jobs = []
            for job in pending_jobs:
                job_id = str(job["id"])
                if _job_needs_vm(job):
                    vm_ctx = _get_vm_context(job)
                    if not vm_ctx.get("status"):
                        # VM needed but not provisioned yet — provision now
                        if vm_provisioner.is_available:
                            config_override = job.get("config_override") or {}
                            if isinstance(config_override, str):
                                config_override = json.loads(config_override)
                            vm_cfg = config_override.get("workspace", {}).get("vm", {})
                            ok = await vm_provisioner.create_vm(
                                job_id=job_id,
                                agent_config=job.get("config_name", "defaults"),
                                vm_image=vm_cfg.get("image"),
                                cpu_cores=vm_cfg.get("cpu_cores", 2),
                                memory=vm_cfg.get("memory", "4Gi"),
                                description=job.get("description", ""),
                            )
                            if ok:
                                logger.info(f"Dispatcher: auto-provisioned VM for job {job_id}")
                            else:
                                logger.warning(f"Dispatcher: VM provisioning failed for job {job_id}")
                        continue  # Skip this job — wait for VM to register
                    elif vm_ctx.get("status") not in ("ready",):
                        # VM is provisioning/creating — skip, wait
                        continue
                    # else: VM is ready, proceed with dispatch
                dispatchable_jobs.append(job)

            if not dispatchable_jobs:
                return

            # Get available agents (ready, cooldown passed)
            available_agents = await postgres_db.get_available_agents(limit=50)

            # Phase 1: Direct assignment
            matched_job_ids = set()
            matched_agent_ids = set()

            agents_iter = iter(available_agents)
            for job in dispatchable_jobs:
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


class JobCompleteRequest(BaseModel):
    """Result payload sent by the agent after a job finishes processing."""

    should_stop: bool = Field(False, description="Whether the graph stopped")
    goal_achieved: bool = Field(False, description="Whether the goal was achieved")
    error: dict[str, Any] | None = Field(None, description="Error dict if job failed")
    freeze_data: dict[str, Any] | None = Field(None, description="Freeze data from the graph state")


class BuilderSessionCreate(BaseModel):
    """Request body for creating a builder session."""

    expert_id: str | None = Field(None, description="Expert used as starting point")
    user_id: str | None = Field(None, description="User UUID who created this session")


class VMCreateRequest(BaseModel):
    """Request body for creating a VM for a job."""

    job_id: str
    agent_config: str = "defaults"
    vm_image: str | None = None
    cpu_cores: int = Field(2, ge=1, le=16)
    memory: str = "4Gi"
    description: str = ""


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


class McpTokenCreate(BaseModel):
    """Request body for creating an MCP API token."""

    name: str = Field(..., min_length=1, max_length=100, description="Token label")
    scope: str = Field(default="user", description="'user', 'project:<uuid>', or 'all'")
    expires_in_days: int | None = Field(None, description="Days until expiry (null = never)")


class McpTokenVerifyRequest(BaseModel):
    """Internal request from MCP server to verify a token hash."""

    token_hash: str


VALID_API_KEY_PROVIDERS = {"openai", "anthropic", "google", "groq", "openrouter", "tavily", "vision"}


class ApiKeySet(BaseModel):
    """Request body for setting an API key for a provider."""

    api_key: str = Field(..., min_length=1, description="The API key value")
    label: str | None = Field(None, description="Optional label (e.g. 'team key', 'personal')")


class UserSettingsUpdate(BaseModel):
    """Request body for updating user preferences. Null values remove the key."""

    default_model: str | None = None
    default_autonomy: str | None = None
    default_reasoning_level: str | None = None


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




class PromoteRequest(BaseModel):
    """Request body for promoting a job into a dedicated project."""

    name: str = Field(..., description="Name for the new project")
    description: str | None = Field(None, description="Project description")
    goal: str | None = Field(None, description="Project goal")
    user_id: str = Field(..., description="User UUID who owns the new project")


class KnowledgeSearchRequest(BaseModel):
    """Request body for hybrid knowledge search."""

    query: str = Field(..., description="Search query text")
    limit: int = Field(10, ge=1, le=50, description="Max results to return")


class KnowledgeNoteUpdate(BaseModel):
    """Request body for updating a knowledge note."""

    status: str | None = Field(None, description="New status: active, resolved, superseded, archived")
    add_tags: list[str] | None = Field(None, description="Tags to add")
    remove_tags: list[str] | None = Field(None, description="Tags to remove")


class BuilderMessageRequest(BaseModel):
    """Request body for sending a message to the builder."""

    message: str = Field(..., description="User's message text")
    model: str | None = Field(None, description="Builder model override")
    instructions: str | None = Field(None, description="Current instructions content")
    config: dict[str, Any] | None = Field(None, description="Current config override")
    description: str | None = Field(None, description="Current job description")
    active_job_id: str | None = Field(None, description="Active job context for inspection tools")
    active_project_id: str | None = Field(None, description="Active project context for project-scoped tools")


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
    await vector_db.connect()
    await mongodb.connect()

    # Share MongoDB instance with graph_routes
    set_mongodb(mongodb)

    # Initialize Gitea workspace delivery (graceful if unavailable)
    await gitea_client.ensure_initialized()

    # Initialize Keycloak group sync (graceful if unavailable)
    await keycloak_groups.ensure_initialized()

    # Initialize NATS bridge for VM lifecycle (graceful if unavailable)
    await nats_bridge.connect(db=postgres_db, on_vm_ready=_trigger_dispatch)

    # Initialize VM provisioner (uses NATS if available, else direct K8s)
    vm_provisioner.connect(db=postgres_db)

    # Initialize S3 snapshot service (graceful if S3 not configured)
    await snapshot_service.connect(db=postgres_db)

    # Initialize IDE session service
    ide_session_service.connect(
        db=postgres_db,
        snapshot_service=snapshot_service,
        vm_provisioner=vm_provisioner,
        gitea_client=gitea_client,
    )

    # Start background tasks
    _shutdown_event = asyncio.Event()
    stale_detector_task = asyncio.create_task(stale_agent_detector(_shutdown_event))
    token_cleanup_task = asyncio.create_task(cleanup_expired_tokens(postgres_db, _shutdown_event))
    dispatcher_task = asyncio.create_task(auto_assign_dispatcher(_shutdown_event))
    sudo_sweeper_task = asyncio.create_task(sudo_expiration_sweeper(_shutdown_event))
    ide_sweeper_task = asyncio.create_task(ide_session_ttl_sweeper(_shutdown_event))
    gc_sweeper_task = asyncio.create_task(snapshot_gc_sweeper(_shutdown_event))

    yield

    # Signal shutdown to background tasks
    _shutdown_event.set()
    await stale_detector_task
    await token_cleanup_task
    await dispatcher_task
    await sudo_sweeper_task
    await ide_sweeper_task
    await gc_sweeper_task

    # Cleanup clients
    await nats_bridge.disconnect()
    await gitea_client.close()

    # Disconnect from databases
    await mongodb.disconnect()
    await vector_db.disconnect()
    await postgres_db.disconnect()


app = FastAPI(
    title="Debug Cockpit API",
    description="Backend API for the Superhuman Remote Worker Cockpit",
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
    ] + [o for o in os.environ.get("CORS_ORIGINS", "").split(",") if o],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# CSRF middleware — validates X-CSRF-Token header on mutating requests
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


def _get_mcp_scope(request: Request) -> tuple[str | None, str | None]:
    """Extract MCP scope from request headers (set by the MCP server).

    Returns (user_id, scope) if the request comes from an authenticated
    MCP client, otherwise (None, None). Headers are only trusted when
    the X-Internal-Key matches MCP_INTERNAL_KEY.
    """
    mcp_user = request.headers.get("X-MCP-User-Id")
    mcp_scope = request.headers.get("X-MCP-Scope")
    if mcp_user and mcp_scope:
        key = request.headers.get("X-Internal-Key", "")
        expected = os.environ.get("MCP_INTERNAL_KEY", "")
        if expected and key == expected:
            return mcp_user, mcp_scope
    return None, None


@app.get("/api/jobs")
async def list_jobs(
    request: Request,
    status: str | None = Query(default=None),
    user_id: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict[str, Any]]:
    """List jobs with optional status and user filter.

    Returns jobs enriched with audit_count from MongoDB if available.
    MCP scope filtering is applied when the request comes from an
    authenticated MCP client.
    """
    # Apply MCP scope filtering
    mcp_user, mcp_scope = _get_mcp_scope(request)
    if mcp_scope == "user":
        user_id = mcp_user  # Override to show only the token owner's jobs
    elif mcp_scope and mcp_scope.startswith("project:"):
        pass  # project filtering applied below after fetching

    try:
        jobs = await postgres_db.get_jobs(status=status, user_id=user_id, limit=limit)

        # Filter by project if MCP scope is project-scoped
        if mcp_scope and mcp_scope.startswith("project:"):
            project_id = mcp_scope.split(":", 1)[1]
            jobs = [j for j in jobs if str(j.get("project_id", "")) == project_id]

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
async def get_job(request: Request, job_id: str) -> dict[str, Any]:
    """Get a single job by ID."""
    try:
        job = await postgres_db.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

        # MCP scope check
        mcp_user, mcp_scope = _get_mcp_scope(request)
        if mcp_scope == "user" and str(job.get("user_id", "")) != mcp_user:
            raise HTTPException(status_code=403, detail="Access denied by token scope")
        elif mcp_scope and mcp_scope.startswith("project:"):
            project_id = mcp_scope.split(":", 1)[1]
            if str(job.get("project_id", "")) != project_id:
                raise HTTPException(status_code=403, detail="Access denied by token scope")

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

    If ``project_id`` is set (directly or via the user's default project),
    the job is created within that project context: the project's default
    config and config_override are used as fallbacks, and the workspace is
    branched from the project's shared jobs repo instead of getting its own
    per-job repo.
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

        # Resolve project defaults (config name, config override)
        project = None
        config_name = job.config_name
        config_override = job.config_override
        if project_id:
            project = await postgres_db.get_project(project_id)
            if not project:
                raise HTTPException(
                    status_code=404, detail=f"Project '{project_id}' not found"
                )
            if config_name == "default" and project.get("default_config_name"):
                config_name = project["default_config_name"]
            project_default_override = project.get("default_config_override")
            if project_default_override:
                if config_override:
                    # Deep merge: project defaults as base, job overrides on top
                    config_override = _deep_merge_dicts(project_default_override, config_override)
                else:
                    config_override = project_default_override

        result = await postgres_db.create_job(
            description=job.description,
            document_path=job.document_path,
            document_dir=job.document_dir,
            config_name=config_name,
            config_override=config_override,
            context=context if context else None,
            user_id=job.user_id,
            project_id=project_id,
            parent_job_id=job.parent_job_id,
            priority=job.priority,
        )

        # Create Gitea repo/branch for workspace delivery
        job_id_str = str(result["id"])
        short_id = job_id_str[:8]
        if gitea_client.is_initialized:
            if job.parent_job_id:
                # Subjob: branch on parent's repo
                parent = await postgres_db.get_job(job.parent_job_id)
                if parent:
                    # Resolve parent's repo name (parent may be root or itself a subjob)
                    parent_repo_name = parent.get("repo_name")
                    if not parent_repo_name and parent.get("parent_job_id"):
                        root = await postgres_db.get_job(str(parent["parent_job_id"]))
                        if root:
                            parent_repo_name = root.get("repo_name")
                    if not parent_repo_name:
                        # Legacy fallback: try project jobs repo
                        if parent.get("project_id"):
                            repos = await postgres_db.get_project_repositories(
                                str(parent["project_id"]), role="jobs"
                            )
                            if repos:
                                parent_repo_name = repos[0]["name"]
                        if not parent_repo_name:
                            parent_repo_name = f"job-{str(parent['id'])}"

                    from_branch = parent.get("branch_name") or "main"
                    config_name_slug = config_name or "subjob"
                    branch_name = f"subjob/{short_id}/{config_name_slug}"
                    branch_ok = await gitea_client.create_branch(
                        parent_repo_name, branch_name, from_branch=from_branch
                    )
                    if not branch_ok:
                        logger.error(
                            f"Failed to create branch '{branch_name}' from '{from_branch}' "
                            f"in '{parent_repo_name}' for subjob {job_id_str}"
                        )
                    await postgres_db.merge_job_context(job_id_str, {
                        "git_remote_url": parent.get("context", {}).get("git_remote_url", ""),
                    })
                    async with postgres_db.acquire() as conn:
                        await conn.execute(
                            "UPDATE jobs SET branch_name = $1, repo_name = $2 WHERE id = $3",
                            branch_name, parent_repo_name, result["id"],
                        )
                    result["branch_name"] = branch_name
                    result["repo_name"] = parent_repo_name
            elif project_id:
                # Project job: branch in project's shared jobs repo
                repos = await postgres_db.get_project_repositories(
                    project_id, role="jobs"
                )
                if repos:
                    jobs_repo = repos[0]
                    branch_name = f"job/{short_id}"
                    branch_ok = await gitea_client.create_branch(
                        jobs_repo["name"], branch_name, from_branch="main"
                    )
                    if not branch_ok:
                        logger.error(
                            f"Failed to create branch '{branch_name}' in "
                            f"'{jobs_repo['name']}' — main branch may not exist"
                        )
                    await postgres_db.merge_job_context(job_id_str, {
                        "git_remote_url": jobs_repo["repo_url"],
                    })
                    async with postgres_db.acquire() as conn:
                        await conn.execute(
                            "UPDATE jobs SET branch_name = $1, repo_name = $2 WHERE id = $3",
                            branch_name, jobs_repo["name"], result["id"],
                        )
                    result["branch_name"] = branch_name
                    result["repo_name"] = jobs_repo["name"]
                else:
                    # Project has no jobs repo — fall back to per-job repo
                    repo_name = f"job-{short_id}"
                    git_remote_url = await gitea_client.create_repo(repo_name)
                    if git_remote_url:
                        await postgres_db.merge_job_context(job_id_str, {
                            "git_remote_url": git_remote_url,
                        })
                        async with postgres_db.acquire() as conn:
                            await conn.execute(
                                "UPDATE jobs SET repo_name = $1 WHERE id = $2",
                                repo_name, result["id"],
                            )
                        result["repo_name"] = repo_name
            else:
                # Root job without project: create standalone per-job repo
                repo_name = f"job-{short_id}"
                git_remote_url = await gitea_client.create_repo(repo_name)
                if git_remote_url:
                    await postgres_db.merge_job_context(job_id_str, {
                        "git_remote_url": git_remote_url,
                    })
                    async with postgres_db.acquire() as conn:
                        await conn.execute(
                            "UPDATE jobs SET repo_name = $1 WHERE id = $2",
                            repo_name, result["id"],
                        )
                    result["repo_name"] = repo_name

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

        # Spawn scholar subjob if enabled (root jobs only)
        if not job.parent_job_id:
            try:
                # Re-fetch the job so _spawn_scholar_subjob has repo_name etc.
                fresh_job = await postgres_db.get_job(str(result["id"]))
                if fresh_job:
                    scholar_result = await _spawn_scholar_subjob(
                        fresh_job, config_name, config_override, context,
                    )
                    if scholar_result:
                        result["scholar_job_id"] = str(scholar_result["id"])
            except Exception as e:
                logger.warning(f"Failed to spawn scholar for job {result['id']}: {e}")

        # Trigger auto-assignment dispatcher (fire-and-forget)
        _trigger_dispatch()

        return result
    except HTTPException:
        raise
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

        # Clean up Gitea repo/branch
        if gitea_client.is_initialized:
            if job.get("parent_job_id") and job.get("branch_name") and job.get("repo_name"):
                # Subjob: delete the branch (no-op if already merged and deleted)
                await gitea_client.delete_branch(job["repo_name"], job["branch_name"])
            elif job.get("repo_name"):
                # Root job: delete the entire repo (also deletes subjob branches)
                await gitea_client.delete_repo(job["repo_name"])
            elif job.get("project_id") and job.get("branch_name"):
                # Legacy: project jobs repo branch cleanup
                repos = await postgres_db.get_project_repositories(
                    str(job["project_id"]), role="jobs"
                )
                if repos:
                    await gitea_client.delete_branch(repos[0]["name"], job["branch_name"])

        # Clean up vector DB tables (no FK cascade across databases)
        try:
            async with vector_db.acquire() as conn:
                await conn.execute("DELETE FROM memories WHERE job_id = $1", UUID(job_id))
                await conn.execute("DELETE FROM citations WHERE job_id = $1", UUID(job_id))
                await conn.execute("DELETE FROM source_annotations WHERE job_id = $1", UUID(job_id))
                await conn.execute("DELETE FROM source_tags WHERE job_id = $1", UUID(job_id))
                await conn.execute("DELETE FROM source_embeddings WHERE job_id = $1", UUID(job_id))
                await conn.execute("DELETE FROM job_sources WHERE job_id = $1", UUID(job_id))
        except Exception as e:
            logger.warning(f"Failed to clean up vector DB tables for job {job_id}: {e}")

        success = await postgres_db.delete_job(job_id)
        if not success:
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
        return {"status": "deleted"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/api/jobs/{job_id}/subjob-merge")
async def subjob_merge(job_id: str) -> dict[str, Any]:
    """Squash-merge a completed subjob's branch into its parent's branch.

    Called by the agent after a subjob completes (autonomy=full auto-completion).
    Performs pre-merge cleanup of job-scoped files, then squash merges.
    """
    try:
        job = await postgres_db.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

        if not job.get("parent_job_id"):
            raise HTTPException(
                status_code=400,
                detail="Only subjobs (with parent_job_id) can be squash-merged",
            )

        result = await _squash_merge_subjob(job_id)
        if result is None:
            return {"status": "skipped", "reason": "no branch/repo configured"}

        return {"job_id": job_id, **result}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Squash merge failed for subjob {job_id}: {e}", exc_info=True)
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

        # If job has a VM, send terminate and request deletion
        vm_ctx = (job.get("context") or {}).get("vm") if isinstance(job.get("context"), dict) else None
        if vm_ctx:
            await vm_provisioner.send_control(job_id, "terminate")
            if vm_ctx.get("status") not in ("deleted", "deleting"):
                await vm_provisioner.delete_vm(job_id)

        success = await postgres_db.cancel_job(job_id)
        if not success:
            # Agent may have already set status to 'cancelled' before we got here
            refreshed = await postgres_db.get_job(job_id)
            if refreshed and refreshed.get("status") == "cancelled":
                logger.info(f"Job {job_id} already cancelled (agent beat us to it)")
            else:
                raise HTTPException(
                    status_code=400,
                    detail="Job cannot be cancelled (already completed or cancelled)",
                )

        # If this was a scholar, unblock the parent job
        job["status"] = "cancelled"
        try:
            await _handle_scholar_completion(job, [])
        except Exception as e:
            logger.warning(f"Error handling scholar cancellation for {job_id}: {e}")

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

        # If job has a VM, send freeze via NATS (requires management daemon)
        vm_ctx = (job.get("context") or {}).get("vm") if isinstance(job.get("context"), dict) else None
        if vm_ctx:
            await vm_provisioner.send_control(job_id, "freeze")

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


# =============================================================================
# VM Lifecycle Endpoints (optional — requires NATS)
# =============================================================================


@app.post("/api/vms")
async def create_vm(request: VMCreateRequest) -> dict[str, Any]:
    """Create a VM for a job.

    Uses NATS (cross-cluster) or direct Kubernetes API (same-cluster).
    Returns 503 if no VM provisioning backend is available.
    """
    if not vm_provisioner.is_available:
        raise HTTPException(status_code=503, detail="VM provisioning not available (no NATS or K8s)")

    job = await postgres_db.get_job(request.job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{request.job_id}' not found")

    success = await vm_provisioner.create_vm(
        job_id=request.job_id,
        agent_config=request.agent_config,
        vm_image=request.vm_image,
        cpu_cores=request.cpu_cores,
        memory=request.memory,
        description=request.description,
    )
    if not success:
        raise HTTPException(status_code=500, detail="Failed to create VM")

    return {"status": "provisioning", "job_id": request.job_id, "mode": vm_provisioner.mode}


@app.get("/api/vms")
async def list_vms() -> list[dict[str, Any]]:
    """List jobs with active VMs.

    Works from the database (no NATS required) — reads the 'vm' key from
    each job's context JSONB column.
    """
    try:
        async with postgres_db.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, description, status, context->'vm' as vm_context "
                "FROM jobs WHERE context ? 'vm' "
                "ORDER BY updated_at DESC"
            )
        return [
            {
                "job_id": str(row["id"]),
                "description": row["description"],
                "job_status": row["status"],
                "vm": json.loads(row["vm_context"]) if row["vm_context"] else {},
            }
            for row in rows
        ]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/vms/{job_id}")
async def get_vm_status(
    job_id: str,
    live: bool = Query(False, description="Query live status via NATS request/reply"),
) -> dict[str, Any]:
    """Get VM status for a job.

    By default reads from the database. With ?live=true, also queries the VM
    controller via NATS request/reply for real-time status.
    """
    job = await postgres_db.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

    context = job.get("context") or {}
    vm_ctx = context.get("vm") if isinstance(context, dict) else None
    if not vm_ctx:
        raise HTTPException(status_code=404, detail=f"No VM context for job '{job_id}'")

    result: dict[str, Any] = {"job_id": job_id, "vm": vm_ctx}

    if live:
        if not vm_provisioner.is_available:
            result["live_error"] = "VM provisioning not available"
        else:
            live_status = await vm_provisioner.query_status(job_id)
            if live_status:
                result["live"] = live_status
            else:
                result["live_error"] = "No response from VM controller"

    return result


@app.delete("/api/vms/{job_id}")
async def delete_vm(job_id: str) -> dict[str, str]:
    """Delete a VM for a job.

    Uses NATS (cross-cluster) or direct Kubernetes API (same-cluster).
    Returns 503 if no VM provisioning backend is available.
    """
    if not vm_provisioner.is_available:
        raise HTTPException(status_code=503, detail="VM provisioning not available (no NATS or K8s)")

    job = await postgres_db.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

    success = await vm_provisioner.delete_vm(job_id)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to delete VM")

    return {"status": "deleting", "job_id": job_id}


# =============================================================================
# Sudo Approval Gate
# =============================================================================

from services.sudo_gate import sudo_gate  # noqa: E402


class SudoApproveRequest(BaseModel):
    """Request body for approving a sudo request."""

    reason: str = Field("", description="Optional approval reason")


class SudoDenyRequest(BaseModel):
    """Request body for denying a sudo request."""

    reason: str = Field(..., description="Denial reason (required)")


class SudoRuleCreateRequest(BaseModel):
    """Request body for creating an auto-approval rule."""

    pattern: str = Field(..., description="fnmatch pattern (e.g. 'apt-get install *')")
    action: str = Field(..., description="'approve', 'deny', or 'review'")
    priority: int = Field(100, ge=0, le=1000, description="Lower = higher priority")
    description: str = Field("", description="Human-readable description")


@app.get("/api/sudo/events")
async def sudo_sse_events(request: Request) -> StreamingResponse:
    """SSE stream of sudo approval events.

    Pushes events:
      - new_request: a new sudo request is pending
      - request_decided: a request was approved/denied/expired
    """
    queue = sudo_gate.subscribe_sse()

    async def event_stream():
        try:
            while True:
                # Check if client disconnected
                if await request.is_disconnected():
                    break
                try:
                    event_type, data = await asyncio.wait_for(queue.get(), timeout=30.0)
                    yield f"event: {event_type}\ndata: {json.dumps(data, default=str)}\n\n"
                except asyncio.TimeoutError:
                    # Send keepalive comment
                    yield ": keepalive\n\n"
        finally:
            sudo_gate.unsubscribe_sse(queue)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/sudo/requests")
async def list_sudo_requests(
    job_id: str | None = Query(None, description="Filter by job ID"),
    status: str | None = Query(None, description="Filter by status"),
    limit: int = Query(50, ge=1, le=200),
) -> list[dict]:
    """List sudo approval requests."""
    return await sudo_gate.list_requests(job_id=job_id, status=status, limit=limit)


@app.get("/api/sudo/requests/{request_id}")
async def get_sudo_request(request_id: str) -> dict:
    """Get a single sudo approval request."""
    result = await sudo_gate.get_request(request_id)
    if not result:
        raise HTTPException(status_code=404, detail=f"Sudo request '{request_id}' not found")
    return result


@app.post("/api/sudo/requests/{request_id}/approve")
async def approve_sudo_request(request_id: str, body: SudoApproveRequest | None = None) -> dict:
    """Approve a pending sudo request."""
    reason = body.reason if body else ""
    result = await sudo_gate.approve_request(request_id, reason=reason)
    if not result:
        raise HTTPException(status_code=404, detail=f"Sudo request '{request_id}' not found")
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@app.post("/api/sudo/requests/{request_id}/deny")
async def deny_sudo_request(request_id: str, body: SudoDenyRequest) -> dict:
    """Deny a pending sudo request."""
    result = await sudo_gate.deny_request(request_id, reason=body.reason)
    if not result:
        raise HTTPException(status_code=404, detail=f"Sudo request '{request_id}' not found")
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@app.get("/api/sudo/rules")
async def list_sudo_rules() -> list[dict]:
    """List auto-approval rules."""
    return await sudo_gate.list_rules()


@app.post("/api/sudo/rules")
async def create_sudo_rule(body: SudoRuleCreateRequest) -> dict:
    """Create an auto-approval rule."""
    if body.action not in ("approve", "deny", "review"):
        raise HTTPException(status_code=400, detail="action must be 'approve', 'deny', or 'review'")
    result = await sudo_gate.create_rule(
        pattern=body.pattern,
        action=body.action,
        priority=body.priority,
        description=body.description,
    )
    if not result:
        raise HTTPException(status_code=500, detail="Failed to create rule")
    return result


@app.delete("/api/sudo/rules/{rule_id}")
async def delete_sudo_rule(rule_id: str) -> dict:
    """Delete an auto-approval rule."""
    if not await sudo_gate.delete_rule(rule_id):
        raise HTTPException(status_code=404, detail=f"Rule '{rule_id}' not found")
    return {"status": "deleted", "id": rule_id}


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

        # If no agent or agent is unavailable, find a ready one.
        # Includes "working" because critic verdict handlers call resume while the
        # same agent is still finishing post-completion work (agent clears its job
        # status after the graph loop but before the heartbeat propagates).
        if not agent or agent["status"] in ("offline", "failed", "working"):
            ready_agents = await postgres_db.list_agents(status="ready", limit=1)
            if not ready_agents:
                # No agent available right now — queue job for auto-dispatch.
                # Store feedback in context so it's available when dispatched,
                # set status to 'paused' (dispatchable), and let the dispatcher
                # pick it up when an agent becomes free.
                feedback = request.feedback if request else None
                if feedback:
                    job_context = job.get("context") or {}
                    if isinstance(job_context, str):
                        try:
                            job_context = json.loads(job_context)
                        except json.JSONDecodeError:
                            job_context = {}
                    job_context["queued_feedback"] = feedback
                    async with postgres_db.acquire() as conn:
                        await conn.execute(
                            "UPDATE jobs SET context = $1::jsonb WHERE id = $2::uuid",
                            json.dumps(job_context), job_id,
                        )

                async with postgres_db.acquire() as conn:
                    await conn.execute(
                        "UPDATE jobs SET status = 'paused', assigned_agent_id = NULL, "
                        "updated_at = CURRENT_TIMESTAMP WHERE id = $1::uuid",
                        job_id,
                    )
                logger.info(
                    f"No agents available — queued job {job_id} for auto-dispatch "
                    f"(previous status: {job['status']}, feedback: {bool(feedback)})"
                )
                _trigger_dispatch()
                return {
                    "status": "queued",
                    "message": "No agents available, job queued for auto-dispatch",
                    "job_id": job_id,
                }
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
            try:
                job_context = json.loads(job_context)
            except json.JSONDecodeError:
                job_context = {}

        # Same for config_override
        config_override = job.get("config_override")
        if isinstance(config_override, str):
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

        # Restore S3 environment snapshot into the VM before resuming.
        # This gives true "pick up where you left off" (environment + state).
        # Non-blocking: if restore fails, resume proceeds without it.
        snapshot_restored = False
        if snapshot_service.is_available:
            vm_ctx = job_context.get("vm", {}) if job_context else {}
            ssh_host = vm_ctx.get("ssh_host") or vm_ctx.get("pod_ip")
            ssh_port = vm_ctx.get("ssh_port")
            if ssh_host and ssh_port:
                try:
                    snapshot_restored = await ide_session_service.restore_snapshot_for_resume(
                        job_id, ssh_host, int(ssh_port)
                    )
                    if snapshot_restored:
                        logger.info(f"Snapshot restored for job {job_id} resume")
                except Exception as e:
                    logger.warning(f"Snapshot restore failed for job {job_id} resume (non-blocking): {e}")

        resume_payload = {
            "job_id": job_id,
            "config_name": job_config_name,
            "config_upload_id": job_context.get("config_upload_id") if job_context else None,
            "config_override": config_override,
            "datasources": datasources_payload,
            "previous_status": job["status"],
            "snapshot_restored": snapshot_restored,
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
        repo_name, job_branch = await resolve_job_repo(job_id)

        # Primary: read freeze_data from DB
        if job.get("freeze_data"):
            frozen_data = job["freeze_data"]
            if isinstance(frozen_data, str):
                frozen_data = json.loads(frozen_data)

        # Fallback: Gitea
        if frozen_data is None and gitea_client.is_initialized:
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

        # Squash-merge subjob branch into parent if applicable
        merge_result = None
        if job.get("parent_job_id"):
            merge_result = await _squash_merge_subjob(job_id)

        # Agent is freed after completion — trigger dispatcher
        _trigger_dispatch()

        result = {
            "status": "approved",
            "job_id": job_id,
            "summary": completion_data.get("summary", ""),
            "deliverables": completion_data.get("deliverables", []),
            "approved_at": completion_data["approved_at"],
        }
        if merge_result:
            result["merge"] = merge_result
        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Failed to approve job {job_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


# =============================================================================
# Job Completion Handling (orchestrator-side)
# =============================================================================

async def _internal_resume_job(job_id: str, feedback: str) -> None:
    """Queue a job for resume via the auto-dispatcher.

    Stores feedback in the job's context as ``queued_feedback``, sets status
    to ``paused`` (dispatchable), and triggers the dispatcher.  Avoids HTTP
    self-calls — the dispatcher will pick it up and send it to an agent.
    """
    job = await postgres_db.get_job(job_id)
    if not job:
        logger.warning(f"_internal_resume_job: job {job_id} not found")
        return

    # Store feedback in context
    job_context = job.get("context") or {}
    if isinstance(job_context, str):
        try:
            job_context = json.loads(job_context)
        except json.JSONDecodeError:
            job_context = {}
    job_context["queued_feedback"] = feedback
    async with postgres_db.acquire() as conn:
        await conn.execute(
            "UPDATE jobs SET context = $1::jsonb, status = 'paused', "
            "assigned_agent_id = NULL, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = $2::uuid",
            json.dumps(job_context), job_id,
        )
    logger.info(f"Queued job {job_id} for auto-dispatch with feedback")
    _trigger_dispatch()


async def _set_target_to_autonomy_status(target_job_id: str) -> str:
    """Set a target job's status based on its autonomy level.

    Reads ``resolved_config`` from the target job to determine autonomy:
      - ``full`` -> ``completed``
      - anything else -> ``pending_review``

    Returns:
        The new status string.
    """
    from services.completion import get_autonomy_level

    job = await postgres_db.get_job(target_job_id)
    if not job:
        logger.warning(f"_set_target_to_autonomy_status: job {target_job_id} not found")
        return "unknown"

    autonomy = get_autonomy_level(job)

    if autonomy == "full":
        async with postgres_db.acquire() as conn:
            await conn.execute(
                "UPDATE jobs SET status = 'completed', completed_at = NOW() WHERE id = $1::uuid",
                target_job_id,
            )
        logger.info(f"Set target job {target_job_id} to 'completed' (autonomy=full)")
        return "completed"
    else:
        await postgres_db.update_job_status(target_job_id, status="pending_review")
        logger.info(f"Set target job {target_job_id} to 'pending_review' (autonomy={autonomy})")
        return "pending_review"


async def _spawn_scholar_subjob(
    job: dict[str, Any],
    config_name: str,
    config_override: dict[str, Any] | None,
    context: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Spawn a scholar research job before the main job starts.

    Called at job creation time.  Checks the scholar config from disk
    (since resolved_config is not yet available) and, if enabled,
    holds the parent job in 'waiting' while the scholar runs.

    Returns the created scholar job dict, or None if skipped.
    """
    from services.completion import (
        format_scholar_instructions,
        resolve_scholar_config_from_disk,
    )

    job_id = str(job["id"])

    # Guard: never spawn scholars for subjobs (no recursion)
    if job.get("parent_job_id"):
        return None

    scholar_config = resolve_scholar_config_from_disk(config_name, config_override)
    if not scholar_config.get("enabled", False):
        logger.debug(f"Scholar not enabled for job {job_id} (config={config_name})")
        return None

    scholar_config_name = scholar_config.get("scholar_config", "scholar")
    description = job.get("description", "")
    parent_instructions = (context or {}).get("instructions")

    # Format scholar instructions from template
    instructions = format_scholar_instructions(
        parent_job_id=job_id,
        description=description,
        config_name=config_name,
        instructions=parent_instructions,
    )

    scholar_description = f"Research phase for: {description[:200]}"

    scholar_context: dict[str, Any] = {
        "scholar_target": job_id,
        "original_description": description,
        "instructions": instructions,
    }
    if parent_instructions:
        scholar_context["parent_instructions"] = parent_instructions

    # Disable nested subjob spawning on the scholar
    scholar_override: dict[str, Any] = {
        "scholar": {"enabled": False},
        "verification": {"enabled": False},
        "curator": {"enabled": False},
        "autonomy": "full",
    }

    project_id = str(job["project_id"]) if job.get("project_id") else None

    logger.info(
        f"Creating scholar subjob for job {job_id} "
        f"(scholar_config={scholar_config_name})"
    )

    # Hold the parent job
    await postgres_db.update_job_status(job_id, status="waiting")

    scholar_job = await postgres_db.create_job(
        description=scholar_description,
        config_name=scholar_config_name,
        config_override=scholar_override,
        context=scholar_context,
        parent_job_id=job_id,
        project_id=project_id,
        priority=10,
    )

    scholar_job_id = str(scholar_job["id"])
    short_id = scholar_job_id[:8]

    # Set up Gitea branch for the scholar subjob
    if gitea_client.is_initialized:
        parent_repo_name = job.get("repo_name")
        if not parent_repo_name:
            parent_repo_name = f"job-{str(job['id'])[:8]}"

        from_branch = job.get("branch_name") or "main"
        branch_name = f"subjob/{short_id}/{scholar_config_name}"
        try:
            branch_ok = await gitea_client.create_branch(
                parent_repo_name, branch_name, from_branch=from_branch
            )
            if not branch_ok:
                logger.error(
                    f"Failed to create branch '{branch_name}' from '{from_branch}' "
                    f"in '{parent_repo_name}' for scholar {scholar_job_id}"
                )
            # Propagate git remote URL
            parent_context = job.get("context") or {}
            if isinstance(parent_context, str):
                try:
                    parent_context = json.loads(parent_context)
                except (json.JSONDecodeError, ValueError):
                    parent_context = {}
            git_remote_url = parent_context.get("git_remote_url", "")

            scholar_ctx = dict(scholar_context)
            scholar_ctx["git_remote_url"] = git_remote_url
            await postgres_db.update_job_context(scholar_job_id, scholar_ctx)

            async with postgres_db.acquire() as conn:
                await conn.execute(
                    "UPDATE jobs SET branch_name = $1, repo_name = $2 WHERE id = $3::uuid",
                    branch_name, parent_repo_name, scholar_job_id,
                )
        except Exception as e:
            logger.warning(f"Failed to create Gitea branch for scholar {scholar_job_id}: {e}")

    _trigger_dispatch()
    logger.info(f"Scholar job {scholar_job_id} created for parent {job_id}")
    return scholar_job


async def _handle_scholar_completion(
    job: dict[str, Any],
    actions: list[str],
) -> None:
    """After a scholar subjob completes or fails, unblock its parent job.

    The scholar's branch has already been merged by ``_squash_merge_subjob``
    (called earlier in ``complete_job``).  We just need to transition the
    parent from 'waiting' to 'created' so the dispatcher picks it up.
    """
    parent_job_id = job.get("parent_job_id")
    if parent_job_id is None:
        return

    # Identify scholar jobs by context
    ctx_raw = job.get("context")
    if isinstance(ctx_raw, str):
        try:
            ctx = json.loads(ctx_raw)
        except (json.JSONDecodeError, ValueError):
            ctx = {}
    else:
        ctx = ctx_raw or {}

    if not ctx.get("scholar_target"):
        return  # Not a scholar job

    job_id = str(job["id"])
    target_id = str(parent_job_id)
    job_status = job.get("status", "")
    is_failure = job_status in ("failed", "cancelled")

    parent = await postgres_db.get_job(target_id)
    if not parent:
        logger.warning(f"Scholar {job_id} parent {target_id} not found")
        return

    if parent.get("status") != "waiting":
        logger.debug(
            f"Scholar {job_id} parent {target_id} not in 'waiting' "
            f"(status={parent.get('status')}) — skipping unblock"
        )
        return

    # Inject scholar metadata into parent context
    parent_ctx = parent.get("context") or {}
    if isinstance(parent_ctx, str):
        try:
            parent_ctx = json.loads(parent_ctx)
        except (json.JSONDecodeError, ValueError):
            parent_ctx = {}
    parent_ctx = dict(parent_ctx)

    if is_failure:
        parent_ctx["scholar_failed"] = True
        logger.warning(
            f"Scholar {job_id} {job_status} — unblocking parent {target_id} without research"
        )
        actions.append(f"scholar {job_id} {job_status}, parent {target_id} unblocked (no research)")
    else:
        parent_ctx["scholar_completed"] = True
        parent_ctx["scholar_output_dir"] = "research"
        logger.info(f"Scholar {job_id} completed — unblocking parent {target_id}")
        actions.append(f"scholar {job_id} completed, parent {target_id} unblocked")

    await postgres_db.update_job_context(target_id, parent_ctx)
    await postgres_db.update_job_status(target_id, status="created")
    _trigger_dispatch()


async def _handle_critic_verdict_on_complete(
    job: dict[str, Any],
    actions: list[str],
) -> None:
    """Handle deferred critic verdict after a critic job completes.

    If this job has a parent_job_id and freeze_data with a verdict,
    process the approve/return logic.
    """
    from services.completion import (
        _parse_freeze_data,
        is_curation_enabled,
    )

    parent_job_id = job.get("parent_job_id")
    if parent_job_id is None:
        return  # Not a subjob

    job_id = str(job["id"])

    # Skip scholar jobs — they are not critics
    ctx_raw = job.get("context")
    if isinstance(ctx_raw, str):
        try:
            ctx_dict = json.loads(ctx_raw)
        except (json.JSONDecodeError, ValueError):
            ctx_dict = {}
    else:
        ctx_dict = ctx_raw if isinstance(ctx_raw, dict) else {}
    if ctx_dict.get("scholar_target"):
        logger.debug(f"Job {job_id} is a scholar — skipping critic verdict handling")
        return

    target_job_id = str(parent_job_id)

    freeze_data = _parse_freeze_data(job)
    if not freeze_data:
        logger.debug(f"No freeze_data for critic job {job_id} — no verdict to process")
        return

    verdict = freeze_data.get("verdict")
    if not verdict:
        # Critic completed without using approve_job/return_job_with_feedback.
        # If the job completed normally (not failed), treat as implicit approval
        # so the target job doesn't get stuck in "reviewing".
        if job.get("status") == "completed":
            logger.warning(
                f"Critic job {job_id} completed without verdict — "
                f"treating as implicit approval for target {target_job_id}"
            )
            verdict = "approved"
        else:
            logger.debug(f"No verdict in freeze_data for critic job {job_id}")
            return

    # Parse critic context for round tracking
    ctx_raw = job.get("context")
    if isinstance(ctx_raw, str):
        try:
            critic_context = json.loads(ctx_raw)
        except (json.JSONDecodeError, ValueError):
            critic_context = {}
    else:
        critic_context = ctx_raw or {}

    if verdict == "approved":
        logger.info(f"Critic {job_id} approved target {target_job_id}")
        new_status = await _set_target_to_autonomy_status(target_job_id)
        actions.append(f"target {target_job_id} set to '{new_status}' (approved)")

        # Trigger curator final pass if curation is enabled on the TARGET job
        target_job = await postgres_db.get_job(target_job_id)
        if target_job and is_curation_enabled(target_job):
            await _trigger_curation_final_pass(target_job_id, target_job)
            actions.append(f"curation final pass triggered for {target_job_id}")

    elif verdict == "returned":
        current_round = critic_context.get("verification_round", 0)
        max_rounds = critic_context.get("max_verification_rounds", 3)

        if max_rounds > 0 and current_round >= max_rounds:
            # Round limit reached — auto-accept
            logger.warning(
                f"Critic {job_id} returned feedback but round limit reached "
                f"({current_round}/{max_rounds}). Auto-accepting target {target_job_id}."
            )
            await postgres_db.update_job_status(
                job_id,
                status="failed",
                error_message=f"Verification limit reached ({max_rounds} rounds)",
            )
            new_status = await _set_target_to_autonomy_status(target_job_id)
            actions.append(
                f"critic {job_id} failed (round limit), "
                f"target {target_job_id} auto-accepted to '{new_status}'"
            )
            # Trigger curation even on auto-accept
            target_job = await postgres_db.get_job(target_job_id)
            if target_job and is_curation_enabled(target_job):
                await _trigger_curation_final_pass(target_job_id, target_job)
                actions.append(f"curation final pass triggered for {target_job_id}")
        else:
            # Resume target with feedback
            feedback = freeze_data.get("feedback", "")
            logger.info(
                f"Critic {job_id} returned feedback for target {target_job_id} "
                f"(round {current_round}/{max_rounds})"
            )
            await _internal_resume_job(target_job_id, feedback=feedback)
            actions.append(f"target {target_job_id} resumed with feedback (round {current_round})")
    else:
        logger.warning(f"Unknown verdict '{verdict}' for critic job {job_id}")


async def _trigger_verification_on_complete(
    job: dict[str, Any],
    result: dict[str, Any],
    actions: list[str],
) -> None:
    """Spawn or resume a critic verification job after a main job completes.

    Guards:
    1. No error, should_stop is True
    2. Not a subjob (no parent_job_id)
    3. freeze_data indicates job completion (not phase boundary)
    4. Verification enabled in resolved_config
    """
    from services.completion import (
        _parse_freeze_data,
        format_verification_instructions,
        get_verification_config,
        is_job_completion_freeze,
        is_verification_enabled,
    )

    job_id = str(job["id"])

    # Guards
    if result.get("error"):
        return
    if not result.get("should_stop", False):
        return
    if job.get("parent_job_id") is not None:
        logger.debug(f"Skipping verification for {job_id} — it is a sub-job")
        return
    if not is_verification_enabled(job):
        logger.debug(f"Verification not enabled for job {job_id}")
        return
    # Check if this is a job completion (not a phase boundary).
    # Accept freeze_data OR status=reviewing (set by determine_job_status when
    # goal_achieved is True) OR freeze_data sent in the request body.
    if not is_job_completion_freeze(job) and job.get("status") != "reviewing":
        logger.debug(f"Skipping verification for {job_id} — not a job completion freeze")
        return

    verification_config = get_verification_config(job)
    freeze_data = _parse_freeze_data(job) or {}

    # Check for an existing waiting critic job (subsequent rounds)
    async with postgres_db.acquire() as conn:
        critic_row = await conn.fetchrow(
            "SELECT id, status, context FROM jobs "
            "WHERE parent_job_id = $1::uuid AND status = 'waiting'",
            job_id,
        )

    if critic_row:
        # Subsequent round: resume existing critic
        critic_id = str(critic_row["id"])
        ctx_raw = critic_row.get("context")
        if isinstance(ctx_raw, str):
            try:
                critic_context = json.loads(ctx_raw)
            except (json.JSONDecodeError, ValueError):
                critic_context = {}
        else:
            critic_context = ctx_raw or {}

        new_round = critic_context.get("verification_round", 0) + 1
        critic_context["verification_round"] = new_round
        critic_context["deliverables"] = freeze_data.get("deliverables", [])
        critic_context["summary"] = freeze_data.get("summary", "")
        critic_context["confidence"] = freeze_data.get("confidence", 0)

        async with postgres_db.acquire() as conn:
            await conn.execute(
                "UPDATE jobs SET context = $1::jsonb WHERE id = $2::uuid",
                json.dumps(critic_context), critic_id,
            )

        logger.info(f"Resuming existing critic {critic_id} for job {job_id} (round {new_round})")
        await _internal_resume_job(
            critic_id,
            feedback=(
                f"Target job addressed your feedback (round {new_round}). "
                f"Review the updated deliverables and either approve or return with new feedback."
            ),
        )
        actions.append(f"critic {critic_id} resumed (round {new_round})")
    else:
        # First round: create new critic job
        critic_config = verification_config.get("critic_config", "critic")
        max_rounds = verification_config.get("max_rounds", 3)
        config_name = job.get("config_name", "unknown")

        # Format instructions
        instructions = format_verification_instructions(
            job_id=job_id,
            description=job.get("description", ""),
            freeze_data=freeze_data,
            config_name=config_name,
        )
        if not instructions:
            logger.error(f"Failed to format verification instructions for job {job_id}")
            return

        verification_description = (
            f"Verify deliverables of job {job_id} ({config_name}). "
            f"Review output against original requirements and either approve or return with feedback."
        )

        context = {
            "verification_target": job_id,
            "original_description": job.get("description", ""),
            "original_config": config_name,
            "deliverables": freeze_data.get("deliverables", []),
            "summary": freeze_data.get("summary", ""),
            "confidence": freeze_data.get("confidence", 0),
            "verification_round": 0,
            "max_verification_rounds": max_rounds,
        }

        config_override = {
            "autonomy": "full",
            "tools": {
                "evaluation": ["approve_job", "return_job_with_feedback"],
            },
            "llm": {
                "model": "openrouter/minimax/minimax-m2.5",
                "reasoning_level": "xhigh",
                "strategic": {
                    "model": "openrouter/minimax/minimax-m2.5",
                    "reasoning_level": "xhigh",
                },
                "tactical": {
                    "model": "openrouter/minimax/minimax-m2.5",
                    "reasoning_level": "xhigh",
                },
            },
        }

        project_id = str(job["project_id"]) if job.get("project_id") else None

        logger.info(
            f"Creating verification job for {job_id} "
            f"(critic_config={critic_config}, max_rounds={max_rounds})"
        )

        critic_job = await postgres_db.create_job(
            description=verification_description,
            config_name=critic_config,
            config_override=config_override,
            context=context,
            parent_job_id=job_id,
            project_id=project_id,
            priority=10,
        )

        critic_job_id = str(critic_job["id"])
        short_id = critic_job_id[:8]

        # Set up Gitea branch for the subjob (same logic as create_job endpoint)
        if gitea_client.is_initialized:
            parent_repo_name = job.get("repo_name")
            if not parent_repo_name:
                parent_repo_name = f"job-{str(job['id'])[:8]}"

            from_branch = job.get("branch_name") or "main"
            branch_name = f"subjob/{short_id}/{critic_config}"
            try:
                branch_ok = await gitea_client.create_branch(
                    parent_repo_name, branch_name, from_branch=from_branch
                )
                if not branch_ok:
                    logger.error(
                        f"Failed to create branch '{branch_name}' from '{from_branch}' "
                        f"in '{parent_repo_name}' for critic {critic_job_id}"
                    )
                # Propagate git remote URL and update branch/repo on the critic job
                parent_context = job.get("context") or {}
                if isinstance(parent_context, str):
                    try:
                        parent_context = json.loads(parent_context)
                    except (json.JSONDecodeError, ValueError):
                        parent_context = {}
                git_remote_url = parent_context.get("git_remote_url", "")

                critic_ctx = dict(context)
                critic_ctx["git_remote_url"] = git_remote_url
                await postgres_db.update_job_context(critic_job_id, critic_ctx)

                async with postgres_db.acquire() as conn:
                    await conn.execute(
                        "UPDATE jobs SET branch_name = $1, repo_name = $2 WHERE id = $3::uuid",
                        branch_name, parent_repo_name, critic_job_id,
                    )
            except Exception as e:
                logger.warning(f"Failed to create Gitea branch for critic {critic_job_id}: {e}")

        _trigger_dispatch()
        actions.append(f"critic job {critic_job_id} created")
        logger.info(f"Verification job {critic_job_id} created for job {job_id}")


async def _trigger_curation_final_pass(
    target_job_id: str,
    target_job: dict[str, Any] | None = None,
) -> None:
    """Resume the waiting curator with a final-pass signal.

    Called after critic approval (or auto-accept) when curation is enabled.
    """
    from services.completion import get_curation_config, is_curation_enabled

    if target_job is None:
        target_job = await postgres_db.get_job(target_job_id)
    if not target_job:
        return
    if not is_curation_enabled(target_job):
        return

    curator_config_name = get_curation_config(target_job).get("curator_config", "curator")

    # Find a waiting curator for this target job
    async with postgres_db.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT id, status FROM jobs
               WHERE parent_job_id = $1::uuid AND config_name = $2
               AND status IN ('waiting', 'paused')
               ORDER BY created_at DESC LIMIT 1""",
            target_job_id, curator_config_name,
        )

    if not row:
        logger.debug(f"No waiting curator found for job {target_job_id}")
        return

    if row["status"] == "completed":
        return

    curator_id = str(row["id"])
    logger.info(f"Triggering curation final pass via curator {curator_id} for {target_job_id}")
    await _internal_resume_job(
        curator_id,
        feedback=(
            "FINAL CURATION PASS. The target job has been approved by the critic. "
            "Do a comprehensive final sweep: read memories, output/, and the final "
            "workspace.md. Promote valuable memories to knowledge notes. Write a "
            "`state` note summarizing what changed. Check for open questions. "
            "Link all notes. Then call job_complete."
        ),
    )


@app.post("/api/jobs/{job_id}/complete")
async def complete_job(job_id: str, request: JobCompleteRequest) -> dict[str, Any]:
    """Handle job completion reported by the agent.

    The agent calls this after the graph finishes. The orchestrator handles
    all post-completion logic: status determination, critic verdict handling,
    verification job spawning, curation final pass, and dispatch.

    This replaces the agent-side ``_update_job_status_from_result``,
    ``_handle_critic_verdict``, and ``_maybe_trigger_verification`` functions.
    """
    from services.completion import (
        determine_job_status,
        is_curation_enabled,
        is_verification_enabled,
    )

    try:
        job = await postgres_db.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

        if job["status"] not in ("processing", "reviewing", "pending_review", "completed"):
            raise HTTPException(
                status_code=400,
                detail=f"Job cannot be completed (status: {job['status']})",
            )

        result = request.model_dump()
        actions: list[str] = []

        # Backfill freeze_data from the request if the DB doesn't have it
        if not job.get("freeze_data") and result.get("freeze_data"):
            job["freeze_data"] = result["freeze_data"]
            try:
                async with postgres_db.acquire() as conn:
                    await conn.execute(
                        "UPDATE jobs SET freeze_data = $1::jsonb WHERE id = $2::uuid AND freeze_data IS NULL",
                        json.dumps(result["freeze_data"]),
                        job_id,
                    )
            except Exception as e:
                logger.warning(f"Failed to backfill freeze_data for {job_id}: {e}")

        # 0. VM recovery: if workspace became unavailable, re-provision and re-queue
        error = result.get("error") or {}
        if isinstance(error, dict) and error.get("type") == "workspace_unavailable":
            logger.warning(
                f"Job {job_id}: workspace unavailable — attempting VM recovery"
            )
            vm_ctx = _get_vm_context(job)
            # Delete the old (crashed) VM
            if vm_ctx and vm_ctx.get("status") not in ("deleted", "deleting"):
                await vm_provisioner.delete_vm(job_id)
            # Reset VM context to trigger auto-provisioning on next dispatch
            ctx = job.get("context") or {}
            if isinstance(ctx, str):
                ctx = json.loads(ctx)
            ctx["vm"] = {"requested": True, "previous_error": "workspace_unavailable"}
            await postgres_db.update_job_context(job_id, ctx)
            # Put job back in queue as paused (dispatchable, clears assigned_agent_id)
            await postgres_db.pause_job(job_id)
            _trigger_dispatch()
            return {
                "status": "handled",
                "job_id": job_id,
                "new_status": "paused",
                "actions": ["vm recovery: old VM deleted, new VM will be provisioned, job re-queued"],
            }

        # 1. Determine and set the new job status
        new_status, error_message = determine_job_status(job, result)
        if new_status:
            kwargs: dict[str, Any] = {"status": new_status}
            if error_message:
                kwargs["error_message"] = error_message
            await postgres_db.update_job_status(job_id, **kwargs)
            actions.append(f"status -> {new_status}")
            logger.info(f"Job {job_id} status set to '{new_status}'")

            # Update job dict with new status for downstream checks
            job["status"] = new_status

        # 2. Subjob merge (if this is a subjob with a branch)
        if job.get("parent_job_id"):
            merge_result = await _squash_merge_subjob(job_id)
            if merge_result:
                actions.append("subjob branch merged")

        # 3. Handle critic verdict (if this is a critic job)
        try:
            await _handle_critic_verdict_on_complete(job, actions)
        except Exception as e:
            logger.error(f"Error handling critic verdict for {job_id}: {e}", exc_info=True)

        # 3b. Handle scholar completion (unblock parent job)
        try:
            await _handle_scholar_completion(job, actions)
        except Exception as e:
            logger.error(f"Error handling scholar completion for {job_id}: {e}", exc_info=True)

        # 4. Trigger verification (if this is a main job that completed)
        try:
            await _trigger_verification_on_complete(job, result, actions)
        except Exception as e:
            logger.error(f"Error triggering verification for {job_id}: {e}", exc_info=True)

        # 5. Curation final pass (if no verification but curation enabled, and goal achieved)
        if (
            not is_verification_enabled(job)
            and is_curation_enabled(job)
            and result.get("should_stop")
            and result.get("goal_achieved")
        ):
            try:
                await _trigger_curation_final_pass(job_id, job)
                actions.append("curation final pass triggered (no verification)")
            except Exception as e:
                logger.error(f"Error triggering curation for {job_id}: {e}", exc_info=True)

        # 6. Trigger dispatch (freed agent can pick up queued work)
        _trigger_dispatch()

        # 7. Capture environment snapshot to S3 (non-blocking)
        if job.get("status") in ("completed", "failed") and snapshot_service.is_available:
            snapshot_on_failure = job.get("status") == "completed" or True  # on_failure=true by default
            if snapshot_on_failure:
                ctx = job.get("context") or {}
                if isinstance(ctx, str):
                    ctx = json.loads(ctx)
                vm_ctx = ctx.get("vm", {})
                ssh_host = vm_ctx.get("ssh_host")
                ssh_port = vm_ctx.get("ssh_port")

                if ssh_host and ssh_port and vm_ctx.get("status") not in ("deleted", "deleting"):
                    try:
                        config_name = job.get("config_name") or "defaults"
                        await snapshot_service.capture_vm_snapshot(
                            job_id=job_id,
                            ssh_host=ssh_host,
                            ssh_port=int(ssh_port),
                            source_type="vm",
                            agent_config=config_name,
                        )
                        actions.append("snapshot captured")
                    except Exception as e:
                        logger.warning(
                            f"Snapshot capture failed for job {job_id} (non-blocking): {e}"
                        )
                        actions.append(f"snapshot capture failed: {e}")

        # 8. If job had a VM, request teardown
        if job.get("status") in ("completed", "failed") and vm_provisioner.is_available:
            vm_ctx = (job.get("context") or {}).get("vm") if isinstance(job.get("context"), dict) else None
            if vm_ctx and vm_ctx.get("status") not in ("deleted", "deleting"):
                await vm_provisioner.delete_vm(job_id)
                actions.append("vm deletion requested")

        return {
            "status": "handled",
            "job_id": job_id,
            "new_status": new_status or job["status"],
            "actions": actions,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Failed to handle completion for job {job_id}: {e}")
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

        # Fallback: Gitea
        if frozen_data is None and gitea_client.is_initialized:
            repo_name, job_branch = await resolve_job_repo(job_id)
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


@app.get("/api/jobs/{job_id}/snapshot")
async def get_job_snapshot(job_id: str) -> dict[str, Any]:
    """Get snapshot metadata for a job.

    Returns status, source type, size, and environment summary.
    Used by the cockpit to show snapshot availability indicators.
    """
    try:
        result = await snapshot_service.get_snapshot_status(job_id)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.delete("/api/jobs/{job_id}/snapshot")
async def delete_job_snapshot(job_id: str) -> dict[str, Any]:
    """Delete all snapshots for a job from S3."""
    try:
        success = await snapshot_service.delete_snapshot(job_id)
        if not success:
            raise HTTPException(
                status_code=500, detail="Failed to delete snapshot"
            )
        return {"status": "deleted", "job_id": job_id}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.put("/api/jobs/{job_id}/snapshot/pin")
async def toggle_snapshot_pin(job_id: str) -> dict[str, Any]:
    """Toggle pin state on a snapshot (GC exemption).

    Pinned snapshots are exempt from automatic garbage collection.
    """
    try:
        new_value = await snapshot_service.toggle_pin(job_id)
        return {"job_id": job_id, "pinned": new_value}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/snapshots/stats")
async def get_snapshot_stats() -> dict[str, Any]:
    """Get aggregate snapshot storage statistics.

    Returns total snapshot count, total size, GC pending info.
    """
    try:
        return await snapshot_service.get_storage_stats()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


class IdeSessionRequest(BaseModel):
    """Request body for starting an IDE session."""

    cpu_cores: int = Field(2, description="VM CPU cores")
    memory: str = Field("4Gi", description="VM memory")
    idle_timeout_minutes: int | None = Field(
        None, description="Override default idle timeout"
    )


@app.post("/api/jobs/{job_id}/ide")
async def start_ide_session(job_id: str, request: IdeSessionRequest | None = None) -> dict[str, Any]:
    """Start or get an IDE session for a job.

    Idempotent: if a session is already active, returns it.
    If restoring, returns current progress status.
    """
    if request is None:
        request = IdeSessionRequest()

    try:
        result = await ide_session_service.start_session(
            job_id=job_id,
            cpu_cores=request.cpu_cores,
            memory=request.memory,
            idle_timeout_minutes=request.idle_timeout_minutes,
        )
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/jobs/{job_id}/ide")
async def get_ide_session(job_id: str) -> dict[str, Any]:
    """Get IDE session status and URL.

    Used by the cockpit to poll session state and determine
    IDE button visibility/behavior.
    """
    try:
        return await ide_session_service.get_session_status(job_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.delete("/api/jobs/{job_id}/ide")
async def stop_ide_session(job_id: str) -> dict[str, Any]:
    """Tear down an active IDE session.

    Deletes the restored VM and marks the session as expired.
    The underlying S3 snapshot is preserved for future restores.
    """
    try:
        result = await ide_session_service.stop_session(job_id)
        return result
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
        "webdav": {
            "category": "cloud",
            "read": ["cloud_list", "cloud_read", "cloud_info"],
            "write": ["cloud_list", "cloud_read", "cloud_info", "cloud_write", "cloud_delete"],
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
        async with vector_db.acquire() as conn:
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
        async with vector_db.acquire() as conn:
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
        async with vector_db.acquire() as conn:
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
        async with vector_db.acquire() as conn:
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
        async with vector_db.acquire() as conn:
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
        async with vector_db.acquire() as conn:
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
        async with vector_db.acquire() as conn:
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


@app.get("/api/jobs/{job_id}/memory/stats")
async def get_memory_stats(job_id: str) -> dict[str, Any]:
    """Get memory statistics for a job."""
    try:
        async with vector_db.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    COUNT(*) AS total,
                    COALESCE(SUM(token_count), 0) AS total_tokens,
                    COALESCE(SUM(access_count), 0) AS total_accesses,
                    COUNT(*) FILTER (WHERE memory_type = 'factual') AS factual,
                    COUNT(*) FILTER (WHERE memory_type = 'procedural') AS procedural,
                    COUNT(*) FILTER (WHERE memory_type = 'error_solution') AS error_solution,
                    COUNT(*) FILTER (WHERE memory_type = 'vocabulary') AS vocabulary,
                    COUNT(*) FILTER (WHERE memory_type = 'relational') AS relational,
                    COUNT(*) FILTER (WHERE source = 'observer') AS from_observer,
                    COUNT(*) FILTER (WHERE source = 'todo') AS from_todo,
                    COUNT(*) FILTER (WHERE source = 'compaction') AS from_compaction,
                    COUNT(*) FILTER (WHERE source = 'phase_archive') AS from_phase_archive,
                    COUNT(*) FILTER (WHERE source = 'tool_error') AS from_tool_error,
                    AVG(importance) AS avg_importance
                FROM memories
                WHERE job_id = $1::uuid
                """,
                job_id,
            )
            if row:
                result = dict(row)
                # Convert Decimal avg_importance to float for JSON serialization
                if result.get("avg_importance") is not None:
                    result["avg_importance"] = float(result["avg_importance"])
                result["job_id"] = job_id
                return result
            return {"job_id": job_id, "total": 0, "total_tokens": 0}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/projects/{project_id}/memory/stats")
async def get_project_memory_stats(project_id: str) -> dict[str, Any]:
    """Get memory statistics for a project (all memories scoped to this project)."""
    try:
        async with vector_db.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    COUNT(*) AS total,
                    COALESCE(SUM(token_count), 0) AS total_tokens,
                    COALESCE(SUM(access_count), 0) AS total_accesses,
                    COUNT(*) FILTER (WHERE memory_type = 'factual') AS factual,
                    COUNT(*) FILTER (WHERE memory_type = 'procedural') AS procedural,
                    COUNT(*) FILTER (WHERE memory_type = 'error_solution') AS error_solution,
                    COUNT(*) FILTER (WHERE memory_type = 'vocabulary') AS vocabulary,
                    COUNT(*) FILTER (WHERE memory_type = 'relational') AS relational,
                    COUNT(*) FILTER (WHERE source = 'observer') AS from_observer,
                    COUNT(*) FILTER (WHERE source = 'todo') AS from_todo,
                    COUNT(*) FILTER (WHERE source = 'compaction') AS from_compaction,
                    COUNT(*) FILTER (WHERE source = 'phase_archive') AS from_phase_archive,
                    COUNT(*) FILTER (WHERE source = 'tool_error') AS from_tool_error,
                    AVG(importance) AS avg_importance
                FROM memories
                WHERE project_id = $1::uuid
                """,
                project_id,
            )
            if row:
                result = dict(row)
                if result.get("avg_importance") is not None:
                    result["avg_importance"] = float(result["avg_importance"])
                result["project_id"] = project_id
                return result
            return {"project_id": project_id, "total": 0, "total_tokens": 0}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/jobs/{job_id}/memories")
async def list_job_memories(
    job_id: str,
    memory_type: str | None = Query(default=None),
    source: str | None = Query(default=None),
    search: str | None = Query(default=None),
    sort_by: str = Query(default="created_at"),
    sort_order: str = Query(default="desc"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """List individual memories for a job with optional filters and pagination."""
    # Validate sort parameters
    valid_sort_fields = {"created_at", "importance", "access_count", "token_count", "last_accessed"}
    if sort_by not in valid_sort_fields:
        sort_by = "created_at"
    if sort_order not in {"asc", "desc"}:
        sort_order = "desc"

    try:
        async with vector_db.acquire() as conn:
            conditions = ["job_id = $1::uuid"]
            params: list[Any] = [job_id]
            idx = 2

            if memory_type:
                conditions.append(f"memory_type = ${idx}")
                params.append(memory_type)
                idx += 1
            if source:
                conditions.append(f"source = ${idx}")
                params.append(source)
                idx += 1
            if search:
                conditions.append(f"(content ILIKE ${idx} OR summary ILIKE ${idx})")
                params.append(f"%{search}%")
                idx += 1

            where = " AND ".join(conditions)

            # Count total
            count_row = await conn.fetchrow(
                f"SELECT COUNT(*) as cnt FROM memories WHERE {where}",
                *params,
            )
            total = count_row["cnt"] if count_row else 0

            # Fetch page
            params.extend([limit, offset])
            rows = await conn.fetch(
                f"SELECT id, job_id, project_id, agent_id, "
                f"LEFT(content, 300) as content_preview, summary, "
                f"memory_type, source, keywords, importance, "
                f"source_turn_start, source_turn_end, source_phase, "
                f"token_count, access_count, created_at, last_accessed "
                f"FROM memories WHERE {where} "
                f"ORDER BY {sort_by} {sort_order} "
                f"LIMIT ${idx} OFFSET ${idx + 1}",
                *params,
            )
            memories = []
            for r in rows:
                m = dict(r)
                # Convert UUIDs and datetimes for JSON serialization
                for k in ("id", "job_id", "project_id"):
                    if m.get(k) is not None:
                        m[k] = str(m[k])
                for k in ("created_at", "last_accessed"):
                    if m.get(k) is not None:
                        m[k] = m[k].isoformat()
                if m.get("importance") is not None:
                    m["importance"] = float(m["importance"])
                memories.append(m)

        return {"memories": memories, "total": total, "limit": limit, "offset": offset}
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
        async with vector_db.acquire() as conn:
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
        all_lines = [line for line in all_lines if pattern.match(line)]
        filtered = True

    # Grep filter
    if grep:
        grep_lower = grep.lower()
        all_lines = [line for line in all_lines if grep_lower in line.lower()]
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


def _user_dict(user: dict) -> dict:
    """Build the public user dict for API responses."""
    return {
        "id": str(user["id"]),
        "display_name": user["display_name"],
        "avatar_color": user["avatar_color"],
        "email": user.get("email"),
        "default_project_id": str(user["default_project_id"]) if user.get("default_project_id") else None,
        "is_admin": user.get("is_admin", False),
        "created_at": user["created_at"],
    }


async def _create_gitea_repo_for_project(user: dict, project: dict) -> None:
    """Create a Gitea jobs repo for a user's default project (best-effort)."""
    try:
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
        logger.warning(f"Failed to create Gitea repo for user {user['id']}: {e}")


@app.get("/api/auth/me")
async def auth_me(request: Request) -> dict[str, Any]:
    """Get current user from Bearer token (OIDC)."""
    user = await get_current_user(request, postgres_db)
    return {"user": _user_dict(user)}


# =============================================================================
# MCP Token Endpoints
# =============================================================================

_MCP_INTERNAL_KEY = os.environ.get("MCP_INTERNAL_KEY", "")


@app.post("/api/mcp-tokens")
async def create_mcp_token(request: Request, body: McpTokenCreate) -> dict[str, Any]:
    """Generate a new MCP API token. Returns the plaintext token once."""
    user = await get_current_user(request, postgres_db)

    # Validate scope
    scope = body.scope.strip()
    if scope not in ("user", "all") and not scope.startswith("project:"):
        raise HTTPException(status_code=400, detail="Invalid scope. Use 'user', 'all', or 'project:<uuid>'")
    if scope == "all" and not user.get("is_admin", False):
        raise HTTPException(status_code=403, detail="Only admins can create full-access tokens")
    if scope.startswith("project:"):
        project_id = scope.split(":", 1)[1]
        members = await postgres_db.get_project_members(project_id)
        if not any(str(m["user_id"]) == str(user["id"]) for m in members):
            raise HTTPException(status_code=403, detail="Not a member of this project")

    # Generate token
    token = "srw_" + secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    token_prefix = token[:12]

    # Expiration
    expires_at = None
    if body.expires_in_days:
        from datetime import timedelta
        expires_at = datetime.now(timezone.utc) + timedelta(days=body.expires_in_days)

    row = await postgres_db.create_mcp_token(
        user_id=str(user["id"]),
        name=body.name,
        token_hash=token_hash,
        token_prefix=token_prefix,
        scope=scope,
        expires_at=expires_at,
    )

    result = {k: str(v) if isinstance(v, (UUID, datetime)) else v for k, v in row.items()}
    result["token"] = token  # Plaintext returned once only
    return result


@app.get("/api/mcp-tokens")
async def list_mcp_tokens(request: Request) -> list[dict[str, Any]]:
    """List the current user's MCP tokens (no plaintext or hashes)."""
    user = await get_current_user(request, postgres_db)
    rows = await postgres_db.list_mcp_tokens(str(user["id"]))
    return [
        {k: str(v) if isinstance(v, (UUID, datetime)) else v for k, v in r.items()}
        for r in rows
    ]


@app.delete("/api/mcp-tokens/{token_id}")
async def revoke_mcp_token(request: Request, token_id: str) -> dict[str, str]:
    """Revoke an MCP token (soft delete)."""
    user = await get_current_user(request, postgres_db)
    revoked = await postgres_db.revoke_mcp_token(token_id, str(user["id"]))
    if not revoked:
        raise HTTPException(status_code=404, detail="Token not found or already revoked")
    return {"status": "revoked"}


@app.post("/api/internal/mcp-token-verify")
async def internal_mcp_token_verify(request: Request, body: McpTokenVerifyRequest) -> dict[str, Any]:
    """Internal endpoint for MCP server to verify a token hash.

    Protected by X-Internal-Key header (shared secret).
    """
    internal_key = request.headers.get("X-Internal-Key", "")
    if not _MCP_INTERNAL_KEY or internal_key != _MCP_INTERNAL_KEY:
        raise HTTPException(status_code=401, detail="Invalid internal key")

    token_data = await postgres_db.get_mcp_token_by_hash(body.token_hash)
    if not token_data:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    # Update last used
    await postgres_db.update_mcp_token_last_used(body.token_hash)

    return {
        "user_id": str(token_data["user_id"]),
        "scope": token_data["scope"],
        "display_name": token_data["display_name"],
    }


# =============================================================================
# User Settings & API Key Endpoints
# =============================================================================


@app.get("/api/settings/api-keys")
async def list_user_api_keys(request: Request) -> list[dict[str, Any]]:
    """List the current user's API keys (prefix only, no full keys)."""
    user = await get_current_user(request, postgres_db)
    rows = await postgres_db.list_user_api_keys(str(user["id"]))
    return [
        {k: str(v) if isinstance(v, (UUID, datetime)) else v for k, v in r.items()}
        for r in rows
    ]


@app.put("/api/settings/api-keys/{provider}")
async def set_user_api_key(request: Request, provider: str, body: ApiKeySet) -> dict[str, Any]:
    """Set (create or replace) an API key for a provider."""
    user = await get_current_user(request, postgres_db)
    if provider not in VALID_API_KEY_PROVIDERS:
        raise HTTPException(status_code=400, detail=f"Invalid provider '{provider}'. Valid: {sorted(VALID_API_KEY_PROVIDERS)}")

    key_prefix = body.api_key[:8]
    row = await postgres_db.upsert_user_api_key(
        user_id=str(user["id"]),
        provider=provider,
        api_key=body.api_key,
        key_prefix=key_prefix,
        label=body.label,
    )
    return {k: str(v) if isinstance(v, (UUID, datetime)) else v for k, v in row.items()}


@app.delete("/api/settings/api-keys/{provider}")
async def delete_user_api_key(request: Request, provider: str) -> dict[str, str]:
    """Delete the current user's API key for a provider."""
    user = await get_current_user(request, postgres_db)
    deleted = await postgres_db.delete_user_api_key(str(user["id"]), provider)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"No API key for provider '{provider}'")
    return {"status": "deleted"}


@app.get("/api/settings/preferences")
async def get_user_preferences(request: Request) -> dict[str, Any]:
    """Get the current user's preference settings."""
    user = await get_current_user(request, postgres_db)
    return await postgres_db.get_user_settings(str(user["id"]))


@app.patch("/api/settings/preferences")
async def update_user_preferences(request: Request, body: UserSettingsUpdate) -> dict[str, str]:
    """Update the current user's preference settings (patch-merge)."""
    user = await get_current_user(request, postgres_db)
    settings = {k: v for k, v in body.model_dump().items() if v is not None or k in body.model_fields_set}
    if not settings:
        raise HTTPException(status_code=400, detail="No settings provided")
    await postgres_db.update_user_settings(str(user["id"]), settings)
    return {"status": "updated"}


# =============================================================================
# Project API Key Endpoints
# =============================================================================


@app.get("/api/projects/{project_id}/api-keys")
async def list_project_api_keys(request: Request, project_id: str) -> list[dict[str, Any]]:
    """List a project's API keys (prefix only). Requires project membership."""
    user = await get_current_user(request, postgres_db)
    members = await postgres_db.get_project_members(project_id)
    if not any(str(m["user_id"]) == str(user["id"]) for m in members):
        raise HTTPException(status_code=403, detail="Not a member of this project")

    rows = await postgres_db.list_project_api_keys(project_id)
    return [
        {k: str(v) if isinstance(v, (UUID, datetime)) else v for k, v in r.items()}
        for r in rows
    ]


@app.put("/api/projects/{project_id}/api-keys/{provider}")
async def set_project_api_key(
    request: Request, project_id: str, provider: str, body: ApiKeySet
) -> dict[str, Any]:
    """Set (create or replace) a project API key. Requires owner or editor role."""
    user = await get_current_user(request, postgres_db)
    if provider not in VALID_API_KEY_PROVIDERS:
        raise HTTPException(status_code=400, detail=f"Invalid provider '{provider}'. Valid: {sorted(VALID_API_KEY_PROVIDERS)}")

    members = await postgres_db.get_project_members(project_id)
    member = next((m for m in members if str(m["user_id"]) == str(user["id"])), None)
    if not member or member["role"] not in ("owner", "editor"):
        raise HTTPException(status_code=403, detail="Requires owner or editor role")

    key_prefix = body.api_key[:8]
    row = await postgres_db.upsert_project_api_key(
        project_id=project_id,
        provider=provider,
        api_key=body.api_key,
        key_prefix=key_prefix,
        label=body.label,
    )
    return {k: str(v) if isinstance(v, (UUID, datetime)) else v for k, v in row.items()}


@app.delete("/api/projects/{project_id}/api-keys/{provider}")
async def delete_project_api_key(
    request: Request, project_id: str, provider: str
) -> dict[str, str]:
    """Delete a project's API key for a provider. Requires owner or editor role."""
    user = await get_current_user(request, postgres_db)
    members = await postgres_db.get_project_members(project_id)
    member = next((m for m in members if str(m["user_id"]) == str(user["id"])), None)
    if not member or member["role"] not in ("owner", "editor"):
        raise HTTPException(status_code=403, detail="Requires owner or editor role")

    deleted = await postgres_db.delete_project_api_key(project_id, provider)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"No API key for provider '{provider}'")
    return {"status": "deleted"}


# =============================================================================
# User Endpoints
# =============================================================================


@app.get("/api/users")
async def list_users(request: Request) -> list[dict[str, Any]]:
    """List all users (requires authentication)."""
    await get_current_user(request, postgres_db)
    try:
        return await postgres_db.list_users()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/users/{user_id}")
async def get_user(user_id: str, request: Request) -> dict[str, Any]:
    """Get a single user by ID (requires authentication)."""
    await get_current_user(request, postgres_db)
    user = await postgres_db.get_user(user_id)
    if not user:
        raise HTTPException(status_code=404, detail=f"User '{user_id}' not found")
    return user


@app.post("/api/users")
async def create_user(body: UserCreate, request: Request) -> dict[str, Any]:
    """Create a new user with a default project (requires authentication)."""
    await get_current_user(request, postgres_db)
    try:
        user, project = await postgres_db.create_user_with_default_project(
            display_name=body.display_name,
            avatar_color=body.avatar_color or "#89b4fa",
            email=body.email,
        )
        await _create_gitea_repo_for_project(user, project)
        return user
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.put("/api/users/{user_id}")
async def update_user(user_id: str, body: UserUpdate, request: Request) -> dict[str, str]:
    """Update a user (requires authentication)."""
    await get_current_user(request, postgres_db)
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
async def delete_user(user_id: str, request: Request) -> dict[str, str]:
    """Delete a user (requires authentication)."""
    await get_current_user(request, postgres_db)
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

        # Create Keycloak group and add creator
        if keycloak_groups.is_initialized:
            project_id_str = str(project["id"])
            group_id = await keycloak_groups.ensure_project_group(
                project_id_str, body.name
            )
            if group_id and body.user_id:
                user = await postgres_db.get_user(body.user_id)
                if user and user.get("keycloak_sub"):
                    await keycloak_groups.add_user_to_project_group(
                        user["keycloak_sub"], project_id_str
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

    # Clean up knowledge_index in vector DB (no FK cascade across databases)
    try:
        async with vector_db.acquire() as conn:
            await conn.execute("DELETE FROM knowledge_index WHERE project_id = $1", UUID(project_id))
    except Exception as e:
        logger.warning(f"Failed to clean up knowledge_index for project {project_id}: {e}")

    # Detach referencing rows that lack ON DELETE CASCADE/SET NULL
    uuid_val = UUID(project_id)
    async with postgres_db.acquire() as conn:
        await conn.execute("UPDATE jobs SET project_id = NULL WHERE project_id = $1", uuid_val)
        await conn.execute("UPDATE datasources SET project_id = NULL WHERE project_id = $1", uuid_val)

    success = await postgres_db.delete_project(project_id)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to delete project")

    # Clean up Keycloak group
    if keycloak_groups.is_initialized:
        await keycloak_groups.delete_project_group(project_id)

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
        result = await postgres_db.add_project_member(
            project_id=project_id,
            user_id=body.user_id,
            role=body.role,
        )

        # Sync to Keycloak group
        if keycloak_groups.is_initialized:
            user = await postgres_db.get_user(body.user_id)
            if user and user.get("keycloak_sub"):
                await keycloak_groups.add_user_to_project_group(
                    user["keycloak_sub"], project_id
                )

        return result
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

    # Remove from Keycloak group
    if keycloak_groups.is_initialized:
        user = await postgres_db.get_user(user_id)
        if user and user.get("keycloak_sub"):
            await keycloak_groups.remove_user_from_project_group(
                user["keycloak_sub"], project_id
            )

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
    """Create a job within a project — delegates to create_job."""
    job.project_id = project_id
    return await create_job(job)


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
# Project Knowledge Base Endpoints
# =============================================================================

# Lazy-loaded singleton for Neo4j knowledge operations
_knowledge_graph_db = None


def _get_knowledge_graph():
    """Lazily initialise and return the KnowledgeGraphDB singleton."""
    global _knowledge_graph_db
    if _knowledge_graph_db is not None:
        return _knowledge_graph_db
    try:
        import sys
        project_root = str(Path(__file__).parent.parent)
        if project_root not in sys.path:
            sys.path.insert(0, project_root)
        from src.services.knowledge_graph import KnowledgeGraphDB
        _knowledge_graph_db = KnowledgeGraphDB()
        if not _knowledge_graph_db.connect():
            logger.warning("Could not connect to Neo4j for knowledge base")
            _knowledge_graph_db = None
    except Exception as e:
        logger.warning(f"KnowledgeGraphDB not available: {e}")
        _knowledge_graph_db = None
    return _knowledge_graph_db


@app.get("/api/projects/{project_id}/knowledge/summary")
async def get_knowledge_summary(project_id: str) -> dict[str, Any]:
    """Get knowledge base summary statistics for a project."""
    project = await postgres_db.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")

    try:
        async with vector_db.acquire() as conn:
            # Counts by type
            type_rows = await conn.fetch(
                "SELECT note_type, COUNT(*) as cnt FROM knowledge_index "
                "WHERE project_id = $1 GROUP BY note_type ORDER BY cnt DESC",
                project_id,
            )
            by_type = {r["note_type"]: r["cnt"] for r in type_rows}

            # Counts by status
            status_rows = await conn.fetch(
                "SELECT status, COUNT(*) as cnt FROM knowledge_index "
                "WHERE project_id = $1 GROUP BY status ORDER BY cnt DESC",
                project_id,
            )
            by_status = {r["status"]: r["cnt"] for r in status_rows}

            # Total
            total = sum(by_type.values())

            # Recent notes (last 5)
            recent_rows = await conn.fetch(
                "SELECT note_id, title, note_type, status, modified_at "
                "FROM knowledge_index WHERE project_id = $1 "
                "ORDER BY modified_at DESC LIMIT 5",
                project_id,
            )
            recent = [dict(r) for r in recent_rows]

        return {
            "total": total,
            "by_type": by_type,
            "by_status": by_status,
            "recent": recent,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/projects/{project_id}/knowledge")
async def list_knowledge_notes(
    project_id: str,
    note_type: str | None = Query(default=None, alias="type"),
    status: str | None = Query(default=None),
    tag: str | None = Query(default=None),
    job_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """List knowledge notes for a project with optional filters."""
    project = await postgres_db.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")

    try:
        async with vector_db.acquire() as conn:
            conditions = ["project_id = $1"]
            params: list[Any] = [project_id]
            idx = 2

            if note_type:
                conditions.append(f"note_type = ${idx}")
                params.append(note_type)
                idx += 1
            if status:
                conditions.append(f"status = ${idx}")
                params.append(status)
                idx += 1
            if tag:
                conditions.append(f"${idx} = ANY(tags)")
                params.append(tag)
                idx += 1
            if job_id:
                conditions.append(f"job_id = ${idx}::uuid")
                params.append(job_id)
                idx += 1

            where = " AND ".join(conditions)

            # Count total
            count_row = await conn.fetchrow(
                f"SELECT COUNT(*) as cnt FROM knowledge_index WHERE {where}",
                *params,
            )
            total = count_row["cnt"] if count_row else 0

            # Fetch page
            params.extend([limit, offset])
            rows = await conn.fetch(
                f"SELECT id, note_id, title, note_type, status, confidence, "
                f"tags, keywords, job_id, phase, "
                f"LEFT(content, 300) as content_preview, "
                f"created_at, modified_at "
                f"FROM knowledge_index WHERE {where} "
                f"ORDER BY modified_at DESC "
                f"LIMIT ${idx} OFFSET ${idx + 1}",
                *params,
            )
            notes = [dict(r) for r in rows]

        return {"notes": notes, "total": total, "limit": limit, "offset": offset}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/projects/{project_id}/knowledge/{note_id}")
async def get_knowledge_note(project_id: str, note_id: str) -> dict[str, Any]:
    """Get a single knowledge note with full content."""
    try:
        async with vector_db.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM knowledge_index "
                "WHERE project_id = $1 AND note_id = $2",
                project_id, note_id,
            )
        if not row:
            raise HTTPException(
                status_code=404,
                detail=f"Note '{note_id}' not found in project '{project_id}'",
            )
        result = dict(row)
        # Remove binary/vector fields from response
        result.pop("embedding", None)
        result.pop("search_doc", None)

        # Fetch relationships from Neo4j if available
        kg = _get_knowledge_graph()
        if kg:
            try:
                neo4j_note = kg.read_note(project_id, note_id)
                if neo4j_note:
                    result["relationships"] = neo4j_note.get("relationships", [])
            except Exception:
                result["relationships"] = []
        else:
            result["relationships"] = []

        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/api/projects/{project_id}/knowledge/search")
async def search_knowledge(
    project_id: str, body: KnowledgeSearchRequest,
) -> dict[str, Any]:
    """Hybrid search over project knowledge base."""
    project = await postgres_db.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")

    try:
        async with vector_db.acquire() as conn:
            # Try dense+sparse search if embedding service available
            embedding = None
            try:
                import sys
                project_root = str(Path(__file__).parent.parent)
                if project_root not in sys.path:
                    sys.path.insert(0, project_root)
                from src.services.embedding_service import get_embedding_service
                svc = get_embedding_service()
                embedding = await svc.embed(body.query)
            except Exception:
                pass  # Fall back to sparse-only search

            if embedding:
                rows = await conn.fetch(
                    "SELECT * FROM knowledge_hybrid_search($1, $2::vector, $3, $4)",
                    body.query, str(embedding), project_id, body.limit,
                )
            else:
                # Sparse-only fallback: tsvector keyword search
                rows = await conn.fetch(
                    "SELECT * FROM knowledge_index "
                    "WHERE project_id = $1 AND search_doc @@ websearch_to_tsquery($2) "
                    "ORDER BY ts_rank_cd(search_doc, websearch_to_tsquery($2)) DESC "
                    "LIMIT $3",
                    project_id, body.query, body.limit,
                )

            notes = []
            for r in rows:
                d = dict(r)
                d.pop("embedding", None)
                d.pop("search_doc", None)
                notes.append(d)

        return {"notes": notes, "query": body.query, "total": len(notes)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.patch("/api/projects/{project_id}/knowledge/{note_id}")
async def update_knowledge_note(
    project_id: str, note_id: str, body: KnowledgeNoteUpdate,
) -> dict[str, str]:
    """Update a knowledge note's status or tags."""
    valid_statuses = {"active", "resolved", "superseded", "archived"}
    if body.status and body.status not in valid_statuses:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid status '{body.status}'. Must be one of: {valid_statuses}",
        )

    try:
        # Update PostgreSQL search index (vector DB)
        async with vector_db.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT note_id FROM knowledge_index "
                "WHERE project_id = $1 AND note_id = $2",
                project_id, note_id,
            )
            if not row:
                raise HTTPException(
                    status_code=404,
                    detail=f"Note '{note_id}' not found in project '{project_id}'",
                )

            updates = []
            params: list[Any] = [project_id, note_id]
            idx = 3

            if body.status:
                updates.append(f"status = ${idx}")
                params.append(body.status)
                idx += 1

            if body.add_tags:
                updates.append(f"tags = array_cat(tags, ${idx}::text[])")
                params.append(body.add_tags)
                idx += 1

            if body.remove_tags:
                for rm_tag in body.remove_tags:
                    updates.append(f"tags = array_remove(tags, ${idx})")
                    params.append(rm_tag)
                    idx += 1

            if not updates:
                return {"status": "no_changes"}

            updates.append("modified_at = NOW()")
            set_clause = ", ".join(updates)
            await conn.execute(
                f"UPDATE knowledge_index SET {set_clause} "
                f"WHERE project_id = $1 AND note_id = $2",
                *params,
            )

        # Update Neo4j if available
        kg = _get_knowledge_graph()
        if kg:
            try:
                update_kwargs: dict[str, Any] = {}
                if body.status:
                    update_kwargs["status"] = body.status
                if body.add_tags:
                    update_kwargs["add_tags"] = body.add_tags
                if update_kwargs:
                    kg.update_note(project_id, note_id, **update_kwargs)
            except Exception as e:
                logger.warning(f"Neo4j update failed for {note_id}: {e}")

        return {"status": "updated"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.delete("/api/projects/{project_id}/knowledge/{note_id}")
async def delete_knowledge_note(project_id: str, note_id: str) -> dict[str, str]:
    """Hard delete a knowledge note from both stores."""
    try:
        # Delete from vector DB
        async with vector_db.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM knowledge_index "
                "WHERE project_id = $1 AND note_id = $2",
                project_id, note_id,
            )
            if result == "DELETE 0":
                raise HTTPException(
                    status_code=404,
                    detail=f"Note '{note_id}' not found in project '{project_id}'",
                )

        # Delete from Neo4j if available
        kg = _get_knowledge_graph()
        if kg:
            try:
                kg._db.execute_write(
                    "MATCH (n:Note {project_id: $pid, id: $nid}) DETACH DELETE n",
                    {"pid": project_id, "nid": note_id},
                )
            except Exception as e:
                logger.warning(f"Neo4j delete failed for {note_id}: {e}")

        return {"status": "deleted"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/api/projects/{project_id}/knowledge/export")
async def export_knowledge(project_id: str) -> dict[str, Any]:
    """Export project knowledge base as Obsidian-compatible markdown files."""
    project = await postgres_db.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")

    kg = _get_knowledge_graph()
    if not kg:
        raise HTTPException(
            status_code=503,
            detail="Neo4j not available — cannot export knowledge base",
        )

    try:
        import tempfile
        export_dir = Path(tempfile.mkdtemp(prefix="kb_export_"))
        notes = kg.get_all_notes_for_export(project_id)

        for note in notes:
            # Build frontmatter
            fm_lines = ["---"]
            fm_lines.append(f"id: {note['id']}")
            fm_lines.append(f"type: {note['type']}")
            if note.get("tags"):
                fm_lines.append(f"tags: [{', '.join(note['tags'])}]")
            if note.get("keywords"):
                fm_lines.append(f"keywords: [{', '.join(note['keywords'])}]")
            if note.get("confidence"):
                fm_lines.append(f"confidence: {note['confidence']}")
            fm_lines.append(f"status: {note.get('status', 'active')}")
            if note.get("job_id"):
                fm_lines.append(f"job_id: {note['job_id']}")
            if note.get("phase"):
                fm_lines.append(f"phase: {note['phase']}")
            if note.get("created"):
                fm_lines.append(f"created: {note['created']}")
            if note.get("modified"):
                fm_lines.append(f"modified: {note['modified']}")
            fm_lines.append("---")
            fm_lines.append("")

            # Title and content
            fm_lines.append(f"# {note.get('title', note['id'])}")
            fm_lines.append("")
            if note.get("content"):
                fm_lines.append(note["content"])
                fm_lines.append("")

            # Relationships as wikilinks
            if note.get("relationships"):
                by_type: dict[str, list[str]] = {}
                for rel in note["relationships"]:
                    rtype = rel.get("type", "REFERENCES")
                    target = rel.get("target", "")
                    by_type.setdefault(rtype, []).append(target)
                for rtype, targets in by_type.items():
                    links = ", ".join(f"[[{t}]]" for t in targets)
                    fm_lines.append(f"**{rtype}:** {links}")

            file_name = f"{note['id']}.md"
            (export_dir / file_name).write_text("\n".join(fm_lines), encoding="utf-8")

        return {
            "status": "exported",
            "path": str(export_dir),
            "note_count": len(notes),
            "project_name": project.get("name", ""),
        }
    except HTTPException:
        raise
    except Exception as e:
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


# Tools whose results are displayed as rich inspection panels in the frontend
_INSPECTION_TOOLS = {
    "list_jobs", "get_job", "get_job_progress", "get_workspace_file",
    "get_workspace_overview", "get_frozen_job", "get_todos", "get_chat_history",
}


def _format_tool_name(name: str) -> str:
    """Format a tool name for display: 'list_jobs' → 'List Jobs'."""
    return name.replace("_", " ").title()


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
    raw_model_for_prompt = body.model or get_builder_model()
    system_prompt = build_system_prompt(
        model=raw_model_for_prompt,
        instructions_content=body.instructions,
        config_settings=body.config,
        description=body.description,
        active_job_id=body.active_job_id,
        active_project_id=body.active_project_id,
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
        final_steps = []

        try:
            raw_model = body.model or get_builder_model()
            provider = _detect_provider(raw_model)
            model_name, base_url, resolved_key = _resolve_builder_model(raw_model)
            api_key = resolved_key or _get_api_key_for_provider(provider)
            use_responses_api = raw_model in RESPONSES_API_MODELS
            builder_settings = resolve_builder_settings(raw_model)

            for iteration in range(MAX_ITERATIONS):
                step_title = 'Analyzing request...' if iteration == 0 else 'Processing tool results...'
                yield f"event: step\ndata: {json.dumps({'type': 'thought', 'title': step_title})}\n\n"
                final_steps.append({"type": "thought", "title": step_title})
                turn_text = ""
                turn_tool_calls = []  # {"name", "args", "id"} dicts
                error_occurred = False

                # Select streaming function based on provider and API type
                if provider == "anthropic":
                    stream_fn = _stream_anthropic(system_prompt, loop_messages, model_name, api_key, settings=builder_settings)
                elif use_responses_api:
                    input_items = _chat_messages_to_responses_input(loop_messages)
                    stream_fn = _stream_openai_responses(system_prompt, input_items, model_name, api_key, base_url=base_url, settings=builder_settings)
                else:
                    stream_fn = _stream_openai(system_prompt, loop_messages, model_name, api_key, base_url=base_url, settings=builder_settings)

                async for evt_type, evt_data in stream_fn:
                    if evt_type == "token":
                        turn_text += evt_data["text"]
                        yield f"event: token\ndata: {json.dumps(evt_data)}\n\n"
                    elif evt_type == "tool_call":
                        turn_tool_calls.append(evt_data)
                        if evt_data["name"] in SERVER_SIDE_TOOLS:
                            yield f"event: tool_executing\ndata: {json.dumps({'tool': evt_data['name'], 'args': evt_data['args']})}\n\n"
                            tool_display = _format_tool_name(evt_data["name"])
                            if evt_data["name"] == "web_search":
                                step_title = f"Searching: {evt_data['args'].get('query', tool_display)}"
                            else:
                                step_title = f"Running: {tool_display}"
                            final_steps.append({"type": "tool_call", "title": step_title, "content": json.dumps(evt_data['args'])})
                        elif evt_data["name"] in WORKSPACE_EDIT_TOOLS:
                            proposal = _build_workspace_proposal(evt_data["name"], evt_data["args"])
                            if not proposal.get("error"):
                                yield f"event: workspace_proposal\ndata: {json.dumps(proposal)}\n\n"
                                ws_label = "Write" if evt_data["name"] == "write_workspace_file" else "Edit"
                                final_steps.append({"type": "workspace_proposal", "title": f"{ws_label}: {evt_data['args'].get('path', 'file')}", "content": ""})
                        else:
                            yield f"event: tool_call\ndata: {json.dumps({'tool': evt_data['name'], 'args': evt_data['args']})}\n\n"
                            final_tool_calls.append({"tool": evt_data["name"], "args": evt_data["args"]})
                            final_steps.append({"type": "tool_call", "title": _format_tool_name(evt_data["name"]), "content": json.dumps(evt_data['args'])})
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
                            formatted = _format_tool_name(tc["name"])
                            if full_content and tc["name"] in _INSPECTION_TOOLS:
                                final_steps.append({"type": "inspection_result", "title": formatted, "content": full_content})
                            else:
                                final_steps.append({"type": "tool_result", "title": f"Result: {formatted}", "content": ""})
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
                            formatted = _format_tool_name(tc["name"])
                            if full_content and tc["name"] in _INSPECTION_TOOLS:
                                final_steps.append({"type": "inspection_result", "title": formatted, "content": full_content})
                            else:
                                final_steps.append({"type": "tool_result", "title": f"Result: {formatted}", "content": ""})
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
                steps=final_steps if final_steps else None,
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
    """Get API key for the given provider via the KeyRing system.

    Routes through get_builder_api_key() which handles comma-separated keys
    and KeyRing-based rotation.
    """
    return get_builder_api_key(provider)


def _resolve_builder_model(raw_model: str) -> tuple[str, str | None, str | None]:
    """Resolve a prefixed model ID into (model_name, base_url, api_key).

    Handles model routing for different providers:
    - ``openrouter/`` prefix → OpenRouter API
    - ``openai/`` prefix → local vLLM at BUILDER_BASE_URL / OPENAI_BASE_URL / LLM_BASE_URL
    - ``claude-`` prefix → Anthropic (base_url/api_key left to Anthropic client)
    - No prefix → default OpenAI provider
    """
    if raw_model.startswith("openrouter/"):
        model_name = raw_model[len("openrouter/"):]
        base_url = "https://openrouter.ai/api/v1"
        api_key = os.getenv("OPENROUTER_API_KEY")
        return model_name, base_url, api_key
    if raw_model.startswith("openai/"):
        model_name = raw_model[len("openai/"):]
        base_url = os.getenv("BUILDER_BASE_URL") or os.getenv("OPENAI_BASE_URL") or os.getenv("LLM_BASE_URL")
        api_key = get_builder_api_key("openai")
        return model_name, base_url, api_key
    # No prefix — use existing defaults
    return raw_model, get_builder_base_url(), None


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
    _retried: bool = False,
    base_url: str | None = None,
    settings: dict[str, Any] | None = None,
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
        api_key=api_key or get_builder_api_key("openai"),
        base_url=base_url or get_builder_base_url(),
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
        create_kwargs: dict[str, Any] = dict(
            model=model,
            instructions=system_prompt,
            input=input_items,
            tools=response_tools,
            stream=True,
        )
        # Apply inference params from settings matrix
        if settings:
            reasoning_effort = settings.get("reasoning_effort")
            if reasoning_effort:
                create_kwargs["reasoning"] = {"effort": reasoning_effort}
            if settings.get("temperature") is not None:
                create_kwargs["temperature"] = settings["temperature"]
            if settings.get("top_p") is not None:
                create_kwargs["top_p"] = settings["top_p"]
            if settings.get("max_tokens") is not None:
                create_kwargs["max_tokens"] = settings["max_tokens"]

        stream = await client.responses.create(**create_kwargs)

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
        if not _retried and is_auth_or_quota_error(e):
            new_key = rotate_builder_key(str(e), provider="openai")
            if new_key:
                logger.info("Builder: retrying OpenAI Responses API with rotated key")
                async for event in _stream_openai_responses(
                    system_prompt, input_items, model, api_key=new_key, _retried=True, base_url=base_url, settings=settings
                ):
                    yield event
                return
        yield ("error", {"message": str(e)})


async def _stream_openai(
    system_prompt: str,
    context_messages: list[dict],
    model: str,
    api_key: str | None = None,
    _retried: bool = False,
    base_url: str | None = None,
    settings: dict[str, Any] | None = None,
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
        api_key=api_key or get_builder_api_key("openai"),
        base_url=base_url or get_builder_base_url(),
    )

    llm_messages = [{"role": "system", "content": system_prompt}]
    llm_messages.extend(context_messages)

    try:
        create_kwargs: dict[str, Any] = dict(
            model=model,
            messages=llm_messages,
            tools=BUILDER_TOOLS,
            stream=True,
        )
        # Apply inference params from settings matrix
        if settings:
            if settings.get("temperature") is not None:
                create_kwargs["temperature"] = settings["temperature"]
            if settings.get("top_p") is not None:
                create_kwargs["top_p"] = settings["top_p"]
            if settings.get("max_tokens") is not None:
                create_kwargs["max_tokens"] = settings["max_tokens"]
            # Provider-specific params via extra_body (top_k, min_p, etc.)
            extra_body: dict[str, Any] = {}
            if settings.get("top_k") is not None:
                extra_body["top_k"] = settings["top_k"]
            if settings.get("min_p") is not None:
                extra_body["min_p"] = settings["min_p"]
            if extra_body:
                create_kwargs["extra_body"] = extra_body

        stream = await client.chat.completions.create(**create_kwargs)

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
        if not _retried and is_auth_or_quota_error(e):
            new_key = rotate_builder_key(str(e), provider="openai")
            if new_key:
                logger.info("Builder: retrying OpenAI Chat API with rotated key")
                async for event in _stream_openai(
                    system_prompt, context_messages, model, api_key=new_key, _retried=True, base_url=base_url, settings=settings
                ):
                    yield event
                return
        yield ("error", {"message": str(e)})


async def _stream_anthropic(
    system_prompt: str,
    context_messages: list[dict],
    model: str,
    api_key: str | None = None,
    _retried: bool = False,
    settings: dict[str, Any] | None = None,
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

    client = AsyncAnthropic(api_key=api_key or get_builder_api_key("anthropic"))

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
        stream_kwargs: dict[str, Any] = dict(
            model=model,
            system=full_system,
            messages=filtered_messages,
            tools=anthropic_tools,
            max_tokens=(settings or {}).get("max_tokens", 4096),
        )
        if (settings or {}).get("temperature") is not None:
            stream_kwargs["temperature"] = settings["temperature"]
        if (settings or {}).get("top_p") is not None:
            stream_kwargs["top_p"] = settings["top_p"]

        async with client.messages.stream(**stream_kwargs) as stream:
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
        if not _retried and is_auth_or_quota_error(e):
            new_key = rotate_builder_key(str(e), provider="anthropic")
            if new_key:
                logger.info("Builder: retrying Anthropic API with rotated key")
                async for event in _stream_anthropic(
                    system_prompt, context_messages, model, api_key=new_key, _retried=True, settings=settings
                ):
                    yield event
                return
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

    raw_model = get_builder_model()
    provider = _detect_provider(raw_model)
    model_name, base_url, resolved_key = _resolve_builder_model(raw_model)
    builder_settings = resolve_builder_settings(raw_model)
    max_summary_tokens = builder_settings.get("max_summary_tokens", 1024)

    summary_text = ""

    if provider == "anthropic":
        api_key = resolved_key or get_builder_api_key(provider)
        try:
            from anthropic import AsyncAnthropic
            client = AsyncAnthropic(api_key=api_key)
            response = await client.messages.create(
                model=model_name,
                system=summary_prompt[0]["content"],
                messages=[{"role": "user", "content": summary_prompt[1]["content"]}],
                max_tokens=max_summary_tokens,
            )
            summary_text = response.content[0].text
        except Exception as e:
            if is_auth_or_quota_error(e):
                new_key = rotate_builder_key(str(e), provider=provider)
                if new_key:
                    try:
                        client = AsyncAnthropic(api_key=new_key)
                        response = await client.messages.create(
                            model=model_name,
                            system=summary_prompt[0]["content"],
                            messages=[{"role": "user", "content": summary_prompt[1]["content"]}],
                            max_tokens=max_summary_tokens,
                        )
                        summary_text = response.content[0].text
                    except Exception as e2:
                        logger.warning(f"Anthropic summarization failed after key rotation: {e2}")
                        return
                else:
                    logger.warning(f"Anthropic summarization failed (no alt keys): {e}")
                    return
            else:
                logger.warning(f"Anthropic summarization failed: {e}")
                return
    else:
        api_key = resolved_key or get_builder_api_key(provider)
        try:
            from openai import AsyncOpenAI
            client = AsyncOpenAI(
                api_key=api_key,
                base_url=base_url or get_builder_base_url(),
            )
            response = await client.chat.completions.create(
                model=model_name,
                messages=summary_prompt,
                max_tokens=max_summary_tokens,
            )
            summary_text = response.choices[0].message.content or ""
        except Exception as e:
            if is_auth_or_quota_error(e):
                new_key = rotate_builder_key(str(e), provider=provider)
                if new_key:
                    try:
                        client = AsyncOpenAI(
                            api_key=new_key,
                            base_url=base_url or get_builder_base_url(),
                        )
                        response = await client.chat.completions.create(
                            model=model_name,
                            messages=summary_prompt,
                            max_tokens=max_summary_tokens,
                        )
                        summary_text = response.choices[0].message.content or ""
                    except Exception as e2:
                        logger.warning(f"OpenAI summarization failed after key rotation: {e2}")
                        return
                else:
                    logger.warning(f"OpenAI summarization failed (no alt keys): {e}")
                    return
            else:
                logger.warning(f"OpenAI summarization failed: {e}")
                return

    if summary_text:
        await postgres_db.update_builder_session_summary(session_id, summary_text)
