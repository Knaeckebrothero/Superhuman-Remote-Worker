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
import re
import secrets
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
import urllib.parse
from urllib.parse import parse_qs, urlparse

from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv())

if os.environ.get("LICENSE_TERMS_ACCEPTED", "").strip().lower() != "true":
    raise SystemExit(
        "License terms not accepted. Set LICENSE_TERMS_ACCEPTED=true to run. "
        "See https://github.com/Knaeckebrothero/Superhuman-Remote-Worker/blob/main/LICENSE"
    )

# Configure application-level logging (Uvicorn only configures its own loggers).
# JSON when LOG_FORMAT=json (cluster), text otherwise (local/dev). When DEBUG,
# only app namespaces get DEBUG; third-party stays at INFO (DEBUG_ALL=1 to
# include it). See docs/features/centralized_logging.md.
try:
    from logging_config import (  # noqa: E402
        CorrelationIdMiddleware,
        bind_log_context,
        configure_logging,
        reset_log_context,
    )
except ModuleNotFoundError:  # `uvicorn orchestrator.main:app` vs flattened image
    from orchestrator.logging_config import (  # noqa: E402
        CorrelationIdMiddleware,
        bind_log_context,
        configure_logging,
        reset_log_context,
    )

configure_logging(
    component="orchestrator",
    app_namespaces=(
        "orchestrator",
        "main",
        "database",
        "security",
        "services",
        "uploads",
        "mcp",
        "graph_routes",
        "workspace",
    ),
    disable_uvicorn_access=True,
)

from datetime import date, datetime, timedelta, timezone  # noqa: E402
from decimal import Decimal  # noqa: E402
from typing import Any, Literal, Optional  # noqa: E402
from uuid import UUID  # noqa: E402

import asyncpg  # noqa: E402
import yaml  # noqa: E402
from fastapi import (  # noqa: E402
    Body,
    FastAPI,
    File,
    HTTPException,
    Query,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import (  # noqa: E402
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)

from pydantic import BaseModel, Field, model_validator  # noqa: E402

from database import (  # noqa: E402
    PostgresDB,
    MongoDB,
    AuditStore,
    ALLOWED_TABLES,
    FilterCategory,
    MIGRATIONS_VECTOR_DIR,
    MIGRATIONS_AUDIT_DIR,
)
from security.auth import (  # noqa: E402
    get_current_user,
    require_approved_user,
    resolve_ws_user,
    cleanup_expired_tokens,
    cleanup_expired_sessions,
    ensure_user_provisioned,
)
from security.access import (  # noqa: E402
    is_internal_call,
    log_security_event,
    mcp_scope_project_id,
    redact_config_override,
    redact_datasource,
    redact_datasources,
    require_datasource_access,
    require_datasource_owner,
    require_internal,
    require_internal_or_job_access,
    require_job_access,
    require_project_member,
    require_project_owner,
    require_sudo_request_authority,
    require_thread_owner,
    user_can_access_any_job,
    user_can_access_datasource,
    user_can_access_ide_entity,
    user_can_access_job,
    user_can_access_job_or_thread,
    user_visible_project_ids,
)
from security.credential_files import (  # noqa: E402
    CREDENTIAL_FILE_TYPES,
    CredentialFileValidationError,
    normalize_credential_files,
)
from security.csrf import CSRFMiddleware  # noqa: E402
from auth import bff_router  # noqa: E402
from routers import automations_router  # noqa: E402
from routers import project_loops_router  # noqa: E402
from routers.sessions import router as sessions_router  # noqa: E402
from services.cron_dispatcher import cron_dispatcher_loop  # noqa: E402
from services.project_loop_sweeper import project_loop_sweeper_loop  # noqa: E402
from services.litellm_gateway import (  # noqa: E402
    LiteLLMClient,
    _parse_quota_policy,
    compute_project_quota_status,
    ensure_scoped_key,
    get_fleet_key,
    litellm_sync_loop,
    materialize_llm_usage,
)
from services.audit_partitions import (  # noqa: E402
    maintenance_loop as audit_maintenance_loop,
)
from services import workspace_metering  # noqa: E402
from services.usage_ledger import UsageLedger, UsageRates  # noqa: E402
from services.workspace import workspace_service  # noqa: E402
from services.gitea import GiteaClient  # noqa: E402
from services.keycloak_admin import KeycloakGroupSync  # noqa: E402
from services.cloud import (  # noqa: E402
    CloudBackendError,
    CloudMountSubject,
    MainCloudRouter,
    ProjectFolderHandle,
    SessionFolderHandle,
    SupportsRcloneMount,
    UserId,
    build_backend,
)
from services.cloud.reload import (  # noqa: E402
    _reload_from_db_and_swap,
    fire_reload,
    run_listen_loop,
)
from services.llm_endpoint_probe import probe_endpoint_models  # noqa: E402
from services import discovery as discovery_service  # noqa: E402
from services import family_matcher  # noqa: E402
from services import readiness as readiness_service  # noqa: E402
from seed.llm_config import ensure_codex_proxy_endpoint  # noqa: E402

# Registry helpers live in src/ and stay there — the orchestrator imports
# them here so callers don't each do lazy imports.
from src.core.model_registry import (  # noqa: E402
    UnknownModelError,
    resolve_model as _resolve_model,
)

# Lite (no-workspace-pod) backend names. Canonical set lives agent-side in the
# backend factory; imported (not re-declared) so the dispatch/provisioning
# branches here can't drift from what the agent actually constructs.
# (no_workspace_agent_mode.md §4) — importing the frozenset is cheap; the heavy
# backend modules are lazy-imported inside the factory's functions.
from src.core.backends.factory import LITE_BACKENDS  # noqa: E402
from src.utils.ssh_key import (  # noqa: E402
    InvalidSSHKeyError,
    generate_ed25519_keypair as _generate_ed25519_keypair,
    validate_private_key as _validate_ssh_private_key,
)
from services.nats_bridge import nats_bridge  # noqa: E402
from services.vm_provisioner import vm_provisioner  # noqa: E402
from services.container_provisioner import container_provisioner  # noqa: E402
from services.workspace_lifecycle import (  # noqa: E402
    EnsureOutcome,
    WorkspaceOwner,
    ensure_workspace,
)
from services.session_provisioner import (  # noqa: E402
    ensure_session_workspace,
    reconcile_session_workspaces,
)
from services.docker_provisioner import docker_provisioner  # noqa: E402
from services.persistent_provisioner import persistent_provisioner  # noqa: E402
from services.agent_provisioner import agent_provisioner  # noqa: E402
from services.config_resolver import (  # noqa: E402
    inject_blob_credentials,
    resolve_config,
    unrouted_model_slots,
)
from services.session_router import SessionRouterService  # noqa: E402
from services.session_tokens import SessionTokenService  # noqa: E402
from services.lifecycle import (  # noqa: E402
    AgentInstanceManager,
    InstanceLifecycleReconciler,
    VMInstanceManager,
    WorkspaceInstanceManager,
)
from services.workspace_suspension import workspace_suspension_service  # noqa: E402
from services.snapshot_service import snapshot_service  # noqa: E402
from services.ide_session import ide_session_service  # noqa: E402
from services.ide_proxy import ide_proxy_service  # noqa: E402
from services.email import email_service  # noqa: E402
from services import headless_notifications  # noqa: E402
from services.imap_poller import imap_poller  # noqa: E402
from services.notification_service import notification_service  # noqa: E402
import httpx  # noqa: E402
from graph_routes import router as graph_router, set_audit_reader, set_postgres_db  # noqa: E402
from uploads import router as uploads_router  # noqa: E402

logger = logging.getLogger(__name__)

# =============================================================================
# Database Instances (singleton pattern)
# =============================================================================

postgres_db = PostgresDB()
# Retired audit backend — unused at runtime after QW-4 (the Mongo reader + boot
# calls were removed). The object, class, and import are deleted in D-5.
# See docs/features/database_optimization_plan.md.
mongodb = MongoDB()
gitea_client = GiteaClient()
keycloak_groups = KeycloakGroupSync()
main_cloud_router = MainCloudRouter(build_backend())

# Vector DB — separate pgvector instance for citations, memories + knowledge_index.
from utils.db_url import build_postgres_url as _build_pg_url  # noqa: E402

_vector_url = _build_pg_url("VECTOR_POSTGRES", fallback_env="VECTOR_DB_URL")
if not _vector_url:
    raise RuntimeError(
        "Vector DB credentials missing — set VECTOR_POSTGRES_USER + "
        "VECTOR_POSTGRES_PASSWORD (with VECTOR_POSTGRES_HOST/PORT/DB from "
        "ConfigMap), or fall back to VECTOR_DB_URL"
    )
vector_db = PostgresDB(
    connection_string=_vector_url,
    migrations_dir=MIGRATIONS_VECTOR_DIR,
)

# Audit DB — observability-tier instance that replaces the MongoDB collections
# (llm_requests / agent_audit / chat_history). Unlike the vector DB this is
# NON-load-bearing: when its credentials are absent (AUDIT_POSTGRES_* unset /
# databases.audit.enabled=false) the orchestrator runs without it — no
# migrations, no partition maintenance — exactly as it tolerates MongoDB being
# unavailable. Stood up behind the AUDIT_BACKEND flag; nothing reads or writes
# it until the writer/reader land (PR 2/3). Hence: skip silently, never raise.
_audit_url = _build_pg_url("AUDIT_POSTGRES", fallback_env="AUDIT_DB_URL")
audit_db = (
    PostgresDB(connection_string=_audit_url, migrations_dir=MIGRATIONS_AUDIT_DIR)
    if _audit_url
    else None
)

# Audit READS: the cockpit-facing read backend. Served exclusively by the
# Postgres AuditStore — the legacy Mongo reader was retired
# (docs/features/database_optimization_plan.md QW-4/D-5). AuditStore is
# null-safe: is_available stays False (the read endpoints' degraded shapes,
# never a crash) until connect() runs on a real DSN in the lifespan.
audit_store = AuditStore(_audit_url)
audit_reader = audit_store

# Usage-metering ledger (Slice 4). Instantiated in the lifespan once the audit +
# app pools and the usage_rates migration are ready; None until then (and on
# deployments without the audit tier — metering disabled, non-load-bearing).
usage_ledger: UsageLedger | None = None


async def _workspace_metering_attribution(
    owner_kind: str, owner_id: str
) -> dict[str, Any] | None:
    """Resolve a workspace owner → {user_id, project_id} for ledger attribution.

    Best-effort (used by the Slice 4b metering loop): a missing/deleted owner
    yields None — the compute row is still recorded, just unattributed.
    """
    try:
        row = (
            await postgres_db.get_job(owner_id)
            if owner_kind == "job"
            else await postgres_db.get_thread(owner_id)
        )
        if not row:
            return None
        return {"user_id": row.get("user_id"), "project_id": row.get("project_id")}
    except Exception:
        return None


# Session router singletons — see docs/features/direct_session_websockets.md
import json as _session_json  # noqa: E402

_session_annotations_raw = os.environ.get("SESSION_INGRESS_ANNOTATIONS", "{}")
try:
    _session_annotations = _session_json.loads(_session_annotations_raw)
    if not isinstance(_session_annotations, dict):
        _session_annotations = {}
except (ValueError, TypeError):
    logger.warning(
        "SESSION_INGRESS_ANNOTATIONS env not valid JSON: %r — falling back to {}",
        _session_annotations_raw,
    )
    _session_annotations = {}

session_router = SessionRouterService(
    namespace=os.environ.get("SESSION_INGRESS_NAMESPACE", "default"),
    ingress_host=os.environ.get("SESSION_INGRESS_HOST", "api.example.com"),
    ingress_class=os.environ.get("SESSION_INGRESS_CLASS", "traefik"),
    annotations=_session_annotations,
    tls_secret_name=os.environ.get("SESSION_INGRESS_TLS_SECRET") or None,
)

_session_jwt_secret = os.environ.get("SESSION_JWT_SECRET", "")
if _session_jwt_secret:
    session_tokens = SessionTokenService(
        secret=_session_jwt_secret,
        ttl_seconds=int(os.environ.get("SESSION_JWT_TTL_S", "60")),
    )
else:
    # Allow boot without session_tokens (e.g., during chart install before
    # the Secret is set). Calls to GET /connection will fail at runtime
    # with a clear error.
    session_tokens = None  # type: ignore[assignment]
    logger.warning("SESSION_JWT_SECRET not set — direct WS session endpoints will fail")


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


async def _next_output_ordinal(repo_name: str, base_branch: str) -> str:
    """Return the next zero-padded ordinal for `outputs/<n>-...` on base_branch.

    Per-repo, recency-ordered. Sequential (no async subjobs), so max+1 is race-free.
    """
    entries = (
        await gitea_client.list_contents(repo_name, "outputs", ref=base_branch) or []
    )
    nums = []
    for entry in entries:
        if entry.get("type") == "dir":
            m = re.match(r"(\d+)-", entry.get("name", ""))
            if m:
                nums.append(int(m.group(1)))
    nxt = (max(nums) + 1) if nums else 1
    return f"{nxt:03d}"


async def _graft_subjob_output(job_id: str) -> dict[str, Any] | None:
    """Graft a completed subjob's ``output/`` onto its parent's branch.

    Copies the subjob branch's ``output/`` subtree to
    ``outputs/<n>-<config>-<short_id>/`` on the parent branch in a single
    commit. Purely additive — never modifies/deletes parent content, so
    collisions and clobbering are impossible. Critic subjobs graft nothing
    (verdict is consumed from the DB).
    See docs/superpowers/specs/2026-05-24-subjob-output-merge-model-design.md.
    """
    import base64

    job = await postgres_db.get_job(job_id)
    if not job or not job.get("parent_job_id"):
        return None
    if not job.get("branch_name") or not job.get("repo_name"):
        logger.debug(f"Subjob {job_id} has no branch/repo — skipping graft")
        return None
    if not gitea_client.is_initialized:
        logger.warning(f"Gitea not initialized — cannot graft subjob {job_id}")
        return None

    # Critic contributes nothing to the branch (verdict lives in the DB).
    ctx = job.get("context") or {}
    if isinstance(ctx, str):
        try:
            ctx = json.loads(ctx)
        except (json.JSONDecodeError, ValueError):
            ctx = {}
    if isinstance(ctx, dict) and ctx.get("verification_target"):
        await postgres_db.update_job_merge_status(job_id, merge_status="skipped")
        return {"status": "skipped", "reason": "critic-not-merged"}

    # Idempotency: never graft twice — a second run would copy the output under
    # a fresh ordinal and duplicate it. If a graft path is already recorded, skip.
    if isinstance(ctx, dict) and ctx.get("graft_output_path"):
        return {
            "status": "skipped",
            "reason": "already-grafted",
            "output_path": ctx["graft_output_path"],
        }

    repo_name = job["repo_name"]
    subjob_branch = job["branch_name"]
    short_id = str(job_id)[:8]
    config_name = job.get("config_name") or "subjob"

    parent = await postgres_db.get_job(str(job["parent_job_id"]))
    base_branch = (parent.get("branch_name") if parent else None) or "main"

    tree = await gitea_client.list_tree(repo_name, ref=subjob_branch) or []
    output_blobs = [
        e["path"]
        for e in tree
        if e.get("type") == "blob" and e["path"].startswith("output/")
    ]
    if not output_blobs:
        await postgres_db.update_job_merge_status(job_id, merge_status="skipped")
        return {"status": "skipped", "reason": "no-output"}

    ordinal = await _next_output_ordinal(repo_name, base_branch)
    dest = f"outputs/{ordinal}-{config_name}-{short_id}"

    files: list[dict] = []
    for path in output_blobs:
        data = await gitea_client.get_file_bytes(repo_name, path, ref=subjob_branch)
        if data is None:
            logger.warning(f"Graft {job_id}: failed to read {path}; aborting graft")
            await postgres_db.update_job_merge_status(
                job_id, merge_status="graft-failed"
            )
            return {"status": "error", "reason": "read-failed", "path": path}
        rel = path[len("output/") :]
        files.append(
            {
                "path": f"{dest}/{rel}",
                "content_b64": base64.b64encode(data).decode("ascii"),
            }
        )

    ok = await gitea_client.change_files(
        repo_name, base_branch, files, message=f"Graft {dest} from subjob {short_id}"
    )
    if not ok:
        await postgres_db.update_job_merge_status(job_id, merge_status="graft-failed")
        return {"status": "error", "reason": "write-failed"}

    await postgres_db.update_job_merge_status(job_id, merge_status="grafted")
    new_ctx = dict(ctx)
    new_ctx["graft_output_path"] = dest
    await postgres_db.update_job_context(job_id, new_ctx)

    logger.info(
        f"Grafted subjob {short_id}/{config_name} output ({len(files)} files) "
        f"to {base_branch}:{dest}"
    )
    return {
        "status": "grafted",
        "base_branch": base_branch,
        "output_path": dest,
        "ordinal": ordinal,
        "files": len(files),
    }


async def _maybe_graft_completed_subjob(job: dict[str, Any]) -> dict[str, Any] | None:
    """Graft any completed subjob's output onto its parent. Applies uniformly
    to scholar, delegation children, and any other subjob; critic is skipped
    inside _graft_subjob_output. Root jobs (no parent) are ignored."""
    if not job.get("parent_job_id"):
        return None
    return await _graft_subjob_output(str(job["id"]))


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


# =============================================================================
# Background Tasks
# =============================================================================

# Flag to signal shutdown to background tasks
_shutdown_event: asyncio.Event | None = None

# Auto-assignment toggle (env var, default true)
AUTO_ASSIGN_ENABLED = os.environ.get("AUTO_ASSIGN_ENABLED", "true").lower() in (
    "true",
    "1",
    "yes",
)

# Dispatcher lock prevents concurrent dispatch (double-assignment)
_dispatch_lock = asyncio.Lock()

# Track jobs with pending pause requests (prevent re-preemption)
_pause_pending_job_ids: set[str] = set()


async def stale_agent_detector(shutdown_event: asyncio.Event) -> None:
    """Background task that reconciles agent state every 60 seconds.

    Two dimensions of reconciliation:

    1. Heartbeat freshness — agents that stopped reporting get marked offline,
       which in turn flips their threads to 'ended' and pauses their jobs.
    2. Self-reported consistency — agents that *are* heartbeating but report
       internally inconsistent state (working with no job, session bound to
       an ended thread) get flipped back to 'ready' so the dispatcher can
       reuse the slot. These zombies pass the heartbeat check and would
       otherwise hold pool slots indefinitely.

    Finally, offline agents older than 24h are GC'd to keep the table small.
    """
    logger.info("Stale agent detector started")
    while not shutdown_event.is_set():
        try:
            # 1. Heartbeat-based: mark non-responsive agents offline
            count = await postgres_db.mark_stale_agents_offline(timeout_minutes=3)
            if count > 0:
                logger.info(
                    f"Marked {count} agent(s) as offline due to missed heartbeats"
                )

            # 2. Consistency-based: release slots held by zombie agents
            stuck_working = await postgres_db.mark_stuck_working_agents_ready()
            if stuck_working > 0:
                logger.info(
                    f"Released {stuck_working} agent(s) stuck in 'working' with no job"
                )
                _trigger_dispatch()
            stuck_session = await postgres_db.mark_stuck_session_agents_ready()
            if stuck_session > 0:
                logger.info(
                    f"Released {stuck_session} agent(s) stuck in 'session' "
                    f"on ended thread"
                )

            # 2b. STOPGAP — reap session agents wedged with NO bound thread/job.
            # mark_stuck_session_agents_ready (above) can't reach these: its
            # predicate needs thread_id IS NOT NULL, and a *live* agent
            # re-asserts 'session' on every 5s heartbeat so a flip-to-ready
            # never sticks — deleting the pod is the only actuation that does.
            # Scoped to thread_id + current_job_id both NULL (holds nothing
            # user-visible), so it never touches a thread-bound live session
            # (the 2026-06-10 incident). Proper fix = the intent/observed split
            # in docs/features/unified_instance_lifecycle.md. Tracking:
            # docs/issues/lifecycle_session_agents_without_thread_never_drain.md
            orphaned_sessions = await postgres_db.reap_orphaned_session_agents(
                grace_minutes=5
            )
            for orphan in orphaned_sessions:
                deleted = await agent_provisioner.delete_agent_pod(orphan["hostname"])
                logger.warning(
                    "Reaped orphaned session agent %s (pod=%s, deleted=%s): "
                    "'session' with no thread/job past grace",
                    orphan["id"],
                    orphan["hostname"],
                    deleted,
                )

            # 3. Propagate: threads bound to offline agents → 'ended'
            ended_ids = await postgres_db.mark_orphaned_threads_ended()
            if ended_ids:
                logger.info(f"Marked {len(ended_ids)} thread(s) as ended (orphaned)")
                # The DB transition leaves workspace + agent pods alive — release
                # them here so we don't depend on the idle sweeper as the only
                # backstop (it's disabled whenever S3 snapshots are unavailable).
                for thread_id in ended_ids:
                    await _release_thread_resources(thread_id)

            # 3b. Propagate: PAUSED threads (awaiting_user / suspended) bound to
            # offline agents → 'suspended' with agent_id cleared, so the next
            # open re-provisions instead of 409-looping. These are the paused
            # states mark_orphaned_threads_ended intentionally skips; suspend
            # (not release) keeps them resumable.
            suspended_ids = await postgres_db.mark_orphaned_threads_suspended()
            if suspended_ids:
                logger.info(f"Suspended {len(suspended_ids)} orphaned paused thread(s)")
                for thread_id in suspended_ids:
                    await _suspend_thread_resources(thread_id)

            # 4. Propagate: jobs assigned to offline agents → paused
            recovered = await postgres_db.recover_orphaned_jobs()
            if recovered > 0:
                logger.info(
                    f"Recovered {recovered} orphaned job(s) from offline agents"
                )
                _trigger_dispatch()

            # 5. GC: drop offline agent rows older than 24h
            gc_count = await postgres_db.gc_offline_agents(retention_hours=24)
            if gc_count > 0:
                logger.info(f"GC'd {gc_count} offline agent record(s) > 24h old")
        except Exception as e:
            logger.error(f"Error in stale agent detector: {e}")

        # Wait 60 seconds or until shutdown
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=60.0)
            break  # Shutdown signaled
        except asyncio.TimeoutError:
            pass  # Continue loop

    logger.info("Stale agent detector stopped")


async def agent_pool_reconciler(shutdown_event: asyncio.Event) -> None:
    """Background task that maintains the dynamic agent pool.

    Runs every 60 seconds:
    - Ensures MIN_AGENTS warm pods exist (instant dispatch)
    - Reaps completed / stale / unstartable agent pods (single dispatcher)

    Drift-based draining lives in ``lifecycle_reconciler_loop`` now —
    this loop only owns capacity (warm pool + scale-down) and crash GC.
    """
    logger.info("Agent pool reconciler started")
    while not shutdown_event.is_set():
        try:
            if agent_provisioner.is_available:
                await agent_provisioner.ensure_warm_pool()
                await agent_provisioner.reap_pods()
                await agent_provisioner.scale_down_idle()
        except Exception as e:
            logger.error("Error in agent pool reconciler: %s", e)

        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=60.0)
            break
        except asyncio.TimeoutError:
            pass

    logger.info("Agent pool reconciler stopped")


async def lifecycle_reconciler_loop(
    shutdown_event: asyncio.Event,
    reconciler: InstanceLifecycleReconciler,
) -> None:
    """Background task driving the unified instance lifecycle reconciler.

    Runs every 60 seconds. The reconciler delegates to per-kind
    managers (``AgentInstanceManager`` etc.) for drift detection and
    drain. Crash detection still flows through ``reap_pods`` in the
    sibling ``agent_pool_reconciler`` for now; consolidation is a
    follow-up.
    """
    logger.info("Lifecycle reconciler loop started")
    while not shutdown_event.is_set():
        try:
            await reconciler.tick()
        except Exception:
            logger.exception("Lifecycle reconciler tick failed")

        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=60.0)
            break
        except asyncio.TimeoutError:
            pass

    logger.info("Lifecycle reconciler loop stopped")


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


async def workspace_idle_sweeper(shutdown_event: asyncio.Event) -> None:
    """Background loop: reconciles failed/missing session workspaces.

    Idle suspension and teardown now live in the lifecycle reconciler's reap
    path (``services/lifecycle/reconciler.py`` → ``WorkspaceInstanceManager``),
    which snapshots-then-deletes reapable workspaces and force-deletes ones it
    can never reach (bounded retry) instead of keeping them alive forever.

    This loop retains only the session-workspace recovery reconcile —
    recreating failed/missing workspaces for active sessions — which is
    independent of idle policy. Runs every 60 seconds.
    """
    logger.info("Workspace idle sweeper started (reconcile-only)")
    while not shutdown_event.is_set():
        # Session workspace reconcile (safety-net): recreate failed/missing
        # workspaces for active sessions. Runs regardless of whether idle
        # suspension is enabled — recovering a wedged workspace is independent
        # of idle policy. This is the session-side equivalent of the job
        # dispatcher's per-cycle workspace reconcile.
        # (reconcile_session_workspaces never raises; the try/except is a
        # belt-and-suspenders guard so a future change can't kill this loop.)
        try:
            await reconcile_session_workspaces(
                db=postgres_db,
                provisioner=container_provisioner,
                suspension=workspace_suspension_service,
            )
        except Exception as e:
            logger.error("Error in session workspace reconcile: %s", e)

        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=60.0)
            break
        except asyncio.TimeoutError:
            pass

    logger.info("Workspace idle sweeper stopped")


async def code_server_settings_sweeper(shutdown_event: asyncio.Event) -> None:
    """Background loop: reconcile per-user code-server IDE settings.

    Workspaces are network-isolated from the orchestrator (egress is denied), so
    instead of the workspace pushing changes, the orchestrator pulls inward on a
    ~10-minute cycle: it reads each active workspace's code-server config files
    (settings.json, keybindings.json, snippets) over SSH and merges any newer
    edits into the owning user's stored settings (``users.settings['ide']``).
    Conflict resolution is by filesystem mtime — newest wins, per file — so the
    cycle order across a user's workspaces doesn't matter. See
    orchestrator/services/ide_settings.py.
    """
    if os.environ.get("IDE_SETTINGS_SYNC_ENABLED", "true").lower() not in (
        "1",
        "true",
        "yes",
    ):
        logger.info("Code-server settings sweeper disabled (IDE_SETTINGS_SYNC_ENABLED)")
        return

    from services.ide_settings import (
        IdeSettingsStore,
        OpenVsxClassifier,
        _coerce_context,
        capture_ide_profile,
        list_ide_extensions,
        pull_ide_config,
        reconcile_extensions,
        reconcile_ide_settings,
        resolve_ssh_target,
    )

    interval = float(os.environ.get("IDE_SETTINGS_SYNC_INTERVAL_S", "600"))
    store = IdeSettingsStore(postgres_db)
    classifier = OpenVsxClassifier()  # cache persists across cycles for this process
    logger.info("Code-server settings sweeper started (interval=%.0fs)", interval)
    while not shutdown_event.is_set():
        try:
            workspaces = await postgres_db.list_active_ide_workspaces()
            if workspaces:
                count = await reconcile_ide_settings(store, workspaces, pull_ide_config)
                if count:
                    logger.info("IDE settings sweeper: synced %d file(s)", count)
                try:
                    ext_changed = await reconcile_extensions(
                        store, workspaces, list_ide_extensions, classifier
                    )
                    if ext_changed:
                        logger.info(
                            "IDE settings sweeper: synced %d extension(s)", ext_changed
                        )
                except Exception as e:  # noqa: BLE001
                    logger.error("Error reconciling extensions: %s", e)

                # Capture license/globalStorage + non-Open-VSX bytes to S3 when a
                # workspace's content signature changed (Phase B). Signature-gated
                # inside capture_ide_profile so most cycles are a cheap no-op.
                if snapshot_service.is_available:
                    from services.ide_profile_store import IdeProfileStore

                    profile = IdeProfileStore(
                        snapshot_service._s3, snapshot_service._bucket
                    )
                    for ws in workspaces:
                        uid = ws.get("user_id")
                        if not uid:
                            continue
                        tgt = resolve_ssh_target(_coerce_context(ws.get("context")))
                        if not tgt:
                            continue
                        try:
                            await capture_ide_profile(
                                store, str(uid), tgt[0], tgt[1], profile
                            )
                        except Exception as e:  # noqa: BLE001
                            logger.warning("ide profile capture failed: %s", e)
        except Exception as e:
            logger.error("Error in code-server settings sweeper: %s", e)

        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=interval)
            break
        except asyncio.TimeoutError:
            pass

    logger.info("Code-server settings sweeper stopped")


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


async def quiet_hours_digest_loop(shutdown_event: asyncio.Event) -> None:
    """Background task that flushes queued notifications when quiet hours end.

    Runs every 5 minutes. For each user whose quiet hours have ended and
    who has pending notifications, sends a batched digest.
    """
    while not shutdown_event.is_set():
        try:
            users = await postgres_db.get_users_exiting_quiet_hours(
                check_window_minutes=5
            )
            for user_data in users:
                user_id = str(user_data["user_id"])
                user_settings = user_data.get("settings") or {}

                # Only process if quiet hours actually ended (not still in them)
                if notification_service._is_in_quiet_hours(user_settings):
                    continue

                pending = await postgres_db.get_pending_notifications(user_id)
                if not pending:
                    continue

                await notification_service.dispatch_digest(
                    user_id=user_id,
                    notifications=[dict(n) for n in pending],
                )

                ids = [str(n["id"]) for n in pending]
                await postgres_db.mark_notifications_delivered(ids)

                logger.info(
                    "Digest sent to user %s: %d notification(s)",
                    user_id[:8],
                    len(pending),
                )
        except Exception as e:
            logger.error(f"Quiet hours digest loop error: {e}")

        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=300)  # 5 minutes
            break
        except asyncio.TimeoutError:
            pass

    logger.info("Quiet hours digest loop stopped")


async def imap_poll_loop(shutdown_event: asyncio.Event) -> None:
    """Background task that polls IMAP for inbound email replies.

    Runs every IMAP_POLL_INTERVAL seconds (default: 30).
    Gracefully disabled when IMAP is not configured.
    """
    if not imap_poller.is_available:
        logger.info("IMAP poller not started (not configured)")
        return

    logger.info("IMAP poller started (interval=%ds)", imap_poller.poll_interval)
    while not shutdown_event.is_set():
        try:
            count = await imap_poller.poll_once()
            if count > 0:
                logger.info("IMAP poller: processed %d email reply(ies)", count)
        except Exception as e:
            logger.error("IMAP poller error: %s", e)

        try:
            await asyncio.wait_for(
                shutdown_event.wait(),
                timeout=imap_poller.poll_interval,
            )
            break
        except asyncio.TimeoutError:
            pass

    logger.info("IMAP poller stopped")


# =============================================================================
# Job Auto-Assignment Dispatcher
# =============================================================================


def _is_experts_db_enabled() -> bool:
    """True when DB-backed experts / orchestrator-resolved config is on (env).

    Gates whether the orchestrator resolves the full config at dispatch/attach
    and emits a ``resolved_config`` blob. Off → the agent uses its ``from_config``
    fallback (today's path). Dev on / prod off (helm ``expertsDbEnabled``).
    """
    return os.getenv("EXPERTS_DB_ENABLED", "").lower().strip() in ("true", "1", "yes")


def _is_skills_db_enabled() -> bool:
    """True when DB-backed Agent Skills are on (env). Dev on / prod off (helm
    ``skillsDbEnabled``). Mirrors ``EXPERTS_DB_ENABLED``."""
    return os.getenv("SKILLS_DB_ENABLED", "").lower().strip() in ("true", "1", "yes")


async def _resolve_default_models(user_id: str | None) -> dict[str, Any]:
    """Effective default chat + auxiliary MODEL NAMES for a user (no transport).

    Mirrors the model selection in ``_inject_dispatch_credentials`` /
    ``_inject_thread_dispatch_credentials``: a user's pinned default wins, else
    the system capability default. Returned as a config layer
    (``{"llm": {"model": ...}, "auxiliary": {"model": ...}}``) that
    ``resolve_config`` applies above the base config's placeholder model and below
    the expert. base_url/api_key for the chosen models are injected into the
    delivery blob, not here. Reused by job dispatch AND session attach.
    """
    out: dict[str, Any] = {}
    user_settings: dict[str, Any] = {}
    if user_id:
        user_settings = await postgres_db.get_user_settings(str(user_id)) or {}
    chat = user_settings.get(
        "default_model"
    ) or await postgres_db.resolve_default_for_capability("chat")
    aux = user_settings.get(
        "default_auxiliary_model"
    ) or await postgres_db.resolve_default_for_capability("auxiliary")
    if chat:
        out.setdefault("llm", {})["model"] = chat
    if aux:
        out.setdefault("auxiliary", {})["model"] = aux
    return out


async def _resolve_session_config(
    thread: dict[str, Any],
    metadata: dict[str, Any],
    *,
    config_override: dict[str, Any] | None = None,
    status: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Resolve a persistent thread's full config to a delivery blob (credential-
    injected), or ``None`` when experts are off / resolution fails (→ the agent's
    ``config_name`` + ``config_override`` fallback).

    The session sibling of the job-dispatch resolve. Sessions **re-resolve on
    every (re)attach** — there is no freeze (mutable run; spec delivery table).
    Layers: persistent_defaults base + default-model floor + DB expert
    (``metadata.expert_id``) + thread ``config_override`` (request) → creds.
    ``config_override`` overrides ``metadata.config_override`` for the warm-pool
    path (it carries the attach-time lite-workspace backend).
    """
    if not _is_experts_db_enabled() or not await _user_experts_enabled():
        if status is not None:
            status["state"] = "disabled"
        return None
    try:
        user_id = str(thread["user_id"]) if thread.get("user_id") else None
        project_id = str(thread["project_id"]) if thread.get("project_id") else None
        expert_id = metadata.get("expert_id")
        expert_row = (
            await postgres_db.get_expert_by_id(str(expert_id)) if expert_id else None
        )
        base = thread.get("config_name") or "persistent_defaults"
        if base in ("default", "persistent_default") or _looks_like_uuid(base):
            # Sentinel / cockpit-conflated expert UUID → resolve onto the real
            # session base; the expert is delivered via expert_id, not the name.
            base = "persistent_defaults"
        request_override = (
            config_override
            if config_override is not None
            else (metadata.get("config_override") or None)
        )
        base_defaults = await _resolve_default_models(user_id)
        _cap: dict = {}
        _skills_payload = await _gather_in_scope_skills(
            user_id, [project_id] if project_id else None
        )
        resolved = resolve_config(
            base_config_name=base,
            base_defaults=base_defaults,
            expert_row=expert_row,
            request_override=request_override,
            expert_type="session",
            capture=_cap,
            skills=_skills_payload,
        )
        # Bound skills are delivered deterministically (instructions channel);
        # strip them from the model-invoked catalog so they aren't double-offered.
        from src.core.skill_resolution import filter_bound_skills

        filter_bound_skills(resolved)
        # Session dispatch PEP (decision 9): the merged config — including
        # interactive.permission_mode and any persistent_agent keys baked into
        # config_override — must fit the runner's grants. GrantDenied escapes the
        # generic except below (fail closed: never deliver the unvetted override).
        await _enforce_dispatch_grants(
            _cap["merged_fragment"],
            runner_user_id=user_id,
            project_ids=[project_id] if project_id else [],
        )
        delivered = await inject_blob_credentials(
            resolved,
            lambda co: _inject_thread_dispatch_credentials(
                co, user_id=user_id, project_id=project_id
            ),
        )
        if status is not None:
            status["state"] = "ok"
        return delivered
    except GrantDenied:
        if status is not None:
            status["state"] = "denied"
        raise
    except Exception:
        logger.exception(
            "Session resolve failed for thread %s; falling back to config_name",
            thread.get("id"),
        )
        if status is not None:
            status["state"] = "error"
        return None


def _looks_like_uuid(value: Any) -> bool:
    """True if ``value`` parses as a UUID (a cockpit-conflated expert id in the
    config_name slot, which must not be treated as a config file name)."""
    try:
        UUID(str(value))
        return True
    except (ValueError, TypeError, AttributeError):
        return False


def _gateway_routing_target() -> tuple[str, str] | None:
    """OpenAI-compatible ``(base_url, api_key)`` for the LiteLLM gateway, or None.

    Returns None unless the gateway is enabled (``LITELLM_BASE_URL`` set — the
    chart only populates it when ``litellm.enabled``). When set, **endpoint-kind**
    models are pointed here instead of straight at their upstream, so all of their
    traffic is measured at the one chokepoint (Slice 1). See
    docs/features/usage_monitoring_and_rate_limiting.md.

    The ``/v1`` suffix matches what the agent's OpenAI factory expects (direct
    endpoint base_urls carry it too); the orchestrator's admin/health client uses
    the bare ``LITELLM_BASE_URL`` instead.

    Credential: the shared **fleet key** (Slice 2a) — a non-admin key carrying
    the aggregate backstop, so the admin master key (which bypasses all limits)
    never reaches agents. Falls back to the master key only in the brief startup
    window before the fleet key is provisioned. Slice 2b replaces the shared key
    with per-job least-privilege keys minted at dispatch (per-job-client-rebuild
    prereq: docs/issues/agent_loop_mode_pod_reuse.md).
    """
    base = os.getenv("LITELLM_BASE_URL", "").strip()
    if not base:
        return None
    key = get_fleet_key() or os.getenv("LITELLM_MASTER_KEY", "").strip()
    if not key:
        return None
    return f"{base.rstrip('/')}/v1", key


async def _gateway_routing_target_scoped(
    user_id: str | None, project_id: str | None
) -> tuple[str, str] | None:
    """Gateway ``(base_url, api_key)`` using the per-(user,project) scoped key (2b).

    Ensures the job's scoped key (+ its team/internal-user, carrying the
    per-project / per-user limits) exists, then routes the agent onto it so
    LiteLLM enforces those limits. Falls back to the shared fleet key (2a) when
    the job has no user/project, when the gateway is briefly unreachable, or when
    the scoped-key ensure fails — the fleet key still caps the aggregate and keeps
    agents off the admin master key, so a transient gateway blip degrades to
    "measured + aggregate-capped" rather than failing the dispatch.

    Returns None only when the gateway is disabled entirely (so callers keep their
    direct-endpoint path), matching :func:`_gateway_routing_target`.
    """
    base = os.getenv("LITELLM_BASE_URL", "").strip()
    if not base:
        return None
    master = os.getenv("LITELLM_MASTER_KEY", "").strip()
    if user_id and project_id and master:
        client = LiteLLMClient(base, master)
        try:
            if await client.is_ready():
                scoped = await ensure_scoped_key(
                    client,
                    postgres_db,
                    master,
                    user_id=user_id,
                    project_id=project_id,
                )
                if scoped:
                    return f"{base.rstrip('/')}/v1", scoped
        except Exception:
            logger.exception(
                "Scoped-key ensure failed (user=%s project=%s); "
                "falling back to fleet key",
                user_id,
                project_id,
            )
        finally:
            await client.aclose()
    return _gateway_routing_target()


# ---------------------------------------------------------------------------
# Slice 3 — longer-window quota stop (orchestrator-enforced).
#
# LiteLLM has no native request/token daily quota (only a dollar cron budget —
# capability gap 2), so the orchestrator polls per-project daily usage from the
# gateway and, when a project crosses its quota, freezes that project's running
# jobs (pause + workspace release) AND blocks the dispatcher from re-dispatching
# them. The two enforcement points share one in-memory set, owned by the poll
# loop. Per-project only for v1 (the per-user activity read is unreliable); daily
# UTC window (rolling deferred). See docs/features/usage_monitoring_and_rate_limiting.md.
# ---------------------------------------------------------------------------

# Projects currently frozen for crossing their daily quota. Rebound (not mutated)
# by the quota poll loop so the dispatcher always reads a consistent snapshot.
_over_quota_projects: frozenset[str] = frozenset()


def is_project_over_quota(project_id: str | None) -> bool:
    """True if this project is currently quota-frozen (cheap in-memory lookup).

    Called by the dispatcher per candidate job, so it must never touch the
    gateway/DB — it only consults the set the poll loop maintains.
    """
    return bool(project_id) and str(project_id) in _over_quota_projects


async def _active_project_ids() -> set[str]:
    """Distinct project ids with running/pending jobs — the quota poll's scope.

    Bounded to projects that actually have work, so the poll only reads usage for
    relevant teams. (At scale this becomes one ``SELECT DISTINCT project_id``; the
    few-hundred-row scan is fine for now — noted in the Scaling section.)
    """
    ids: set[str] = set()
    for status in ("processing", "created", "paused"):
        try:
            for job in await postgres_db.get_jobs(status=status, limit=500):
                pid = job.get("project_id")
                if pid:
                    ids.add(str(pid))
        except Exception:
            logger.exception("Quota poll: failed to list %s jobs", status)
    return ids


async def _freeze_project_over_quota(
    project_id: str, *, usage: dict[str, int], quota: dict[str, int]
) -> int:
    """Freeze a project's in-flight jobs after it crossed its daily quota.

    Pauses each ``processing`` job (frees its agent, stops re-dispatch via the
    gate) and releases its workspace snapshot-first, so it resumes cleanly once
    the quota resets. Tags ``freeze_data`` with ``type=quota_exceeded`` so the UI
    can tell a quota stop from a normal pause. Returns the count actually frozen.
    """
    try:
        jobs = await postgres_db.get_jobs(
            status="processing", scope_project_id=project_id, limit=200
        )
    except Exception:
        logger.exception("Quota freeze: failed to list jobs for project %s", project_id)
        return 0

    frozen = 0
    for job in jobs:
        job_id = str(job["id"])
        try:
            # pause_job guards on status='processing' + clears the agent; if it
            # lost the race (already paused/done), skip — nothing to freeze.
            if not await postgres_db.pause_job(job_id):
                continue
            await postgres_db.update_job_status(
                job_id,
                freeze_data={
                    "type": "quota_exceeded",
                    "scope": "project",
                    "project_id": project_id,
                    "usage": usage,
                    "quota": quota,
                },
            )
            await _cascade_pause_to_children(job_id)
            if container_provisioner.is_available:
                await container_provisioner.release_workspace(
                    WorkspaceOwner.job(job_id)
                )
            frozen += 1
            logger.warning(
                "Quota stop: froze job %s — project %s over daily quota (%s / %s)",
                job_id,
                project_id,
                usage,
                quota,
            )
        except Exception:
            logger.exception("Quota freeze: failed to freeze job %s", job_id)
    return frozen


async def _quota_poll_tick(client: LiteLLMClient) -> None:
    """One quota evaluation: refresh the over-quota set + freeze newly-over jobs."""
    global _over_quota_projects
    # Cheap short-circuit when no quota is configured (the common case until an
    # admin sets one): don't even scan jobs. Also clears a stale gate if the
    # policy was just removed.
    if not _parse_quota_policy():
        if _over_quota_projects:
            _over_quota_projects = frozenset()
        return

    project_ids = await _active_project_ids()
    if not project_ids:
        _over_quota_projects = frozenset()
        return

    day = datetime.now(timezone.utc).date().isoformat()
    status = await compute_project_quota_status(client, list(project_ids), day=day)
    now_over = frozenset(pid for pid, st in status.items() if st["over"])
    newly_over = now_over - _over_quota_projects
    newly_under = _over_quota_projects - now_over
    # Atomic rebind (not clear+update) so the dispatcher never sees a half-empty
    # set. Projects that dropped back under quota (e.g. the midnight reset) leave
    # the set here → their paused jobs become dispatchable again automatically.
    _over_quota_projects = now_over

    if newly_under:
        logger.info(
            "Quota: project(s) %s back under daily quota — dispatch re-enabled",
            sorted(newly_under),
        )
    for pid in newly_over:
        st = status[pid]
        logger.warning(
            "Quota: project %s crossed daily quota (usage=%s quota=%s) — freezing jobs",
            pid,
            st["usage"],
            st["quota"],
        )
        await _freeze_project_over_quota(pid, usage=st["usage"], quota=st["quota"])


async def quota_poll_loop(
    shutdown_event: asyncio.Event,
    postgres_db: Any,
    *,
    interval: float = 120.0,
) -> None:
    """Background loop: enforce per-project daily usage quotas (Slice 3).

    No-op when the gateway is unconfigured (``LITELLM_BASE_URL`` unset), mirroring
    the catalog sync. Slower cadence than the dispatcher (quotas move over hours,
    not seconds). Never raises into the lifespan: a tick failure is logged and
    retried. ``postgres_db`` is accepted for symmetry with the other loops (the
    helpers use the module global).
    """
    base_url = os.getenv("LITELLM_BASE_URL", "").strip()
    master_key = os.getenv("LITELLM_MASTER_KEY", "").strip()
    if not base_url or not master_key:
        logger.info("Quota poll loop disabled (gateway not configured)")
        return

    client = LiteLLMClient(base_url, master_key)
    logger.info("Quota poll loop starting (interval=%ss)", interval)
    try:
        while not shutdown_event.is_set():
            try:
                if await client.is_ready():
                    await _quota_poll_tick(client)
            except Exception:
                logger.exception("Quota poll tick failed (non-fatal)")
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
    finally:
        await client.aclose()
        logger.info("Quota poll loop stopped")


async def llm_usage_poll_loop(
    shutdown_event: asyncio.Event,
    *,
    interval: float = 120.0,
) -> None:
    """Background loop: materialize the LiteLLM spend log into usage_events (4c).

    No-op when the gateway is unconfigured or the ledger is unavailable. The
    spend-log scan is idempotent at the ledger (dedupe on request_id+unit+ts), so
    re-polling is safe; an in-memory cursor (max startTime processed) keeps each
    tick to only the new rows. Never raises into the lifespan.
    """
    base_url = os.getenv("LITELLM_BASE_URL", "").strip()
    master_key = os.getenv("LITELLM_MASTER_KEY", "").strip()
    if not base_url or not master_key:
        logger.info("LLM usage poll loop disabled (gateway not configured)")
        return
    if usage_ledger is None or not usage_ledger.is_available:
        logger.info("LLM usage poll loop disabled (usage ledger unavailable)")
        return

    client = LiteLLMClient(base_url, master_key)
    cursor = None
    logger.info("LLM usage poll loop starting (interval=%ss)", interval)
    try:
        while not shutdown_event.is_set():
            try:
                if await client.is_ready():
                    res = await materialize_llm_usage(
                        client, usage_ledger, since=cursor
                    )
                    cursor = res.get("cursor") or cursor
            except Exception:
                logger.exception("LLM usage poll tick failed (non-fatal)")
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
    finally:
        await client.aclose()
        logger.info("LLM usage poll loop stopped")


async def _inject_dispatch_credentials(
    job: dict[str, Any],
    config_override: dict[str, Any] | None,
) -> dict[str, Any]:
    """Resolve and inject API keys, model routing, and capability defaults.

    Mutates ``config_override`` in place (creating it if None) with everything
    the agent needs to reach its configured LLM endpoints: per-user/project
    API keys, endpoint base_url + api_key for catalog-routed models, user-
    preference fallbacks (default chat/auxiliary/strategic/tactical models,
    autonomy, reasoning level, vision/whisper/tts), and the system-level
    default chat model when no override pinned one.

    Called from both first-dispatch (``_dispatch_job_to_agent``) and resume
    (``_resume_job_on_agent``) — without this on resume, an orphaned/paused
    job re-dispatched to a fresh agent would inherit only the bare
    creation-time config_override (no model/api_key) and the agent would
    silently fall back to ``OPENAI_API_KEY=not-needed``, producing 401s
    against the user's router.

    Returns the (mutated) ``config_override`` dict so callers can rebind
    locals when the input was None.
    """
    job_id = str(job["id"])
    user_id_str = str(job["user_id"]) if job.get("user_id") else None
    project_id_str = str(job["project_id"]) if job.get("project_id") else None

    resolved_keys = await postgres_db.resolve_api_keys_for_job(
        user_id=user_id_str,
        project_id=project_id_str,
    )

    # Resolve the gateway routing target once for the whole dispatch: the
    # per-(user,project) scoped key (Slice 2b) when both are known, else the
    # shared fleet key (2a). Computed here so every model section the job pins
    # (top-level + strategic/tactical/aux) routes through the *same* key, so the
    # job's per-project/per-user limits apply uniformly across its LLM surface.
    _gw_scoped = await _gateway_routing_target_scoped(user_id_str, project_id_str)

    config_override = config_override or {}
    llm_over = config_override.setdefault("llm", {})
    model_id = llm_over.get("model")
    meta = None
    if model_id:
        try:
            meta = await _resolve_model(model_id, user_id=user_id_str)
        except UnknownModelError:
            meta = None

    if (
        meta is not None
        and meta.origin in ("custom", "system", "catalog")
        and meta.endpoint_id
    ):
        # Endpoint-backed models carry their agent-side factory in meta.provider
        # (``openai`` for the OpenAI-compatible wire, ``codex`` for the Responses-
        # API Codex proxy). Inject it so the agent builds the right factory — the
        # endpoint branch otherwise leaves provider unset and the agent defaults
        # to the openai factory, which forces Chat Completions and strips gpt-5.x
        # reasoning. See docs/done/litellm_gateway_drops_gpt_codex_reasoning_capture.md
        if meta.provider:
            llm_over.setdefault("provider", meta.provider)
        # The Codex proxy speaks ONLY the Responses API; the LiteLLM gateway
        # normalizes to Chat Completions and drops the reasoning summary, so codex
        # models bypass the gateway and hit their endpoint directly.
        _gw = _gw_scoped if meta.provider != "codex" else None
        if _gw is not None:
            # Endpoint-kind model + gateway enabled → route through LiteLLM so the
            # traffic is measured at the chokepoint AND the job's scoped key
            # (2b) applies its per-project/per-user limits. The gateway resolves
            # the model_id to its real upstream (registered by the catalog sync).
            llm_over.setdefault("base_url", _gw[0])
            llm_over.setdefault("api_key", _gw[1])
            logger.info(
                f"Dispatch: routed {model_id} via LiteLLM gateway "
                f"(endpoint {meta.endpoint_id})"
            )
        else:
            endpoint_row = await postgres_db.get_user_llm_endpoint(meta.endpoint_id)
            if endpoint_row:
                if endpoint_row.get("base_url"):
                    llm_over.setdefault("base_url", endpoint_row["base_url"])
                if endpoint_row.get("api_key"):
                    llm_over.setdefault("api_key", endpoint_row["api_key"])
                logger.info(
                    f"Dispatch: routed {model_id} to {meta.origin} endpoint "
                    f"{endpoint_row.get('label') or meta.endpoint_id}"
                )
    elif resolved_keys:
        if meta is not None and meta.api_key_ref:
            provider_for_key: str | None = meta.api_key_ref
        else:
            provider_for_key = _dispatch_llm_provider_fallback(job, config_override)
        # Route to the right agent-side LLM factory. System-anchored catalog
        # rows carry no endpoint base_url, so without an explicit provider the
        # agent's create_llm defaults to the OpenAI factory (api.openai.com)
        # and rejects e.g. an OpenRouter sk-or-v1 key. meta.provider already
        # holds the factory name (_factory_provider); fall back to the
        # key-inference result for registry misses.
        factory_provider = meta.provider if meta is not None else provider_for_key
        if factory_provider:
            llm_over.setdefault("provider", factory_provider)
        if (
            provider_for_key
            and provider_for_key in resolved_keys
            and "api_key" not in llm_over
        ):
            llm_over["api_key"] = resolved_keys[provider_for_key]

    # Per-model context window: drive the agent's working window from the
    # catalog/admin value. Lands in llm.model_max_context_tokens (a flat llm
    # key) so it survives the agent-side settings-matrix re-run and becomes the
    # base for the derived limits. Truthy guard rejects None and an explicit 0.
    if meta is not None and meta.context_window:
        llm_over.setdefault("model_max_context_tokens", meta.context_window)

    if resolved_keys:
        _ENV_KEY_MAP = {"vision": "VISION_API_KEY"}
        env_keys = {
            _ENV_KEY_MAP[p]: resolved_keys[p] for p in ("vision",) if p in resolved_keys
        }
        if env_keys:
            config_override.setdefault("env_keys", {}).update(env_keys)
        logger.info(
            f"Dispatch: injected API keys for providers: {list(resolved_keys.keys())}"
        )

    # Resolve credentials for any capability/phase section the job explicitly
    # pinned a model on. The top-level branch above only inspects `llm.model`;
    # without this loop, an override like
    # `{"llm": {"tactical": {"model": "X"}}}` ships the model name with no
    # `base_url`/`api_key`, the agent's LLM factory falls back to the parent's
    # base_url, and X's endpoint never gets hit — producing opaque 404s when
    # X lives behind a non-default endpoint. The user-default phase pin block
    # further down catches the same hole for unpinned phases, so the two
    # blocks together cover both shapes: explicit job overrides (here) and
    # user-default fallback (below).
    for _section_name, _parent, _capability in (
        ("auxiliary", config_override, "auxiliary"),
        ("strategic", llm_over, "chat"),
        ("tactical", llm_over, "chat"),
    ):
        _section = _parent.get(_section_name)
        if not isinstance(_section, dict):
            continue
        _section_model = _section.get("model")
        if not _section_model or _section.get("base_url"):
            continue
        await _inject_model_credentials(
            section=_section,
            model_id=_section_model,
            user_id=user_id_str,
            resolved_keys=resolved_keys,
            capability=_capability,
            gateway_override=_gw_scoped,
        )
        if "api_key" not in _section and "base_url" not in _section:
            logger.warning(
                f"Dispatch: job {job_id} pinned {_section_name} model "
                f"{_section_model!r} but no endpoint or provider key was "
                f"resolvable — the agent will fall back to the parent "
                f"base_url and almost certainly 404."
            )
        else:
            logger.info(
                f"Dispatch: injected credentials for {_section_name} "
                f"override: {_section_model}"
            )

    if job.get("user_id"):
        user_settings = await postgres_db.get_user_settings(str(job["user_id"]))
        aux_model = user_settings.get("default_auxiliary_model")
        if not aux_model:
            aux_model = await postgres_db.resolve_default_for_capability("auxiliary")
        if aux_model:
            aux_override = config_override.setdefault("auxiliary", {})
            if "model" not in aux_override:
                aux_override["model"] = aux_model
                await _inject_model_credentials(
                    section=aux_override,
                    model_id=aux_model,
                    user_id=user_id_str,
                    resolved_keys=resolved_keys,
                    capability="auxiliary",
                    gateway_override=_gw_scoped,
                )
                logger.info(f"Dispatch: injected auxiliary model override: {aux_model}")

        default_model = user_settings.get("default_model")
        if default_model:
            llm_override = config_override.setdefault("llm", {})
            if "model" not in llm_override:
                llm_override["model"] = default_model
                await _inject_model_credentials(
                    section=llm_override,
                    model_id=default_model,
                    user_id=user_id_str,
                    resolved_keys=resolved_keys,
                    gateway_override=_gw_scoped,
                )
                logger.info(f"Dispatch: injected user default_model: {default_model}")

        for _phase, _setting_key in (
            ("strategic", "default_strategic_model"),
            ("tactical", "default_tactical_model"),
        ):
            _phase_model = user_settings.get(_setting_key)
            if not _phase_model:
                continue
            llm_block = config_override.setdefault("llm", {})
            if _phase in llm_block and llm_block[_phase].get("model"):
                continue
            phase_section: dict = {}
            await _inject_model_credentials(
                section=phase_section,
                model_id=_phase_model,
                user_id=user_id_str,
                resolved_keys=resolved_keys,
                gateway_override=_gw_scoped,
            )
            if "api_key" not in phase_section and "base_url" not in phase_section:
                logger.warning(
                    f"Dispatch: skipping {_phase} phase pin {_phase_model} — "
                    f"no credentials resolvable; configure the provider key "
                    f"in system_api_keys or clear the {_setting_key} preference."
                )
                continue
            phase_section["model"] = _phase_model
            llm_block[_phase] = phase_section
            logger.info(f"Dispatch: injected {_phase} phase pin: {_phase_model}")

        default_autonomy = user_settings.get("default_autonomy")
        if default_autonomy and "autonomy" not in config_override:
            config_override["autonomy"] = default_autonomy
            logger.info(f"Dispatch: injected user default_autonomy: {default_autonomy}")

        default_reasoning = user_settings.get("default_reasoning_level")
        if default_reasoning:
            llm_override = config_override.setdefault("llm", {})
            if "reasoning_level" not in llm_override:
                llm_override["reasoning_level"] = default_reasoning
                logger.info(
                    f"Dispatch: injected user default_reasoning_level: {default_reasoning}"
                )

        for _kind, _prefix, _user_key, _capability in (
            ("vision", "VISION", "default_vision_model", "vision"),
            ("whisper", "WHISPER", "default_whisper_model", "whisper"),
            ("tts", "TTS", "default_tts_model", "tts"),
            ("citation", "CITATION_LLM", "default_citation_model", "chat"),
        ):
            _model = user_settings.get(_user_key)
            if not _model:
                _model = await postgres_db.resolve_default_for_capability(_kind)
            if not _model:
                continue
            env_keys_block = config_override.setdefault("env_keys", {})
            if f"{_prefix}_MODEL" in env_keys_block:
                continue
            await _inject_env_key_credentials(
                env_keys=env_keys_block,
                prefix=_prefix,
                model_id=_model,
                user_id=user_id_str,
                resolved_keys=resolved_keys,
                capability=_capability,
            )
            # The citation_engine package reads CITATION_LLM_URL (not _BASE_URL)
            # and falls back to OPENAI_API_KEY for auth. Alias the URL key here
            # so the upstream package picks up the dispatched endpoint.
            if _kind == "citation" and "CITATION_LLM_BASE_URL" in env_keys_block:
                env_keys_block.setdefault(
                    "CITATION_LLM_URL", env_keys_block["CITATION_LLM_BASE_URL"]
                )
            logger.info(f"Dispatch: injected {_kind} model: {_model}")

        embedding_provider = user_settings.get("embedding_provider")
        embedding_model = user_settings.get("default_embedding_model")
        if not embedding_model:
            embedding_model = await postgres_db.resolve_default_for_capability(
                "embedding"
            )
        if embedding_provider or embedding_model:
            env_keys_block = config_override.setdefault("env_keys", {})
            if embedding_provider and "EMBEDDING_PROVIDER" not in env_keys_block:
                env_keys_block["EMBEDDING_PROVIDER"] = embedding_provider
            if embedding_model:
                # Resolve endpoint base_url + api_key even when EMBEDDING_MODEL
                # was already set — _inject_env_key_credentials uses setdefault,
                # so a pre-present MODEL must not suppress the _API_KEY (the bug
                # that left jobs with MODEL+BASE_URL but no key:
                # docs/issues/embedding_key_missing_silently_disables_memory_and_kb.md).
                await _inject_env_key_credentials(
                    env_keys=env_keys_block,
                    prefix="EMBEDDING",
                    model_id=embedding_model,
                    user_id=user_id_str,
                    resolved_keys=resolved_keys,
                    capability="embedding",
                )
            if (
                embedding_provider == "openrouter"
                and resolved_keys
                and "openrouter" in resolved_keys
            ):
                env_keys_block["OPENROUTER_API_KEY"] = resolved_keys["openrouter"]
            logger.info(
                f"Dispatch: injected embedding: "
                f"provider={embedding_provider}, model={embedding_model}"
            )

    # System-default fallback for the worker chat model. Runs after the
    # user-preference block (or whenever there's no user) so jobs that
    # arrived without an llm.model still pick up the admin-curated default
    # from the catalog instead of falling through to the agent's YAML
    # default — which has no base_url/api_key for self-hosted models and
    # silently routes to api.openai.com with "not-needed".
    llm_override_check = config_override.get("llm") or {}
    if "model" not in llm_override_check:
        system_chat_model = await postgres_db.resolve_default_for_capability("chat")
        if system_chat_model:
            llm_override = config_override.setdefault("llm", {})
            llm_override["model"] = system_chat_model
            await _inject_model_credentials(
                section=llm_override,
                model_id=system_chat_model,
                user_id=user_id_str,
                resolved_keys=resolved_keys,
                gateway_override=_gw_scoped,
            )
            logger.info(
                f"Dispatch: injected system default chat model: {system_chat_model} "
                f"(job {job_id})"
            )

    # System-default fallback for the embedding credential — same rationale as
    # the chat model above. Embedding (memory + KB) was otherwise resolved ONLY
    # inside the user-preference block, so a job whose user has no embedding
    # preference (or no user at all) silently fell back to provider 'local' with
    # no key, disabling memory + KB with no signal. Inject the admin-curated
    # system embedding here so every job gets it the same way it gets its chat
    # model. docs/issues/embedding_key_missing_silently_disables_memory_and_kb.md
    _emb_env = config_override.setdefault("env_keys", {})
    if "EMBEDDING_API_KEY" not in _emb_env:
        _emb_model = _emb_env.get(
            "EMBEDDING_MODEL"
        ) or await postgres_db.resolve_default_for_capability("embedding")
        if _emb_model:
            await _inject_env_key_credentials(
                env_keys=_emb_env,
                prefix="EMBEDDING",
                model_id=_emb_model,
                user_id=user_id_str,
                resolved_keys=resolved_keys,
                capability="embedding",
            )
            if _emb_env.get("EMBEDDING_API_KEY"):
                logger.info(
                    f"Dispatch: injected system default embedding: {_emb_model} "
                    f"(job {job_id})"
                )
            else:
                logger.error(
                    f"Dispatch: system embedding model {_emb_model!r} resolved no "
                    f"usable API key for job {job_id} — memory/KB will be "
                    f"unavailable. Check the embedding endpoint (Admin → Models)."
                )

    return config_override


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

    _log_token = bind_log_context(job_id=job_id, agent_id=agent_id)
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
        extracted_keys = {
            "upload_id",
            "config_upload_id",
            "instructions_upload_id",
            "instructions",
            "git_remote_url",
        }
        remaining_context = {
            k: v for k, v in job_context.items() if k not in extracted_keys
        }

        # Pass worktree_path to agent via context (for git worktree creation)
        if job.get("worktree_path"):
            remaining_context["worktree_path"] = job["worktree_path"]

        # Resolve project repositories if this is a project job
        repositories_payload = None
        if job.get("project_id"):
            try:
                repos = await postgres_db.get_project_repositories(
                    str(job["project_id"])
                )
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
                logger.warning(
                    f"Dispatch: failed to resolve project repos for job {job_id}: {e}"
                )

            # Derive git_remote_url from jobs repo if not already set
            if repositories_payload and not git_remote_url:
                jobs_repo = next(
                    (r for r in repositories_payload if r["role"] == "jobs"), None
                )
                if jobs_repo and jobs_repo.get("repo_url"):
                    git_remote_url = jobs_repo["repo_url"]

        # Resolve datasources for this job (job > project > global)
        resolved_ds = await postgres_db.resolve_datasources_for_job(
            job_id, project_id=str(job["project_id"]) if job.get("project_id") else None
        )
        _apply_cloud_storage_override(resolved_ds, job_context)
        datasources_payload = _build_datasources_payload(resolved_ds)

        # Apply datasource-driven tool override (inject/strip db tool categories)
        if resolved_ds:
            config_override = _build_datasource_tool_override(
                resolved_ds, config_override
            )

        # Inject VM workspace config if job has a ready VM
        vm_ctx = _get_vm_context(job)
        if vm_ctx.get("status") == "ready" and vm_ctx.get("ssh_host"):
            config_override = config_override or {}
            ws = config_override.setdefault("workspace", {})
            ws["backend"] = "vm"
            remote = ws.setdefault("remote", {})
            remote.setdefault("host", vm_ctx["ssh_host"])
            remote.setdefault("port", vm_ctx.get("ssh_port", 22))
            remote.setdefault("username", "agent-host")
            remote.setdefault("key_path", "/run/secrets/vm-ssh-key")
            remote.setdefault("workspace_path", "/home/agent-host/workspace")
            # VM has its own sudo gate — allow sudo through
            config_override.setdefault("shell", {})["sudo_action"] = "allow"
            logger.info(
                f"Dispatch: injected VM workspace config for job {job_id} "
                f"(host={vm_ctx['ssh_host']}:{vm_ctx.get('ssh_port', 22)})"
            )

        # Inject workspace container config if job has a ready container
        container_ctx = _get_container_context(job)
        container_host = container_ctx.get("pod_ip") or container_ctx.get("host")
        if container_ctx.get("status") == "ready" and container_host:
            config_override = config_override or {}
            ws = config_override.setdefault("workspace", {})
            ws["backend"] = "sandbox"
            remote = ws.setdefault("remote", {})
            remote.setdefault("host", container_host)
            remote.setdefault("port", container_ctx.get("port", 22))
            remote.setdefault("username", "agent-host")
            # SSH key path resolution:
            #   SSH_KEY_PATH env var (dev compose: .dev/ssh-keys/id_ed25519)
            #   Docker Compose mode default: /run/secrets/ssh/id_ed25519
            #   Kubernetes mode default: /run/secrets/vm-ssh-key
            ssh_key_override = os.environ.get("SSH_KEY_PATH", "").strip()
            if ssh_key_override:
                remote.setdefault("key_path", ssh_key_override)
            elif container_ctx.get("provisioner") == "docker":
                remote.setdefault("key_path", "/run/secrets/ssh/id_ed25519")
            else:
                remote.setdefault("key_path", "/run/secrets/vm-ssh-key")
            remote.setdefault("workspace_path", "/home/agent-host/workspace")
            # Sandbox uses sudo freeze mechanism (VM upgrade prompt)
            config_override.setdefault("shell", {}).setdefault("sudo_action", "freeze")
            logger.info(
                f"Dispatch: injected workspace container config for job {job_id} "
                f"(host={container_host}:{container_ctx.get('port', 22)}, "
                f"provisioner={container_ctx.get('provisioner', 'k8s')})"
            )

        # Inject lite workspace config (virtual/none — no SSH, no provisioning).
        # The user's config_override already names the backend; here we attach
        # the object-store mounts (virtual) with deployment-sourced credentials,
        # in-flight only. A repository datasource needs a real workspace to
        # clone into, so reject the combination up front (§4/§7).
        # Defense-in-depth: the submit-time guard already rejects an explicitly
        # selected repo, and create_job filters repos out of an *inherited*
        # lite selection — but this re-checks the fully resolved set, covering
        # resume / VM-resume and any future path that could attach a repo the
        # submit guard never saw.
        if _backend_from_override(config_override) in LITE_BACKENDS:
            repo_names = _repository_datasource_names(resolved_ds)
            if repo_names:
                msg = (
                    "workspace.backend is a lite tier (virtual/none) but a "
                    f"repository datasource is attached ({', '.join(repo_names)}). "
                    "Repository datasources need a full workspace — use "
                    "backend='sandbox' or 'vm', or detach the repository."
                )
                logger.error("Dispatch: job %s rejected — %s", job_id, msg)
                await postgres_db.update_job_status(
                    job_id=job_id, status="failed", error_message=msg
                )
                return False
            try:
                config_override = _inject_lite_workspace_config(
                    config_override, prefix=f"jobs/{job_id}/"
                )
            except LiteWorkspaceConfigError as exc:
                logger.error("Dispatch: job %s lite-config error: %s", job_id, exc)
                await postgres_db.update_job_status(
                    job_id=job_id, status="failed", error_message=str(exc)
                )
                return False
            logger.info(
                "Dispatch: job %s using lite workspace (backend=%s, no pod)",
                job_id,
                config_override["workspace"]["backend"],
            )

        # Override workspace_path with worktree_path for subjobs on shared backends
        worktree_path = job.get("worktree_path")
        if worktree_path and config_override:
            ws = config_override.get("workspace", {})
            remote = ws.get("remote", {})
            if remote:
                remote["workspace_path"] = worktree_path
                logger.info(
                    f"Dispatch: using worktree path {worktree_path} for job {job_id}"
                )

        # Orchestrator-resolved config (supersedes agent-side Decision 6): when
        # experts are enabled, resolve the full config here with the same loader
        # the agent uses, freeze the secret-free copy into jobs.resolved_config,
        # and deliver a credential-injected blob. The agent hydrates it and skips
        # local resolution. On ANY failure we fall back to config_name +
        # config_override below — the blob's absence is always safe.
        resolved_config: dict[str, Any] | None = None
        if _is_experts_db_enabled():
            try:
                expert_row = None
                if job.get("expert_id"):
                    expert_row = await postgres_db.get_expert_by_id(
                        str(job["expert_id"])
                    )
                _base_name = job.get("config_name") or "defaults"
                if _base_name == "default":
                    # JobCreate / JobStartRequest sentinel for "the default
                    # config"; the real base file is defaults.yaml (a literal
                    # resolve of "default" 404s). The agent boots "defaults" too.
                    _base_name = "defaults"
                # Default-model floor (model names only): the base config carries
                # a placeholder model; the effective default is the user's pinned
                # model else the system capability default. Resolution applies it
                # below the expert; inject_blob_credentials adds the transport.
                _base_defaults = await _resolve_default_models(job.get("user_id"))
                _cap: dict = {}
                _skills_payload = await _gather_in_scope_skills(
                    str(job["user_id"]) if job.get("user_id") else None,
                    [str(job["project_id"])] if job.get("project_id") else None,
                )
                _resolved = resolve_config(
                    base_config_name=_base_name,
                    base_defaults=_base_defaults,
                    expert_row=expert_row,
                    request_override=config_override,
                    expert_type="worker",
                    capture=_cap,
                    skills=_skills_payload,
                )
                # Bound skills are delivered deterministically (instructions channel);
                # strip them from the model-invoked catalog so they aren't double-offered.
                from src.core.skill_resolution import filter_bound_skills

                filter_bound_skills(_resolved)
                # Dispatch PEP (decision 9): the merged config must fit the runner's
                # grants. GrantDenied is caught BELOW the generic fallback so a denial
                # is never downgraded to the unchecked config_override (fail closed).
                if await _user_experts_enabled():
                    await _enforce_dispatch_grants(
                        _cap["merged_fragment"],
                        runner_user_id=str(job["user_id"])
                        if job.get("user_id")
                        else None,
                        project_ids=[str(job["project_id"])]
                        if job.get("project_id")
                        else [],
                    )
                resolved_config = await inject_blob_credentials(
                    _resolved,
                    lambda co: _inject_dispatch_credentials(job, co),
                )
                await postgres_db.store_resolved_config(
                    job_id, redact_config_override(resolved_config)
                )
                logger.info(
                    "Dispatch: resolved config for job %s (expert_id=%s)",
                    job_id,
                    job.get("expert_id"),
                )
            except GrantDenied as gd:
                logger.warning("Dispatch denied for job %s: %s", job_id, gd)
                await postgres_db.update_job_status(
                    job_id,
                    status="failed",
                    error_message=_grant_violations_detail(gd.violations),
                )
                return False
            except Exception:
                logger.exception(
                    "Dispatch: resolve_config failed for job %s; falling back "
                    "to config_name + config_override",
                    job_id,
                )
                resolved_config = None

        # Resolve API keys, model routing, and capability defaults.
        # Same helper drives both first-dispatch and resume so an orphaned
        # job re-dispatched to a fresh agent doesn't lose its credentials.
        # (Still injected into config_override for the no-blob fallback path.)
        config_override = await _inject_dispatch_credentials(job, config_override)
        # Log injected env-key NAMES (never values) so a missing credential —
        # e.g. EMBEDDING_API_KEY, which silently disables memory + KB — is
        # greppable at dispatch (embedding_key_missing_silently_disables_memory_and_kb.md).
        logger.info(
            "Dispatch: job %s injected env_key names=%s",
            job_id,
            sorted((config_override.get("env_keys") or {}).keys()),
        )

        # Fail fast on a pinned model with no resolvable transport rather than
        # letting the agent silently fall back to api.openai.com and 401/404 with
        # an opaque error (eec20eeb). Only the blob path is validated; the
        # no-blob fallback keeps its legacy behaviour.
        if resolved_config:
            _unrouted = unrouted_model_slots(resolved_config)
            if _unrouted:
                msg = (
                    "Pinned model(s) have no resolvable endpoint or provider after "
                    f"dispatch resolution: {', '.join(_unrouted)}. Set the model's "
                    "endpoint/provider key (Admin → Providers / Models) or pin a "
                    "different model."
                )
                logger.error(
                    "Dispatch: job %s has unroutable model slot(s) — %s",
                    job_id,
                    _unrouted,
                )
                await postgres_db.update_job_status(
                    job_id, status="failed", error_message=msg
                )
                return False

        # Build job start request. resolved_config and config_override are
        # mutually exclusive on the wire: a delivered blob is complete, so we
        # send config_override=None to keep the agent from flat-merging an
        # override on top of the resolved layers (the degradation we set out to
        # fix).
        job_start = JobStartRequest(
            job_id=job_id,
            description=job["description"],
            upload_id=upload_id,
            config_upload_id=config_upload_id,
            instructions_upload_id=instructions_upload_id,
            instructions=instructions,
            document_path=job.get("document_path"),
            config_name=job.get("config_name", "default"),
            config_override=None if resolved_config else config_override,
            resolved_config=resolved_config,
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
            logger.warning(
                f"Dispatch: agent {agent_id} rejected job {job_id}: {response.text}"
            )
            return False

        # Update job status and assign to agent
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

        logger.info(
            f"Dispatch: assigned job {job_id} (priority={job.get('priority', '?')}) to agent {agent_id}"
        )
        return True

    except Exception as e:
        logger.error(
            f"Dispatch: failed to assign job {job_id} to agent {agent_id}: {e}",
            exc_info=True,
        )
        return False
    finally:
        reset_log_context(_log_token)


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
        job_context = job.get("context") or {}
        if isinstance(job_context, str):
            job_context = json.loads(job_context)
        _apply_cloud_storage_override(resolved_ds, job_context)
        datasources_payload = _build_datasources_payload(resolved_ds)

        config_override = job.get("config_override")
        if isinstance(config_override, str):
            config_override = json.loads(config_override)

        # Resume PEP (decision 9, B3): resume replays the write-once frozen blob and
        # would otherwise skip grant enforcement — a grant revoked since dispatch must
        # still block the resume. Re-resolve the merged config and re-check the
        # runner's CURRENT grants. Fail closed: deny -> mark failed + refuse.
        if await _user_experts_enabled():
            try:
                _rbase = job.get("config_name") or "defaults"
                if _rbase == "default":
                    _rbase = "defaults"
                _rcap: dict = {}
                resolve_config(
                    base_config_name=_rbase,
                    base_defaults=await _resolve_default_models(job.get("user_id")),
                    expert_row=(
                        await postgres_db.get_expert_by_id(str(job["expert_id"]))
                        if job.get("expert_id")
                        else None
                    ),
                    request_override=config_override,
                    expert_type="worker",
                    capture=_rcap,
                )
                await _enforce_dispatch_grants(
                    _rcap["merged_fragment"],
                    runner_user_id=str(job["user_id"]) if job.get("user_id") else None,
                    project_ids=[str(job["project_id"])]
                    if job.get("project_id")
                    else [],
                )
            except GrantDenied as gd:
                logger.warning("Resume denied for job %s: %s", job.get("id"), gd)
                await postgres_db.update_job_status(
                    str(job["id"]),
                    status="failed",
                    error_message=_grant_violations_detail(gd.violations),
                )
                return False

        if resolved_ds:
            config_override = _build_datasource_tool_override(
                resolved_ds, config_override
            )

        # Re-inject API keys and model routing. The first dispatch already
        # did this, but the result was passed inline to the agent and never
        # written back to jobs.config_override — so on resume the persisted
        # row only carries the bare creation-time override. Without this
        # call, an orphaned/paused job picked up by a fresh agent would have
        # no llm.api_key and the loader would silently fall back to
        # OPENAI_API_KEY=not-needed and 401 against the user's router.
        config_override = await _inject_dispatch_credentials(job, config_override)
        logger.info(
            "Dispatch (resume): job %s injected env_key names=%s",
            job_id,
            sorted((config_override.get("env_keys") or {}).keys()),
        )

        # Inject VM workspace config if job has a ready VM
        vm_ctx = _get_vm_context(job)
        if vm_ctx.get("status") == "ready" and vm_ctx.get("ssh_host"):
            config_override = config_override or {}
            ws = config_override.setdefault("workspace", {})
            ws["backend"] = "vm"
            remote = ws.setdefault("remote", {})
            remote.setdefault("host", vm_ctx["ssh_host"])
            remote.setdefault("port", vm_ctx.get("ssh_port", 22))
            remote.setdefault("username", "agent-host")
            remote.setdefault("key_path", "/run/secrets/vm-ssh-key")
            remote.setdefault("workspace_path", "/home/agent-host/workspace")
            # VM has its own sudo gate — allow sudo through
            config_override.setdefault("shell", {})["sudo_action"] = "allow"
            logger.info(
                f"Resume dispatch: injected VM workspace config for job {job_id} "
                f"(host={vm_ctx['ssh_host']}:{vm_ctx.get('ssh_port', 22)})"
            )

        # Re-inject lite workspace config on resume. Mounts + credentials are
        # injected in-flight and never persisted to jobs.config_override, so the
        # paused row only carries the bare backend — without this a resumed
        # `virtual` job would reach the agent with no mounts and fail to build
        # its backend. (Same rationale as the credential re-injection above.)
        if _backend_from_override(config_override) in LITE_BACKENDS:
            repo_names = _repository_datasource_names(resolved_ds)
            if repo_names:
                msg = (
                    "workspace.backend is a lite tier (virtual/none) but a "
                    f"repository datasource is attached ({', '.join(repo_names)}). "
                    "Repository datasources need a full workspace — use "
                    "backend='sandbox' or 'vm', or detach the repository."
                )
                logger.error("Resume dispatch: job %s rejected — %s", job_id, msg)
                await postgres_db.update_job_status(
                    job_id=job_id, status="failed", error_message=msg
                )
                return False
            try:
                config_override = _inject_lite_workspace_config(
                    config_override, prefix=f"jobs/{job_id}/"
                )
            except LiteWorkspaceConfigError as exc:
                logger.error(
                    "Resume dispatch: job %s lite-config error: %s", job_id, exc
                )
                await postgres_db.update_job_status(
                    job_id=job_id, status="failed", error_message=str(exc)
                )
                return False

        # Extract queued feedback (stored by resume endpoint when no agent was available)
        job_context = job.get("context") or {}
        if isinstance(job_context, str):
            job_context = json.loads(job_context)
        queued_feedback = job_context.pop("queued_feedback", None)
        delegation_results = job_context.pop("delegation_results", None)

        resume_payload = {
            "job_id": job_id,
            "config_name": job.get("config_name", "default"),
            "config_override": config_override,
            "datasources": datasources_payload,
            "project_id": str(job["project_id"]) if job.get("project_id") else None,
            "previous_status": job.get("status"),
        }
        if queued_feedback:
            resume_payload["feedback"] = queued_feedback
        if delegation_results:
            resume_payload["delegation_results"] = delegation_results
        # Clean up consumed context keys so they're not re-injected on retry
        if queued_feedback or delegation_results:
            async with postgres_db.acquire() as conn:
                await conn.execute(
                    "UPDATE jobs SET context = $1::jsonb WHERE id = $2::uuid",
                    json.dumps(job_context),
                    job_id,
                )

        agent_url = f"http://{agent['pod_ip']}:{agent['pod_port']}/job/resume"
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                agent_url,
                json={k: v for k, v in resume_payload.items() if v is not None},
            )

        if response.status_code not in (200, 202):
            logger.warning(
                f"Dispatch: agent {agent_id} rejected resume for job {job_id}: {response.text}"
            )
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

        logger.info(
            f"Dispatch: resumed job {job_id} (priority={job.get('priority', '?')}) on agent {agent_id}"
        )
        return True

    except Exception as e:
        logger.error(
            f"Dispatch: failed to resume job {job_id} on agent {agent_id}: {e}"
        )
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
            logger.info(
                f"Preempt: pause request sent for job {job_id} on agent {agent_id}"
            )
            # DB update handled by agent + orchestrator fallback
            await postgres_db.pause_job(job_id)
        else:
            logger.warning(
                f"Preempt: agent returned {response.status_code} for pause of job {job_id}"
            )

    except Exception as e:
        logger.warning(f"Preempt: failed to pause job {job_id}: {e}")
    finally:
        _pause_pending_job_ids.discard(job_id)


def _expected_agent_shas() -> set[str]:
    """Extract short commit SHAs from configured agent image tags.

    Reads AGENT_IMAGE and PERSISTENT_AGENT_IMAGE env vars and extracts
    the SHA suffix from tags formatted as ``...:sha-<hash>``.
    """
    shas: set[str] = set()
    for var in ("AGENT_IMAGE", "PERSISTENT_AGENT_IMAGE"):
        tag = os.environ.get(var, "")
        if ":sha-" in tag:
            shas.add(tag.rsplit(":sha-", 1)[-1])
    return shas


def _agent_sha_is_current(metadata: dict | None) -> bool:
    """Check if an agent's build SHA matches any expected image SHA."""
    expected = _expected_agent_shas()
    if not expected:
        # No SHA-tagged images configured (local dev) — skip check
        return True
    if not metadata:
        return False
    build_sha = metadata.get("build_sha")
    if not build_sha:
        return False
    return build_sha in expected


async def _find_idle_persistent_agent() -> Optional[dict]:
    """Find an idle persistent or dual-mode agent in the pool.

    Returns the agent row dict or None if no idle agents are available.
    An agent is idle when: agent_mode in ('persistent', 'dual'),
    status in ('ready'), and no thread currently attached.

    Agents whose build SHA doesn't match the current expected images
    are skipped here; the lifecycle reconciler is responsible for
    actually draining them.
    """
    try:
        rows = await postgres_db.fetch(
            """
            SELECT id, pod_ip, pod_port, hostname, status, config_name,
                   metadata
            FROM agents
            WHERE agent_mode IN ('persistent', 'dual')
              AND status IN ('ready')
              AND thread_id IS NULL
            ORDER BY last_heartbeat DESC
            LIMIT 10
            """,
        )
        for row in rows:
            agent = dict(row)
            meta = agent.get("metadata") or {}
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except (json.JSONDecodeError, ValueError):
                    meta = {}
            if _agent_sha_is_current(meta):
                return agent
            logger.debug(
                "Skipping stale agent %s (build_sha=%s, expected=%s)",
                agent["id"],
                meta.get("build_sha", ""),
                _expected_agent_shas(),
            )
        return None
    except Exception:
        logger.exception("Failed to find idle persistent agent")
        return None


async def _send_session_attach(
    agent: dict,
    thread_id: str,
    config_override: Optional[dict] = None,
    project_ids: Optional[list] = None,
    datasources: Optional[list] = None,
    config_name: Optional[str] = None,
) -> bool:
    """Send a session attach request to an idle persistent agent.

    ``config_name`` is the thread's config — pool pods boot as workers
    ('defaults'), so the agent must re-resolve the session base config
    from this name instead of its boot config
    (docs/issues/session_config_name_plumbing.md, hole B).

    Returns True if the agent accepted the session.
    """
    # Lite tiers (virtual/none) carry no SSH endpoint. For `virtual` we attach
    # the object-store mounts here — deployment-sourced credentials, in-flight
    # only (never persisted to the thread row), keyed under threads/<id>/.
    # A misconfigured `virtual` deployment refuses the attach with a clear log.
    try:
        config_override = _inject_lite_workspace_config(
            config_override, prefix=f"threads/{thread_id}/"
        )
    except LiteWorkspaceConfigError as exc:
        logger.error("Session attach: thread %s lite-config error: %s", thread_id, exc)
        return False

    # Orchestrator-resolved config for the warm-pool agent: this is the expert
    # delivery channel the warm path lacked (the 3-minute-stall bug). Re-resolve
    # on every attach (no freeze). None when experts are off / resolve fails →
    # the agent uses the config_name + config_override fallback below.
    resolved_config: dict[str, Any] | None = None
    _sess_status: dict[str, Any] = {}
    try:
        _thread = await postgres_db.get_thread(thread_id)
        if _thread:
            _meta = _thread.get("metadata") or {}
            if isinstance(_meta, str):
                try:
                    _meta = json.loads(_meta)
                except (json.JSONDecodeError, TypeError):
                    _meta = {}
            resolved_config = await _resolve_session_config(
                _thread, _meta, config_override=config_override, status=_sess_status
            )
    except GrantDenied as gd:
        logger.warning("Session attach denied for thread %s: %s", thread_id, gd)
        return False
    except Exception:
        logger.exception(
            "Session attach: resolve failed for thread %s; using fallback", thread_id
        )
    # Fail closed: a resolution ERROR (experts on, resolve threw) must not deliver
    # the unvetted config_override — the grant check never ran. The 'disabled' state
    # (experts off) intentionally falls through to the legacy fallback below.
    if _sess_status.get("state") == "error":
        logger.warning(
            "Session attach: resolve errored for thread %s; refusing (fail closed)",
            thread_id,
        )
        return False

    agent_url = f"http://{agent['pod_ip']}:{agent['pod_port']}/session/attach"
    payload = {
        "thread_id": thread_id,
        "config_override": None if resolved_config else config_override,
        "resolved_config": resolved_config,
        "project_ids": project_ids,
        "datasources": datasources,
        "config_name": config_name,
    }
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(agent_url, json=payload)
        if response.status_code == 200:
            logger.info(
                "Assigned thread %s to persistent agent %s (%s:%s)",
                thread_id,
                agent["id"],
                agent["pod_ip"],
                agent["pod_port"],
            )
            # Update both sides of the agent↔thread binding
            try:
                async with postgres_db.acquire() as conn:
                    await conn.execute(
                        "UPDATE agents SET thread_id = $2 WHERE id = $1",
                        str(agent["id"]),
                        thread_id,
                    )
                    await conn.execute(
                        "UPDATE threads SET agent_id = $2 WHERE id = $1",
                        thread_id,
                        str(agent["id"]),
                    )
            except Exception:
                logger.warning("Failed to update agent/thread binding in DB")
            return True
        else:
            logger.warning(
                "Persistent agent %s rejected session attach: %s %s",
                agent["id"],
                response.status_code,
                response.text[:200],
            )
            return False
    except Exception:
        logger.exception("Failed to send session attach to agent %s", agent["id"])
        return False


class LiteWorkspaceConfigError(Exception):
    """A ``virtual`` tier was requested but this deployment has no object store.

    Raised by :func:`_inject_lite_workspace_config`; dispatch fails the job and
    session-attach refuses, both with the actionable message carried here.
    """


def _backend_from_override(config_override: Any) -> Optional[str]:
    """Extract ``workspace.backend`` from a (dict | JSON-string | None) override.

    Mirrors the parsing the dispatch path already does for ``config_override``
    so every caller reads the backend the same way.
    """
    co = config_override
    if isinstance(co, str):
        try:
            co = json.loads(co)
        except (json.JSONDecodeError, TypeError):
            return None
    if not isinstance(co, dict):
        return None
    ws = co.get("workspace")
    if not isinstance(ws, dict):
        return None
    return ws.get("backend")


def _is_lite_config_override(config_override: Any) -> bool:
    """True if ``config_override`` selects a lite (``virtual``/``none``) backend.

    Lite tiers have no git/workspace, so the orchestrator's git-graft lifecycle
    subjobs (scholar/critic/curator) can neither hand their ``output/`` back to
    the parent nor read the parent's deliverables — that handoff is entirely
    Gitea-branch-based (see ``_graft_subjob_output``). They are therefore skipped
    for lite jobs; the main agent researches/curates inline. A lite-compatible
    handoff (object-store copy instead of git graft) is deferred to v2
    (no_workspace_agent_mode.md §8).
    """
    return _backend_from_override(config_override) in LITE_BACKENDS


def _validated_session_workspace_override(
    config_override: Any,
) -> Optional[dict[str, Any]]:
    """Extract + validate the ``workspace`` sub-dict from a New Session request's
    ``config_override`` (the cockpit 'Backend' selector + Advanced→Workspace
    fragment). Returns the workspace dict for ``create_thread`` to merge, or
    ``None`` when no workspace fragment was sent.

    ``create_thread`` provisions only a lite tier (no pod) or a sandbox container
    — it has no VM-provisioner (NATS/KubeVirt) wiring — so ``vm`` is rejected
    here: VM is reached by starting on a lite tier and upgrading
    (``_enforce_workspace_upgrade_grants`` + the workspace-upgrade path). Unknown
    backends are rejected too. A workspace fragment with no ``backend`` (e.g.
    word-limit tweaks only) passes through untouched.

    Raises ``HTTPException(400)`` on a disallowed/invalid backend.
    """
    ws = config_override.get("workspace") if isinstance(config_override, dict) else None
    if not isinstance(ws, dict) or not ws:
        return None
    backend = ws.get("backend")
    if backend == "vm":
        raise HTTPException(
            status_code=400,
            detail=(
                "VM workspaces can't be selected at session creation; start the "
                "session on the Virtual tier and upgrade it to VM."
            ),
        )
    if backend is not None and backend not in ("sandbox", "virtual", "none"):
        raise HTTPException(
            status_code=400, detail=f"Invalid workspace backend '{backend}'"
        )
    return ws


def _virtual_workspace_rclone_spec() -> Optional[dict[str, Any]]:
    """The deployment's object-store spec for the ``virtual`` tier, or None.

    Built from env wired by Helm (``virtualWorkspace.*``):

    - ``VIRTUAL_WORKSPACE_RCLONE_TYPE`` — rclone backend (``s3`` or, for a
      non-durable dev store, ``memory``). Empty ⇒ tier disabled (None).
    - ``VIRTUAL_WORKSPACE_RCLONE_ROOT`` — bucket or ``bucket/subpath``.
    - For ``s3``: discrete fields rather than a JSON blob, mirroring the
      snapshot S3 wiring. ``VIRTUAL_WORKSPACE_S3_ACCESS_KEY_ID`` /
      ``_SECRET_ACCESS_KEY`` are the (Secret-held) credentials; ``_ENDPOINT`` /
      ``_REGION`` / ``_PROVIDER`` are non-secret config. Each ``config`` key
      becomes ``RCLONE_CONFIG_*`` env in the agent, so secrets never reach argv.

    Returns the ``{type, config, root}`` shape the agent's
    ``object_store_from_spec`` consumes as ``rclone_spec`` (§4/§5). ``config``
    holds rclone backend key/values; for ``memory`` it is empty.
    """
    rtype = os.environ.get("VIRTUAL_WORKSPACE_RCLONE_TYPE", "").strip()
    if not rtype:
        return None
    root = os.environ.get("VIRTUAL_WORKSPACE_RCLONE_ROOT", "").strip()
    config: dict[str, Any] = {}
    if rtype == "s3":
        config = {
            "provider": os.environ.get("VIRTUAL_WORKSPACE_S3_PROVIDER", "").strip()
            or "Minio",
            "access_key_id": os.environ.get("VIRTUAL_WORKSPACE_S3_ACCESS_KEY_ID", ""),
            "secret_access_key": os.environ.get(
                "VIRTUAL_WORKSPACE_S3_SECRET_ACCESS_KEY", ""
            ),
            "endpoint": os.environ.get("VIRTUAL_WORKSPACE_S3_ENDPOINT", "").strip(),
            "region": os.environ.get("VIRTUAL_WORKSPACE_S3_REGION", "").strip()
            or "us-east-1",
            # The agent's scoped key can't create buckets and the bucket is
            # pre-provisioned, so tell rclone not to probe/create it. String
            # (not bool) — config values reach rclone as RCLONE_CONFIG_* env.
            "no_check_bucket": "true",
        }
    # "memory" (dev) carries no credentials — empty config.
    return {"type": rtype, "config": config, "root": root}


def _inject_lite_workspace_config(
    config_override: Optional[dict[str, Any]], *, prefix: str
) -> Optional[dict[str, Any]]:
    """Enrich ``config_override.workspace`` for the lite tiers (``virtual``/``none``).

    No-op for any other backend. For ``virtual`` it attaches the object-store
    ``mounts`` (credentials sourced from deployment env and injected *in-flight*
    only — never persisted to the job/thread row, matching how dispatch keeps
    API keys inline). For ``none`` it strips any stray mounts. Both force
    ``git_versioning`` off (§8 — lite tiers have no git).

    ``prefix`` is the object-store key prefix for this owner
    (``jobs/<id>/`` for jobs, ``threads/<id>/`` for sessions).

    Raises:
        LiteWorkspaceConfigError: ``virtual`` requested but no object store is
            configured for this deployment.
    """
    backend = _backend_from_override(config_override)
    if backend not in LITE_BACKENDS:
        return config_override

    config_override = config_override or {}
    ws = config_override.setdefault("workspace", {})
    ws["backend"] = backend
    ws["git_versioning"] = False

    if backend == "virtual":
        spec = _virtual_workspace_rclone_spec()
        if spec is None:
            raise LiteWorkspaceConfigError(
                "workspace.backend='virtual' needs an object store, but this "
                "deployment has none configured. Set virtualWorkspace.rclone.type "
                "(+ .root) and, for s3, virtualWorkspace.s3.* plus the "
                "VIRTUAL_WORKSPACE_S3_ACCESS_KEY_ID / _SECRET_ACCESS_KEY secrets "
                "— or use backend='none' for a no-file-tools agent, or "
                "'sandbox'/'vm' for a full workspace."
            )
        ws["mounts"] = [
            {
                "name": "workspace",
                "rclone_spec": spec,
                "prefix": prefix,
                "access": "read_write",
            }
        ]
    else:  # "none" — no file tools, so no object-store mounts
        ws.pop("mounts", None)

    return config_override


def _repository_datasource_names(datasources: Any) -> list[str]:
    """Names of any ``repository``-type datasources in a resolved list.

    Repository datasources require a shell-capable workspace to clone into;
    the lite tiers have none (§4/§7), so their presence is the tier boundary.
    Returns a (possibly empty) list of human-readable names for the error.
    """
    names: list[str] = []
    for ds in datasources or []:
        if not isinstance(ds, dict):
            continue
        if (ds.get("type") or "").lower() == "repository":
            names.append(str(ds.get("name") or ds.get("id") or "?"))
    return names


async def _inherit_parent_datasource_ids(
    *, thread_id: str | None, parent_job_id: str | None
) -> list[str]:
    """Datasource IDs a parented subjob inherits when it passes no explicit
    selection (delegation keeps working without force-attaching anything).

    Prefers the parent thread's persisted selection
    (``threads.metadata.datasource_ids``), then the parent job's
    ``job_datasources``. Returns [] when neither yields a selection.
    """
    if thread_id:
        try:
            thread = await postgres_db.get_thread(thread_id)
        except Exception:
            thread = None
        if thread:
            meta = thread.get("metadata") or {}
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except (json.JSONDecodeError, TypeError):
                    meta = {}
            ids = meta.get("datasource_ids") or []
            if ids:
                return [str(x) for x in ids]
    if parent_job_id:
        try:
            return await postgres_db.list_job_datasource_ids(parent_job_id)
        except Exception:
            return []
    return []


async def _propagate_datasources_to_subjob(
    parent_job_id: str, child_job_id: str
) -> None:
    """Copy the parent job's datasource selection to a spawned subjob.

    Explicit-only resolution means a subjob otherwise resolves no datasources;
    scholar/critic/curator run on the parent's workspace and need the same
    DB/repo sources. These subjobs are skipped for lite backends, so no
    repository filtering is needed here.
    """
    try:
        for ds_id in await postgres_db.list_job_datasource_ids(parent_job_id):
            await postgres_db.link_datasource_to_job(child_job_id, ds_id)
    except Exception as e:
        logger.warning(
            "Failed to propagate datasources %s -> %s: %s",
            parent_job_id,
            child_job_id,
            e,
        )


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
    # Config override specifies VM workspace
    co = job.get("config_override") or {}
    if isinstance(co, str):
        try:
            co = json.loads(co)
        except (json.JSONDecodeError, TypeError):
            co = {}
    backend = co.get("workspace", {}).get("backend")
    return backend in ("vm", "remote")  # "remote" is legacy for VM


def _get_vm_context(job: dict) -> dict:
    """Extract the vm sub-dict from job context."""
    ctx = job.get("context") or {}
    if isinstance(ctx, str):
        try:
            ctx = json.loads(ctx)
        except (json.JSONDecodeError, TypeError):
            ctx = {}
    return ctx.get("vm", {})


def _job_needs_sandbox(job: dict) -> bool:
    """Check if a job needs a sandbox workspace container.

    Returns True if:
    - config_override.workspace.backend == "sandbox" (or legacy "container"), OR
    - backend is not explicitly set to "vm" AND a workspace
      provisioner is available (k8s ContainerProvisioner OR DockerProvisioner).

    Returns False if the job already has a ready VM or container inherited
    from a parent job (worktree sharing — no new container needed).
    """
    # If job inherits a ready workspace backend from parent, skip provisioning
    ctx = job.get("context") or {}
    if isinstance(ctx, str):
        try:
            ctx = json.loads(ctx)
        except (json.JSONDecodeError, TypeError):
            ctx = {}
    if ctx.get("vm", {}).get("status") == "ready":
        return False
    if ctx.get("workspace_container", {}).get("status") == "ready":
        return False

    co = job.get("config_override") or {}
    if isinstance(co, str):
        try:
            co = json.loads(co)
        except (json.JSONDecodeError, TypeError):
            co = {}
    backend = co.get("workspace", {}).get("backend")
    if backend in ("sandbox", "container"):  # "container" is legacy
        return True
    if backend in ("vm", "remote"):  # "remote" is legacy for VM
        return False
    if backend in LITE_BACKENDS:
        # virtual/none run with no workspace pod at all (no_workspace_agent_mode.md
        # §4). Without this the next line would provision a sandbox whenever a
        # provisioner is available — defeating the whole tier.
        return False
    # No explicit backend — default to sandbox if any provisioner is available
    return container_provisioner.is_available or docker_provisioner.is_available


def _get_container_context(job: dict) -> dict:
    """Extract the workspace_container sub-dict from job context."""
    ctx = job.get("context") or {}
    if isinstance(ctx, str):
        try:
            ctx = json.loads(ctx)
        except (json.JSONDecodeError, TypeError):
            ctx = {}
    return ctx.get("workspace_container", {})


# =============================================================================
# Capability grants (User-Defined Experts, Slice 2) — PEPs + helpers
#
# One pure PDP (src/core/capability_grants.evaluate) is enforced at four points:
# save-time (the 3 expert endpoints), job dispatch, job resume, and session
# attach. Deny-by-default for security keys; existing approved users were
# grandfathered by migration 0030 (shell_tools + delegation). See
# docs/done/global_expert_management.md (decisions 8, 9, 19, 21-23).
# =============================================================================


class GrantDenied(Exception):
    """A merged config exceeds the runner's grants (dispatch PEP). Must NOT be
    swallowed by a resolve fallback (fail closed)."""

    def __init__(self, violations: list[str]):
        self.violations = violations
        super().__init__("; ".join(violations))


async def _user_experts_enabled() -> bool:
    """Runtime kill-switch (decision 8). Absent row = enabled (fail-open for fresh
    installs). When disabled, DB-expert creation + grant enforcement are off."""
    try:
        row = await postgres_db.get_system_setting("user_experts")
    except Exception:
        logger.exception("user_experts read failed; fail-open")
        return True
    value = (row or {}).get("value") or {}
    return not (isinstance(value, dict) and value.get("enabled") is False)


def _grant_violations_detail(violations: list[str]) -> str:
    return "config exceeds your capability grants: " + "; ".join(violations)


async def _grant_project_ids(user: dict) -> list[str]:
    """Project scope ids for grant resolution. user_visible_project_ids returns
    'all' for admins (who bypass anyway) — treat as no project constraint."""
    vis = await user_visible_project_ids(user, postgres_db)  # security/access.py
    return [] if vis == "all" else [str(p) for p in vis]


async def _scan_raw_request_fragment(request: Request) -> None:
    """Slice-2 hardening (decision 10): scan the RAW request bytes for duplicate
    or non-ASCII keys (parser-differential + unicode-confusable defenses) that the
    parsed body has already silently collapsed. 422 on offence. Best-effort — if
    the body can't be re-read the parsed-dict hard-deny scan still ran."""
    from src.core.expert_resolution import scan_fragment_text

    try:
        raw = (await request.body()).decode("utf-8")
    except Exception:
        return
    if not raw.strip():
        return
    offending = scan_fragment_text(raw)
    if offending:
        raise HTTPException(
            status_code=422,
            detail="config rejected (malformed, duplicate/non-ASCII, or credential "
            "keys): " + "; ".join(offending),
        )


async def _enforce_save_grants(config: dict[str, Any], *, user: dict[str, Any]) -> None:
    """Save-time PEP (decision 9): the author's grants must cover the raw fragment.
    422 naming offending keys. Admins bypass."""
    if user.get("is_admin"):
        return
    from src.core.capability_grants import evaluate
    from services.grants_service import resolve_grants_for

    grants = await resolve_grants_for(
        postgres_db, user_id=str(user["id"]), project_ids=await _grant_project_ids(user)
    )
    violations = evaluate(config, grants)
    if violations:
        raise HTTPException(
            status_code=422, detail=_grant_violations_detail(violations)
        )


async def _enforce_expert_save(
    request: Request, config: dict[str, Any], *, user: dict[str, Any]
) -> None:
    """Combined save-time gate: kill-switch (403) + raw dup/non-ASCII key scan
    (422) + capability-grant enforcement (422). Admins bypass grants, not the
    kill-switch."""
    if not await _user_experts_enabled():
        raise HTTPException(
            status_code=403,
            detail="User-defined experts are disabled by the administrator",
        )
    await _scan_raw_request_fragment(request)
    await _enforce_save_grants(config, user=user)


async def _enforce_dispatch_grants(
    merged: dict, *, runner_user_id: str | None, project_ids: list[str]
) -> None:
    """Authoritative dispatch PEP (decision 9): the merged config must fit the
    RUNNER's grants. Raises GrantDenied on violation. Admin runner bypasses.
    NOTE (v1 stance): runner = job owner (job['user_id']); for delegation children
    inheriting a privileged owner this bypasses (spec defers transitive checks)."""
    user = await postgres_db.get_user(runner_user_id) if runner_user_id else None
    if user and user.get("is_admin"):
        return
    from src.core.capability_grants import evaluate
    from services.grants_service import resolve_grants_for

    grants = await resolve_grants_for(
        postgres_db, user_id=runner_user_id, project_ids=project_ids
    )
    violations = evaluate(merged, grants)
    if violations:
        raise GrantDenied(violations)


async def _check_vm_permission(
    user: dict | None,
    *,
    job_needs_vm: bool,
) -> None:
    """Enforce admin VM-workspace controls.

    Raises HTTPException(403) when either:
      - The global kill-switch `system_settings['vm_workspaces']` is set
        to `{"enabled": false}` (blocks everyone, including admins).
      - The user is a non-admin without `can_use_vm=True`.

    No-op when ``job_needs_vm`` is False. Absent/malformed setting row is
    treated as enabled (fail-open for fresh installs).
    """
    if not job_needs_vm:
        return
    row: dict | None = None
    try:
        row = await postgres_db.get_system_setting("vm_workspaces")
    except Exception:
        # DB read failure is non-fatal for the gate — defer to per-user check.
        logger.exception("Failed to read vm_workspaces kill-switch; fail-open")
    value = (row or {}).get("value") or {}
    if isinstance(value, dict) and value.get("enabled") is False:
        raise HTTPException(
            status_code=403,
            detail="VM workspaces are globally disabled by the administrator",
        )
    if user and user.get("is_admin"):
        return
    if not user or not await postgres_db.user_can_use_vm(user):
        raise HTTPException(
            status_code=403,
            detail="User is not permitted to use VM workspaces",
        )


async def _enforce_workspace_upgrade_grants_for_config(
    *,
    owner_id: Any,
    config_override: dict | None,
    target_tier: str,
) -> None:
    """Sec-1 — shared upgrade-authorization gate core (server-side, fail-closed).

    Parameterized by the raw ``(owner_id, config_override)`` so BOTH the session
    path (``thread.metadata.config_override``) and the worker path (the
    ``jobs.config_override`` column) reuse one PEP — see the thin
    ``_enforce_workspace_upgrade_grants`` (session) /
    ``_enforce_job_workspace_upgrade_grants`` (worker, §4.3 W2) wrappers.

    Re-runs the dispatch PDP (``capability_grants.evaluate``) on the POST-UPGRADE
    config — the stored ``config_override`` with ``workspace.backend`` flipped to
    ``target_tier`` — exactly as ``_enforce_dispatch_grants`` does at dispatch,
    just re-run at upgrade time:

    - ``target_tier='vm'`` trips the ``vm_workspace`` grant requirement, and
      additionally keeps the operator gate (the global ``vm_workspaces``
      kill-switch + per-user ``can_use_vm`` via ``_check_vm_permission``).
    - ``target_tier='sandbox'`` is NOT gated by the backend (the PDP gates only
      ``vm`` and explicitly-declared tool flags), so it passes by default —
      matching "sandbox is the ungated default tier" — unless the config already
      declares a gated tool (e.g. ``tools.shell``) the owner lacks, in which case
      dispatch would have rejected it too.

    Raises ``HTTPException(403)`` on violation. No new grant key, no
    sandbox-specific rule — identical to dispatch-time enforcement.
    """
    # owner_id comes back from asyncpg as a UUID object; get_user (and
    # _enforce_dispatch_grants below) expect a string — coerce once, matching the
    # str(user["id"]) convention used elsewhere.
    owner_id = str(owner_id) if owner_id is not None else None
    owner = await postgres_db.get_user(owner_id) if owner_id else None

    # vm keeps its operator gate (global kill-switch + can_use_vm), on top of the
    # vm_workspace grant the PDP enforces below.
    if target_tier == "vm":
        await _check_vm_permission(owner, job_needs_vm=True)

    if not isinstance(config_override, dict):
        config_override = {}
    # Post-upgrade config = the frozen override with the backend flipped. A
    # shallow merge of the workspace sub-dict suffices — the PDP only reads
    # workspace.backend plus declared tool/autonomy flags.
    post_upgrade = {
        **config_override,
        "workspace": {
            **(config_override.get("workspace") or {}),
            "backend": target_tier,
        },
    }
    project_ids = await _grant_project_ids(owner) if owner else []
    try:
        await _enforce_dispatch_grants(
            post_upgrade, runner_user_id=owner_id, project_ids=project_ids
        )
    except GrantDenied as exc:
        raise HTTPException(
            status_code=403, detail=_grant_violations_detail(exc.violations)
        ) from exc


async def _enforce_workspace_upgrade_grants(
    thread: dict,
    *,
    target_tier: str,
) -> None:
    """Session wrapper over the shared Sec-1 gate — extracts the owner +
    ``config_override`` from a thread row (``metadata.config_override``) and
    delegates to ``_enforce_workspace_upgrade_grants_for_config``."""
    metadata = thread.get("metadata") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (json.JSONDecodeError, TypeError):
            metadata = {}
    await _enforce_workspace_upgrade_grants_for_config(
        owner_id=thread.get("user_id"),
        config_override=metadata.get("config_override") or {},
        target_tier=target_tier,
    )


async def _enforce_job_workspace_upgrade_grants(
    job: dict,
    *,
    target_tier: str,
) -> None:
    """Worker wrapper over the shared Sec-1 gate (§4.3 W2). Jobs carry
    ``config_override`` as a top-level JSONB column (not under ``metadata``) and
    ``user_id`` as the owner — extract those and delegate to the shared core."""
    config_override = job.get("config_override") or {}
    if isinstance(config_override, str):
        try:
            config_override = json.loads(config_override)
        except (json.JSONDecodeError, TypeError):
            config_override = {}
    await _enforce_workspace_upgrade_grants_for_config(
        owner_id=job.get("user_id"),
        config_override=config_override,
        target_tier=target_tier,
    )


async def _archive_and_cleanup_workspace(
    entity_id: str,
    entity_type: str = "jobs",
) -> list[str]:
    """Snapshot workspace to S3, then delete container/VM.

    Centralized cleanup for all workspace teardown paths (job completion,
    cancellation, cascade cleanup, thread end). Each provisioner's release
    method handles snapshot-before-delete internally.

    Args:
        entity_id: Job or thread UUID.
        entity_type: "jobs" or "threads".

    Returns:
        List of action descriptions for logging.
    """
    actions: list[str] = []

    if entity_type == "threads":
        thread = await postgres_db.get_thread(entity_id)
        if not thread:
            return actions
        metadata = thread.get("metadata") or {}
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except (json.JSONDecodeError, TypeError):
                metadata = {}
        ws_ctx = metadata.get("workspace_container") or {}
        vm_ctx = metadata.get("vm") or {}

        # Workspace container cleanup (snapshot + delete)
        if ws_ctx.get("status") not in ("deleted", "deleting", "released", None):
            if ws_ctx.get("provisioner") == "docker":
                await docker_provisioner.release_thread_workspace(entity_id)
                actions.append("docker thread workspace released")
            elif container_provisioner.is_available:
                await container_provisioner.release_workspace(
                    WorkspaceOwner.session(entity_id)
                )
                actions.append("k8s thread workspace released")

        # VM cleanup (snapshot + delete)
        if vm_ctx.get("status") in ("provisioning", "created", "ready"):
            if vm_provisioner.is_available:
                await vm_provisioner.release_thread_vm(entity_id)
                actions.append("thread vm released")

    else:
        job = await postgres_db.get_job(entity_id)
        if not job:
            return actions
        ws_ctx = _get_container_context(job)
        vm_ctx = _get_vm_context(job)

        # VM cleanup (snapshot + delete)
        if vm_ctx and vm_ctx.get("status") not in ("deleted", "deleting"):
            if vm_provisioner.is_available:
                await vm_provisioner.release_vm(entity_id)
                actions.append("vm released")

        # Workspace container cleanup (snapshot + delete)
        if ws_ctx and ws_ctx.get("status") not in (
            "deleted",
            "deleting",
            "released",
            None,
        ):
            if ws_ctx.get("provisioner") == "docker":
                await docker_provisioner.release_workspace(entity_id)
                actions.append("docker workspace released")
            else:
                await container_provisioner.release_workspace(
                    WorkspaceOwner.job(entity_id)
                )
                actions.append("k8s workspace released")

    return actions


async def _detach_agent_session(thread_id: str, timeout: float = 150.0) -> bool:
    """Ask the thread's live agent to terminate its session, and wait.

    Gives the agent the chance to run its full terminate path — final
    memory capture (memory_bugs.md B11) and the workspace git push —
    BEFORE ``_release_thread_resources`` tears down the workspace and
    pod. Without this, the user-facing DELETE deleted the pod outright
    and the session's final extraction died with it (the agent kept
    heartbeating through the grace period, then got SIGKILLed).

    Best-effort by design: returns False (and never raises) when the
    thread has no bound agent, the agent isn't serving a session, or the
    call fails — teardown then proceeds exactly as before. The read
    timeout is sized to the persistent auxiliary extraction budget
    (auxiliary.timeout=120s) plus git-push headroom; unreachable pods
    fail in seconds via the connect timeout, and an already-terminated
    agent answers "already_idle" instantly.
    """
    try:
        thread = await postgres_db.get_thread(thread_id)
        agent_id = (thread or {}).get("agent_id")
        if not agent_id:
            return False
        row = await postgres_db.fetchrow(
            "SELECT pod_ip, pod_port, status FROM agents WHERE id = $1",
            str(agent_id),
        )
        agent = dict(row) if row else None
        # 'session' is the heartbeat status of an agent serving a live
        # session — anything else (ready/offline/busy) either has nothing
        # to capture or is unreachable, so skip fast and let the normal
        # teardown run.
        if not agent or not agent.get("pod_ip") or agent.get("status") != "session":
            return False
        url = (
            f"http://{agent['pod_ip']}:{int(agent.get('pod_port') or 8001)}"
            "/session/detach"
        )
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=3.0)
        ) as client:
            resp = await client.post(url)
        if resp.status_code == 200:
            logger.info(
                "Thread %s: agent detach completed before teardown",
                thread_id,
            )
            return True
        logger.warning(
            "Thread %s: pre-teardown detach returned %s — proceeding",
            thread_id,
            resp.status_code,
        )
        return False
    except Exception as e:
        logger.warning(
            "Thread %s: pre-teardown detach failed (%s: %s) — proceeding",
            thread_id,
            type(e).__name__,
            e,
        )
        return False


async def _release_thread_resources(thread_id: str) -> None:
    """Release a thread's workspace container/VM and agent pod.

    Centralized so the user-facing DELETE, the agent-facing status flip,
    and the orphan reaper share one teardown sequence. Each step swallows
    its own exception — a failure in snapshotting must not block the
    agent-pod delete and vice versa, otherwise resources leak.
    """
    # Let a live session agent terminate cleanly (final memory capture +
    # git push) while its workspace still exists. No-op in seconds for
    # agent-initiated endings (already terminated → "already_idle") and
    # for the orphan reaper (agent not in 'session' status).
    await _detach_agent_session(thread_id)

    try:
        await _archive_and_cleanup_workspace(thread_id, entity_type="threads")
    except Exception:
        logger.exception("Workspace cleanup failed for thread %s", thread_id)

    try:
        if agent_provisioner.is_available:
            await agent_provisioner.delete_agent_pod_by_thread(thread_id)
        elif persistent_provisioner.is_available:
            await persistent_provisioner.delete_agent_pod(thread_id)
            await persistent_provisioner.delete_agent_pvc(thread_id)
    except Exception:
        logger.exception("Agent pod cleanup failed for thread %s", thread_id)


# Threads with a suspend currently in flight. Two triggers can race on the
# same thread within a second (e.g. the disconnect watchdog and the agent's
# own status→ended PUT); without this guard the loser found the workspace
# already suspended, misread it as a failure, and deleted the agent pod a
# second time (docs/issues/session_silent_failure_audit.md #13).
_threads_suspending: set[str] = set()


async def _suspend_thread_resources(thread_id: str) -> None:
    """Suspend a thread's workspace to S3 and release the agent pod.

    Used for agent-initiated `ended` transitions where the user has not
    asked to destroy data — idle timeout, drain, watchdog, WS disconnect.
    Preserves the workspace via S3 snapshot so /resume can restore it
    later (resume already routes through restore_thread_workspace when
    it sees workspace_container.status == 'suspended').

    Falls back gracefully if the suspension service is disabled or the
    snapshot fails: the workspace stays alive (reconciler will reap it
    eventually) but we still delete the agent pod so the slot frees.
    """
    if thread_id in _threads_suspending:
        logger.info(
            "Thread %s: suspend already in flight — skipping duplicate", thread_id
        )
        return
    _threads_suspending.add(thread_id)
    try:
        await _suspend_thread_resources_inner(thread_id)
    finally:
        _threads_suspending.discard(thread_id)


async def _suspend_thread_resources_inner(thread_id: str) -> None:
    suspended = False
    try:
        if workspace_suspension_service.is_enabled:
            suspended = await workspace_suspension_service.suspend_thread_workspace(
                thread_id
            )
    except Exception:
        logger.exception("Workspace suspend failed for thread %s", thread_id)

    if suspended:
        # suspend_thread_workspace already deletes the agent pod.
        return

    logger.warning(
        "Workspace suspend unavailable or failed for thread %s — keeping "
        "workspace alive (reconciler will reap) but deleting the agent pod",
        thread_id,
    )
    try:
        if agent_provisioner.is_available:
            await agent_provisioner.delete_agent_pod_by_thread(thread_id)
        elif persistent_provisioner.is_available:
            await persistent_provisioner.delete_agent_pod(thread_id)
            await persistent_provisioner.delete_agent_pvc(thread_id)
    except Exception:
        logger.exception("Agent pod cleanup failed for thread %s", thread_id)


def _provider_of_model(model: str) -> str | None:
    """Sync prefix-based provider heuristic for legacy dispatch paths.

    Catalog rows carry ``provider_ref`` explicitly; this helper exists for
    the small set of code paths that don't have a row in hand and only
    need the factory name (aux-model key injection, vision-model key
    lookup, dispatcher provider-key inference). Returns None on any miss
    so callers fall through to their config_name / env-var heuristics.

    The legacy ``resolve_builtin`` lookup that this replaced was the entry
    point for the YAML fallback path — removed in chunk 6 of the
    models_yaml_removal work.
    """
    if not model:
        return None
    name = model.lower()
    for prefix in ("openrouter/", "groq/"):
        if name.startswith(prefix):
            return prefix.rstrip("/")
    if name.startswith("codex/"):
        return "codex"
    if name.startswith("openai/"):
        return "openai"
    if name.startswith(("claude-",)):
        return "anthropic"
    if name.startswith("gemini-") or name.startswith("gemma-"):
        return "google"
    if name.startswith(("gpt-", "o1", "o3", "o4", "text-embedding-")):
        return "openai"
    return None


async def _inject_model_credentials(
    *,
    section: dict,
    model_id: str,
    user_id: str | None,
    resolved_keys: dict[str, str] | None,
    capability: str = "chat",
    gateway_override: tuple[str, str] | None = None,
) -> None:
    """Populate a config-override section with the right base_url + api_key
    for a given model ID.

    For endpoint-backed models (``origin`` in {``custom``, ``system``}):
    looks up the endpoint row and inlines its ``base_url`` + ``api_key``.
    Custom endpoints are user-scoped; system endpoints are helm-seeded or
    managed via Admin → Providers. Both live in llm_endpoints.

    For built-ins: injects the named provider's key from ``resolved_keys``
    (the user > project > env resolution chain). No base_url injection —
    the agent's own registry handles env-driven base URLs for local models.

    Never overwrites fields that are already set; this helper is always
    additive so caller-supplied overrides win.
    """
    if "base_url" in section and "api_key" in section:
        return

    meta = None
    try:
        meta = await _resolve_model(model_id, user_id=user_id, capability=capability)
    except UnknownModelError:
        meta = None

    # Per-model context window (chat capability only — auxiliary/vision sections
    # carry their own windows and aren't derived this way). Set before the
    # endpoint/provider branches so it also reaches endpoint-backed (self-hosted)
    # models. setdefault keeps a caller-pinned value; truthy guard skips None/0.
    if capability == "chat" and meta is not None and meta.context_window:
        section.setdefault("model_max_context_tokens", meta.context_window)

    # Inject the agent-side factory name so the section always routes to the
    # correct LLM factory — e.g. an OpenRouter row → _create_openrouter_llm
    # (openrouter.ai), not the OpenAI default at api.openai.com. meta.provider
    # already holds the factory name ("openai" for endpoint-backed rows).
    # This must happen for endpoint rows too: a session hot-swap deep-merges
    # the enriched override into the existing config, so leaving `provider`
    # unset keeps the PREVIOUS model's factory (e.g. minimax via openrouter →
    # gpt-5.5 endpoint row kept routing through _create_openrouter_llm).
    if meta is not None and meta.provider:
        section.setdefault("provider", meta.provider)

    if (
        meta is not None
        and meta.origin in ("custom", "system", "catalog")
        and meta.endpoint_id
    ):
        # Prefer the caller's pre-resolved target (the dispatch's scoped key, 2b);
        # otherwise resolve the shared fleet key here (callers without user/project
        # context, e.g. some hot-swap paths). The Codex proxy is Responses-API only
        # and the gateway would normalize it to Chat Completions and drop reasoning,
        # so codex models bypass the gateway and hit their endpoint directly.
        _gw = (
            None
            if meta.provider == "codex"
            else (gateway_override or _gateway_routing_target())
        )
        if _gw is not None:
            # Route endpoint-kind phase/aux models through the gateway too, so
            # measurement covers the full chat/auxiliary surface (Slice 1) and the
            # scoped key's limits apply to every section (Slice 2b).
            section.setdefault("base_url", _gw[0])
            section.setdefault("api_key", _gw[1])
            return
        endpoint_row = await postgres_db.get_user_llm_endpoint(meta.endpoint_id)
        if endpoint_row:
            if endpoint_row.get("base_url"):
                section.setdefault("base_url", endpoint_row["base_url"])
            if endpoint_row.get("api_key"):
                section.setdefault("api_key", endpoint_row["api_key"])
        return

    provider = meta.api_key_ref if meta is not None else _provider_of_model(model_id)
    if meta is None and provider:
        section.setdefault("provider", provider)
    if (
        provider
        and resolved_keys
        and provider in resolved_keys
        and "api_key" not in section
    ):
        section["api_key"] = resolved_keys[provider]


async def _inject_env_key_credentials(
    *,
    env_keys: dict,
    prefix: str,
    model_id: str,
    user_id: str | None,
    resolved_keys: dict[str, str] | None,
    capability: str = "chat",
) -> None:
    """Populate ``env_keys`` with ``{PREFIX}_MODEL/_BASE_URL/_API_KEY``.

    Sibling of ``_inject_model_credentials`` for capabilities that travel as
    flat env vars (vision, whisper, tts, ...) rather than structured config
    sections. Endpoint-backed models (origin in {'custom','system','catalog'}
    with an endpoint_id) contribute the inline base_url+api_key from the
    endpoint row; built-ins and system-anchored catalog rows resolve the
    api_key via ``resolved_keys[provider]``. All writes are setdefault so
    caller / earlier overrides win.
    """
    env_keys.setdefault(f"{prefix}_MODEL", model_id)

    meta = None
    try:
        meta = await _resolve_model(model_id, user_id=user_id, capability=capability)
    except UnknownModelError:
        meta = None

    if (
        meta is not None
        and meta.origin in ("custom", "system", "catalog")
        and meta.endpoint_id
    ):
        endpoint_row = await postgres_db.get_user_llm_endpoint(meta.endpoint_id)
        if endpoint_row:
            base_url = endpoint_row.get("base_url")
            api_key = endpoint_row.get("api_key")
            if base_url and not api_key:
                # The endpoint is configured but its stored key didn't decrypt
                # (get_user_llm_endpoint -> _decrypt_stored already logged the
                # cause) or is empty. Surface it loudly and do NOT emit a
                # half-credential (base_url without api_key) that silently
                # degrades the agent to a keyless 'local' provider — the failure
                # mode in docs/issues/embedding_key_missing_silently_disables_memory_and_kb.md.
                logger.error(
                    "Dispatch: %s endpoint %s resolved a base_url but no usable "
                    "api_key (decrypt failed or empty) — not injecting "
                    "%s_BASE_URL/_API_KEY; re-add the key in Admin → Models.",
                    prefix,
                    meta.endpoint_id,
                    prefix,
                )
                return
            if base_url:
                env_keys.setdefault(f"{prefix}_BASE_URL", base_url)
            if api_key:
                env_keys.setdefault(f"{prefix}_API_KEY", api_key)
        return

    provider = meta.api_key_ref if meta is not None else _provider_of_model(model_id)
    if provider and resolved_keys and provider in resolved_keys:
        env_keys.setdefault(f"{prefix}_API_KEY", resolved_keys[provider])


async def _inject_thread_dispatch_credentials(
    config_override: dict[str, Any],
    *,
    user_id: str | None,
    project_id: str | None = None,
    user_settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve + inject LLM / auxiliary / embedding credentials into a thread's
    ``config_override`` IN PLACE (creating sections as needed). Returns the dict.

    The persistent-session sibling of the worker-job ``_inject_dispatch_credentials``.
    Secrets travel **in-flight only** — at thread create, and re-injected at session
    attach/resume (the agent workspace endpoint + the resume dispatcher) — and are
    stripped via ``redact_config_override`` before persistence, so
    ``threads.metadata.config_override`` never stores plaintext keys.

    Idempotent + re-injection-safe: ``_inject_model_credentials`` only early-returns
    when both ``base_url`` and ``api_key`` are already present, and
    ``_inject_env_key_credentials`` is fully ``setdefault``-based. So running this on
    an already-enriched dict is a no-op, and running it on a *stripped* copy (model /
    base_url / EMBEDDING_MODEL survive, the keys were removed) repopulates exactly the
    removed secrets.
    """
    user_settings = user_settings or {}

    # Drop None-valued keys in the model sections before injecting. A prior
    # hot-swap persists explicit ``provider/base_url/api_key = None`` sentinels
    # (they make the live agent's deep_merge CLEAR the previous model's
    # transport); in a stored copy those Nones would block the setdefault-based
    # injection below. Treat them as absent so the transport is repopulated.
    for _sect_name in ("llm", "auxiliary"):
        _sect = config_override.get(_sect_name)
        if isinstance(_sect, dict):
            for _k in [_k for _k, _v in _sect.items() if _v is None]:
                del _sect[_k]

    resolved_keys = await postgres_db.resolve_api_keys_for_job(
        user_id=user_id,
        project_id=project_id,
    )

    # Route this session's LLM traffic through its per-(user,project) scoped key
    # (2b) so per-project/per-user limits apply to sessions too; falls back to the
    # fleet key when user/project is absent. Sessions are long-lived, so the
    # scoped key's hash-gated ensure runs once and is cheap on re-injection.
    _gw_scoped = await _gateway_routing_target_scoped(user_id, project_id)

    # Chat model. Fall back to the system default chat pin so the agent never
    # boots on its YAML default (which has no transport → api.openai.com 401).
    llm_section = config_override.get("llm") or {}
    if not llm_section.get("model"):
        system_chat_model = await postgres_db.resolve_default_for_capability("chat")
        if system_chat_model:
            llm_section["model"] = system_chat_model
            logger.info(
                "Thread dispatch: injected system default chat model: %s",
                system_chat_model,
            )
    if llm_section.get("model"):
        await _inject_model_credentials(
            section=llm_section,
            model_id=llm_section["model"],
            user_id=user_id,
            resolved_keys=resolved_keys,
            gateway_override=_gw_scoped,
        )
        config_override["llm"] = llm_section

    # Auxiliary slot (title generation, memory extraction, knowledge curation).
    aux_section = config_override.get("auxiliary") or {}
    if not aux_section.get("model"):
        aux_model = user_settings.get("default_auxiliary_model")
        if not aux_model:
            aux_model = await postgres_db.resolve_default_for_capability("auxiliary")
        if aux_model:
            aux_section["model"] = aux_model
            logger.info("Thread dispatch: injected auxiliary model: %s", aux_model)
    if aux_section.get("model"):
        await _inject_model_credentials(
            section=aux_section,
            model_id=aux_section["model"],
            user_id=user_id,
            resolved_keys=resolved_keys,
            capability="auxiliary",
            gateway_override=_gw_scoped,
        )
        config_override["auxiliary"] = aux_section

    # Embedding capability travels as flat env vars. Source provider/model from
    # the (possibly stripped) persisted block first so re-injection on resume is
    # stable, then user settings, then the system default. Unconditionally call
    # the env-key injector when a model is known: it is setdefault-based, so it
    # re-adds the stripped EMBEDDING_API_KEY without clobbering surviving
    # EMBEDDING_MODEL / EMBEDDING_BASE_URL.
    env_keys_block = config_override.setdefault("env_keys", {})
    embedding_provider = env_keys_block.get("EMBEDDING_PROVIDER") or user_settings.get(
        "embedding_provider"
    )
    embedding_model = env_keys_block.get("EMBEDDING_MODEL") or user_settings.get(
        "default_embedding_model"
    )
    if not embedding_model:
        embedding_model = await postgres_db.resolve_default_for_capability("embedding")
    if embedding_provider:
        env_keys_block.setdefault("EMBEDDING_PROVIDER", embedding_provider)
    if embedding_model:
        await _inject_env_key_credentials(
            env_keys=env_keys_block,
            prefix="EMBEDDING",
            model_id=embedding_model,
            user_id=user_id,
            resolved_keys=resolved_keys,
            capability="embedding",
        )
    if (
        embedding_provider == "openrouter"
        and resolved_keys
        and "openrouter" in resolved_keys
    ):
        env_keys_block.setdefault("OPENROUTER_API_KEY", resolved_keys["openrouter"])
    if not env_keys_block:
        config_override.pop("env_keys", None)

    return config_override


def _dispatch_llm_provider_fallback(
    job: dict, config_override: dict | None
) -> str | None:
    """Legacy dispatcher provider detection, used only when the model ID
    can't be resolved through the registry.

    Mirrors the pre-registry behavior: explicit ``llm.provider`` wins,
    then the known built-in model catalog, then a config-name heuristic
    (only ``anthropic`` today), finally ``openai``.
    """
    if config_override:
        llm = config_override.get("llm", {})
        if llm.get("provider"):
            return llm["provider"].lower()
        model = llm.get("model")
        if model:
            prov = _provider_of_model(model)
            if prov is not None:
                return prov

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

            # Pre-filter: auto-provision VMs/containers for jobs that need one
            dispatchable_jobs = []
            for job in pending_jobs:
                job_id = str(job["id"])
                # Slice 3 quota gate: a project that crossed its daily quota is
                # frozen — don't dispatch/resume or provision for its jobs until
                # the quota poll clears it (at the daily reset, or when usage
                # drops). The poll loop separately freezes already-running jobs.
                if is_project_over_quota(job.get("project_id")):
                    logger.debug(
                        "Dispatcher: skipping job %s — project %s over quota",
                        job_id,
                        job.get("project_id"),
                    )
                    continue
                if _job_needs_vm(job):
                    # Admin-gated permission check (kill-switch + per-user grant).
                    # Re-verified here in case a grant was revoked or the
                    # kill-switch flipped after the job was submitted. Already
                    # running VMs aren't torn down by this check — the gate
                    # only blocks jobs that haven't been dispatched yet.
                    creator = None
                    creator_id = job.get("user_id")
                    if creator_id:
                        try:
                            creator = await postgres_db.get_user(str(creator_id))
                        except Exception:
                            creator = None
                    try:
                        await _check_vm_permission(creator, job_needs_vm=True)
                    except HTTPException as permission_error:
                        logger.error(
                            "Dispatcher: job %s denied VM workspace: %s",
                            job_id,
                            permission_error.detail,
                        )
                        await postgres_db.update_job_status(
                            job_id,
                            status="failed",
                            error_message=str(permission_error.detail),
                        )
                        continue
                    vm_ctx = _get_vm_context(job)
                    if not vm_ctx.get("status"):
                        # VM needed but not provisioned yet
                        if not vm_provisioner.is_available:
                            # VM explicitly requested but no provisioner — fail
                            logger.error(
                                "Dispatcher: job %s requires VM workspace but VM "
                                "provisioner is not available (no NATS or KubeVirt). "
                                "Failing job.",
                                job_id,
                            )
                            await postgres_db.update_job_status(
                                job_id,
                                status="failed",
                                error_message=(
                                    "VM workspace requested but VM provisioner is not "
                                    "available. This deployment has no NATS or KubeVirt "
                                    "configured. Use workspace.backend='container' or "
                                    "remove the explicit backend override."
                                ),
                            )
                            continue
                        config_override = job.get("config_override") or {}
                        if isinstance(config_override, str):
                            config_override = json.loads(config_override)
                        vm_cfg = config_override.get("workspace", {}).get("vm", {})
                        ok = await vm_provisioner.create_vm(
                            job_id=job_id,
                            agent_config=job.get("config_name", "defaults"),
                            vm_image=vm_cfg.get("image"),
                            cpu_cores=vm_cfg.get("cpu_cores", 8),
                            memory=vm_cfg.get("memory", "16Gi"),
                            description=job.get("description", ""),
                        )
                        if ok:
                            logger.info(
                                "Dispatcher: auto-provisioned VM for job %s",
                                job_id,
                            )
                        else:
                            logger.warning(
                                "Dispatcher: VM provisioning failed for job %s",
                                job_id,
                            )
                        continue  # Skip this job — wait for VM to register
                    elif vm_ctx.get("status") not in ("ready",):
                        # VM is provisioning/creating — skip, wait
                        continue
                    # else: VM is ready, proceed with dispatch
                    logger.info("Dispatcher: job %s using VM workspace", job_id)
                elif _job_needs_sandbox(job):
                    container_ctx = _get_container_context(job)
                    container_status = container_ctx.get("status")
                    # K8s in-cluster takes priority; a local kubeconfig must not
                    # shadow Docker Compose when running outside the cluster.
                    use_k8s = container_provisioner.is_available and (
                        container_provisioner.in_cluster
                        or not docker_provisioner.is_available
                    )
                    # States that mean "no live workspace yet" → (re)create.
                    needs_create = container_status in (None, "", "deleted", "none")
                    if needs_create and not use_k8s:
                        # Docker Compose pool / no-provisioner CREATE path (unchanged).
                        if docker_provisioner.is_available:
                            logger.info(
                                "Dispatcher: job %s assigning workspace from "
                                "Docker Compose pool",
                                job_id,
                            )
                            result = await docker_provisioner.assign_workspace(job_id)
                            if not result:
                                logger.warning(
                                    "Dispatcher: no free workspace for job %s "
                                    "— all containers occupied, will retry",
                                    job_id,
                                )
                        else:
                            logger.error(
                                "Dispatcher: job %s needs workspace but no "
                                "provisioner available. Failing job.",
                                job_id,
                            )
                            await postgres_db.update_job_status(
                                job_id,
                                status="failed",
                                error_message=(
                                    "No workspace provisioner available. "
                                    "Neither Kubernetes API nor WORKSPACE_HOSTS "
                                    "configured."
                                ),
                            )
                        continue  # Skip — wait for container to become ready
                    # K8s create (when status absent) + all lifecycle states route
                    # through the shared, owner-agnostic state machine.
                    config_override = job.get("config_override") or {}
                    if isinstance(config_override, str):
                        config_override = json.loads(config_override)
                    ws_cfg = config_override.get("workspace", {}).get("container", {})
                    res = await ensure_workspace(
                        WorkspaceOwner.job(job_id),
                        provisioner=container_provisioner,
                        suspension=workspace_suspension_service,
                        current_status=container_status,
                        ws_config={
                            k: ws_cfg[k]
                            for k in (
                                "cpu",
                                "memory",
                                "cpu_limit",
                                "memory_limit",
                                "image",
                            )
                            if k in ws_cfg
                        },
                    )
                    if res.outcome is EnsureOutcome.FAILED:
                        if container_status == "failed":
                            error = container_ctx.get("error", "unknown error")
                            msg = f"Workspace container failed: {error}"
                        else:
                            msg = (
                                "Workspace container could not be created. Check "
                                "orchestrator logs for details (image pull failures, "
                                "insufficient resources, RBAC issues)."
                            )
                        logger.error(
                            "Dispatcher: workspace ensure failed for job %s: %s. "
                            "Failing job.",
                            job_id,
                            msg,
                        )
                        await postgres_db.update_job_status(
                            job_id, status="failed", error_message=msg
                        )
                        continue
                    if res.outcome is EnsureOutcome.PENDING:
                        if container_status not in (
                            None,
                            "",
                            "deleted",
                            "none",
                            "created",
                            "creating",
                            "restoring",
                            "suspending",
                            "pending",
                        ):
                            logger.warning(
                                "Dispatcher: job %s has unexpected workspace "
                                "container status %r — waiting",
                                job_id,
                                container_status,
                            )
                        continue  # in progress — wait for next cycle
                    # READY → proceed with dispatch
                    logger.info("Dispatcher: job %s using workspace container", job_id)
                else:
                    # No VM or container provisioning needed — check if a workspace
                    # was already assigned (e.g. Docker provisioner assigned it on a
                    # previous cycle and the job is now ready for dispatch).
                    existing_ctx = _get_container_context(job)
                    if existing_ctx.get("status") == "ready":
                        logger.info(
                            "Dispatcher: job %s using pre-assigned workspace (%s)",
                            job_id,
                            existing_ctx.get("host", "unknown"),
                        )
                    else:
                        logger.debug(
                            "Dispatcher: job %s — no workspace provisioner needed",
                            job_id,
                        )
                dispatchable_jobs.append(job)

            if not dispatchable_jobs:
                return

            # Get available agents (ready, cooldown passed), skip stale images
            all_agents = await postgres_db.get_available_agents(limit=50)
            available_agents = []
            for ag in all_agents:
                meta = ag.get("metadata") or {}
                if isinstance(meta, str):
                    try:
                        meta = json.loads(meta)
                    except (json.JSONDecodeError, ValueError):
                        meta = {}
                if _agent_sha_is_current(meta):
                    available_agents.append(ag)
                else:
                    # Stale-SHA agents are skipped here; the lifecycle
                    # reconciler is responsible for draining them.
                    logger.debug(
                        "Skipping stale worker agent %s (build_sha=%s)",
                        ag["id"],
                        meta.get("build_sha", ""),
                    )

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

            # Phase 1.5: Provision agent pods for unmatched jobs (K8s only)
            remaining = [
                j for j in dispatchable_jobs if str(j["id"]) not in matched_job_ids
            ]
            if remaining and agent_provisioner.is_available:
                for job in remaining:
                    if (
                        await agent_provisioner.active_count()
                        >= agent_provisioner.max_agents
                    ):
                        break
                    pod_name = await agent_provisioner.provision_agent(purpose="job")
                    if pod_name:
                        logger.info(
                            "Provisioned agent %s for pending job %s",
                            pod_name,
                            str(job["id"]),
                        )
                    else:
                        break  # At capacity or error
                    # Don't assign yet — pod needs to register first.
                    # Agent heartbeats "ready" → _trigger_dispatch() → next
                    # cycle matches it.

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


class CodexCallbackRequest(BaseModel):
    """Request body for manually completing a Codex OAuth callback."""

    url: str | None = Field(
        None, description="Full callback URL from browser address bar"
    )
    code: str | None = Field(None, description="OAuth authorization code")
    state: str | None = Field(None, description="OAuth state parameter")


class DatasourceCreate(BaseModel):
    """Request body for creating a datasource."""

    name: str = Field(..., description="User-provided label")
    type: str = Field(
        ...,
        description="Datasource type: generic, repository, postgresql, neo4j, mongodb, webdav, kubeconfig, ssh_key, generic_file",
    )
    connection_url: str | None = Field(
        None, description="Connection string (nullable for generic)"
    )
    description: str | None = Field(None, description="What this datasource contains")
    credentials: dict[str, Any] | None = Field(
        None,
        description="Auth details (env_vars for generic, auth_method+token/ssh_key for repository, type-specific for managed)",
    )
    job_id: str | None = Field(None, description="Job UUID (null for global)")
    cli_hint: str | None = Field(
        None, description="Suggested CLI command (e.g. 'psql $DATABASE_URL')"
    )
    default_branch: str | None = Field(
        None, description="Branch to clone (repository type)"
    )
    is_global: bool = Field(
        False, description="Whether this datasource is visible to all users"
    )


class DatasourceUpdate(BaseModel):
    """Request body for updating a datasource."""

    name: str | None = Field(None, description="New label")
    description: str | None = Field(None, description="New description")
    connection_url: str | None = Field(None, description="New connection string")
    credentials: dict[str, Any] | None = Field(None, description="New auth details")
    cli_hint: str | None = Field(None, description="New CLI hint")
    default_branch: str | None = Field(None, description="New default branch")


class SSHKeyGenerateRequest(BaseModel):
    """Request body for generating an SSH keypair for a repository datasource."""

    comment: str | None = Field(
        None,
        description="Optional comment to embed in the public key (e.g. datasource name)",
        max_length=200,
    )


class SSHKeyGenerateResponse(BaseModel):
    """Response containing a freshly generated ed25519 SSH keypair."""

    private_key: str = Field(..., description="OpenSSH PEM private key (no passphrase)")
    public_key: str = Field(
        ..., description="Single-line OpenSSH public key for the deploy-keys field"
    )


class ProjectDatasourceSettings(BaseModel):
    """Project-level settings when linking a datasource."""

    read_only: bool | None = Field(
        None,
        description="Managed connectors: true = read-only tools, false/null = CLI mode",
    )
    description: str | None = Field(None, description="Project-specific usage context")


class AgentRegistration(BaseModel):
    """Request body for agent registration."""

    config_name: str = Field(..., description="Agent configuration name")
    pod_ip: str = Field(..., description="Agent IP address for receiving commands")
    hostname: str | None = Field(None, description="Pod/host name")
    pod_port: int = Field(8001, description="Agent API port")
    pid: int | None = Field(None, description="Process ID")
    agent_mode: str = Field(
        "worker", description="Agent mode: 'worker' or 'persistent'"
    )
    thread_id: str | None = Field(None, description="Thread UUID for persistent agents")
    build_sha: str | None = Field(
        None, description="Build commit SHA baked into the agent image"
    )
    pod_uid: str | None = Field(
        None,
        description=(
            "K8s-assigned metadata.uid of the agent pod, self-reported via "
            "the Kubernetes downward API. Used by the session router to "
            "stamp ownerReferences on per-session Service/Ingress resources."
        ),
    )


class AgentRegistrationResponse(BaseModel):
    """Response from agent registration."""

    agent_id: str
    heartbeat_interval_seconds: int


class AgentHeartbeat(BaseModel):
    """Request body for agent heartbeat."""

    status: str = Field(
        ...,
        description="Agent status",
        pattern="^(booting|available|ready|working|session|draining|completed|failed)$",
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

    description: str = Field(
        ..., description="Job description - what the agent should accomplish"
    )
    upload_id: str | None = Field(
        None, description="Upload ID for document files (from /api/uploads)"
    )
    config_upload_id: str | None = Field(
        None, description="Upload ID for config YAML override"
    )
    instructions_upload_id: str | None = Field(
        None, description="Upload ID for instructions markdown"
    )
    document_path: str | None = Field(
        None, description="Path to a document (deprecated, use upload_id)"
    )
    document_dir: str | None = Field(
        None, description="Directory containing documents (deprecated)"
    )
    config_name: str = Field("default", description="Agent configuration name")
    expert_id: str | None = Field(
        None,
        description=(
            "DB-backed expert UUID. Preferred over config_name for expert "
            "selection — the orchestrator resolves it into the job's config. "
            "config_name stays the base profile."
        ),
    )
    config_override: dict[str, Any] | None = Field(
        None, description="Per-job configuration overrides"
    )
    context: dict[str, Any] | None = Field(
        None, description="Optional context dictionary"
    )
    instructions: str | None = Field(
        None, description="Additional inline instructions for the agent"
    )
    kickoff_message: str | None = Field(
        None, description="Opening message to the agent (task brief)"
    )
    datasource_ids: list[str] | None = Field(
        None, description="Global datasource IDs to clone as job-scoped"
    )
    user_id: str | None = Field(None, description="User UUID who created this job")
    project_id: str | None = Field(
        None, description="Project UUID to associate this job with"
    )
    thread_id: str | None = Field(
        None,
        description=(
            "Persistent-session thread UUID. When provided and user_id "
            "is unset, the owning user (and project) are inherited from "
            "the thread row so dispatch can apply user preferences."
        ),
    )
    parent_job_id: str | None = Field(
        None, description="Parent job UUID for verification/follow-up jobs"
    )
    priority: int = Field(
        5, ge=0, le=10, description="Job priority (0=low, 5=normal, 10=high)"
    )
    creation_order: int | None = Field(
        None, description="0-based index for delegation subagent merge ordering"
    )
    worktree_path: str | None = Field(
        None, description="Git worktree path for delegation subagents"
    )
    delegation_context: str | None = Field(
        None, description="Shared context string from parent delegation"
    )


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
    resolved_config: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Orchestrator-resolved config blob (serialize_resolved_config shape). "
            "Delivered instead of config_override when EXPERTS_DB_ENABLED; the "
            "agent hydrates it. The orchestrator owns resolution and the freeze."
        ),
    )
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
    delegation_context: str | None = Field(
        default=None,
        description="Shared context from parent delegation",
    )


class JobCompleteRequest(BaseModel):
    """Result payload sent by the agent after a job finishes processing."""

    should_stop: bool = Field(False, description="Whether the graph stopped")
    goal_achieved: bool = Field(False, description="Whether the goal was achieved")
    error: dict[str, Any] | None = Field(None, description="Error dict if job failed")
    freeze_data: dict[str, Any] | None = Field(
        None, description="Freeze data from the graph state"
    )


class VMCreateRequest(BaseModel):
    """Request body for creating a VM for a job."""

    job_id: str
    agent_config: str = "defaults"
    vm_image: str | None = None
    cpu_cores: int = Field(8, ge=1, le=16)
    memory: str = "16Gi"
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


class AdminUserUpdate(BaseModel):
    """Admin-only update body for toggling privileged user flags."""

    is_admin: bool | None = None
    can_use_vm: bool | None = None
    is_approved: bool | None = None


class AdminBulkApprove(BaseModel):
    """Admin-only body for bulk-approving pending users."""

    user_ids: list[str] = Field(..., min_length=1, description="User UUIDs to approve")


class McpTokenCreate(BaseModel):
    """Request body for creating an MCP API token."""

    name: str = Field(..., min_length=1, max_length=100, description="Token label")
    scope: str = Field(default="user", description="'user', 'project:<uuid>', or 'all'")
    expires_in_days: int | None = Field(
        None, description="Days until expiry (null = never)"
    )


class McpTokenVerifyRequest(BaseModel):
    """Internal request from MCP server to verify a token hash."""

    token_hash: str


class McpTokenCreateInternal(BaseModel):
    """Internal request from OAuth bridge to create an srw_* token."""

    user_sub: str = Field(..., description="Keycloak subject ID")
    user_email: str = Field(default="", description="User email for JIT user creation")
    name: str = Field(..., min_length=1, max_length=200)
    token_hash: str
    token_prefix: str
    scope: str = Field(default="user")
    origin: str | None = None
    expires_at: str | None = Field(None, description="ISO 8601 datetime")


# ----- API keys (Personal Access Tokens) -----
# Distinct from `/api/settings/api-keys` (LLM provider keys). PATs are
# Bearer-auth credentials for n8n / scripts hitting the orchestrator API
# directly. See docs/features/auth_bff_and_api_tokens.md §3.

VALID_PAT_SCOPES = {
    "jobs:read",
    "jobs:write",
    "chat:read",
    "chat:write",
    "knowledge:read",
    "knowledge:write",
    "admin",
}


class ApiKeyCreate(BaseModel):
    """Request body for creating a Personal Access Token."""

    name: str = Field(..., min_length=1, max_length=100, description="Display name")
    scopes: list[str] = Field(
        default_factory=lambda: ["jobs:read", "chat:read"],
        description="Action scopes — see VALID_PAT_SCOPES",
    )
    expires_in_days: int | None = Field(
        365,
        ge=1,
        le=3650,
        description="Days until expiry (null = never). Default 1 year per design.",
    )


VALID_API_KEY_PROVIDERS = {
    "openai",
    "anthropic",
    "google",
    "groq",
    "openrouter",
    "codex",
    "vision",
}


class ApiKeySet(BaseModel):
    """Request body for setting an API key for a provider."""

    api_key: str = Field(..., min_length=1, description="The API key value")
    label: str | None = Field(
        None, description="Optional label (e.g. 'team key', 'personal')"
    )


class LlmEndpointCreate(BaseModel):
    """Request body for registering a new LLM endpoint.

    The endpoint must be OpenAI-compatible (vLLM, Ollama, private gateway).
    ``base_url`` should be the full OpenAI path prefix, e.g.
    ``https://my-vllm.example/v1``. ``api_key`` is optional — some local
    servers don't require auth.
    """

    label: str = Field(..., min_length=1, max_length=200)
    base_url: str = Field(..., min_length=1)
    api_key: str | None = None
    allow_insecure: bool = Field(
        False,
        description=(
            "Opt-in for http:// URLs. Default rejects non-HTTPS to guard "
            "against copy-paste accidents."
        ),
    )


class LlmEndpointUpdate(BaseModel):
    """Partial update — only non-None fields are applied.

    ``clear_api_key=True`` nulls the stored key (for endpoints that
    transition from authenticated to anonymous). Ignored when ``api_key``
    is also set.
    """

    label: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    clear_api_key: bool = False
    allow_insecure: bool = False


class ConfigOverrideCreate(BaseModel):
    """Request body for creating or replacing a config override.

    ``kind`` selects the config subsection. Text kinds (prompts, instructions)
    populate ``content``; structured kinds (settings, guardrails) populate
    ``value_json``. ``name`` is the resolver entry_type / settings leaf (dotted
    for limits, e.g. 'limits.context_threshold_tokens'). ``family=None`` means a
    global default.
    """

    family: str | None = Field(None, max_length=64)
    kind: Literal["prompts", "instructions", "settings", "guardrails"]
    name: str = Field(..., min_length=1, max_length=128)
    content: str | None = Field(None, min_length=1)
    content_format: Literal["text", "markdown", "jinja", "yaml"] = "text"
    value_json: Any = None
    notes: str | None = None

    @model_validator(mode="after")
    def _check_payload(self) -> "ConfigOverrideCreate":
        """Enforce the content/value_json XOR by kind (mirrors the DB check)."""
        if self.kind in ("prompts", "instructions"):
            if self.content is None:
                raise ValueError(f"{self.kind} override requires 'content'")
            if self.value_json is not None:
                raise ValueError(f"{self.kind} override must not set 'value_json'")
        else:  # settings, guardrails
            if self.value_json is None:
                raise ValueError(f"{self.kind} override requires 'value_json'")
            if self.content is not None:
                raise ValueError(f"{self.kind} override must not set 'content'")
        return self


class ConfigOverrideUpdate(BaseModel):
    """Update an existing override's payload; family/kind/name are immutable.

    The acting kind is taken from the stored row, so the route picks ``content``
    (text kinds) or ``value_json`` (structured kinds).
    """

    content: str | None = Field(None, min_length=1)
    content_format: Literal["text", "markdown", "jinja", "yaml"] = "text"
    value_json: Any = None
    notes: str | None = None


LLM_MODEL_CAPABILITIES = ("chat", "vision", "embedding", "auxiliary", "whisper", "tts")


class AdminDefaultModelSet(BaseModel):
    """Request body for setting a default LLM model on the system.

    ``model`` is the model ID to resolve via the registry (e.g.
    ``RedHatAI/gemma-4-31B-it-FP8-Dynamic``, ``gpt-4o``). Pass an empty
    string to clear the default.
    """

    model: str = Field(..., description="Model ID; empty string clears the default")


# Locked enum for the admin-curated catalog. Adding a new capability requires
# touching every consumer (resolver, dispatcher, default-model fallback),
# so the schema-level CHECK constraint and this Literal are kept in sync.
VALID_CATALOG_CAPABILITIES = (
    "chat",
    "auxiliary",
    "embedding",
    "vision",
    "whisper",
    "tts",
)
VALID_CATALOG_PROVIDER_KINDS = ("system", "endpoint")


CatalogCapabilityLiteral = Literal[
    "chat", "auxiliary", "embedding", "vision", "whisper", "tts"
]


class CatalogModelCreate(BaseModel):
    """Request body for inserting a catalog row (Admin → Models).

    ``capabilities`` is the source of truth — one row can serve multiple
    roles (e.g. ``['chat', 'auxiliary']`` for a chat-capable LLM,
    ``['chat', 'auxiliary', 'vision']`` for a multimodal one). The legacy
    singular ``capability`` form is no longer accepted; clients post the
    array directly.
    """

    provider_kind: Literal["system", "endpoint"]
    provider_ref: str = Field(
        ...,
        min_length=1,
        description=(
            "system_api_keys.provider slug for provider_kind='system' "
            "(e.g. 'anthropic'); llm_endpoints.id (UUID as text) for "
            "provider_kind='endpoint'."
        ),
    )
    model_id: str = Field(..., min_length=1, max_length=500)
    display_label: str = Field(..., min_length=1, max_length=200)
    capabilities: list[CatalogCapabilityLiteral] = Field(
        ...,
        min_length=1,
        description=(
            "The set of capabilities this model row claims. One row can "
            "serve multiple roles (e.g. ['chat', 'auxiliary'] for a "
            "chat-capable LLM, ['chat', 'auxiliary', 'vision'] for a "
            "multimodal one). Must be non-empty."
        ),
    )
    family: str = Field(
        ...,
        min_length=1,
        description="model_config_matrix.yaml key (e.g. 'claude-opus', 'gemini').",
    )
    context_window: int | None = Field(
        None,
        description=(
            "Optional override; falls back to model_config_matrix family "
            "default. Pass null (the default) to use the matrix; pass an "
            "explicit int to override (zero is allowed and round-trips as "
            "zero)."
        ),
    )
    reasoning_level: str | None = None
    params_json: dict[str, Any] | None = Field(
        None,
        description=(
            "Optional inference param overrides (e.g. {'temperature': 0.0}). "
            "Null means 'use family defaults'; explicit zero/false values "
            "round-trip as themselves (LiteLLM #14661 hazard guarded by "
            "create_model accessor)."
        ),
    )
    enabled: bool = True
    notes: str | None = None


class CatalogModelUpdate(BaseModel):
    """Partial update — only fields explicitly set in the request body are
    applied. Pass ``null`` to clear an optional column to NULL.
    """

    provider_kind: Literal["system", "endpoint"] | None = None
    provider_ref: str | None = Field(None, min_length=1)
    model_id: str | None = Field(None, min_length=1, max_length=500)
    display_label: str | None = Field(None, min_length=1, max_length=200)
    capabilities: list[CatalogCapabilityLiteral] | None = Field(None, min_length=1)
    family: str | None = Field(None, min_length=1)
    context_window: int | None = None
    reasoning_level: str | None = None
    params_json: dict[str, Any] | None = None
    enabled: bool | None = None
    notes: str | None = None


# Slots admins can pin cluster-wide via Admin → Providers → Defaults. The
# system_settings key pattern is ``llm.default_<kind>_model``. ``tts`` is
# present even without a current consumer in src/services/ — landing the
# plumbing keeps the registry path uniform across audio capabilities.
#
# The ``chat`` slot is the cluster-wide chat default — used by the
# orchestrator dispatcher when a job/session doesn't carry its own model
# override (see resolve_default_for_capability("chat") at the dispatch
# call sites). Surfacing it in the cockpit's Defaults panel lets the
# readiness gate's ``Pin a default for: chat`` requirement actually have
# a UI to fulfill (it was previously phantom — the gate asked but the
# panel didn't render the dropdown).
VALID_DEFAULT_MODEL_KINDS = {
    "chat",
    "browser",
    "citation",
    "embedding",
    "vision",
    "auxiliary",
    "whisper",
    "tts",
}

# System-scoped API keys only cover shared providers. Codex auth is
# user-bound through the proxy and isn't appropriate for a system key.
VALID_SYSTEM_API_KEY_PROVIDERS = {
    "openai",
    "anthropic",
    "google",
    "groq",
    "openrouter",
    "vision",
}


class UserSettingsUpdate(BaseModel):
    """Request body for updating user preferences. Null values remove the key."""

    default_model: str | None = None
    default_autonomy: str | None = None
    default_reasoning_level: str | None = None
    default_chat_model: str | None = None
    default_auxiliary_model: str | None = None
    default_vision_model: str | None = None
    default_whisper_model: str | None = None
    default_tts_model: str | None = None
    default_session_model: str | None = None
    default_strategic_model: str | None = None
    default_tactical_model: str | None = None
    default_embedding_model: str | None = None
    embedding_provider: str | None = None
    # Admin "View as" preference: 'all' = fleet-wide visibility (default),
    # 'me' = shadow regular-user visibility. Read by the cockpit's
    # ViewModeService; the live request narrowing rides the X-Admin-View-As
    # header (orchestrator/security/auth.py), this just persists the choice.
    admin_view_mode: Literal["me", "all"] | None = None
    # Phase 6: persistent_agent sub-object covers headless_mode,
    # headless_attention_sleep_minutes, notification_channels, plus the
    # existing model/permission_mode/greeting/idle_timeout_minutes/command_allowlist
    # keys already read in create_thread. Patch-replaces the whole sub-object.
    persistent_agent: dict[str, Any] | None = None


class ProjectCreate(BaseModel):
    """Request body for creating a project."""

    name: str = Field(..., description="Project name")
    description: str | None = Field(None, description="Project description")
    goal: str | None = Field(None, description="Project goal statement")
    default_config_name: str | None = Field(
        None, description="Default agent config for new jobs"
    )
    default_config_override: dict[str, Any] | None = Field(
        None, description="Default config overrides"
    )
    user_id: str = Field(..., description="Owner user UUID")


class ProjectUpdate(BaseModel):
    """Request body for updating a project."""

    name: str | None = None
    description: str | None = None
    goal: str | None = None
    status: str | None = None
    default_config_name: str | None = None
    default_config_override: dict[str, Any] | None = None
    cloud_storage_read_only: bool | None = None
    # Workspace egress tier. Admin-only — see PATCH /api/projects/{id}.
    network_tier: str | None = None


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

    status: str | None = Field(
        None, description="New status: active, resolved, superseded, archived"
    )
    add_tags: list[str] | None = Field(None, description="Tags to add")
    remove_tags: list[str] | None = Field(None, description="Tags to remove")


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

    # Hard-fail if the legacy LLM_BASE_URL env var is set. The env-var-driven
    # routing for self-hosted "Local" group models was removed in chunk 6 of
    # the models_yaml_removal work. Operators currently relying on it must
    # migrate to a helm-seeded llm_endpoints row + catalog rows referencing
    # it. ERROR + sys.exit(1) (not WARN + ignore) because the var being set
    # with no consumer is an active misconfiguration that won't self-heal —
    # the legacy code path silently fell through to api.openai.com with
    # `not-needed` (the bug captured in docs/llm_routing_issues.md).
    if os.getenv("LLM_BASE_URL"):
        logger.error(
            "LLM_BASE_URL is set but no longer honoured. Self-hosted models "
            "must now be configured via Admin → Providers (system endpoint) "
            "+ Admin → Models (catalog row) or via "
            "helm.llm.seed.systemEndpoints[]. Unset LLM_BASE_URL and seed "
            "the endpoint in helm to migrate. See "
            "docs/features/models_yaml_removal.md."
        )
        sys.exit(1)

    # Connect to databases
    await postgres_db.connect()
    await vector_db.connect()

    # Audit DB is the non-load-bearing observability tier: a connect failure
    # must NOT abort startup (unlike the control-plane + vector DBs above).
    # Log loudly, then degrade — product flow survives the audit store's outage.
    audit_ready = False
    if audit_db is None:
        logger.info(
            "Audit DB disabled (AUDIT_POSTGRES_* unset) — Postgres audit store "
            "inactive; archiving continues via MongoDB / no-op."
        )
    else:
        try:
            await audit_db.connect()
            audit_ready = True
        except Exception:
            logger.exception(
                "Audit DB connect failed — continuing without the audit store. "
                "Check AUDIT_POSTGRES_* and the srw-auditdb server."
            )

    # Audit reads are served by the Postgres AuditStore (audit_reader is bound to
    # it at construction). Connect its read pool when the tier is present; a
    # connect failure leaves is_available=False -> the endpoints' degraded shapes,
    # never fatal (non-load-bearing tier). The legacy Mongo reader was retired
    # (docs/features/database_optimization_plan.md QW-4/D-5).
    if audit_db is not None:
        await audit_store.connect()
        logger.info("Audit reads served by Postgres AuditStore")
    if os.getenv("AUDIT_BACKEND", "postgres").strip().lower() == "mongodb":
        logger.warning(
            "AUDIT_BACKEND=mongodb is no longer supported — MongoDB was removed; "
            "serving audit reads from Postgres. Drop databases.audit.backend / the "
            "AUDIT_BACKEND override."
        )

    # Apply pending migrations on each DB. Each PostgresDB instance is
    # bound to its migrations directory at construction time; the runner
    # serializes via pg_advisory_xact_lock and refuses to proceed on
    # checksum drift or a dirty row from a prior failure (see
    # docs/db_migration.md §Operational runbook for repair steps).
    await postgres_db.apply_migrations()
    await vector_db.apply_migrations()
    if audit_db is not None and audit_ready:
        try:
            await audit_db.apply_migrations()
        except Exception:
            logger.exception(
                "Audit DB migrations failed — disabling audit store for this "
                "process (non-load-bearing)."
            )
            audit_ready = False
    logger.info("Database migrations applied")

    # Usage-metering ledger (Slice 4). Writes go to the auditdb usage_events
    # table (None → no-op when the audit tier is absent); rates resolve against
    # the app-DB usage_rates table created by the migration above. Built here so
    # both pools + the schema are ready. Emitters (compute / LLM materialization)
    # and /api/usage read this singleton.
    global usage_ledger
    usage_ledger = UsageLedger(
        audit_db.pool if (audit_db is not None and audit_ready) else None,
        UsageRates(postgres_db.pool),
    )

    # Encrypt any legacy plaintext datasource credentials. Idempotent — once
    # all rows are v1 ciphertexts this is a fast no-op. Lives in lifespan
    # (not init.py) because init.py is not reliably invoked at deploy time, and
    # this is data-integrity critical for the encryption-at-rest guarantee.
    try:
        _bf = await postgres_db.backfill_encrypt_datasource_credentials()
        if _bf["encrypted"] > 0:
            logger.info(
                "Encrypted %d legacy plaintext datasource credentials "
                "(%d skipped, %d errors)",
                _bf["encrypted"],
                _bf["skipped"],
                _bf["errors"],
            )
        elif _bf["errors"] > 0:
            logger.warning(
                "Datasource credentials backfill: %d errors (%d skipped)",
                _bf["errors"],
                _bf["skipped"],
            )
    except Exception as _e:
        logger.error("Datasource credentials backfill failed: %s", _e)

    # Strip any legacy plaintext secrets from threads.metadata.config_override.
    # Persistent-session credentials are injected in-flight at attach/resume and
    # must never be stored (see redact_config_override). Idempotent — once all
    # rows are secret-free this is a fast no-op. Lives in lifespan (not init.py)
    # for the same reason as the datasource backfill above.
    try:
        _sf = await postgres_db.backfill_strip_thread_config_secrets()
        if _sf["stripped"] > 0:
            logger.info(
                "Stripped secrets from %d thread config_override(s) "
                "(%d skipped, %d errors)",
                _sf["stripped"],
                _sf["skipped"],
                _sf["errors"],
            )
        elif _sf["errors"] > 0:
            logger.warning(
                "Thread config_override strip backfill: %d errors (%d skipped)",
                _sf["errors"],
                _sf["skipped"],
            )
    except Exception as _e:
        logger.error("Thread config_override strip backfill failed: %s", _e)

    # Dev-only: seed a fixed admin MCP token from MCP_DEV_TOKEN so a committed
    # .mcp.json works out of the box against a local cluster. Only fires when
    # MCP_DEV_TOKEN is set (unset in prod → no-op, no surprise auto-generated
    # token). Lives in lifespan (not init.py) for the same reason as the
    # backfill above — init.py is not reliably invoked at deploy time. Idempotent
    # and no-ops on a fresh DB with no admin yet; the JIT-provision path in
    # security/auth.py re-fires it the moment the admin user is first created.
    if os.environ.get("MCP_DEV_TOKEN", "").strip():
        try:
            from init import _seed_admin_mcp_token

            await _seed_admin_mcp_token(postgres_db)
        except Exception as _e:
            logger.warning("MCP dev token seed at startup failed (non-fatal): %s", _e)

    # Wire the model registry's catalog lookup to the DB. The registry lives
    # in src/core/ and must not import orchestrator/, so the hook is injected
    # here (and unset on shutdown below). custom/system lookups were retired
    # along with user_llm_endpoint_models — the catalog covers both scopes.
    from src.core.model_registry import register_catalog_lookup

    register_catalog_lookup(postgres_db.resolve_catalog_model)

    # Share the selected audit reader + the app DB with graph_routes.
    set_audit_reader(audit_reader)
    set_postgres_db(postgres_db)

    # Initialize Gitea workspace delivery (graceful if unavailable)
    await gitea_client.ensure_initialized()

    # Configure Gitea OIDC auth source (graceful if unconfigured)
    await gitea_client.ensure_oidc_configured()

    # Initialize Keycloak group sync (graceful if unavailable)
    await keycloak_groups.ensure_initialized()
    await main_cloud_router.ensure_initialized()

    # Phase 4: if a persisted overlay exists, apply it now so the
    # active backend matches the cockpit admin UI's view. Non-fatal —
    # if the overlay is broken the env-var-only path from the initial
    # ensure_initialized() above stays in place.
    try:
        _persisted_overlay = await postgres_db.get_system_setting("main_cloud")
    except Exception as _e:
        logger.warning("Main cloud overlay read failed at startup (non-fatal): %s", _e)
        _persisted_overlay = None
    if _persisted_overlay:
        _overlay_ok = await main_cloud_router.reload_from_db(_persisted_overlay)
        if not _overlay_ok:
            logger.warning(
                "Main cloud overlay present in system_settings.main_cloud but "
                "rebuild failed — active backend stays on env-var config"
            )

    # Issue 5: warn loudly if the *active* backend's required secrets are not
    # present in the env — it is silently running on built-in DEV credentials
    # and will fail at the first cloud call. Non-fatal (graceful-degradation
    # convention + local/dev stacks legitimately set their own secrets), but no
    # longer silent. The PUT/test endpoints refuse this at swap time; this
    # catches a Helm-misconfigured deployment that booted straight into it.
    try:
        from services.cloud.config import missing_secret_envs

        _active_id = main_cloud_router.active.backend_id
        # Only trust the overlay's credentials_ref if the overlay actually drove
        # the active backend; otherwise check the plain env path.
        _active_overlay = (
            _persisted_overlay
            if _persisted_overlay
            and (_persisted_overlay.get("value") or {}).get("backend_id") == _active_id
            else None
        )
        _missing_secrets = missing_secret_envs(_active_id, _active_overlay)
        if _missing_secrets:
            logger.warning(
                "Main cloud backend %r is active but required secret env var(s) "
                "are unset: %s — it is running on built-in DEV credentials and "
                "will fail at the first cloud call. Set them via Helm/Vault.",
                _active_id,
                ", ".join(sorted({m["env_var"] for m in _missing_secrets})),
            )
    except Exception as _e:
        logger.debug("Main cloud secret presence check skipped at startup: %s", _e)

    # Initialize NATS bridge for VM lifecycle (graceful if unavailable)
    await nats_bridge.connect(db=postgres_db, on_vm_ready=_trigger_dispatch)

    # Initialize S3 snapshot service (graceful if S3 not configured)
    await snapshot_service.connect(db=postgres_db)

    # Initialize VM provisioner (uses NATS if available, else direct K8s)
    vm_provisioner.connect(db=postgres_db, snapshot_service=snapshot_service)

    # Initialize container provisioner for workspace containers (direct K8s)
    container_provisioner.connect(db=postgres_db, snapshot_service=snapshot_service)

    # Initialize Docker Compose provisioner (static workspace pool, used when k8s unavailable)
    docker_provisioner.connect(db=postgres_db, snapshot_service=snapshot_service)

    # Log deployment mode.
    # Priority: K8s in-cluster → Docker Compose → K8s via kubeconfig.
    # A local kubeconfig should not shadow Docker Compose when running outside the cluster.
    if container_provisioner.is_available and container_provisioner.in_cluster:
        logger.info(
            "Deployment mode: KUBERNETES (in-cluster) — dynamic provisioning via k8s API"
        )
    elif docker_provisioner.is_available:
        logger.info(
            "Deployment mode: DOCKER COMPOSE — static workspace pool (%s)",
            ",".join(docker_provisioner.workspace_hosts),
        )
        if container_provisioner.is_available:
            logger.info(
                "Deployment mode: Kubernetes also reachable via kubeconfig "
                "but Docker Compose takes priority (not running in-cluster)"
            )
    elif container_provisioner.is_available:
        logger.info(
            "Deployment mode: KUBERNETES (kubeconfig) — dynamic provisioning via k8s API"
        )
    else:
        logger.warning(
            "Deployment mode: NO WORKSPACE PROVISIONER — "
            "neither k8s API nor WORKSPACE_HOSTS available. "
            "Jobs requiring workspaces will fail."
        )

    # Initialize IDE session service
    ide_session_service.connect(
        db=postgres_db,
        snapshot_service=snapshot_service,
        vm_provisioner=vm_provisioner,
        gitea_client=gitea_client,
        container_provisioner=container_provisioner,
    )

    # Initialize persistent agent provisioner (legacy, kept for backward compat)
    persistent_provisioner.connect(db=postgres_db)

    # Initialize unified agent provisioner (on-demand pods for jobs + sessions)
    agent_provisioner.connect(db=postgres_db)

    # Initialize workspace suspension service (idle timeout → S3 snapshot → pod deletion)
    workspace_suspension_service.connect(
        db=postgres_db,
        snapshot_service=snapshot_service,
        container_provisioner=container_provisioner,
        docker_provisioner=docker_provisioner,
        vm_provisioner=vm_provisioner,
        agent_provisioner=agent_provisioner,
    )

    # Initialize IDE proxy service (pod IP resolution + cache)
    ide_proxy_service.connect(db=postgres_db)

    # Initialize notification feed (SSE broadcast for cockpit)
    from services.notification_feed import notification_feed

    # Initialize notification service (unified dispatcher for email + webhooks)
    notification_service.connect(
        db=postgres_db,
        email_service=email_service,
        notification_feed=notification_feed,
    )

    # Initialize IMAP poller for email reply routing (graceful if unconfigured)
    async def _imap_reply_handler(
        job_id: str,
        thread_id: str,
        message: str,
        sender_email: str | None = None,
        email_message_id: str | None = None,
    ) -> str:
        """Adapter: strips sequence number from _route_inbound_reply return."""
        strategy, _seq = await _route_inbound_reply(
            job_id,
            thread_id,
            message,
            sender_email=sender_email,
            email_message_id=email_message_id,
        )
        return strategy

    imap_poller.connect(db=postgres_db, reply_handler=_imap_reply_handler)

    # Start background tasks
    _shutdown_event = asyncio.Event()
    stale_detector_task = asyncio.create_task(stale_agent_detector(_shutdown_event))
    token_cleanup_task = asyncio.create_task(
        cleanup_expired_tokens(postgres_db, _shutdown_event)
    )
    session_cleanup_task = asyncio.create_task(
        cleanup_expired_sessions(postgres_db, _shutdown_event)
    )
    dispatcher_task = asyncio.create_task(auto_assign_dispatcher(_shutdown_event))
    sudo_sweeper_task = asyncio.create_task(sudo_expiration_sweeper(_shutdown_event))
    thread_events_prune_task = asyncio.create_task(
        thread_events_prune_sweeper(_shutdown_event)
    )
    security_events_prune_task = asyncio.create_task(
        security_events_prune_sweeper(_shutdown_event)
    )
    headless_notify_task = asyncio.create_task(
        thread_permission_notify_sweeper(_shutdown_event)
    )
    attention_sleep_task = asyncio.create_task(attention_sleep_sweeper(_shutdown_event))
    ide_sweeper_task = asyncio.create_task(ide_session_ttl_sweeper(_shutdown_event))
    ws_sweeper_task = asyncio.create_task(workspace_idle_sweeper(_shutdown_event))
    ide_settings_sweeper_task = asyncio.create_task(
        code_server_settings_sweeper(_shutdown_event)
    )
    gc_sweeper_task = asyncio.create_task(snapshot_gc_sweeper(_shutdown_event))
    imap_task = asyncio.create_task(imap_poll_loop(_shutdown_event))
    digest_task = asyncio.create_task(quiet_hours_digest_loop(_shutdown_event))
    delegation_timeout_task = asyncio.create_task(
        delegation_timeout_sweeper(_shutdown_event)
    )
    pool_reconciler_task = asyncio.create_task(agent_pool_reconciler(_shutdown_event))
    automation_cron_task = asyncio.create_task(
        cron_dispatcher_loop(
            postgres_db, _shutdown_event, on_job_created=_trigger_dispatch
        )
    )
    # Safety-net for project self-improvement loops: recover any loop whose
    # current job went terminal without the completion hook advancing it.
    project_loop_sweeper_task = asyncio.create_task(
        project_loop_sweeper_loop(
            postgres_db, _shutdown_event, advance_fn=_advance_project_loop
        )
    )

    # LiteLLM gateway catalog sync — registers endpoint-kind catalog models into
    # the in-chart LiteLLM proxy so agent LLM traffic can be measured (and, in
    # later slices, rate-limited). Self-disables when LITELLM_BASE_URL is unset
    # (i.e. litellm.enabled=false), so it's a no-op on deployments without the
    # gateway. See docs/features/usage_monitoring_and_rate_limiting.md.
    litellm_sync_task = asyncio.create_task(
        litellm_sync_loop(_shutdown_event, postgres_db)
    )

    # Longer-window quota stop (Slice 3): polls per-project daily usage from the
    # gateway and freezes jobs whose project crossed its quota. Self-disables with
    # the gateway (LITELLM_BASE_URL unset). See usage_monitoring_and_rate_limiting.md.
    quota_poll_task = asyncio.create_task(quota_poll_loop(_shutdown_event, postgres_db))

    # Workspace compute metering (Slice 4b): materialize CLOSED workspace
    # intervals into the usage ledger + reconcile leaked opens. Self-disables
    # when the app pool or ledger is absent (non-load-bearing tier).
    workspace_metering_task = asyncio.create_task(
        workspace_metering.workspace_metering_loop(
            _shutdown_event,
            postgres_db,
            usage_ledger,
            _workspace_metering_attribution,
        )
    )

    # LLM usage materialization (Slice 4c): poll the LiteLLM spend log into the
    # usage ledger as category='llm' rows. Self-disables when the gateway or the
    # ledger is absent.
    llm_usage_task = asyncio.create_task(llm_usage_poll_loop(_shutdown_event))

    # Audit-store partition maintenance (creation + ANALYZE + lookahead alarms;
    # retention deferred — see services/audit_partitions.py). Only when the
    # audit DB is configured; otherwise the store is inactive and there is
    # nothing to maintain.
    audit_maintenance_task = (
        asyncio.create_task(audit_maintenance_loop(audit_db.pool, _shutdown_event))
        if (audit_db is not None and audit_ready)
        else None
    )

    # Unified instance lifecycle reconciler (drift-based draining and,
    # in future phases, crash recovery + cross-kind primitives). Runs
    # peer to agent_pool_reconciler — pool owns capacity, lifecycle
    # owns version/health.
    lifecycle_reconciler = InstanceLifecycleReconciler()
    lifecycle_reconciler.register(
        AgentInstanceManager(provisioner=agent_provisioner, db=postgres_db)
    )
    lifecycle_reconciler.register(
        WorkspaceInstanceManager(
            container_provisioner=container_provisioner,
            suspension_service=workspace_suspension_service,
            snapshot_service=snapshot_service,
            db=postgres_db,
        )
    )
    lifecycle_reconciler.register(
        VMInstanceManager(
            vm_provisioner=vm_provisioner,
            suspension_service=workspace_suspension_service,
            snapshot_service=snapshot_service,
            db=postgres_db,
        )
    )
    # Startup reconciliation: rebuild the in-memory view from K8s
    # before the heartbeat endpoint starts accepting traffic. Phase 1b
    # logs the discovered pod set; future phases may also flag DB-row
    # divergence and reap pods that lack a registration.
    try:
        startup_pods = await lifecycle_reconciler.managers[0].list_pods()
        logger.info(
            "Lifecycle startup: discovered %d agent pod(s) from K8s",
            len(startup_pods),
        )
    except Exception:
        logger.exception("Lifecycle startup reconciliation failed (non-fatal)")
    lifecycle_reconciler_task = asyncio.create_task(
        lifecycle_reconciler_loop(_shutdown_event, lifecycle_reconciler)
    )

    # Phase 4: main-cloud config LISTEN task — reacts to pg_notify when
    # an admin PUTs a new config via /api/admin/system-settings/main_cloud.
    async def _main_cloud_reload_callback() -> None:
        await _reload_from_db_and_swap(postgres_db, main_cloud_router)

    main_cloud_listen_task = asyncio.create_task(
        run_listen_loop(postgres_db, _main_cloud_reload_callback, _shutdown_event)
    )

    yield

    # Signal shutdown to background tasks
    _shutdown_event.set()
    await stale_detector_task
    await token_cleanup_task
    await session_cleanup_task
    await dispatcher_task
    await sudo_sweeper_task
    await thread_events_prune_task
    await security_events_prune_task
    await headless_notify_task
    await attention_sleep_task
    await ide_sweeper_task
    await ws_sweeper_task
    await ide_settings_sweeper_task
    await gc_sweeper_task
    await imap_task
    await digest_task
    await delegation_timeout_task
    await pool_reconciler_task
    await lifecycle_reconciler_task
    await main_cloud_listen_task
    await automation_cron_task
    await project_loop_sweeper_task
    await litellm_sync_task
    await quota_poll_task
    await workspace_metering_task
    await llm_usage_task
    if audit_maintenance_task is not None:
        await audit_maintenance_task

    # Cleanup clients
    await nats_bridge.disconnect()
    await vm_provisioner.disconnect()
    await gitea_client.close()

    # Unregister the registry's DB hook before disconnecting the pool so
    # any stragglers don't hit a closed connection.
    from src.core.model_registry import register_catalog_lookup

    register_catalog_lookup(None)

    # Disconnect from databases
    await vector_db.disconnect()
    if audit_store is not None:
        await audit_store.disconnect()
    if audit_db is not None:
        await audit_db.disconnect()
    await postgres_db.disconnect()


app = FastAPI(
    title="Debug Cockpit API",
    description="Backend API for the Superhuman Remote Worker Cockpit",
    version="0.1.0",
    lifespan=lifespan,
    default_response_class=CustomJSONResponse,
)

# CSRF defense for the cookie BFF. Middleware order matters: Starlette
# runs the OUTERMOST `add_middleware` last, so we add CSRF first and CORS
# second. Result: incoming request → CORS preflight/origin handling →
# CSRF check → app. That means OPTIONS preflights are still answered by
# CORS (which is good — preflights are unauthenticated), while real
# POST/PUT/DELETE/PATCH requests get the layered Sec-Fetch-Site +
# X-CSRF + Origin allowlist check before they reach any handler.
app.add_middleware(CSRFMiddleware)

# CORS for Angular frontend (dev server on 4200, production/SSR on 4000)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:4200",
        "http://127.0.0.1:4200",
        "http://localhost:4000",
        "http://127.0.0.1:4000",
    ]
    + [o for o in os.environ.get("CORS_ORIGINS", "").split(",") if o],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    # Expose nothing extra — the BFF only returns JSON + Set-Cookie, and
    # browsers already see Set-Cookie. Keeping this explicit makes the
    # CSP-style header surface visible.
)


@app.exception_handler(UnknownModelError)
async def _unknown_model_handler(
    request: Request, exc: UnknownModelError
) -> JSONResponse:
    """Translate registry misses into a helpful 400 instead of a 500.

    Fires whenever a request references a model ID that isn't in the
    built-in catalog or any of the user's custom endpoints. Points users
    at the settings UI where they can register the model.
    """
    return JSONResponse(
        status_code=400,
        content={
            "detail": str(exc),
            "model_id": exc.model_id,
            "hint": (
                "Register this model under a custom endpoint at "
                "/api/settings/llm-endpoints, or pick an ID from /api/models."
            ),
        },
    )


@app.exception_handler(Exception)
async def _unhandled_exception_handler(
    request: Request, exc: Exception
) -> JSONResponse:
    """Return a CORS-friendly 500 for any otherwise-unhandled exception.

    Without this, Starlette's outermost ServerErrorMiddleware produces a
    bare 500 that skips the CORSMiddleware on the way out. Browsers then
    drop the response (no Access-Control-Allow-Origin header) and Angular
    surfaces it as a status-0 "network failure" instead of a real 5xx,
    which makes server-side bugs look like client-side connectivity
    issues. Handling here keeps the response inside the middleware stack
    so CORS headers are attached.

    HTTPException / RequestValidationError / UnknownModelError are
    dispatched to their own handlers first, so they don't reach here.
    """
    logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal Server Error"},
    )


# Request logging middleware — replaces uvicorn's shallow access log with
# app-level logging that includes response timing and error tracebacks.
_SILENT_PATHS = {"/api/health"}
_SILENT_PREFIXES = ("/api/ide/",)  # suppress per-asset log spam from IDE proxy


@app.middleware("http")
async def request_logging_middleware(request: Request, call_next):
    path = request.url.path
    if path in _SILENT_PATHS or path.startswith(_SILENT_PREFIXES):
        return await call_next(request)

    method = request.method
    start = time.perf_counter()
    # request_id is bound upstream by CorrelationIdMiddleware (outermost), so it
    # tags both this access line and the route handler's logs.
    try:
        response = await call_next(request)
    except Exception:
        elapsed = (time.perf_counter() - start) * 1000
        logger.exception(
            "%s %s 500 (%dms) — unhandled exception", method, path, elapsed
        )
        return JSONResponse(
            status_code=500, content={"detail": "Internal server error"}
        )

    elapsed = (time.perf_counter() - start) * 1000
    status = response.status_code
    if status >= 500:
        logger.warning("%s %s %d (%dms)", method, path, status, elapsed)
    else:
        logger.info("%s %s %d (%dms)", method, path, status, elapsed)
    return response


# request_id correlation — added last so it is OUTERMOST (wraps the access-log
# middleware above). See CorrelationIdMiddleware in logging_config.
app.add_middleware(CorrelationIdMiddleware)


# Include routers
app.include_router(bff_router)
app.include_router(graph_router)
app.include_router(uploads_router)
app.include_router(automations_router)
app.include_router(project_loops_router)
app.include_router(sessions_router)


@app.get("/api/tables")
async def list_tables(request: Request) -> list[dict[str, Any]]:
    """List available tables with row counts. **Admin only** (P4d) —
    raw postgres table dump."""
    await _require_admin(request)
    return await postgres_db.get_tables()


@app.get("/api/tables/{table_name}")
async def get_table_data(
    request: Request,
    table_name: str,
    page: int = Query(default=1, ge=-1),
    page_size: int = Query(default=50, ge=1, le=500, alias="pageSize"),
) -> dict[str, Any]:
    """Get paginated table data. Use page=-1 to request the last page.
    **Admin only** (P4d) — raw postgres rows."""
    await _require_admin(request)
    if table_name not in ALLOWED_TABLES:
        raise HTTPException(status_code=404, detail=f"Table '{table_name}' not found")

    try:
        return await postgres_db.get_table_data(table_name, page, page_size)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/tables/{table_name}/schema")
async def get_table_schema(request: Request, table_name: str) -> list[dict[str, Any]]:
    """Get column definitions for a table. **Admin only** (P4d)."""
    await _require_admin(request)
    if table_name not in ALLOWED_TABLES:
        raise HTTPException(status_code=404, detail=f"Table '{table_name}' not found")

    try:
        return await postgres_db.get_table_schema(table_name)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


# nosec: public k8s-liveness-probe
@app.get("/api/health")
async def health_check() -> dict[str, str]:
    """Health check endpoint."""
    return {"status": "ok"}


@app.get("/api/workspace/status")
async def workspace_status(request: Request) -> dict[str, Any]:
    """Get workspace configuration status for debugging.

    **Admin only** (P4a): leaks job UUIDs, filesystem paths, and env-var
    values, so it shouldn't be anonymous. No callers currently rely on it.

    Returns:
        Dict with workspace path, availability, and sample job directories
    """
    await _require_admin(request)

    import os

    base_path = workspace_service.base_path
    is_available = workspace_service.is_available

    # List top-level entries (workspace is a flat directory now, no job_* subdirs)
    entries = []
    if is_available:
        try:
            entries = [d.name for d in base_path.iterdir()][:20]
        except Exception:
            pass

    return {
        "configured_path": str(base_path),
        "resolved_path": str(base_path.resolve()) if base_path.exists() else None,
        "is_available": is_available,
        "env_workspace_path": os.environ.get("WORKSPACE_PATH"),
        "entries": entries,
    }


@app.get("/api/jobs")
async def list_jobs(
    request: Request,
    status: str | None = Query(default=None),
    user_id: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict[str, Any]]:
    """List jobs visible to the caller.

    Visibility model (G1):
        * Admins see the full fleet, optionally narrowed by ``?user_id=`` or
          by an MCP ``project:<uuid>`` token scope.
        * Non-admins see jobs they own OR jobs in projects they're a member
          of, additionally narrowed by any MCP project scope.
        * A non-admin passing ``?user_id=`` for anyone other than themselves
          is rejected (403). Self-query (``?user_id=<self>``) is allowed but
          redundant — the visibility OR-clause already covers it.

    Returns jobs enriched with audit_count from MongoDB if available.
    """
    user = await require_approved_user(request, postgres_db)
    is_admin = bool(user.get("is_admin"))
    scope_pid = mcp_scope_project_id(user)

    if user_id is not None and not is_admin and str(user_id) != str(user["id"]):
        raise HTTPException(
            status_code=403,
            detail="Not authorized to query other users' jobs",
        )

    try:
        if is_admin:
            jobs = await postgres_db.get_jobs(
                status=status,
                user_id=user_id,
                limit=limit,
                scope_project_id=str(scope_pid) if scope_pid else None,
            )
        else:
            visible = await user_visible_project_ids(user, postgres_db)
            # Non-admin always lands on a concrete set (never "all").
            project_ids = [str(p) for p in visible] if visible != "all" else []
            jobs = await postgres_db.get_visible_jobs(
                owner_user_id=str(user["id"]),
                visible_project_ids=project_ids,
                status=status,
                scope_project_id=str(scope_pid) if scope_pid else None,
                limit=limit,
            )

        if audit_reader.is_available:
            counts = await audit_reader.get_audit_counts(
                [str(job["id"]) for job in jobs]
            )
            for job in jobs:
                job["audit_count"] = counts.get(str(job["id"]), 0)
        else:
            for job in jobs:
                job["audit_count"] = None

        return [
            _with_cloud_review_mode(_redact_job_config_override(job)) for job in jobs
        ]
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


def _redact_job_config_override(job: dict[str, Any]) -> dict[str, Any]:
    """Strip credential fields from a job's ``config_override`` before it leaves
    over REST. asyncpg returns the JSONB column as a JSON string; we redact and
    re-serialize to the original representation so the response shape is
    unchanged. See ``security.access.redact_config_override``.
    """
    co = job.get("config_override")
    if co is None:
        return job
    was_str = isinstance(co, str)
    if was_str:
        try:
            co = json.loads(co)
        except (json.JSONDecodeError, TypeError):
            # Opaque/garbage — drop it rather than risk returning a raw secret.
            job = dict(job)
            job["config_override"] = None
            return job
    job = dict(job)
    cleaned = redact_config_override(co)
    job["config_override"] = json.dumps(cleaned) if was_str else cleaned
    return job


def _with_cloud_review_mode(job: dict[str, Any]) -> dict[str, Any]:
    """Attach the computed ``cloud_review_mode`` and drop the raw join column.

    Routing signal for the cockpit's job-review UI: a job whose project has a
    main-cloud folder goes through the Mode A diff-review flow (``'diff'``);
    everything else — loose jobs and projects without a cloud folder, including
    the auto-assigned default project — gets the Mode B "Open cloud folder"
    affordance (``'open_folder'``). Mirrors the seed-time gate in
    ``services/job_cloud_baseline.py``. ``project_has_cloud_folder`` is computed
    by the ``LEFT JOIN projects`` in the postgres read queries; we pop it so the
    raw column never leaves over REST.
    """
    job = dict(job)
    job["cloud_review_mode"] = (
        "diff" if job.pop("project_has_cloud_folder", False) else "open_folder"
    )
    return job


def _redact_thread_metadata(thread: dict[str, Any]) -> dict[str, Any]:
    """Strip credential fields from a thread's ``metadata.config_override``
    before it leaves over REST. ``metadata`` is a JSONB column returned as a
    JSON string; redact and re-serialize to the original representation.
    """
    md = thread.get("metadata")
    was_str = isinstance(md, str)
    if was_str:
        try:
            md = json.loads(md)
        except (json.JSONDecodeError, TypeError):
            return thread  # unparseable — cannot contain our config_override
    if isinstance(md, dict) and "config_override" in md:
        thread = dict(thread)
        md = dict(md)
        md["config_override"] = redact_config_override(md["config_override"])
        thread["metadata"] = json.dumps(md) if was_str else md
    return thread


@app.get("/api/jobs/{job_id}")
async def get_job(request: Request, job_id: str) -> dict[str, Any]:
    """Get a single job by ID."""
    _, job = await require_job_access(request, postgres_db, job_id)
    try:
        if audit_reader.is_available:
            job["audit_count"] = await audit_reader.get_audit_count(job_id)
        else:
            job["audit_count"] = None
        return _with_cloud_review_mode(_redact_job_config_override(job))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/api/jobs")
async def create_job(request: Request, job: JobCreate) -> dict[str, Any]:
    """Create a new job. **Dual-callable** (P4b):

    * Cockpit / user path → ``require_approved_user``; if ``body.project_id``
      is set, the caller must be at least an editor of that project; the
      submitted ``user_id`` is forced to ``caller.id`` so a malicious body
      can't create jobs attributed to someone else.
    * Agent path (delegation child jobs from ``src/api/orchestrator_client.py``)
      → bypass via ``X-Internal-Key``. The agent supplies the correct
      ``user_id`` from the parent job's context; we trust the in-cluster
      internal key here.

    Creates a job with status 'created'. The job must be assigned to an agent
    to start processing.

    If ``project_id`` is set (directly or via the user's default project),
    the job is created within that project context: the project's default
    config and config_override are used as fallbacks, and the workspace is
    branched from the project's shared jobs repo instead of getting its own
    per-job repo.
    """
    if not is_internal_call(request):
        caller = await require_approved_user(request, postgres_db)
        # Force user_id to caller; never honor body.user_id from a cockpit
        # caller (F2 pattern). Project membership is checked below once we
        # know the resolved project_id.
        job.user_id = str(caller["id"])
        if job.project_id:
            await require_project_member(
                request, postgres_db, str(job.project_id), min_role="editor"
            )
    await _enforce_readiness_gate()
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

        # Inherit user_id (and optionally project_id) from the originating
        # persistent-session thread when the caller didn't supply them.
        # This is how session-spawned worker jobs (create_worker_job tool)
        # get attributed to the right user; without it the dispatch path
        # skips per-user model preferences and the worker boots with the
        # YAML defaults pointing at api.openai.com with "not-needed".
        effective_user_id = job.user_id
        thread_project_id: str | None = None
        if job.thread_id and not effective_user_id:
            try:
                thread_row = await postgres_db.get_thread(job.thread_id)
            except Exception as e:
                logger.warning(
                    f"Failed to load thread {job.thread_id} for user inheritance: {e}"
                )
                thread_row = None
            if thread_row:
                if thread_row.get("user_id"):
                    effective_user_id = str(thread_row["user_id"])
                if thread_row.get("project_id"):
                    thread_project_id = str(thread_row["project_id"])

        # Resolve project_id: use provided, fall back to thread's, then user's default
        project_id = job.project_id or thread_project_id
        if not project_id and effective_user_id:
            try:
                user = await postgres_db.get_user(effective_user_id)
                if user and user.get("default_project_id"):
                    project_id = str(user["default_project_id"])
            except Exception as e:
                logger.warning(
                    f"Failed to resolve default project for user {effective_user_id}: {e}"
                )

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
                # asyncpg may return JSONB as a string — parse it
                if isinstance(project_default_override, str):
                    project_default_override = json.loads(project_default_override)
                if config_override:
                    # Deep merge: project defaults as base, job overrides on top
                    config_override = _deep_merge_dicts(
                        project_default_override, config_override
                    )
                else:
                    config_override = project_default_override

        # VM permission gate: refuse at submit time so the user gets a clear
        # 403 instead of a silent failure later in the dispatcher. The
        # dispatcher re-checks too (in case the grant is revoked after
        # submission) — defense in depth.
        needs_vm = _job_needs_vm(
            {"context": context, "config_override": config_override}
        )
        if needs_vm:
            creator = None
            if effective_user_id:
                try:
                    creator = await postgres_db.get_user(effective_user_id)
                except Exception:
                    creator = None
            await _check_vm_permission(creator, job_needs_vm=True)

        # Lite-tier guard: virtual/none agents have no shell-capable workspace
        # to clone into, so a repository datasource is the tier boundary (§4).
        # Reject at submit time with a clear 400 instead of a silent dispatch
        # failure later. (The dispatcher re-checks resolved datasources too.)
        lite_backend = _backend_from_override(config_override)
        if lite_backend in LITE_BACKENDS and job.datasource_ids:
            repo_names: list[str] = []
            for ds_id in job.datasource_ids:
                try:
                    ds = await postgres_db.get_datasource(ds_id)
                except Exception:
                    ds = None
                if ds and (ds.get("type") or "").lower() == "repository":
                    repo_names.append(str(ds.get("name") or ds_id))
            if repo_names:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"workspace.backend='{lite_backend}' is a lite tier and "
                        f"cannot use repository datasources "
                        f"({', '.join(repo_names)}). Use backend='sandbox' or "
                        f"'vm' for coding workloads, or remove the repository "
                        f"datasource."
                    ),
                )

        result = await postgres_db.create_job(
            description=job.description,
            document_path=job.document_path,
            document_dir=job.document_dir,
            config_name=config_name,
            expert_id=job.expert_id,
            config_override=config_override,
            context=context if context else None,
            user_id=effective_user_id,
            project_id=project_id,
            parent_job_id=job.parent_job_id,
            priority=job.priority,
            creation_order=job.creation_order,
            worktree_path=job.worktree_path,
            delegation_context=job.delegation_context,
        )

        # Create Gitea repo/branch + grant creator access + seed the Mode A
        # cloud baseline. Shared with the automation paths (cron + run-now)
        # via services.job_provisioning so every job-creation path provisions
        # identically. Mutates `result` in place (repo_name/branch_name).
        from services.job_provisioning import provision_job_repo

        await provision_job_repo(
            job_row=result,
            gitea_client=gitea_client,
            postgres_db=postgres_db,
            main_cloud_router=main_cloud_router,
        )

        # Persist the explicit datasource selection as job_datasources links —
        # the picker is the source of truth; resolution returns exactly these,
        # nothing global/project force-attaches. When no selection is passed
        # but the job has a parent (subjob / delegation), inherit the parent's
        # selection so delegation keeps working; an explicit [] opts out.
        new_job_id = str(result["id"])
        if job.datasource_ids is not None:
            selected_ds_ids = list(job.datasource_ids)
        elif job.thread_id or job.parent_job_id:
            selected_ds_ids = await _inherit_parent_datasource_ids(
                thread_id=job.thread_id, parent_job_id=job.parent_job_id
            )
        else:
            selected_ds_ids = []

        # Lite tiers can't clone repos; drop any inherited repository
        # datasources (explicitly-attached repos were already rejected above
        # with a 400; the dispatch guard is the final backstop).
        if selected_ds_ids and lite_backend in LITE_BACKENDS:
            filtered: list[str] = []
            for ds_id in selected_ds_ids:
                try:
                    ds = await postgres_db.get_datasource(ds_id)
                except Exception:
                    ds = None
                if ds and (ds.get("type") or "").lower() == "repository":
                    logger.info(
                        "Dropping repository datasource %s from lite job %s "
                        "(backend=%s)",
                        ds_id,
                        new_job_id,
                        lite_backend,
                    )
                    continue
                filtered.append(ds_id)
            selected_ds_ids = filtered

        # Link each selection, verifying the creator can see it (owner OR
        # is_global OR member of a linked project). Internal/trusted callers
        # (agent, MCP) bypass the per-user check. Skip + log the rest.
        if selected_ds_ids:
            creator = None
            if not is_internal_call(request) and effective_user_id:
                try:
                    creator = await postgres_db.get_user(effective_user_id)
                except Exception:
                    creator = None
            for ds_id in selected_ds_ids:
                try:
                    if creator is not None:
                        ds = await postgres_db.get_datasource(ds_id)
                        if not ds:
                            logger.warning("Skipping datasource %s: not found", ds_id)
                            continue
                        allowed = bool(ds.get("is_global")) or (
                            await user_can_access_datasource(creator, postgres_db, ds)
                        )
                        if not allowed:
                            logger.warning(
                                "Skipping datasource %s for job %s: caller %s "
                                "cannot access it",
                                ds_id,
                                new_job_id,
                                effective_user_id,
                            )
                            continue
                    await postgres_db.link_datasource_to_job(new_job_id, ds_id)
                except Exception as e:
                    logger.warning(
                        "Failed to link datasource %s to job %s: %s",
                        ds_id,
                        new_job_id,
                        e,
                    )

        # Spawn scholar subjob if enabled (root jobs only)
        if not job.parent_job_id:
            try:
                # Re-fetch the job so _spawn_scholar_subjob has repo_name etc.
                fresh_job = await postgres_db.get_job(str(result["id"]))
                if fresh_job:
                    scholar_result = await _spawn_scholar_subjob(
                        fresh_job,
                        config_name,
                        config_override,
                        context,
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
        logger.exception("Failed to create job: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.delete("/api/jobs/{job_id}")
async def delete_job(request: Request, job_id: str) -> dict[str, str]:
    """Delete a job and its requirements.

    P4c: destructive. Caller must own the job OR be project-owner OR admin.
    Plain project membership is not enough — mirrors G3 sudo-authority gate.
    """
    caller, job = await require_job_access(request, postgres_db, job_id)
    if not caller.get("is_admin"):
        is_job_owner = str(job.get("user_id") or "") == str(caller["id"])
        is_project_owner = False
        if not is_job_owner and job.get("project_id"):
            role = await postgres_db.get_user_role_in_project(
                str(job["project_id"]), str(caller["id"])
            )
            is_project_owner = role == "owner"
        if not (is_job_owner or is_project_owner):
            raise HTTPException(
                status_code=403,
                detail="Only the job owner, the project owner, or an admin may delete this job",
            )
    try:
        # Clean up Gitea repo/branch
        if gitea_client.is_initialized:
            if (
                job.get("parent_job_id")
                and job.get("branch_name")
                and job.get("repo_name")
            ):
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
                    await gitea_client.delete_branch(
                        repos[0]["name"], job["branch_name"]
                    )

        # Clean up vector DB tables (no FK cascade across databases)
        try:
            async with vector_db.acquire() as conn:
                await conn.execute(
                    "DELETE FROM memories WHERE job_id = $1", UUID(job_id)
                )
                await conn.execute(
                    "DELETE FROM citations WHERE job_id = $1", UUID(job_id)
                )
                await conn.execute(
                    "DELETE FROM source_annotations WHERE job_id = $1", UUID(job_id)
                )
                await conn.execute(
                    "DELETE FROM source_tags WHERE job_id = $1", UUID(job_id)
                )
                await conn.execute(
                    "DELETE FROM source_embeddings WHERE job_id = $1", UUID(job_id)
                )
                await conn.execute(
                    "DELETE FROM job_sources WHERE job_id = $1", UUID(job_id)
                )
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
async def subjob_merge(request: Request, job_id: str) -> dict[str, Any]:
    """Graft a completed subjob's output/ onto its parent's branch.
    **Internal** (P4b) — requires ``X-Internal-Key``. Ingress strips this
    path.

    Called by the agent after a subjob completes (autonomy=full auto-completion).
    Grafts the subjob's output/ onto the parent as a namespaced outputs/ folder.
    """
    await require_internal(request)
    try:
        job = await postgres_db.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

        if not job.get("parent_job_id"):
            raise HTTPException(
                status_code=400,
                detail="Only subjobs (with parent_job_id) can be grafted",
            )

        result = await _graft_subjob_output(job_id)
        if result is None:
            return {"status": "skipped", "reason": "no branch/repo configured"}

        return {"job_id": job_id, **result}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Squash merge failed for subjob {job_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e


async def _cascade_cancel_to_children(job_id: str) -> None:
    """Cancel all non-terminal descendant jobs of a parent.

    Fetches the full descendant tree (recursive), signals processing agents
    to stop, cleans up VMs/containers, and bulk-updates DB status.
    """
    children = await postgres_db.get_descendant_jobs(job_id)
    if not children:
        return

    # Signal processing agents concurrently
    async def _signal_cancel(child: dict) -> None:
        child_id = str(child["id"])
        agent_id = child.get("assigned_agent_id")
        if child["status"] != "processing" or not agent_id:
            return
        agent = await postgres_db.get_agent(str(agent_id))
        if not agent or not agent.get("pod_ip") or agent["status"] == "offline":
            return
        agent_url = f"http://{agent['pod_ip']}:{agent['pod_port']}/job/cancel"
        try:
            async with httpx.AsyncClient(timeout=130.0) as client:
                await client.post(
                    agent_url,
                    json={"reason": f"Parent job {job_id} cancelled"},
                )
        except Exception as e:
            logger.warning(f"Could not reach agent to cancel child {child_id}: {e}")

    async def _cleanup_child(child: dict) -> None:
        child_id = str(child["id"])
        vm_ctx = _get_vm_context(child)
        if vm_ctx:
            try:
                await vm_provisioner.send_control(child_id, "terminate")
            except Exception as e:
                logger.warning(f"VM terminate signal failed for child {child_id}: {e}")
        try:
            await _archive_and_cleanup_workspace(child_id)
        except Exception as e:
            logger.warning(f"Workspace cleanup failed for child {child_id}: {e}")

    await asyncio.gather(
        *[_signal_cancel(c) for c in children],
        *[_cleanup_child(c) for c in children],
        return_exceptions=True,
    )

    # Bulk cancel in DB
    child_ids = [str(c["id"]) for c in children]
    async with postgres_db.acquire() as conn:
        await conn.execute(
            """
            UPDATE jobs
            SET status = 'cancelled',
                assigned_agent_id = NULL,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ANY($1::uuid[])
              AND status NOT IN ('completed', 'cancelled')
            """,
            child_ids,
        )

    logger.info(f"Cascade-cancelled {len(child_ids)} descendant(s) of job {job_id}")


async def _cascade_pause_to_children(job_id: str) -> None:
    """Pause all processing descendant jobs of a parent.

    Only actively-processing children are signaled and paused in DB.
    Non-processing children (created, waiting) are implicitly held by
    the dispatcher's ancestor guard.
    """
    children = await postgres_db.get_descendant_jobs(job_id)
    processing = [c for c in children if c["status"] == "processing"]
    if not processing:
        return

    async def _signal_pause(child: dict) -> None:
        child_id = str(child["id"])
        agent_id = child.get("assigned_agent_id")
        if not agent_id:
            return
        agent = await postgres_db.get_agent(str(agent_id))
        if not agent or not agent.get("pod_ip") or agent["status"] == "offline":
            return
        agent_url = f"http://{agent['pod_ip']}:{agent['pod_port']}/job/pause"
        try:
            async with httpx.AsyncClient(timeout=130.0) as client:
                await client.post(agent_url)
        except Exception as e:
            logger.warning(f"Could not reach agent to pause child {child_id}: {e}")

        vm_ctx = _get_vm_context(child)
        if vm_ctx:
            try:
                await vm_provisioner.send_control(child_id, "freeze")
            except Exception as e:
                logger.warning(f"VM freeze failed for child {child_id}: {e}")

        await postgres_db.pause_job(child_id)

    await asyncio.gather(
        *[_signal_pause(c) for c in processing],
        return_exceptions=True,
    )

    logger.info(
        f"Cascade-paused {len(processing)} processing descendant(s) of job {job_id}"
    )


@app.put("/api/jobs/{job_id}/cancel")
async def cancel_job(request: Request, job_id: str) -> dict[str, str]:
    """Cancel a running job. **Dual-callable** (P4b): cockpit user with job
    access (``require_job_access``) OR agent with valid ``X-Internal-Key``
    (agent's `cancel_worker_job` tool path).

    If the job is assigned to an agent, this will also send a cancel request
    to the agent pod.
    """
    _, job = await require_internal_or_job_access(request, postgres_db, job_id)
    try:
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
                                logger.info(
                                    f"Agent confirmed graceful cancel for job {job_id}"
                                )
                            else:
                                logger.warning(
                                    f"Agent hard-killed job {job_id} after cooperative timeout"
                                )
                        elif response.status_code == 408:
                            logger.warning(
                                f"Agent cancel timed out for job {job_id} — may still stop after current node"
                            )
                        else:
                            logger.warning(
                                f"Agent cancel returned {response.status_code}: {response.text}"
                            )
                except Exception as e:
                    # Agent might be unreachable — still cancel in DB
                    logger.warning(f"Could not reach agent to cancel job {job_id}: {e}")

        # Send terminate signal to VM management daemon (if applicable)
        vm_ctx = _get_vm_context(job)
        if vm_ctx:
            await vm_provisioner.send_control(job_id, "terminate")

        # Archive workspace (snapshot to S3) and clean up VM/container
        try:
            await _archive_and_cleanup_workspace(job_id)
        except Exception as e:
            logger.warning(
                "Workspace cleanup failed for cancelled job %s: %s", job_id, e
            )

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

        # Cascade cancel to all child/subjobs
        await _cascade_cancel_to_children(job_id)

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
async def pause_job(request: Request, job_id: str) -> dict[str, str]:
    """Pause a running job. **Dual-callable** (P4b): cockpit user with
    job access OR agent with ``X-Internal-Key`` (`pause_worker_job` tool).

    If the job is assigned to an agent, sends a graceful pause request
    to the agent pod. The agent finishes its current graph node, saves
    the checkpoint, and becomes available for new work.

    The paused job re-enters the dispatch queue and will be auto-resumed
    when an agent becomes available.
    """
    _, job = await require_internal_or_job_access(request, postgres_db, job_id)
    try:
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
                            logger.warning(
                                f"Pause timed out for job {job_id} — will pause after current node"
                            )
                        else:
                            logger.warning(
                                f"Agent pause returned {response.status_code}: {response.text}"
                            )
                except httpx.TimeoutException:
                    logger.warning(f"Timeout sending pause to agent for job {job_id}")
                except Exception as e:
                    logger.warning(f"Could not reach agent to pause job {job_id}: {e}")

        # If job has a VM, send freeze via NATS (requires management daemon)
        vm_ctx = (
            (job.get("context") or {}).get("vm")
            if isinstance(job.get("context"), dict)
            else None
        )
        if vm_ctx:
            await vm_provisioner.send_control(job_id, "freeze")

        # Update DB — the agent also does this, but we ensure it here as fallback
        success = await postgres_db.pause_job(job_id)
        if not success:
            raise HTTPException(
                status_code=400,
                detail="Job cannot be paused (status may have changed)",
            )

        # Cascade pause to processing child/subjobs
        await _cascade_pause_to_children(job_id)

        return {"status": "paused", "job_id": job_id}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.put("/api/jobs/{job_id}/agent-release")
async def agent_release_job(request: Request, job_id: str) -> dict[str, str]:
    """Agent-initiated job release (no agent callback). **Internal** (P4b)
    — requires ``X-Internal-Key``. Ingress strips this path.

    Called by an agent that is shutting down or otherwise releasing a job
    it was working on.  Unlike the regular pause endpoint, this does NOT
    try to contact the agent pod (since the caller *is* the agent).
    It simply sets the job to 'paused' and clears the agent assignment
    so the dispatcher can reassign it.
    """
    await require_internal(request)
    try:
        success = await postgres_db.pause_job(job_id)
        if not success:
            raise HTTPException(
                status_code=400,
                detail="Job cannot be paused (not found or status changed)",
            )
        logger.info(f"Agent released job {job_id} — paused for reassignment")
        _trigger_dispatch()
        return {"status": "paused", "job_id": job_id}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


# =============================================================================
# Agent Messaging Endpoints (Live Communication)
# =============================================================================


class MessageSendRequest(BaseModel):
    """Request body for agent-initiated message send."""

    to: str = Field(
        ...,
        description="Recipient: 'user' for job owner, or display name / email of a project member",
    )
    subject: str = Field(..., max_length=200, description="Subject line")
    message: str = Field(..., max_length=5000, description="Message body (markdown)")
    mode: str = Field("async", description="'async' or 'blocking'")
    thread_id: str | None = Field(
        None, description="Existing thread ID, or null for new thread"
    )
    project_id: str | None = Field(
        None, description="Project ID for member resolution (auto-filled from job)"
    )


class MessageReplyRequest(BaseModel):
    """Request body for human reply to agent message."""

    message: str = Field(..., description="Reply body")
    urgent: bool = Field(False, description="Deliver as immediate interrupt")


def _mask_email(email: str) -> str:
    """Mask email for display: alice@example.com -> a***@example.com"""
    if not email or "@" not in email:
        return email or ""
    local, domain = email.rsplit("@", 1)
    return f"{local[0]}***@{domain}" if len(local) > 1 else f"*@{domain}"


@app.post("/api/jobs/{job_id}/messages/send")
async def send_agent_message(
    req: Request,
    job_id: str,
    request: MessageSendRequest,
) -> dict[str, Any]:
    """Send a message from an agent to a human. **Internal** (P4b) —
    requires ``X-Internal-Key``. Ingress strips this path.

    The Pydantic body keeps its historical name ``request`` to avoid
    churning the body of this long handler; the FastAPI Request handle
    is named ``req`` for the gate call only.

    Resolves recipient from job ownership, checks rate limits, sends
    email, and logs to message_log.
    """
    await require_internal(req)
    try:
        # Validate job exists and has an owner
        job = await postgres_db.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

        user_id = str(job.get("user_id", "")) if job.get("user_id") else None
        if not user_id:
            raise HTTPException(
                status_code=404,
                detail="Job has no associated user. Cannot resolve recipient.",
            )

        # Resolve recipient
        if request.to == "user":
            # Job owner
            user = await postgres_db.get_user(user_id)
            if not user or not user.get("email"):
                raise HTTPException(
                    status_code=404,
                    detail="Job owner has no email address.",
                )
            recipient_email = user["email"]
            recipient_name = user.get("display_name", "User")
        else:
            # Multi-recipient: resolve from project members
            project_id = request.project_id or (
                str(job["project_id"]) if job.get("project_id") else None
            )
            if not project_id:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Cannot resolve recipient '{request.to}': "
                        "job has no project_id. Use to='user' for the job owner."
                    ),
                )
            members = await postgres_db.get_project_members(project_id)
            if not members:
                raise HTTPException(
                    status_code=404,
                    detail="No project members found.",
                )
            # Match by display_name or email (case-insensitive)
            to_lower = request.to.lower()
            match = None
            for m in members:
                if (
                    m.get("email", "").lower() == to_lower
                    or m.get("display_name", "").lower() == to_lower
                ):
                    match = m
                    break
            if not match:
                # Fallback: try external contacts for this project
                ext_contact = await postgres_db.resolve_external_contact(
                    project_id,
                    request.to,
                )
                if ext_contact:
                    recipient_email = ext_contact["email"]
                    recipient_name = ext_contact.get("display_name", "Contact")
                    # External contacts don't have a user_id — keep job owner's
                else:
                    available = ", ".join(m.get("display_name", "?") for m in members)
                    # Also list external contacts
                    ext_contacts = await postgres_db.get_external_contacts(project_id)
                    if ext_contacts:
                        ext_names = ", ".join(
                            c.get("display_name", "?") for c in ext_contacts
                        )
                        available += f" | External: {ext_names}"
                    raise HTTPException(
                        status_code=404,
                        detail=(
                            f"Recipient '{request.to}' not found among project members or external contacts. "
                            f"Available: {available}"
                        ),
                    )
            else:
                recipient_email = match["email"]
                recipient_name = match.get("display_name", "User")
                user_id = str(match["user_id"])

        # Check rate limits
        limits = await postgres_db.check_message_rate_limit(job_id, user_id)
        if limits["job_hourly"] >= 5:
            await postgres_db.log_message(
                job_id=job_id,
                thread_id=request.thread_id or "?",
                direction="outbound",
                subject=request.subject,
                message=request.message,
                status="rate_limited",
                user_id=user_id,
                mode=request.mode,
                error_message="Rate limit: 5 per hour per job",
            )
            return JSONResponse(
                status_code=429,
                content={
                    "status": "rate_limited",
                    "error": "Rate limit exceeded: 5 messages per hour per job",
                    "retry_after_seconds": 3600,
                },
            )
        if limits["job_daily"] >= 15:
            await postgres_db.log_message(
                job_id=job_id,
                thread_id=request.thread_id or "?",
                direction="outbound",
                subject=request.subject,
                message=request.message,
                status="rate_limited",
                user_id=user_id,
                mode=request.mode,
                error_message="Rate limit: 15 per day per job",
            )
            return JSONResponse(
                status_code=429,
                content={
                    "status": "rate_limited",
                    "error": "Rate limit exceeded: 15 messages per 24 hours per job",
                    "retry_after_seconds": 86400,
                },
            )
        if limits["user_daily"] >= 30:
            return JSONResponse(
                status_code=429,
                content={
                    "status": "rate_limited",
                    "error": "Rate limit exceeded: 30 messages per 24 hours per user",
                    "retry_after_seconds": 86400,
                },
            )

        # Generate thread_id if not provided
        thread_id = request.thread_id or secrets.token_hex(3)

        # Get sequence number
        sequence = await postgres_db.get_message_sequence(job_id, thread_id)

        # Dispatch to all configured notification channels
        dispatch_results = await notification_service.dispatch(
            user_id=user_id,
            job_id=job_id,
            subject=request.subject,
            message_md=request.message,
            job_description=job.get("description", "")[:100],
            config_name=job.get("config_name", "default"),
            thread_id=thread_id,
            recipient_email=recipient_email,
            recipient_name=recipient_name,
        )

        email_sent = dispatch_results.get("email", False)
        email_msg_id = dispatch_results.get("email_message_id")
        status = "sent"  # Message logged even if delivery fails
        error_msg = None if email_sent else "Email not configured or send failed"

        # Log to message_log
        await postgres_db.log_message(
            job_id=job_id,
            user_id=user_id,
            thread_id=thread_id,
            direction="outbound",
            recipient_email=recipient_email,
            subject=request.subject,
            message=request.message,
            mode=request.mode,
            status=status,
            error_message=error_msg,
            email_message_id=email_msg_id,
        )

        # If blocking mode, update job status and store freeze data
        if request.mode == "blocking":
            freeze_data = {
                "status": "waiting_for_reply",
                "freeze_type": "blocking_message",
                "thread_id": thread_id,
                "subject": request.subject,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "job_id": job_id,
            }
            await postgres_db.update_job_status(
                job_id=job_id,
                status="waiting_for_reply",
                freeze_data=freeze_data,
            )

        file_path = f"messages/{thread_id}/{sequence:03d}_sent.md"

        return {
            "status": "sent",
            "thread_id": thread_id,
            "sequence": sequence,
            "file_path": file_path,
            "recipient": _mask_email(recipient_email),
            "to_name": recipient_name,
            "email_delivered": email_sent,
            "channels": dispatch_results,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Failed to send agent message for job {job_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


def _format_freeze_notification(
    freeze_type: str,
    freeze_data: dict[str, Any],
    job_id: str,
    config_name: str,
    description: str,
) -> tuple[str, str]:
    """Format notification subject and body for a freeze event."""
    short_id = job_id[:8]

    if freeze_type == "vm_upgrade_required":
        command = freeze_data.get("command", "unknown")
        subject = f"Job {short_id} needs VM upgrade (sudo detected)"
        message_md = (
            f"**Job `{short_id}`** (`{config_name}`) attempted a sudo command "
            f"and needs approval to continue.\n\n"
            f"**Command:** `{command}`\n\n"
            f"**Description:** {description}\n\n"
            f"Approve a VM upgrade or reject to keep the job paused."
        )

    elif freeze_type == "job_complete":
        summary = freeze_data.get("summary", "No summary provided")
        confidence = freeze_data.get("confidence", 0)
        deliverables = freeze_data.get("deliverables", [])
        confidence_str = (
            f"{confidence:.0%}"
            if isinstance(confidence, (int, float))
            else str(confidence)
        )
        deliverables_str = (
            "\n".join(f"- `{d}`" for d in deliverables)
            if deliverables
            else "*(none listed)*"
        )
        subject = f"Job {short_id} completed — review required"
        message_md = (
            f"**Job `{short_id}`** (`{config_name}`) has completed and is awaiting review.\n\n"
            f"**Summary:** {summary}\n\n"
            f"**Confidence:** {confidence_str}\n\n"
            f"**Deliverables:**\n{deliverables_str}"
        )

    elif freeze_type == "budget_exceeded":
        phase_number = freeze_data.get("phase_number", "?")
        reason = freeze_data.get("reason", "Tool call budget exceeded")
        tool_calls = freeze_data.get("tool_calls_this_phase", "?")
        subject = f"Job {short_id} frozen — budget exceeded (phase {phase_number})"
        message_md = (
            f"**Job `{short_id}`** (`{config_name}`) has been frozen because "
            f"the tool call budget was exceeded.\n\n"
            f"**Phase:** #{phase_number}\n"
            f"**Tool calls this phase:** {tool_calls}\n"
            f"**Reason:** {reason}\n\n"
            f"**Description:** {description}"
        )

    else:
        subject = f"Job {short_id} frozen — {freeze_type}"
        message_md = (
            f"**Job `{short_id}`** (`{config_name}`) has frozen with type "
            f"`{freeze_type}` and requires attention.\n\n"
            f"**Description:** {description}"
        )

    return subject, message_md


async def _notify_operator_freeze(
    job: dict[str, Any],
    job_id: str,
    freeze_type: str,
    freeze_data: dict[str, Any],
    sudo_request_id: str | None = None,
) -> None:
    """Send operator notification when a job freezes for human action."""
    user_id = str(job["user_id"]) if job.get("user_id") else None
    if not user_id:
        logger.debug(f"Job {job_id} has no user_id — skipping freeze notification")
        return

    user = await postgres_db.get_user(user_id)
    if not user:
        logger.debug(f"User {user_id} not found — skipping freeze notification")
        return

    recipient_email = user.get("email")
    recipient_name = user.get("display_name", "User")
    config_name = job.get("config_name", "default")
    description = (job.get("description") or "")[:100]

    subject, message_md = _format_freeze_notification(
        freeze_type=freeze_type,
        freeze_data=freeze_data,
        job_id=job_id,
        config_name=config_name,
        description=description,
    )

    await notification_service.dispatch(
        user_id=user_id,
        job_id=job_id,
        subject=subject,
        message_md=message_md,
        job_description=description,
        config_name=config_name,
        recipient_email=recipient_email,
        recipient_name=recipient_name,
        sudo_request_id=sudo_request_id,
    )
    logger.info(f"Freeze notification sent for job {job_id} ({freeze_type})")


async def _route_inbound_reply(
    job_id: str,
    thread_id: str,
    message: str,
    sender_email: str | None = None,
    email_message_id: str | None = None,
    urgent: bool = False,
) -> tuple[str, int]:
    """Route an inbound reply to the correct job/thread.

    Shared by the cockpit reply endpoint and the IMAP poller.

    Args:
        job_id: Target job UUID
        thread_id: Target thread ID
        message: Reply body
        sender_email: Sender's email (for user resolution, IMAP only)
        email_message_id: RFC822 Message-ID for dedup (IMAP only)
        urgent: Whether the reply should interrupt immediately

    Returns:
        Tuple of (delivery_strategy, sequence_number).

    Raises:
        ValueError: If the job is not found.
    """
    job = await postgres_db.get_job(job_id)
    if not job:
        raise ValueError(f"Job '{job_id}' not found")

    # Resolve user_id from sender email or job owner
    user_id = None
    if sender_email:
        async with postgres_db.acquire() as conn:
            user_row = await conn.fetchrow(
                "SELECT id FROM users WHERE email = $1",
                sender_email,
            )
        if user_row:
            user_id = str(user_row["id"])
    if not user_id:
        user_id = str(job.get("user_id", "")) if job.get("user_id") else None

    # Get sequence number
    sequence = await postgres_db.get_message_sequence(job_id, thread_id)

    # Log inbound message
    await postgres_db.log_message(
        job_id=job_id,
        user_id=user_id,
        thread_id=thread_id,
        direction="inbound",
        subject="(reply)",
        message=message,
        status="delivered",
        email_message_id=email_message_id,
    )

    # Check if job is waiting for a reply on this thread
    job_status = job.get("status", "")
    freeze_data = job.get("freeze_data")
    if isinstance(freeze_data, str):
        try:
            freeze_data = json.loads(freeze_data)
        except json.JSONDecodeError:
            freeze_data = None

    is_blocking_reply = (
        job_status == "waiting_for_reply"
        and freeze_data
        and freeze_data.get("thread_id") == thread_id
    )

    if is_blocking_reply:
        await _internal_resume_job(job_id, feedback=message)
        return "immediate_resume", sequence

    # Look up user delivery preferences
    user_prefs = {}
    if user_id:
        try:
            user_settings = await postgres_db.get_user_settings(user_id)
            user_prefs = (
                (user_settings or {}).get("communication", {}).get("delivery", {})
            )
        except Exception:
            pass  # Non-critical — fall back to defaults

    # Check urgent flag (explicit from cockpit, or user preference)
    urgent_override = user_prefs.get("urgent_override", True)
    if urgent and urgent_override:
        await _internal_resume_job(job_id, feedback=message)
        return "immediate_interrupt", sequence

    # Check user's async reply preference
    async_pref = user_prefs.get("async_reply", "next_strategic_phase")
    if async_pref == "immediate_interrupt":
        await _internal_resume_job(job_id, feedback=message)
        return "immediate_interrupt", sequence

    # LLM triage: let auxiliary model decide interrupt vs queue
    if async_pref == "llm_triage" and job.get("status") == "processing":
        try:
            from services.message_triage import triage_message

            decision = await triage_message(
                message=message,
                job_status=job.get("status", ""),
                job_description=job.get("description", ""),
                phase_number=job.get("phase_number"),
                db=postgres_db,
            )
            if decision.get("action") == "interrupt":
                await _internal_resume_job(job_id, feedback=message)
                logger.info(
                    "LLM triage: interrupt job %s — %s",
                    job_id[:8],
                    decision.get("reason", ""),
                )
                return "llm_triage_interrupt", sequence
        except Exception as e:
            logger.warning("LLM triage failed, falling through to queue: %s", e)

    # Default: queue for next strategic phase
    job_context = job.get("context") or {}
    if isinstance(job_context, str):
        try:
            job_context = json.loads(job_context)
        except json.JSONDecodeError:
            job_context = {}
    queued_replies = job_context.get("queued_replies", [])
    queued_replies.append(
        {
            "thread_id": thread_id,
            "message": message,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    )
    job_context["queued_replies"] = queued_replies
    await postgres_db.update_job_context(job_id, job_context)

    # Broadcast reply_delivered to cockpit SSE
    try:
        from services.notification_feed import notification_feed

        job_owner_id = str(job.get("user_id", "")) if job.get("user_id") else None
        if job_owner_id:
            notification_feed.broadcast(
                user_id=job_owner_id,
                event_type="reply_delivered",
                data={"job_id": job_id, "thread_id": thread_id},
            )
    except Exception:
        pass  # Non-critical

    return "next_strategic_phase", sequence


@app.post("/api/jobs/{job_id}/messages/{thread_id}/reply")
async def reply_to_agent_message(
    request: Request,
    job_id: str,
    thread_id: str,
    body: MessageReplyRequest,
) -> dict[str, Any]:
    """Reply to an agent's message (cockpit UI or IMAP).

    If the job is in 'waiting_for_reply' status and the thread matches,
    resumes the job with the reply as feedback. Otherwise, queues the reply
    for the next strategic phase.
    """
    await require_job_access(request, postgres_db, job_id)
    try:
        delivery_strategy, sequence = await _route_inbound_reply(
            job_id=job_id,
            thread_id=thread_id,
            message=body.message,
            urgent=body.urgent,
        )

        file_path = f"messages/{thread_id}/{sequence:03d}_received.md"

        return {
            "status": "delivered",
            "sequence": sequence,
            "file_path": file_path,
            "delivery_strategy": delivery_strategy,
        }

    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(
            f"Failed to deliver reply for job {job_id} thread {thread_id}: {e}"
        )
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/jobs/{job_id}/messages")
async def list_message_threads(request: Request, job_id: str) -> dict[str, Any]:
    """List message threads for a job."""
    _, job = await require_job_access(request, postgres_db, job_id)
    try:
        threads = await postgres_db.get_message_threads(job_id)

        # Enrich with job freeze status
        freeze_data = job.get("freeze_data")
        if isinstance(freeze_data, str):
            try:
                freeze_data = json.loads(freeze_data)
            except json.JSONDecodeError:
                freeze_data = None

        waiting_thread = None
        if job.get("status") == "waiting_for_reply" and freeze_data:
            waiting_thread = freeze_data.get("thread_id")

        for thread in threads:
            thread["status"] = (
                "waiting_for_reply"
                if thread["thread_id"] == waiting_thread
                else "active"
            )

        return {"threads": threads}

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Failed to list message threads for job {job_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


# =============================================================================
# Thread Detail & Action Center Endpoints
# =============================================================================


@app.get("/api/jobs/{job_id}/messages/{thread_id}")
async def get_thread_detail(
    request: Request, job_id: str, thread_id: str
) -> dict[str, Any]:
    """Get full ordered messages within a thread."""
    _, job = await require_job_access(request, postgres_db, job_id)
    try:
        thread = await postgres_db.get_thread_messages(job_id, thread_id)
        if not thread:
            raise HTTPException(
                status_code=404,
                detail=f"Thread '{thread_id}' not found for job '{job_id}'",
            )

        # Enrich with job freeze status
        freeze_data = job.get("freeze_data")
        if isinstance(freeze_data, str):
            try:
                freeze_data = json.loads(freeze_data)
            except json.JSONDecodeError:
                freeze_data = None

        if (
            job.get("status") == "waiting_for_reply"
            and freeze_data
            and freeze_data.get("thread_id") == thread_id
        ):
            thread["status"] = "waiting_for_reply"
        else:
            thread["status"] = "active"

        return thread

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(
            f"Failed to get thread detail for job {job_id} thread {thread_id}: {e}"
        )
        raise HTTPException(status_code=500, detail=str(e)) from e


# Cache key is the caller's user id (or "__admin__" for the unfiltered admin
# path). 5s TTL keeps the cockpit's polling cheap without leaking other
# users' counts across cache slots.
_pending_actions_cache: dict[str, dict[str, Any]] = {}


@app.get("/api/actions/pending")
async def get_pending_actions(request: Request) -> dict[str, Any]:
    """Get counts of pending actions visible to the caller. Cached 5s per user.

    **P4e** — pre-fix this was anonymous and returned global counts AND the
    most-urgent sudo's command string. Now caller must be approved, and
    non-admins see only their own / project-member jobs.
    """
    import time

    caller = await require_approved_user(request, postgres_db)
    is_admin = bool(caller.get("is_admin"))
    cache_key = "__admin__" if is_admin else str(caller["id"])

    now = time.monotonic()
    cached = _pending_actions_cache.get(cache_key)
    if cached and now < cached["expires_at"]:
        return cached["data"]

    try:
        if is_admin:
            data = await postgres_db.get_pending_action_counts()
        else:
            projects = await postgres_db.get_projects_for_user(str(caller["id"]))
            project_ids = [str(p["id"]) for p in projects]
            data = await postgres_db.get_pending_action_counts(
                owner_user_id=str(caller["id"]),
                visible_project_ids=project_ids,
            )
        _pending_actions_cache[cache_key] = {
            "data": data,
            "expires_at": now + 5.0,
        }
        return data
    except Exception as e:
        logger.exception(f"Failed to get pending action counts: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


# =============================================================================
# External Contacts Endpoints (Phase 3 Live Communication)
# =============================================================================


class ExternalContactCreate(BaseModel):
    """Request body for adding an external contact."""

    display_name: str = Field(..., max_length=200)
    email: str = Field(..., max_length=320)


@app.post("/api/projects/{project_id}/contacts")
async def add_external_contact(
    project_id: str,
    body: ExternalContactCreate,
    request: Request,
) -> dict[str, Any]:
    """Add an external contact to a project. Requires editor or higher."""
    await require_project_member(request, postgres_db, project_id, min_role="editor")
    try:
        # Basic email format validation
        if "@" not in body.email or "." not in body.email.split("@")[-1]:
            raise HTTPException(status_code=400, detail="Invalid email format")

        contact = await postgres_db.add_external_contact(
            project_id=project_id,
            display_name=body.display_name,
            email=body.email,
        )
        return {
            "status": "created",
            "contact": {
                "id": str(contact["id"]),
                "display_name": contact["display_name"],
                "email": _mask_email(contact["email"]),
                "created_at": contact["created_at"].isoformat()
                if contact.get("created_at")
                else None,
            },
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Failed to add external contact: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/projects/{project_id}/contacts")
async def list_external_contacts(request: Request, project_id: str) -> dict[str, Any]:
    """List external contacts for a project."""
    await require_project_member(request, postgres_db, project_id)
    try:
        contacts = await postgres_db.get_external_contacts(project_id)
        return {
            "contacts": [
                {
                    "id": str(c["id"]),
                    "display_name": c["display_name"],
                    "email": _mask_email(c["email"]),
                    "created_at": c["created_at"].isoformat()
                    if c.get("created_at")
                    else None,
                }
                for c in contacts
            ],
        }
    except Exception as e:
        logger.exception(f"Failed to list external contacts: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.delete("/api/projects/{project_id}/contacts/{contact_id}")
async def delete_external_contact(
    request: Request, project_id: str, contact_id: str
) -> dict[str, str]:
    """Delete an external contact. Requires editor or higher."""
    await require_project_member(request, postgres_db, project_id, min_role="editor")
    try:
        deleted = await postgres_db.delete_external_contact(contact_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Contact not found")
        return {"status": "deleted"}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Failed to delete external contact: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


# =============================================================================
# Notification Feed Endpoints (Phase 3 Live Communication)
# =============================================================================


@app.get("/api/notifications")
async def list_notifications(
    request: Request,
    limit: int = Query(50, le=200),
    unread_only: bool = Query(False),
) -> dict[str, Any]:
    """List notifications for the current user."""
    try:
        user = await require_approved_user(request, postgres_db)
        user_id = str(user["id"])
        notifications = await postgres_db.get_user_notifications(
            user_id,
            limit=limit,
            unread_only=unread_only,
        )
        unread_count = await postgres_db.get_unread_count(user_id)

        return {
            "notifications": [
                {
                    "id": str(n["id"]),
                    "job_id": str(n["job_id"]) if n.get("job_id") else None,
                    "thread_id": n.get("thread_id"),
                    "subject": n.get("subject"),
                    "message": (n.get("message") or "")[:200],
                    "job_description": (n.get("job_description") or "")[:80],
                    "config_name": n.get("config_name"),
                    "status": n.get("status"),
                    "read_at": n["read_at"].isoformat() if n.get("read_at") else None,
                    "created_at": n["created_at"].isoformat()
                    if n.get("created_at")
                    else None,
                }
                for n in notifications
            ],
            "unread_count": unread_count,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Failed to list notifications: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.patch("/api/notifications/{notification_id}")
async def mark_notification_read(
    notification_id: str,
    request: Request,
) -> dict[str, Any]:
    """Mark a notification as read."""
    try:
        user = await require_approved_user(request, postgres_db)
        user_id = str(user["id"])
        updated = await postgres_db.mark_notification_read(notification_id, user_id)
        if not updated:
            raise HTTPException(
                status_code=404, detail="Notification not found or already read"
            )
        return {"status": "read"}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Failed to mark notification read: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/notifications/events")
async def notification_sse_events(request: Request) -> StreamingResponse:
    """SSE endpoint for real-time notification updates.

    Clients connect via EventSource to receive live notification events.
    Events: new_message, reply_delivered.
    """
    from services.notification_feed import notification_feed

    try:
        user = await require_approved_user(request, postgres_db)
        user_id = str(user["id"])
    except Exception:
        # Allow unauthenticated connections for development
        user_id = "anonymous"

    queue = notification_feed.subscribe_sse(user_id)

    async def event_stream():
        try:
            # Kickstart: flush immediately so EventSource.onopen fires at once and
            # buffering proxies (Cloudflare Tunnel, Traefik) don't idle-timeout
            # before the first byte — otherwise the next byte is the 30s keepalive
            # below. Comments (`:`-prefixed) are ignored by EventSource.
            yield ": open\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=30)
                    yield f"data: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                except asyncio.CancelledError:
                    break
        finally:
            notification_feed.unsubscribe_sse(user_id, queue)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# =============================================================================
# VM Lifecycle Endpoints (optional — requires NATS)
# =============================================================================


@app.post("/api/vms")
async def create_vm(request: Request, body: VMCreateRequest) -> dict[str, Any]:
    """Create a VM for a job.

    **P4f** — gated by `require_job_access` on ``body.job_id``. VM
    provisioning is job-scoped, so callers must already be able to see
    the job. Admins (and project members) inherit access via the gate.

    Uses NATS (cross-cluster) or direct Kubernetes API (same-cluster).
    Returns 503 if no VM provisioning backend is available.
    """
    await require_job_access(request, postgres_db, body.job_id)
    if not vm_provisioner.is_available:
        raise HTTPException(
            status_code=503, detail="VM provisioning not available (no NATS or K8s)"
        )

    success = await vm_provisioner.create_vm(
        job_id=body.job_id,
        agent_config=body.agent_config,
        vm_image=body.vm_image,
        cpu_cores=body.cpu_cores,
        memory=body.memory,
        description=body.description,
    )
    if not success:
        raise HTTPException(status_code=500, detail="Failed to create VM")

    return {
        "status": "provisioning",
        "job_id": body.job_id,
        "mode": vm_provisioner.mode,
    }


@app.get("/api/vms")
async def list_vms(request: Request) -> list[dict[str, Any]]:
    """List jobs with active VMs. **Admin only** (P4d) — lists VMs across
    all users; the per-job VM detail/lifecycle endpoints stay job-scoped
    under P4f.

    Works from the database (no NATS required) — reads the 'vm' key from
    each job's context JSONB column.
    """
    await _require_admin(request)
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
    request: Request,
    job_id: str,
    live: bool = Query(False, description="Query live status via NATS request/reply"),
) -> dict[str, Any]:
    """Get VM status for a job.

    **P4f** — gated by `require_job_access`. VM status leaks pod/IP/host
    details, so caller must already be able to see the job.

    By default reads from the database. With ?live=true, also queries the VM
    controller via NATS request/reply for real-time status.
    """
    _, job = await require_job_access(request, postgres_db, job_id)

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
async def delete_vm(request: Request, job_id: str) -> dict[str, str]:
    """Delete a VM for a job.

    **P4f** — destructive. Caller must own the job OR be project-owner OR
    admin (mirrors `DELETE /api/jobs/{job_id}` from P4c). Plain project
    membership isn't enough.

    Uses NATS (cross-cluster) or direct Kubernetes API (same-cluster).
    Returns 503 if no VM provisioning backend is available.
    """
    caller, job = await require_job_access(request, postgres_db, job_id)
    if not caller.get("is_admin"):
        is_job_owner = str(job.get("user_id") or "") == str(caller["id"])
        is_project_owner = False
        if not is_job_owner and job.get("project_id"):
            role = await postgres_db.get_user_role_in_project(
                str(job["project_id"]), str(caller["id"])
            )
            is_project_owner = role == "owner"
        if not (is_job_owner or is_project_owner):
            raise HTTPException(
                status_code=403,
                detail="Only the job owner, the project owner, or an admin may delete this VM",
            )

    if not vm_provisioner.is_available:
        raise HTTPException(
            status_code=503, detail="VM provisioning not available (no NATS or K8s)"
        )

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

    F6: per-user filtering. Admins see every event; non-admins see only
    events for jobs they can access (owner OR project member). Orphan
    events with no ``job_id`` are admin-only. Filtering is applied per
    event inside the stream rather than at connect time so a member who
    later gains access doesn't have to reconnect.
    """
    user = await require_approved_user(request, postgres_db)
    queue = sudo_gate.subscribe_sse()

    async def event_stream():
        try:
            # Kickstart: flush immediately so EventSource.onopen fires at once and
            # buffering proxies (Cloudflare Tunnel, Traefik) don't idle-timeout
            # before the first byte — otherwise the next byte is the 30s keepalive
            # below. Comments (`:`-prefixed) are ignored by EventSource.
            yield ": open\n\n"
            while True:
                # Check if client disconnected
                if await request.is_disconnected():
                    break
                try:
                    event_type, data = await asyncio.wait_for(queue.get(), timeout=30.0)
                    job_id = data.get("job_id") if isinstance(data, dict) else None
                    if not await user_can_access_job(user, postgres_db, job_id):
                        # Silently drop — the caller isn't authorized to
                        # see this event. We don't reveal existence.
                        continue
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
    request: Request,
    job_id: str | None = Query(None, description="Filter by job ID"),
    status: str | None = Query(None, description="Filter by status"),
    request_type: str | None = Query(
        None, description="Filter by type (sudo_command, vm_upgrade)"
    ),
    limit: int = Query(50, ge=1, le=200),
) -> list[dict]:
    """List sudo approval requests visible to the caller (G3).

    With ``?job_id=``: gate on ``require_job_access``. Without: admins
    see the full feed; non-admins receive only requests whose underlying
    job they can access (post-fetch filter).
    """
    if job_id:
        await require_job_access(request, postgres_db, job_id)
        return await sudo_gate.list_requests(
            job_id=job_id,
            status=status,
            request_type=request_type,
            limit=limit,
        )

    caller = await require_approved_user(request, postgres_db)
    rows = await sudo_gate.list_requests(
        job_id=None,
        status=status,
        request_type=request_type,
        limit=limit,
    )
    if caller.get("is_admin") and mcp_scope_project_id(caller) is None:
        return rows
    visible: list[dict] = []
    for row in rows:
        if await user_can_access_job(caller, postgres_db, row.get("job_id")):
            visible.append(row)
    return visible


@app.get("/api/sudo/requests/{request_id}")
async def get_sudo_request(request: Request, request_id: str) -> dict:
    """Get a single sudo approval request (G3: caller must access the underlying job)."""
    caller = await require_approved_user(request, postgres_db)
    result = await sudo_gate.get_request(request_id)
    if not result:
        raise HTTPException(
            status_code=404, detail=f"Sudo request '{request_id}' not found"
        )
    if not await user_can_access_job(caller, postgres_db, result.get("job_id")):
        raise HTTPException(
            status_code=403, detail="Not authorized to access this sudo request"
        )
    return result


@app.post("/api/sudo/requests/{request_id}/approve")
async def approve_sudo_request(
    request_id: str,
    request: Request,
    body: SudoApproveRequest | None = None,
) -> dict:
    """Approve a pending sudo request. Caller must be project owner of the related job, or admin."""
    # H4: pre-fix, any authenticated user could approve any job's
    # privileged shell command — a trust escalation, not just info leak.
    await require_sudo_request_authority(request, postgres_db, request_id)
    reason = body.reason if body else ""
    result = await sudo_gate.approve_request(request_id, reason=reason)
    if not result:
        raise HTTPException(
            status_code=404, detail=f"Sudo request '{request_id}' not found"
        )
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@app.post("/api/sudo/requests/{request_id}/deny")
async def deny_sudo_request(
    request_id: str, body: SudoDenyRequest, request: Request
) -> dict:
    """Deny a pending sudo request. Caller must be project owner of the related job, or admin."""
    await require_sudo_request_authority(request, postgres_db, request_id)
    result = await sudo_gate.deny_request(request_id, reason=body.reason)
    if not result:
        raise HTTPException(
            status_code=404, detail=f"Sudo request '{request_id}' not found"
        )
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@app.post("/api/sudo/requests/{request_id}/approve-upgrade")
async def approve_sudo_vm_upgrade(
    request_id: str,
    request: Request,
    body: SudoApproveRequest | None = None,
) -> dict:
    """Approve a vm_upgrade sudo request — provisions a VM and resumes the job. Caller must be project owner of the related job, or admin."""
    await require_sudo_request_authority(request, postgres_db, request_id)
    reason = body.reason if body else "VM upgrade approved"
    result = await sudo_gate.approve_request(request_id, reason=reason)
    if not result:
        raise HTTPException(
            status_code=404, detail=f"Sudo request '{request_id}' not found"
        )
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    job_id = result.get("job_id")
    if not job_id:
        raise HTTPException(status_code=400, detail="No job_id in sudo request")
    return await upgrade_job_to_vm(str(job_id))


@app.post("/api/sudo/requests/{request_id}/resume-without-vm")
async def resume_sudo_without_vm(
    request_id: str,
    request: Request,
    body: SudoApproveRequest | None = None,
) -> dict:
    """Approve a vm_upgrade request but resume without provisioning a VM. Caller must be project owner of the related job, or admin."""
    await require_sudo_request_authority(request, postgres_db, request_id)
    reason = body.reason if body else "Resume without VM"
    result = await sudo_gate.approve_request(request_id, reason=reason)
    if not result:
        raise HTTPException(
            status_code=404, detail=f"Sudo request '{request_id}' not found"
        )
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    job_id = result.get("job_id")
    if not job_id:
        raise HTTPException(status_code=400, detail="No job_id in sudo request")
    return await approve_job(str(job_id))


@app.get("/api/sudo/rules")
async def list_sudo_rules(request: Request) -> list[dict]:
    """List auto-approval rules. **Admin only** (P4d) — global pattern rules."""
    await _require_admin(request)
    return await sudo_gate.list_rules()


@app.post("/api/sudo/rules")
async def create_sudo_rule(request: Request, body: SudoRuleCreateRequest) -> dict:
    """Create an auto-approval rule. **Admin only** (P4d) — global pattern rules."""
    await _require_admin(request)
    if body.action not in ("approve", "deny", "review"):
        raise HTTPException(
            status_code=400, detail="action must be 'approve', 'deny', or 'review'"
        )
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
async def delete_sudo_rule(request: Request, rule_id: str) -> dict:
    """Delete an auto-approval rule. **Admin only** (P4d)."""
    await _require_admin(request)
    if not await sudo_gate.delete_rule(rule_id):
        raise HTTPException(status_code=404, detail=f"Rule '{rule_id}' not found")
    return {"status": "deleted", "id": rule_id}


class JobResumeRequest(BaseModel):
    """Request body for resuming a failed or paused job."""

    feedback: str | None = Field(
        None, description="Optional feedback to inject before resuming"
    )
    agent_id: str | None = Field(
        None, description="Override agent ID if original is offline"
    )


def _resume_reject_should_requeue(status_code: int) -> bool:
    """Whether an agent's rejection of a resume POST should re-queue the job for
    auto-dispatch instead of surfacing a 502.

    A 409 means the agent's DB ``status='ready'`` was stale — its pod is
    actually non-idle (a zombie that leaked ``_pod_state=WORKING`` on a prior
    cancel/pause, or an agent still finishing post-completion work) and refused
    the resume. Re-queuing lets a genuinely-ready agent pick the job up. Any
    other non-2xx is a real failure → 502. See
    docs/done/worker_pod_state_zombie_on_cancel.md.
    """
    return status_code == 409


@app.post("/api/jobs/{job_id}/resume")
async def resume_job(
    req: Request,
    job_id: str,
    request: JobResumeRequest | None = None,
) -> dict[str, str]:
    """Resume a failed or paused job from its checkpoint. **Dual-callable**
    (P4b): cockpit user with job access OR agent with ``X-Internal-Key``
    (autoresume + ``resume_worker_job`` tool). The Pydantic body keeps the
    historical ``request`` name; the FastAPI Request handle is ``req``.

    This endpoint:
    1. Validates the job exists and is not 'completed'
    2. Gets the assigned agent (or uses override agent_id from request)
    3. Validates the agent is ready or completed (not offline/working)
    4. Sends a resume request to the agent's pod
    5. Updates job and agent status on success

    Returns:
        Status message indicating resume result
    """
    _, job = await require_internal_or_job_access(req, postgres_db, job_id)
    if request is None:
        request = JobResumeRequest()

    # Resume PEP (decision 9, B3): re-check the runner's CURRENT grants against the
    # job's stored config before replaying it. Placed before the resume try so a 403
    # is not downgraded by the broad handler below (fail closed on denial). A resolve
    # infra error proceeds — the dispatch-time grant check already passed.
    if await _user_experts_enabled():
        try:
            _rco = job.get("config_override")
            if isinstance(_rco, str):
                _rco = json.loads(_rco)
            _rbase = job.get("config_name") or "defaults"
            if _rbase == "default":
                _rbase = "defaults"
            _rcap: dict = {}
            resolve_config(
                base_config_name=_rbase,
                base_defaults=await _resolve_default_models(job.get("user_id")),
                expert_row=(
                    await postgres_db.get_expert_by_id(str(job["expert_id"]))
                    if job.get("expert_id")
                    else None
                ),
                request_override=_rco,
                expert_type="worker",
                capture=_rcap,
            )
            await _enforce_dispatch_grants(
                _rcap["merged_fragment"],
                runner_user_id=str(job["user_id"]) if job.get("user_id") else None,
                project_ids=[str(job["project_id"])] if job.get("project_id") else [],
            )
        except GrantDenied as gd:
            logger.warning("Resume denied for job %s: %s", job_id, gd)
            raise HTTPException(
                status_code=403, detail=_grant_violations_detail(gd.violations)
            )
        except Exception:
            logger.exception(
                "Resume PEP: grant re-check failed for job %s; proceeding "
                "(dispatch-time check stands)",
                job_id,
            )

    try:
        # Allow resuming jobs in any status except completed
        # This handles cancelled jobs (user wants to retry) and cases where
        # agents disappear without marking jobs as failed
        if job["status"] == "completed":
            raise HTTPException(
                status_code=400,
                detail=f"Job cannot be resumed (status: {job['status']}).",
            )

        async def _queue_for_dispatch(message: str) -> dict[str, str]:
            """Park the job as 'paused' (dispatchable, unassigned) and kick the
            auto-dispatcher. Used both when no agent is ready and when the
            picked agent rejects the resume (its DB 'ready' was stale). Stashes
            feedback into context so it survives until a real agent picks it up.
            """
            feedback = request.feedback if request else None
            if feedback:
                ctx = job.get("context") or {}
                if isinstance(ctx, str):
                    try:
                        ctx = json.loads(ctx)
                    except json.JSONDecodeError:
                        ctx = {}
                ctx["queued_feedback"] = feedback
                async with postgres_db.acquire() as conn:
                    await conn.execute(
                        "UPDATE jobs SET context = $1::jsonb WHERE id = $2::uuid",
                        json.dumps(ctx),
                        job_id,
                    )
            async with postgres_db.acquire() as conn:
                await conn.execute(
                    "UPDATE jobs SET status = 'paused', assigned_agent_id = NULL, "
                    "updated_at = CURRENT_TIMESTAMP WHERE id = $1::uuid",
                    job_id,
                )
            logger.info(
                f"Queued job {job_id} for auto-dispatch (previous status: "
                f"{job['status']}, feedback: {bool(feedback)})"
            )
            _trigger_dispatch()
            return {"status": "queued", "message": message, "job_id": job_id}

        # Determine which agent to use
        # Convert to string since DB returns asyncpg UUID objects
        assigned_agent_id = job.get("assigned_agent_id")
        agent_id = request.agent_id or (
            str(assigned_agent_id) if assigned_agent_id else None
        )
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
                # No agent available right now — queue for auto-dispatch and let
                # the dispatcher pick the job up when an agent becomes free.
                return await _queue_for_dispatch(
                    "No agents available, job queued for auto-dispatch"
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
        _apply_cloud_storage_override(resolved_ds, job_context)
        datasources_payload = _build_datasources_payload(resolved_ds)

        # Apply datasource-driven tool override (inject/strip db tool categories)
        if resolved_ds:
            config_override = _build_datasource_tool_override(
                resolved_ds, config_override
            )

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
                    snapshot_restored = (
                        await ide_session_service.restore_snapshot_for_resume(
                            job_id, ssh_host, int(ssh_port)
                        )
                    )
                    if snapshot_restored:
                        logger.info(f"Snapshot restored for job {job_id} resume")
                except Exception as e:
                    logger.warning(
                        f"Snapshot restore failed for job {job_id} resume (non-blocking): {e}"
                    )

        resume_payload = {
            "job_id": job_id,
            "config_name": job_config_name,
            "config_upload_id": job_context.get("config_upload_id")
            if job_context
            else None,
            "config_override": config_override,
            "datasources": datasources_payload,
            "project_id": str(job["project_id"]) if job.get("project_id") else None,
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
            if _resume_reject_should_requeue(response.status_code):
                # The agent's DB 'ready' was stale — its pod is non-idle (a
                # zombie, or still finishing prior work) and rejected the resume
                # with 409. Demote it out of the ready pool and re-queue instead
                # of surfacing a 502 to the user. See
                # docs/done/worker_pod_state_zombie_on_cancel.md.
                logger.warning(
                    f"Agent {agent_id} rejected resume for job {job_id} (409, "
                    f"stale 'ready'); demoting and re-queuing: {response.text}"
                )
                try:
                    async with postgres_db.acquire() as conn:
                        await conn.execute(
                            "UPDATE agents SET status = 'working' "
                            "WHERE id = $1::uuid AND status = 'ready'",
                            agent_id,
                        )
                except Exception as demote_err:
                    logger.warning(
                        f"Could not demote stale agent {agent_id}: {demote_err}"
                    )
                return await _queue_for_dispatch(
                    "Agent was not ready, job re-queued for auto-dispatch"
                )
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

        # Parent resumed — trigger dispatch so paused children become dispatchable
        _trigger_dispatch()

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
async def approve_job(
    req: Request,
    job_id: str,
    request: JobApproveRequest | None = None,
) -> dict[str, Any]:
    """Approve a frozen job, marking it as completed. **Dual-callable** (P4b):
    cockpit user with job access OR agent with ``X-Internal-Key`` (autonomous
    approve flow + ``approve_worker_job`` tool). Body keeps the historical
    ``request`` name; FastAPI Request handle is ``req``.

    This endpoint mirrors the logic from agent.py:approve_frozen_job but runs
    entirely on the orchestrator side — no agent pod needs to be running.

    Steps:
    1. Validates job exists and is in 'pending_review' status
    2. Reads job_frozen.json from the Gitea repo
    3. Writes job_completion.json to the Gitea repo
    4. Removes job_frozen.json from the Gitea repo
    5. Updates DB status to 'completed' with completed_at timestamp
    """
    _, job = await require_internal_or_job_access(req, postgres_db, job_id)
    if request is None:
        request = JobApproveRequest()

    try:
        # 1. Validate status (gate already loaded the job and raised 404 if missing)
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
            workspace_path = workspace_service.base_path / "output" / "job_frozen.json"
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

        if freeze_type in ("phase_boundary", "vm_upgrade_required"):
            # Phase boundary or VM upgrade freeze: approve to continue execution
            # (not complete). For vm_upgrade_required, this is the "resume without
            # VM" path — the agent continues in the container and adapts.
            # Remove job_frozen.json from local workspace
            local_frozen = workspace_service.base_path / "output" / "job_frozen.json"
            if local_frozen.exists():
                local_frozen.unlink()

            # Update DB: status → processing, clear freeze_data
            async with postgres_db.acquire() as conn:
                await conn.execute(
                    "UPDATE jobs SET status = 'processing', freeze_data = NULL, "
                    "updated_at = CURRENT_TIMESTAMP WHERE id = $1::uuid",
                    job_id,
                )

            msg = (
                f"Job {job_id} phase boundary approved (resume execution)"
                if freeze_type == "phase_boundary"
                else f"Job {job_id} vm_upgrade_required approved without VM (resume in container)"
            )
            logger.info(msg)

            return {
                "status": "approved_continue",
                "job_id": job_id,
                "freeze_type": freeze_type,
                "phase_type": frozen_data.get("phase_type"),
                "phase_number": frozen_data.get("phase_number"),
                "command": frozen_data.get("command"),
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
        local_output = workspace_service.base_path / "output"
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

        # Graft subjob output onto parent branch if applicable
        merge_result = None
        if job.get("parent_job_id"):
            merge_result = await _graft_subjob_output(job_id)

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


@app.post("/api/jobs/{job_id}/upgrade-to-vm")
async def upgrade_job_to_vm(request: Request, job_id: str) -> dict[str, Any]:
    """Upgrade a frozen job from container workspace to a VM.

    This endpoint is used when a job freezes with ``freeze_type: vm_upgrade_required``
    (i.e. the agent attempted a sudo command in a hardened container). It:

    1. Validates the job is frozen with the correct freeze type
    2. Sets ``context.vm.requested = true`` so the dispatcher provisions a VM
    3. Clears the freeze data
    4. Sets status to ``paused`` (dispatchable)
    5. Triggers the auto-dispatcher

    The dispatcher then provisions a VM via ``vm_provisioner``, waits for it to
    become ready, and dispatches the job to an agent with ``RemoteBackend`` pointed
    at the VM. The agent resumes from its checkpoint with full sudo access (gated
    by the VM's sudo approval system).

    The original workspace container is NOT deleted immediately — it is cleaned up
    when the job eventually completes or is cancelled (existing cleanup logic).
    """
    await require_job_access(request, postgres_db, job_id)
    try:
        # 1. Validate job
        job = await postgres_db.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

        if job["status"] not in ("pending_review", "reviewing", "paused"):
            raise HTTPException(
                status_code=400,
                detail=f"Job cannot be upgraded (status: {job['status']}). "
                f"Only frozen jobs can be upgraded to VMs.",
            )

        # 2. Read freeze data to validate freeze type
        frozen_data = None
        if job.get("freeze_data"):
            frozen_data = job["freeze_data"]
            if isinstance(frozen_data, str):
                frozen_data = json.loads(frozen_data)

        if frozen_data is None:
            repo_name, job_branch = await resolve_job_repo(job_id)
            if gitea_client.is_initialized:
                frozen_data = await gitea_client.get_file(
                    repo_name, "output/job_frozen.json", ref=job_branch
                )
            if frozen_data is None:
                workspace_path = (
                    workspace_service.base_path / "output" / "job_frozen.json"
                )
                if workspace_path.exists():
                    frozen_data = json.loads(workspace_path.read_text())

        if frozen_data is None:
            raise HTTPException(
                status_code=404,
                detail=f"No freeze data found for job '{job_id}'",
            )

        freeze_type = frozen_data.get("freeze_type")
        if freeze_type != "vm_upgrade_required":
            raise HTTPException(
                status_code=400,
                detail=f"Job freeze type is '{freeze_type}', not 'vm_upgrade_required'. "
                f"Use POST /api/jobs/{job_id}/approve instead.",
            )

        # 3. Check VM provisioner is available
        if not vm_provisioner.is_available:
            raise HTTPException(
                status_code=503,
                detail="VM provisioner is not available. Cannot upgrade to VM.",
            )

        # 4. Set context.vm.requested = true
        job_context = job.get("context") or {}
        if isinstance(job_context, str):
            try:
                job_context = json.loads(job_context)
            except json.JSONDecodeError:
                job_context = {}

        vm_ctx = job_context.setdefault("vm", {})
        vm_ctx["requested"] = True
        vm_ctx["upgrade_from"] = "container"
        vm_ctx["upgrade_command"] = frozen_data.get("command", "")

        # 5. Remove freeze file from local workspace
        local_frozen = workspace_service.base_path / "output" / "job_frozen.json"
        if local_frozen.exists():
            local_frozen.unlink()

        # 6. Update DB: clear freeze, set status to paused (dispatchable),
        #    unassign agent so dispatcher can re-dispatch
        async with postgres_db.acquire() as conn:
            await conn.execute(
                "UPDATE jobs SET context = $1::jsonb, status = 'paused', "
                "freeze_data = NULL, assigned_agent_id = NULL, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = $2::uuid",
                json.dumps(job_context),
                job_id,
            )

        logger.info(
            f"Job {job_id} approved for VM upgrade "
            f"(command={frozen_data.get('command', 'N/A')!r})"
        )

        # 7. Trigger dispatcher — it will provision a VM and dispatch
        _trigger_dispatch()

        return {
            "status": "approved_vm_upgrade",
            "job_id": job_id,
            "freeze_type": freeze_type,
            "command": frozen_data.get("command"),
            "vm_provisioner_mode": vm_provisioner.mode,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Failed to upgrade job {job_id} to VM: {e}")
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
            json.dumps(job_context),
            job_id,
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
        logger.info(
            f"Set target job {target_job_id} to 'pending_review' (autonomy={autonomy})"
        )
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

    # Lite tiers (virtual/none) have no git workspace for the scholar -> parent
    # output graft, so skip the research subjob (§8). The parent agent still
    # researches inline (web/SQL/graph/Mongo/KB all work without a workspace).
    if _is_lite_config_override(config_override):
        logger.info(
            f"Scholar skipped for job {job_id}: lite workspace backend has no "
            f"git workspace for the research-phase graft handoff"
        )
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

    # Inherit parent's workspace backend so subjob runs on the same VM/container
    parent_ctx = job.get("context") or {}
    if isinstance(parent_ctx, str):
        try:
            parent_ctx = json.loads(parent_ctx)
        except (json.JSONDecodeError, ValueError):
            parent_ctx = {}
    if parent_ctx.get("vm"):
        scholar_context["vm"] = parent_ctx["vm"]
    elif parent_ctx.get("workspace_container"):
        scholar_context["workspace_container"] = parent_ctx["workspace_container"]

    # Disable nested subjob spawning on the scholar
    scholar_override: dict[str, Any] = {
        "scholar": {"enabled": False},
        "verification": {"enabled": False},
        "curator": {"enabled": False},
        "autonomy": "full",
    }

    # Propagate parent's LLM override so the scholar uses the same model
    if config_override and isinstance(config_override.get("llm"), dict):
        scholar_override["llm"] = config_override["llm"]

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
        user_id=str(job["user_id"]) if job.get("user_id") else None,
    )

    scholar_job_id = str(scholar_job["id"])
    short_id = scholar_job_id[:8]

    # Inherit the parent's datasource selection (explicit-only resolution).
    await _propagate_datasources_to_subjob(job_id, scholar_job_id)

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

            # Set worktree_path if subjob inherits a workspace backend
            worktree_path = None
            if parent_ctx.get("vm") or parent_ctx.get("workspace_container"):
                worktree_path = f"/home/agent-host/workspace/worktrees/{short_id}-{scholar_config_name}"

            async with postgres_db.acquire() as conn:
                await conn.execute(
                    "UPDATE jobs SET branch_name = $1, repo_name = $2, worktree_path = $3 WHERE id = $4::uuid",
                    branch_name,
                    parent_repo_name,
                    worktree_path,
                    scholar_job_id,
                )
        except Exception as e:
            logger.warning(
                f"Failed to create Gitea branch for scholar {scholar_job_id}: {e}"
            )

    _trigger_dispatch()
    logger.info(f"Scholar job {scholar_job_id} created for parent {job_id}")
    return scholar_job


async def _handle_scholar_completion(
    job: dict[str, Any],
    actions: list[str],
) -> None:
    """After a scholar subjob completes or fails, unblock its parent job.

    The scholar's ``output/`` has already been grafted onto the parent branch by
    ``_graft_subjob_output`` (called earlier in ``complete_job``); here we point
    the parent at that grafted ``outputs/`` folder and transition it from
    'waiting' to 'created' so the dispatcher picks it up.
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
        actions.append(
            f"scholar {job_id} {job_status}, parent {target_id} unblocked (no research)"
        )
    else:
        parent_ctx["scholar_completed"] = True
        # The graft (run earlier in complete_job) wrote graft_output_path to the
        # scholar's DB context; the in-memory `job` here predates that write, so
        # re-fetch to read the freshly-grafted outputs/ path (None if no output).
        fresh = await postgres_db.get_job(job_id)
        fresh_ctx = (fresh or {}).get("context") or {}
        if isinstance(fresh_ctx, str):
            try:
                fresh_ctx = json.loads(fresh_ctx)
            except (json.JSONDecodeError, ValueError):
                fresh_ctx = {}
        parent_ctx["scholar_output_dir"] = (fresh_ctx or {}).get("graft_output_path")
        logger.info(f"Scholar {job_id} completed — unblocking parent {target_id}")
        actions.append(f"scholar {job_id} completed, parent {target_id} unblocked")

    await postgres_db.update_job_context(target_id, parent_ctx)
    await postgres_db.update_job_status(
        target_id, status="created", assigned_agent_id=""
    )
    _trigger_dispatch()


async def _handle_delegation_child_completion(
    job: dict[str, Any],
    actions: list[str],
) -> None:
    """After a delegation child completes, check if all siblings are done.

    Delegation children are identified by having a non-NULL creation_order
    (distinguishes them from critic/scholar subjobs which also use parent_job_id).

    When all siblings reach a terminal status, the parent job is unblocked:
    child results are stored in the parent's context and the parent transitions
    from 'waiting' to 'created' so the dispatcher picks it up for resume.
    """
    parent_job_id = job.get("parent_job_id")
    if parent_job_id is None:
        return

    # Only handle delegation children (have creation_order set)
    if job.get("creation_order") is None:
        return

    job_id = str(job["id"])
    target_id = str(parent_job_id)

    all_done = await postgres_db.all_delegation_children_terminal(target_id)
    if not all_done:
        logger.debug(
            f"Delegation child {job_id} done, but not all siblings terminal yet "
            f"(parent {target_id})"
        )
        return

    parent = await postgres_db.get_job(target_id)
    if not parent:
        logger.warning(f"Delegation child {job_id}: parent {target_id} not found")
        return

    if parent.get("status") != "waiting":
        logger.debug(
            f"Delegation child {job_id}: parent {target_id} not in 'waiting' "
            f"(status={parent.get('status')}) — skipping unblock"
        )
        return

    # Build results summary from children in creation order
    children = await postgres_db.get_delegation_children(target_id)
    child_results = []
    for child in children:
        child_id = str(child["id"])
        child_status = child.get("status", "unknown")

        # Parse freeze_data for summary/confidence
        freeze = child.get("freeze_data")
        if isinstance(freeze, str):
            try:
                freeze = json.loads(freeze)
            except (json.JSONDecodeError, ValueError):
                freeze = {}
        freeze = freeze or {}

        child_ctx = child.get("context") or {}
        if isinstance(child_ctx, str):
            try:
                child_ctx = json.loads(child_ctx)
            except (json.JSONDecodeError, ValueError):
                child_ctx = {}
        child_output_path = (child_ctx or {}).get("graft_output_path")

        child_results.append(
            {
                "job_id": child_id,
                "description": child.get("description", ""),
                "status": child_status,
                "config_name": child.get("config_name", "default"),
                "output_path": child_output_path,
                "creation_order": child.get("creation_order"),
                "branch_name": child.get("branch_name"),
                "worktree_path": child.get("worktree_path"),
                "merge_status": child.get("merge_status"),
                "summary": freeze.get("summary", ""),
                "confidence": freeze.get("confidence", 0.0),
                "deliverables": freeze.get("deliverables", []),
            }
        )

    # Store results in parent context for resume injection
    parent_ctx = parent.get("context") or {}
    if isinstance(parent_ctx, str):
        try:
            parent_ctx = json.loads(parent_ctx)
        except (json.JSONDecodeError, ValueError):
            parent_ctx = {}
    parent_ctx = dict(parent_ctx)
    parent_ctx["delegation_results"] = child_results

    await postgres_db.update_job_context(target_id, parent_ctx)

    # Unblock parent: waiting → paused (dispatcher picks it up via /job/resume,
    # preserving checkpoint state from before delegation).
    await postgres_db.update_job_status(
        target_id, status="paused", assigned_agent_id=""
    )
    _trigger_dispatch()

    completed_count = sum(1 for c in child_results if c["status"] == "completed")
    total_count = len(child_results)
    logger.info(
        f"All {total_count} delegation children done for parent {target_id} "
        f"({completed_count} completed) — parent re-queued for resume"
    )
    actions.append(
        f"delegation: all {total_count} children done, "
        f"parent {target_id} re-queued ({completed_count} completed)"
    )


async def _check_delegation_timeouts() -> int:
    """Check for timed-out delegation parents and cancel their remaining children.

    Scans jobs in 'waiting' status with freeze_type='delegation'. If the
    delegation timeout has elapsed, cancels remaining non-terminal children
    and resumes the parent with partial results.

    Returns the number of timed-out delegations handled.
    """
    handled = 0
    try:
        async with postgres_db.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, freeze_data, config_override, context
                FROM jobs
                WHERE status = 'waiting'
                  AND freeze_data IS NOT NULL
                """,
            )

        for row in rows:
            job_id = str(row["id"])
            freeze = row["freeze_data"]
            if isinstance(freeze, str):
                try:
                    freeze = json.loads(freeze)
                except (json.JSONDecodeError, ValueError):
                    continue
            if not freeze or freeze.get("freeze_type") != "delegation":
                continue

            timestamp_str = freeze.get("timestamp")
            timeout = freeze.get("timeout", 7200)
            if not timestamp_str:
                continue

            from datetime import datetime, timezone

            try:
                delegation_start = datetime.fromisoformat(timestamp_str)
                if delegation_start.tzinfo is None:
                    delegation_start = delegation_start.replace(tzinfo=timezone.utc)
                elapsed = (
                    datetime.now(timezone.utc) - delegation_start
                ).total_seconds()
            except (ValueError, TypeError):
                continue

            if elapsed < timeout:
                continue

            # Timeout reached — cancel remaining children and resume parent
            logger.warning(
                f"Delegation timeout for job {job_id}: "
                f"{elapsed:.0f}s elapsed > {timeout}s limit"
            )

            # Cancel non-terminal children
            children = await postgres_db.get_delegation_children(job_id)
            cancelled_count = 0
            for child in children:
                child_status = child.get("status", "")
                if child_status not in ("completed", "failed", "cancelled"):
                    child_id = str(child["id"])
                    try:
                        await postgres_db.cancel_job(child_id)
                        cancelled_count += 1
                    except Exception as e:
                        logger.warning(
                            f"Failed to cancel timed-out child {child_id}: {e}"
                        )

            # Build partial results and resume parent
            # Re-trigger the completion handler by faking an "all done" state
            # The simplest approach: just unblock the parent directly
            child_results = []
            refreshed_children = await postgres_db.get_delegation_children(job_id)
            for child in refreshed_children:
                child_id = str(child["id"])
                freeze_child = child.get("freeze_data")
                if isinstance(freeze_child, str):
                    try:
                        freeze_child = json.loads(freeze_child)
                    except (json.JSONDecodeError, ValueError):
                        freeze_child = {}
                freeze_child = freeze_child or {}

                child_ctx = child.get("context") or {}
                if isinstance(child_ctx, str):
                    try:
                        child_ctx = json.loads(child_ctx)
                    except (json.JSONDecodeError, ValueError):
                        child_ctx = {}
                child_output_path = (child_ctx or {}).get("graft_output_path")

                child_results.append(
                    {
                        "job_id": child_id,
                        "description": child.get("description", ""),
                        "status": child.get("status", "unknown"),
                        "config_name": child.get("config_name", "default"),
                        "output_path": child_output_path,
                        "creation_order": child.get("creation_order"),
                        "branch_name": child.get("branch_name"),
                        "summary": freeze_child.get("summary", ""),
                        "confidence": freeze_child.get("confidence", 0.0),
                        "timed_out": child.get("status") == "cancelled",
                    }
                )

            parent_ctx = {}
            raw_ctx = row.get("context")
            if raw_ctx:
                if isinstance(raw_ctx, str):
                    try:
                        parent_ctx = json.loads(raw_ctx)
                    except (json.JSONDecodeError, ValueError):
                        parent_ctx = {}
                else:
                    parent_ctx = dict(raw_ctx)

            parent_ctx["delegation_results"] = child_results
            parent_ctx["delegation_timed_out"] = True
            await postgres_db.update_job_context(job_id, parent_ctx)

            await postgres_db.update_job_status(
                job_id, status="paused", assigned_agent_id=""
            )
            _trigger_dispatch()

            logger.info(
                f"Delegation timeout handled for {job_id}: "
                f"cancelled {cancelled_count} children, parent re-queued"
            )
            handled += 1

    except Exception as e:
        logger.error(f"Error checking delegation timeouts: {e}", exc_info=True)

    return handled


async def delegation_timeout_sweeper(shutdown_event: asyncio.Event) -> None:
    """Background task that checks for timed-out delegations every 60 seconds."""
    logger.info("Delegation timeout sweeper started")
    while not shutdown_event.is_set():
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=60.0)
            break  # shutdown requested
        except asyncio.TimeoutError:
            pass  # 60s elapsed — run the check
        try:
            handled = await _check_delegation_timeouts()
            if handled:
                logger.info(f"Delegation timeout sweeper: handled {handled} timeouts")
        except Exception as e:
            logger.error(f"Delegation timeout sweeper error: {e}", exc_info=True)
    logger.info("Delegation timeout sweeper stopped")


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
            actions.append(
                f"target {target_job_id} resumed with feedback (round {current_round})"
            )
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
    if _is_lite_config_override(job.get("config_override")):
        logger.info(
            f"Critic skipped for job {job_id}: lite workspace backend has no "
            f"git workspace for the verification subjob handoff"
        )
        return
    if not is_verification_enabled(job):
        logger.debug(f"Verification not enabled for job {job_id}")
        return
    # Check if this is a job completion (not a phase boundary).
    # Accept freeze_data OR status=reviewing (set by determine_job_status when
    # goal_achieved is True) OR freeze_data sent in the request body.
    if not is_job_completion_freeze(job) and job.get("status") != "reviewing":
        logger.debug(
            f"Skipping verification for {job_id} — not a job completion freeze"
        )
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
                json.dumps(critic_context),
                critic_id,
            )

        logger.info(
            f"Resuming existing critic {critic_id} for job {job_id} (round {new_round})"
        )
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

        # Inherit parent's workspace backend so critic runs on the same VM/container
        parent_ctx = job.get("context") or {}
        if isinstance(parent_ctx, str):
            try:
                parent_ctx = json.loads(parent_ctx)
            except (json.JSONDecodeError, ValueError):
                parent_ctx = {}
        if parent_ctx.get("vm"):
            context["vm"] = parent_ctx["vm"]
        elif parent_ctx.get("workspace_container"):
            context["workspace_container"] = parent_ctx["workspace_container"]

        config_override = {
            "autonomy": "full",
            "tools": {
                "evaluation": ["approve_job", "return_job_with_feedback"],
            },
        }

        # Propagate parent's LLM override so the critic uses the same model
        parent_override = job.get("config_override")
        if isinstance(parent_override, str):
            try:
                parent_override = json.loads(parent_override)
            except (json.JSONDecodeError, ValueError):
                parent_override = None
        if parent_override and isinstance(parent_override.get("llm"), dict):
            config_override["llm"] = parent_override["llm"]

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
            user_id=str(job["user_id"]) if job.get("user_id") else None,
        )

        critic_job_id = str(critic_job["id"])
        short_id = critic_job_id[:8]

        # Inherit the parent's datasource selection (explicit-only resolution).
        await _propagate_datasources_to_subjob(job_id, critic_job_id)

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

                # Set worktree_path if subjob inherits a workspace backend
                worktree_path = None
                if parent_ctx.get("vm") or parent_ctx.get("workspace_container"):
                    worktree_path = f"/home/agent-host/workspace/worktrees/{short_id}-{critic_config}"

                async with postgres_db.acquire() as conn:
                    await conn.execute(
                        "UPDATE jobs SET branch_name = $1, repo_name = $2, worktree_path = $3 WHERE id = $4::uuid",
                        branch_name,
                        parent_repo_name,
                        worktree_path,
                        critic_job_id,
                    )
            except Exception as e:
                logger.warning(
                    f"Failed to create Gitea branch for critic {critic_job_id}: {e}"
                )

        _trigger_dispatch()
        actions.append(f"critic job {critic_job_id} created")
        logger.info(f"Verification job {critic_job_id} created for job {job_id}")


def _loop_deadline_passed(run_until: Any) -> bool:
    """True if the project loop's run_until deadline has passed (tz-aware)."""
    if run_until is None:
        return False
    if isinstance(run_until, str):
        try:
            run_until = datetime.fromisoformat(run_until)
        except ValueError:
            return False
    if run_until.tzinfo is None:
        run_until = run_until.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) >= run_until


async def _spawn_loop_job(
    loop: dict[str, Any],
    *,
    role: str,
    iteration: int,
) -> dict[str, Any]:
    """Create + provision + dispatch one bare project-loop job.

    Shared by the loop start endpoint and the ``_advance_project_loop`` hook.
    Mirrors the automation run-now path: ``create_loop_job`` does the DB write,
    then we provision the Gitea repo and nudge the dispatcher. Raises on a
    failed job create (caller decides how to handle); provisioning / dispatch
    failures are non-fatal (logged), matching POST /api/jobs + run-now.
    """
    from services.job_provisioning import provision_job_repo
    from services.project_loops import create_loop_job

    job = await create_loop_job(postgres_db, loop, role=role, iteration=iteration)

    try:
        await provision_job_repo(
            job_row=job,
            gitea_client=gitea_client,
            postgres_db=postgres_db,
            main_cloud_router=main_cloud_router,
        )
    except Exception:
        logger.exception(
            "project loop %s: repo provisioning failed for job %s (non-fatal)",
            loop.get("id"),
            job.get("id"),
        )

    try:
        _trigger_dispatch()
    except Exception:
        logger.exception("project loop: _trigger_dispatch raised (non-fatal)")

    return job


async def _advance_project_loop(
    job: dict[str, Any],
    result: dict[str, Any],
    actions: list[str],
) -> None:
    """Advance a project self-improvement loop when its current job completes.

    If the completed job belongs to a *running* loop and is that loop's
    in-flight job, decrement the budget, check stop conditions (budget /
    deadline / consecutive-failure cap), and either stop the loop or rotate to
    the next role and spawn the next job. Idempotent on ``current_job_id`` so a
    re-delivered completion can't double-advance. Loop jobs run bare, so this is
    the only completion hook that fires for them.

    Design: docs/features/project_self_improvement_loop.md.
    """
    ctx = job.get("context")
    if isinstance(ctx, str):
        try:
            ctx = json.loads(ctx)
        except (json.JSONDecodeError, ValueError):
            ctx = {}
    loop_id = (ctx or {}).get("loop_id")
    if not loop_id:
        return

    loop = await postgres_db.get_project_loop(str(loop_id))
    if not loop or loop.get("status") != "running":
        return  # paused / stopped / terminal — leave the current job, don't advance
    if str(job["id"]) != str(loop.get("current_job_id") or ""):
        return  # cheap pre-check: only the in-flight job advances the loop

    # Atomic claim: nulls current_job_id iff it still equals this job on a
    # running loop. Guarantees exactly one advance even if the completion hook
    # and the safety-net sweeper fire concurrently for the same terminal job
    # (the loser gets False here and backs off). Uses the pre-claim `loop`
    # snapshot below for seq_index / remaining — the claim only nulls the job.
    if not await postgres_db.claim_project_loop_advance(str(loop_id), str(job["id"])):
        return  # another caller already claimed this advance

    failed = bool(result.get("error")) or job.get("status") == "failed"
    consecutive = (int(loop.get("consecutive_failures") or 0) + 1) if failed else 0
    last_error = (result.get("error") or "job failed") if failed else None

    remaining = loop.get("remaining_iterations")
    next_remaining = (remaining - 1) if remaining is not None else None

    # Stop conditions, re-checked every advance.
    stop_reason: str | None = None
    if next_remaining is not None and next_remaining <= 0:
        stop_reason = "budget"
    elif _loop_deadline_passed(loop.get("run_until")):
        stop_reason = "deadline"
    elif consecutive >= int(loop.get("max_consecutive_failures") or 3):
        stop_reason = "failures"

    if stop_reason:
        await postgres_db.update_project_loop(
            str(loop_id),
            status=("failed" if stop_reason == "failures" else "completed"),
            remaining_iterations=next_remaining,
            consecutive_failures=consecutive,
            last_error=last_error,
            stop_reason=stop_reason,
            current_job_id=None,
        )
        actions.append(f"project loop {str(loop_id)[:8]} stopped ({stop_reason})")
        return

    # Rotate to the next role and spawn the next job.
    roles = loop.get("role_sequence") or ["scholar", "critic", "developer"]
    next_index = (int(loop.get("seq_index") or 0) + 1) % len(roles)
    next_role = roles[next_index]
    total_run = int(loop.get("total_jobs_run") or 0) + 1

    # Reflect the decremented budget in the kickoff the next job sees.
    loop_for_spawn = dict(loop)
    loop_for_spawn["remaining_iterations"] = next_remaining

    try:
        child = await _spawn_loop_job(
            loop_for_spawn, role=next_role, iteration=total_run
        )
    except Exception as e:
        logger.exception("project loop %s: failed to spawn next job", loop_id)
        await postgres_db.update_project_loop(
            str(loop_id),
            status="failed",
            remaining_iterations=next_remaining,
            consecutive_failures=consecutive,
            last_error=f"spawn failed: {e}",
            stop_reason="failures",
            current_job_id=None,
        )
        actions.append(f"project loop {str(loop_id)[:8]} stopped (spawn failed)")
        return

    await postgres_db.update_project_loop(
        str(loop_id),
        seq_index=next_index,
        current_job_id=str(child["id"]),
        remaining_iterations=next_remaining,
        consecutive_failures=consecutive,
        total_jobs_run=total_run,
        last_error=last_error,
    )
    actions.append(
        f"project loop {str(loop_id)[:8]} → {next_role} job {str(child['id'])[:8]}"
    )


async def _resume_project_loop(loop_id: str) -> dict[str, Any] | None:
    """Resume a paused project loop.

    Sets status back to ``running``. If the in-flight job already reached a
    terminal state while the loop was paused (so the advance that would have
    fired was suppressed), re-run that advance now so the rotation continues;
    otherwise the still-running job advances the loop naturally on completion.
    """
    loop = await postgres_db.update_project_loop(loop_id, status="running")
    if not loop:
        return None
    cur = loop.get("current_job_id")
    if cur:
        cur_job = await postgres_db.get_job(str(cur))
        if cur_job and cur_job.get("status") in ("completed", "failed", "cancelled"):
            await _advance_project_loop(cur_job, {}, [])
            loop = await postgres_db.get_project_loop(loop_id)
    return loop


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
    if _is_lite_config_override(target_job.get("config_override")):
        logger.info(
            f"Curation skipped for job {target_job_id}: lite workspace backend "
            f"has no git workspace for the curator subjob handoff"
        )
        return
    if not is_curation_enabled(target_job):
        return

    curator_config_name = get_curation_config(target_job).get(
        "curator_config", "curator"
    )

    # Find a waiting curator for this target job
    async with postgres_db.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT id, status FROM jobs
               WHERE parent_job_id = $1::uuid AND config_name = $2
               AND status IN ('waiting', 'paused')
               ORDER BY created_at DESC LIMIT 1""",
            target_job_id,
            curator_config_name,
        )

    if not row:
        logger.debug(f"No waiting curator found for job {target_job_id}")
        return

    if row["status"] == "completed":
        return

    curator_id = str(row["id"])
    logger.info(
        f"Triggering curation final pass via curator {curator_id} for {target_job_id}"
    )
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
async def complete_job(
    request: Request,
    job_id: str,
    body: JobCompleteRequest,
) -> dict[str, Any]:
    """Handle job completion reported by the agent. **Internal** (P4b) —
    requires ``X-Internal-Key``. Ingress strips this path.

    The agent calls this after the graph finishes. The orchestrator handles
    all post-completion logic: status determination, critic verdict handling,
    verification job spawning, curation final pass, and dispatch.

    This replaces the agent-side ``_update_job_status_from_result``,
    ``_handle_critic_verdict``, and ``_maybe_trigger_verification`` functions.
    """
    await require_internal(request)
    from services.completion import (
        determine_job_status,
        is_curation_enabled,
        is_verification_enabled,
    )

    try:
        job = await postgres_db.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

        if job["status"] not in (
            "processing",
            "reviewing",
            "pending_review",
            "completed",
        ):
            raise HTTPException(
                status_code=400,
                detail=f"Job cannot be completed (status: {job['status']})",
            )

        result = body.model_dump()
        actions: list[str] = []

        # Write freeze_data from the completion report.
        # The orchestrator is the single authority for DB writes — agents
        # report freeze_data in the completion payload, we persist it.
        if result.get("freeze_data"):
            job["freeze_data"] = result["freeze_data"]
            try:
                async with postgres_db.acquire() as conn:
                    await conn.execute(
                        "UPDATE jobs SET freeze_data = $1::jsonb WHERE id = $2::uuid",
                        json.dumps(result["freeze_data"]),
                        job_id,
                    )
            except Exception as e:
                logger.warning(f"Failed to write freeze_data for {job_id}: {e}")

        # Clear any remaining queued_replies from job context on completion.
        # The agent may have consumed them during phase transitions.
        if result.get("should_stop"):
            try:
                async with postgres_db.acquire() as conn:
                    await conn.execute(
                        "UPDATE jobs SET context = context - 'queued_replies' "
                        "WHERE id = $1::uuid AND context ? 'queued_replies'",
                        job_id,
                    )
            except Exception as e:
                logger.warning(f"Failed to clear queued_replies for {job_id}: {e}")

        # 0. VM recovery: if workspace became unavailable, re-provision and re-queue
        error = result.get("error") or {}
        if isinstance(error, dict) and error.get("type") == "workspace_unavailable":
            # Guard: skip if recovery is already in progress (prevents double-dispatch loop)
            vm_ctx = _get_vm_context(job)
            if vm_ctx and vm_ctx.get("recovering"):
                logger.info(
                    f"Job {job_id}: VM recovery already in progress, skipping duplicate"
                )
                return {
                    "status": "handled",
                    "job_id": job_id,
                    "new_status": "paused",
                    "actions": ["vm recovery: duplicate skipped"],
                }

            logger.warning(
                f"Job {job_id}: workspace unavailable — attempting VM recovery"
            )
            # Set recovering flag *before* issuing delete to prevent re-entry
            ctx = job.get("context") or {}
            if isinstance(ctx, str):
                ctx = json.loads(ctx)
            ctx["vm"] = {
                "requested": True,
                "recovering": True,
                "previous_error": "workspace_unavailable",
            }
            await postgres_db.update_job_context(job_id, ctx)

            # Delete the old (crashed) VM
            if vm_ctx and vm_ctx.get("status") not in ("deleted", "deleting"):
                await vm_provisioner.delete_vm(job_id)
            # Put job back in queue as paused (dispatchable, clears assigned_agent_id)
            await postgres_db.pause_job(job_id)
            _trigger_dispatch()
            return {
                "status": "handled",
                "job_id": job_id,
                "new_status": "paused",
                "actions": [
                    "vm recovery: old VM deleted, new VM will be provisioned, job re-queued"
                ],
            }

        # 1. Determine and set the new job status
        new_status, error_message = determine_job_status(job, result)

        # 1·mem. Memory/KB-unavailable bounded retry. determine_job_status has
        # already enforced the cap (paused under MEMORY_RETRY_CAP, failed at it).
        # For the pause we must FREE the agent so the dispatcher re-dispatches the
        # SAME job on a fresh pod — pause_job() does that, but only while the row
        # is still 'processing', so it has to run before the generic status write
        # below. The loop-advance hook is correctly skipped because the job never
        # reaches a terminal status here.
        # docs/issues/embedding_key_missing_silently_disables_memory_and_kb.md
        if new_status == "paused":
            _mfd = result.get("freeze_data")
            if isinstance(_mfd, str):
                try:
                    _mfd = json.loads(_mfd)
                except (ValueError, TypeError):
                    _mfd = {}
            if isinstance(_mfd, dict) and _mfd.get("freeze_type") in (
                "memory_unavailable",
                "kb_unavailable",
            ):
                # Atomic increment (race-proof) — a duplicate re-dispatch of the
                # same paused job must not let two handlers both read the old
                # value and stall the counter, which would defeat the cap.
                _mn = await postgres_db.increment_job_memory_retry(job_id)
                if await postgres_db.pause_job(job_id):
                    _trigger_dispatch()
                    actions.append(
                        f"memory_unavailable: re-queued for retry "
                        f"(memory_retry_count -> {_mn})"
                    )
                    job["status"] = "paused"
                    new_status = None  # generic write + loop-advance must not re-handle

        # 1a. Mode A diff capture (job_cloud_export.md §3.3). If this is a
        # project-attached job that received a baseline at dispatch, see
        # whether the agent made changes under projects/<slug>/. If so,
        # override new_status to pending_review and stamp
        # diff_status='pending'. Skipped for failed/cancelled exits — only
        # an actually-completed run gets a diff review.
        if (
            job.get("cloud_diff_baseline_commit")
            and new_status in ("completed", "pending_review")
            and gitea_client.is_initialized
        ):
            try:
                from services.job_cloud_baseline import capture_diff_for_mode_a_job

                captured = await capture_diff_for_mode_a_job(
                    job=job,
                    postgres_db=postgres_db,
                    gitea_client=gitea_client,
                )
                if captured and new_status == "completed":
                    new_status = "pending_review"
                    actions.append("mode A diff captured -> pending_review")
            except Exception as e:
                logger.warning(
                    f"Mode A: diff capture failed for job {job_id} ({e}); "
                    "proceeding with original status"
                )

        if new_status:
            kwargs: dict[str, Any] = {"status": new_status}
            if error_message:
                kwargs["error_message"] = error_message
            await postgres_db.update_job_status(job_id, **kwargs)
            actions.append(f"status -> {new_status}")
            logger.info(f"Job {job_id} status set to '{new_status}'")

            # Set completed_at for terminal statuses
            if new_status == "completed":
                try:
                    async with postgres_db.acquire() as conn:
                        await conn.execute(
                            "UPDATE jobs SET completed_at = NOW() WHERE id = $1::uuid",
                            job_id,
                        )
                except Exception as e:
                    logger.warning(f"Failed to set completed_at for {job_id}: {e}")

            # Update job dict with new status for downstream checks
            job["status"] = new_status

        # 1b. Notify operator for freeze events that require human action
        _NOTIFIABLE_FREEZE_TYPES = {
            "vm_upgrade_required",
            "job_complete",
            "budget_exceeded",
        }
        if new_status in ("pending_review", "paused") and result.get("freeze_data"):
            fd = result["freeze_data"]
            if isinstance(fd, str):
                fd = json.loads(fd)
            ft = fd.get("freeze_type")
            if ft in _NOTIFIABLE_FREEZE_TYPES:
                sudo_request_id = None

                # For vm_upgrade freezes, create a sudo_approval_requests record
                # so the operator can approve/deny from the Cockpit Sudo tab.
                if ft == "vm_upgrade_required":
                    try:
                        sudo_request_id = await sudo_gate.insert_vm_upgrade_request(
                            job_id=job_id,
                            command=fd.get("command", "unknown"),
                            reason=fd.get("reason", ""),
                            config_name=job.get("config_name", ""),
                        )
                        if sudo_request_id:
                            actions.append(
                                f"sudo request created ({sudo_request_id[:8]})"
                            )
                    except Exception as e:
                        logger.warning(
                            f"Failed to create sudo request for {job_id}: {e}"
                        )

                try:
                    await _notify_operator_freeze(
                        job,
                        job_id,
                        ft,
                        fd,
                        sudo_request_id=sudo_request_id,
                    )
                    actions.append(f"notification sent ({ft})")
                except Exception as e:
                    logger.warning(
                        f"Failed to send freeze notification for {job_id}: {e}"
                    )

        # 2. Subjob output graft (uniform for all subjob types; critic skipped inside)
        if job.get("parent_job_id"):
            graft_result = await _maybe_graft_completed_subjob(job)
            if graft_result and graft_result.get("status") == "grafted":
                actions.append(
                    f"subjob output grafted to {graft_result['output_path']}"
                )

        # 3. Handle critic verdict (if this is a critic job)
        try:
            await _handle_critic_verdict_on_complete(job, actions)
        except Exception as e:
            logger.error(
                f"Error handling critic verdict for {job_id}: {e}", exc_info=True
            )

        # 3b. Handle scholar completion (unblock parent job)
        try:
            await _handle_scholar_completion(job, actions)
        except Exception as e:
            logger.error(
                f"Error handling scholar completion for {job_id}: {e}", exc_info=True
            )

        # 3c. Handle delegation child completion (resume parent when all siblings done)
        try:
            await _handle_delegation_child_completion(job, actions)
        except Exception as e:
            logger.error(
                f"Error handling delegation child completion for {job_id}: {e}",
                exc_info=True,
            )

        # 4. Trigger verification (if this is a main job that completed)
        try:
            await _trigger_verification_on_complete(job, result, actions)
        except Exception as e:
            logger.error(
                f"Error triggering verification for {job_id}: {e}", exc_info=True
            )

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
                logger.error(
                    f"Error triggering curation for {job_id}: {e}", exc_info=True
                )

        # 5d. Advance project self-improvement loop (if this job belongs to one).
        # Loop jobs run bare, so this is the only completion hook that fires for
        # them; it spawns the next role's job or stops the loop on budget.
        # Only a TERMINAL outcome advances the loop: a paused job (e.g. the
        # memory_unavailable bounded-retry) is re-dispatched as the SAME job, so
        # the loop must keep waiting on it rather than rotate to the next role.
        # docs/issues/embedding_key_missing_silently_disables_memory_and_kb.md
        try:
            if job.get("status") in ("completed", "failed", "cancelled"):
                await _advance_project_loop(job, result, actions)
        except Exception as e:
            logger.error(
                f"Error advancing project loop for {job_id}: {e}", exc_info=True
            )

        # 6. Trigger dispatch (freed agent can pick up queued work)
        _trigger_dispatch()

        # 7. Archive workspace (snapshot to S3) and clean up VM/container
        if job.get("status") in ("completed", "failed"):
            try:
                cleanup_actions = await _archive_and_cleanup_workspace(job_id)
                actions.extend(cleanup_actions)
            except Exception as e:
                logger.warning(
                    "Workspace cleanup failed for job %s (non-blocking): %s",
                    job_id,
                    e,
                )
                actions.append(f"workspace cleanup failed: {e}")

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
async def get_frozen_job_data(request: Request, job_id: str) -> dict[str, Any]:
    """Get the frozen job data (job_frozen.json) for a pending_review job.

    Tries Gitea first, falls back to local workspace.

    Returns:
        Contents of job_frozen.json (summary, deliverables, confidence, notes, etc.)
    """
    _, job = await require_job_access(request, postgres_db, job_id)
    try:
        frozen_data = None

        # Primary: read freeze_data from DB
        if job.get("freeze_data"):
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
            workspace_path = workspace_service.base_path / "output" / "job_frozen.json"
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
async def get_job_snapshot(request: Request, job_id: str) -> dict[str, Any]:
    """Get snapshot metadata for a job.

    Returns status, source type, size, and environment summary.
    Used by the cockpit to show snapshot availability indicators.
    """
    await require_job_access(request, postgres_db, job_id)
    try:
        result = await snapshot_service.get_snapshot_status(job_id)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.delete("/api/jobs/{job_id}/snapshot")
async def delete_job_snapshot(request: Request, job_id: str) -> dict[str, Any]:
    """Delete all snapshots for a job from S3."""
    await require_job_access(request, postgres_db, job_id)
    try:
        success = await snapshot_service.delete_snapshot(job_id)
        if not success:
            raise HTTPException(status_code=500, detail="Failed to delete snapshot")
        return {"status": "deleted", "job_id": job_id}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.put("/api/jobs/{job_id}/snapshot/pin")
async def toggle_snapshot_pin(request: Request, job_id: str) -> dict[str, Any]:
    """Toggle pin state on a snapshot (GC exemption).

    Pinned snapshots are exempt from automatic garbage collection.
    """
    await require_job_access(request, postgres_db, job_id)
    try:
        new_value = await snapshot_service.toggle_pin(job_id)
        return {"job_id": job_id, "pinned": new_value}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/snapshots/stats")
async def get_snapshot_stats(request: Request) -> dict[str, Any]:
    """Get aggregate snapshot storage statistics. **Admin only** (G5) —
    storage-level metric with no per-user shape.

    Returns total snapshot count, total size, GC pending info.
    """
    await _require_admin(request)
    try:
        return await snapshot_service.get_storage_stats()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


class IdeSessionRequest(BaseModel):
    """Request body for starting an IDE session."""

    cpu_cores: int = Field(8, description="VM CPU cores")
    memory: str = Field("16Gi", description="VM memory")
    idle_timeout_minutes: int | None = Field(
        None, description="Override default idle timeout"
    )


@app.post("/api/jobs/{job_id}/ide")
async def start_ide_session(
    request: Request,
    job_id: str,
    body: IdeSessionRequest | None = None,
) -> dict[str, Any]:
    """Start or get an IDE session for a job.

    Idempotent: if a session is already active, returns it.
    If restoring, returns current progress status.
    """
    await require_job_access(request, postgres_db, job_id)
    if body is None:
        body = IdeSessionRequest()

    try:
        result = await ide_session_service.start_session(
            job_id=job_id,
            cpu_cores=body.cpu_cores,
            memory=body.memory,
            idle_timeout_minutes=body.idle_timeout_minutes,
        )
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/jobs/{job_id}/ide")
async def get_ide_session(request: Request, job_id: str) -> dict[str, Any]:
    """Get IDE session status and URL.

    Used by the cockpit to poll session state and determine
    IDE button visibility/behavior.
    """
    await require_job_access(request, postgres_db, job_id)
    try:
        return await ide_session_service.get_session_status(job_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.delete("/api/jobs/{job_id}/ide")
async def stop_ide_session(request: Request, job_id: str) -> dict[str, Any]:
    """Tear down an active IDE session.

    Deletes the restored VM and marks the session as expired.
    The underlying S3 snapshot is preserved for future restores.
    """
    await require_job_access(request, postgres_db, job_id)
    try:
        result = await ide_session_service.stop_session(job_id)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


# =============================================================================
# IDE Proxy — reverse proxy HTTP + WebSocket to code-server in workspace pods
# =============================================================================

# Shared httpx client for IDE proxy (long-lived, created once)
_ide_http_client: httpx.AsyncClient | None = None


def _get_ide_http_client() -> httpx.AsyncClient:
    global _ide_http_client
    if _ide_http_client is None:
        _ide_http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=300.0, write=30.0, pool=5.0),
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
            follow_redirects=False,
        )
    return _ide_http_client


# Headers that should not be forwarded to the upstream code-server
_PROXY_HOP_HEADERS = frozenset(
    {
        "host",
        "authorization",
        "connection",
        "upgrade",
        "transfer-encoding",
        "keep-alive",
        "te",
        "trailer",
        "proxy-authorization",
        "proxy-connection",
    }
)


def _is_browser_navigation(request: Request) -> bool:
    """True for a top-level browser navigation (vs an XHR / sub-resource).

    Browsers send ``Sec-Fetch-Mode: navigate`` on top-level navigations;
    code-server's own asset/XHR sub-requests send ``cors``/``no-cors``.
    Fall back to an HTML-preferring Accept header for older browsers.
    """
    if request.headers.get("sec-fetch-mode") == "navigate":
        return True
    return "text/html" in request.headers.get("accept", "")


@app.api_route(
    "/api/ide/{job_id}/proxy/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
)
async def ide_proxy_http(request: Request, job_id: str, path: str = ""):
    """Reverse proxy HTTP requests to code-server in a workspace pod."""
    # Neuter code-server's service worker — it caches aggressively behind the
    # reverse proxy and breaks subsequent visits (infinite loading screen).
    # Return a no-op worker so the browser doesn't intercept fetches.
    # Pre-auth: the script body is non-sensitive and browsers may fetch it
    # in the background even after a cookie expires.
    if path.endswith("serviceWorker.js") or path.endswith("service-worker.js"):
        return Response(
            content="self.addEventListener('install',()=>self.skipWaiting());"
            "self.addEventListener('activate',e=>e.waitUntil(self.clients.claim()));",
            media_type="application/javascript",
            headers={"cache-control": "no-store"},
        )

    # H1: close the zero-auth hole — pre-fix, any caller knowing (or guessing)
    # the job/thread UUID got full code-server access (file r/w, terminal).
    try:
        user = await require_approved_user(request, postgres_db)
    except HTTPException as exc:
        # A top-level browser navigation can only carry the BFF cookie. When
        # that session has idle-expired, send the browser through the cockpit
        # login instead of dumping raw 401 JSON. Only the no-session 401
        # redirects — 403 (pending approval / IDE access denied) stays an error
        # so an authenticated-but-unauthorized user never loops through login.
        if exc.status_code == 401 and _is_browser_navigation(request):
            return RedirectResponse("/auth/login?return_to=/", status_code=302)
        raise
    if not await user_can_access_ide_entity(user, postgres_db, job_id):
        await log_security_event(
            postgres_db,
            user=user,
            resource_type="ide_entity",
            resource_id=job_id,
            detail="IDE access denied",
            request=request,
        )
        raise HTTPException(status_code=403, detail="IDE access denied")

    pod_ip = await ide_proxy_service.resolve_pod_ip(job_id)
    if not pod_ip:
        raise HTTPException(status_code=503, detail="IDE session not active")

    # Build upstream URL (pod_ip may include port for Docker Compose, e.g. "localhost:8081")
    host = f"{pod_ip}:38080" if ":" not in pod_ip else pod_ip
    upstream_url = f"http://{host}/{path}"
    if request.url.query:
        # Strip 'token' param (reserved for future auth) but forward the rest
        query = str(request.url.query)
        upstream_url += f"?{query}"

    # Build upstream headers (strip hop-by-hop and proxy-specific)
    upstream_headers = {}
    for key, value in request.headers.items():
        if key.lower() not in _PROXY_HOP_HEADERS:
            upstream_headers[key] = value
    upstream_headers["host"] = host
    upstream_headers["x-forwarded-for"] = request.client.host if request.client else ""
    upstream_headers["x-forwarded-proto"] = "https"

    client = _get_ide_http_client()

    try:
        upstream_resp = await client.request(
            method=request.method,
            url=upstream_url,
            headers=upstream_headers,
            content=request.stream()
            if request.method in ("POST", "PUT", "PATCH")
            else None,
        )
    except httpx.ConnectError:
        ide_proxy_service.evict(job_id)
        raise HTTPException(status_code=502, detail="code-server unreachable")
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="code-server timeout")

    # Build response headers (pass through relevant ones)
    response_headers = {}
    for key, value in upstream_resp.headers.multi_items():
        lower = key.lower()
        if lower not in ("transfer-encoding", "connection", "keep-alive"):
            response_headers[key] = value

    return StreamingResponse(
        content=upstream_resp.aiter_bytes(),
        status_code=upstream_resp.status_code,
        headers=response_headers,
    )


@app.websocket("/api/ide/{job_id}/proxy/{path:path}")
async def ide_proxy_ws(ws: WebSocket, job_id: str, path: str = ""):
    """Reverse proxy WebSocket connections to code-server in a workspace pod."""
    import asyncio

    import websockets

    # H1: close the zero-auth hole. Browsers send the BFF session cookie on
    # WS handshake automatically; pre-fix, this endpoint accepted any caller
    # who knew (or guessed) the entity UUID. Same pattern as persistent_ws_proxy.
    user = await resolve_ws_user(ws, postgres_db)
    if not user:
        await ws.close(code=4401, reason="Authentication required")
        return
    if not user.get("is_approved"):
        await ws.close(code=4403, reason="Account pending approval")
        return
    if not await user_can_access_ide_entity(user, postgres_db, job_id):
        await log_security_event(
            postgres_db,
            user=user,
            resource_type="ide_entity",
            resource_id=job_id,
            detail="IDE access denied",
            request=ws,
            method="WS",
        )
        await ws.close(code=4403, reason="IDE access denied")
        return

    pod_ip = await ide_proxy_service.resolve_pod_ip(job_id)
    if not pod_ip:
        await ws.close(code=4503, reason="IDE session not active")
        return

    await ws.accept()

    # Build upstream WS URL (pod_ip may include port for Docker Compose)
    ws_host = f"{pod_ip}:38080" if ":" not in pod_ip else pod_ip
    upstream_url = f"ws://{ws_host}/{path}"
    if ws.url.query:
        upstream_url += f"?{ws.url.query}"

    try:
        async with websockets.connect(
            upstream_url,
            max_size=16 * 1024 * 1024,  # 16 MB max message
            ping_interval=30,
            ping_timeout=10,
            close_timeout=5,
        ) as upstream_ws:

            async def browser_to_pod():
                """Forward messages from browser to code-server."""
                try:
                    while True:
                        msg = await ws.receive()
                        if msg["type"] == "websocket.receive":
                            if "text" in msg and msg["text"] is not None:
                                await upstream_ws.send(msg["text"])
                            elif "bytes" in msg and msg["bytes"] is not None:
                                await upstream_ws.send(msg["bytes"])
                        elif msg["type"] == "websocket.disconnect":
                            break
                except WebSocketDisconnect:
                    pass

            async def pod_to_browser():
                """Forward messages from code-server to browser."""
                try:
                    async for message in upstream_ws:
                        if isinstance(message, str):
                            await ws.send_text(message)
                        elif isinstance(message, bytes):
                            await ws.send_bytes(message)
                except websockets.ConnectionClosed:
                    pass

            # Run both directions concurrently; when one finishes, cancel the other
            tasks = [
                asyncio.create_task(browser_to_pod()),
                asyncio.create_task(pod_to_browser()),
            ]
            done, pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()

    except (OSError, websockets.InvalidURI, websockets.InvalidHandshake) as e:
        ide_proxy_service.evict(job_id)
        logger.debug("IDE WS proxy failed for job %s: %s", job_id, e)
        try:
            await ws.close(code=4502, reason="code-server unreachable")
        except Exception:
            pass
    except Exception:
        logger.debug("IDE WS proxy ended for job %s", job_id, exc_info=True)
    finally:
        try:
            await ws.close()
        except Exception:
            pass


@app.post("/api/jobs/{job_id}/ensure-workspace-access")
async def ensure_workspace_access(request: Request, job_id: str) -> dict[str, Any]:
    """Ensure the current user has Gitea access to the job's workspace repo.

    Called by the cockpit before navigating to the Gitea workspace URL.
    Re-attempts the access grant that may have been skipped at job creation
    time (if the user hadn't logged into Gitea yet via OIDC).
    """
    try:
        user = await require_approved_user(request, postgres_db)
        job = await postgres_db.get_job(job_id)
        if not job:
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")

        repo_name = job.get("repo_name")
        if not repo_name:
            return {"granted": False, "reason": "no_repo"}

        if not gitea_client.is_initialized:
            return {"granted": False, "reason": "gitea_unavailable"}

        email = user.get("email")
        if not email:
            return {"granted": False, "reason": "no_email"}

        granted = await gitea_client.grant_user_repo_access(email, repo_name)
        return {"granted": granted, "reason": "ok" if granted else "user_not_in_gitea"}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Failed to ensure workspace access for job {job_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/jobs/{job_id}/progress")
async def get_job_progress(request: Request, job_id: str) -> dict[str, Any]:
    """Get detailed progress information for a job including ETA."""
    await require_job_access(request, postgres_db, job_id)
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
    request: Request,
    job_id: str,
    page: int = Query(default=1, ge=-1),
    page_size: int = Query(default=50, ge=1, le=200, alias="pageSize"),
    offset: Optional[int] = Query(default=None, ge=0),
    limit: Optional[int] = Query(default=None, ge=1, le=200),
    order: Literal["asc", "desc"] = Query(default="asc"),
    filter: FilterCategory = Query(default="all"),
) -> dict[str, Any]:
    """Get paginated audit entries for a job from MongoDB.

    Two pagination styles are supported; use whichever you prefer:
        - offset/limit (REST-style): ?offset=50&limit=50
        - page/pageSize (legacy):    ?page=2&pageSize=50
    If both are provided, offset/limit wins. The response echoes both styles.

    Query params:
        offset: Entries to skip (overrides page if set)
        limit: Max entries to return, max 200 (overrides pageSize if set)
        page: 1-indexed page number; -1 = last page
        pageSize: Entries per page, max 200
        order: asc (oldest first, default) or desc (newest first)
        filter: all, messages, tools, or errors
    """
    await require_job_access(request, postgres_db, job_id)
    effective_size = limit if limit is not None else page_size
    if not audit_reader.is_available:
        return {
            "entries": [],
            "total": 0,
            "page": page,
            "pageSize": effective_size,
            "offset": offset if offset is not None else 0,
            "limit": effective_size,
            "hasMore": False,
            "error": "MongoDB not available",
        }

    try:
        return await audit_reader.get_job_audit(
            job_id=job_id,
            page=page,
            page_size=page_size,
            filter_category=filter,
            offset=offset,
            limit=limit,
            order=order,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/requests/{doc_id}")
async def get_request(request: Request, doc_id: str) -> dict[str, Any]:
    """Get a single LLM request by MongoDB document ID.

    Gated by the caller's access to the request's underlying job — admins
    pass; otherwise the embedded `job_id` is run through `require_job_access`.
    Requests without a `job_id` (legacy) are admin-only.
    """
    if not audit_reader.is_available:
        raise HTTPException(
            status_code=503,
            detail="MongoDB not available",
        )

    try:
        llm_doc = await audit_reader.get_request(doc_id)
        if llm_doc is None:
            # Auth before disclosing existence: any approved user may probe.
            await require_approved_user(request, postgres_db)
            raise HTTPException(
                status_code=404,
                detail=f"Request '{doc_id}' not found",
            )
        job_id = llm_doc.get("job_id")
        if job_id:
            await require_job_access(request, postgres_db, str(job_id))
        else:
            await _require_admin(request)
        return llm_doc
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/jobs/{job_id}/audit/timerange")
async def get_audit_time_range(request: Request, job_id: str) -> dict[str, str] | None:
    """Get first and last timestamps for job audit entries.

    Returns:
        Dict with 'start' and 'end' ISO timestamps, or null if no entries/MongoDB unavailable
    """
    await require_job_access(request, postgres_db, job_id)
    if not audit_reader.is_available:
        return None

    try:
        return await audit_reader.get_audit_time_range(job_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/jobs/{job_id}/chat")
async def get_job_chat_history(
    request: Request,
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
    await require_job_access(request, postgres_db, job_id)
    if not audit_reader.is_available:
        return {
            "entries": [],
            "total": 0,
            "page": page,
            "pageSize": page_size,
            "hasMore": False,
            "error": "MongoDB not available",
        }

    try:
        return await audit_reader.get_chat_history(
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
    request: Request,
    job_id: str,
    path: str = Query(default="", description="Directory path within the repo"),
    ref: str | None = Query(default=None, description="Branch, tag, or commit SHA"),
) -> list[dict[str, Any]]:
    """List directory contents of a job's Gitea repository.

    Proxies the Gitea contents API so the cockpit doesn't need Gitea credentials.

    Returns:
        List of entries, each with: name, path, type ("file"|"dir"), size
    """
    await require_job_access(request, postgres_db, job_id)
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
    request: Request,
    job_id: str,
    path: str = Query(..., description="File path within the repo"),
    ref: str | None = Query(default=None, description="Branch, tag, or commit SHA"),
) -> dict[str, Any]:
    """Get file content from a job's Gitea repository.

    Returns:
        Dict with path, content (text), and size
    """
    await require_job_access(request, postgres_db, job_id)
    if not gitea_client.is_initialized:
        raise HTTPException(
            status_code=503,
            detail="Gitea not available",
        )

    repo_name, job_branch = await resolve_job_repo(job_id)
    content = await gitea_client.get_file_content(
        repo_name, path, ref=ref or job_branch
    )

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
    request: Request,
    job_id: str,
    sha: str = Query(
        default="main", description="Branch, tag, or commit SHA to list from"
    ),
    since_ref: str | None = Query(
        default=None, description="Only show commits after this ref"
    ),
    page: int = Query(default=1, ge=1, description="Page number"),
    limit: int = Query(default=20, ge=1, le=100, description="Max commits per page"),
) -> dict[str, Any]:
    """List git commits for a job's repository.

    If since_ref is provided, returns only commits between since_ref and sha
    using git compare. Otherwise lists commits from sha.

    Returns:
        Dict with commits list and total count
    """
    await require_job_access(request, postgres_db, job_id)
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
        commits = await gitea_client.get_commits(
            repo_name, sha=effective_sha, page=page, limit=limit
        )
        if commits is None:
            raise HTTPException(
                status_code=404,
                detail=f"No commits found in repo for job '{job_id}'",
            )
        return {"total_commits": len(commits), "commits": commits}


@app.get("/api/jobs/{job_id}/repo/diff")
async def get_repo_diff(
    request: Request,
    job_id: str,
    base: str = Query(..., description="Base ref (commit SHA, tag, or branch)"),
    head: str = Query(default="HEAD", description="Head ref"),
) -> dict[str, str]:
    """Get unified diff between two refs in a job's repository.

    Returns:
        Dict with base, head, and diff text
    """
    await require_job_access(request, postgres_db, job_id)
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
async def list_repo_tags(
    request: Request, job_id: str, all_jobs: bool = False
) -> list[dict[str, Any]]:
    """List tags in a job's repository.

    By default, only returns tags for the specified job (namespaced by
    job short ID prefix). Set all_jobs=True to return all tags in the repo.

    Returns:
        List of tags with name, sha, and message
    """
    await require_job_access(request, postgres_db, job_id)
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
async def get_job_workspace(request: Request, job_id: str) -> dict[str, Any]:
    """Get workspace overview for a job.

    Returns:
        Dict with workspace files, workspace.md/plan.md content (truncated),
        current todos, and archive count.
    """
    await require_job_access(request, postgres_db, job_id)
    return workspace_service.get_workspace_overview(job_id)


@app.get("/api/jobs/{job_id}/workspace/{path:path}")
async def get_workspace_file(
    request: Request, job_id: str, path: str
) -> dict[str, str]:
    """Get content of a workspace file by relative path.

    Supports any file within the job workspace, including subdirectories
    (e.g., "archive/phase_1_retrospective.md"). Path is sandboxed.

    Args:
        job_id: Job UUID
        path: Relative path within the workspace

    Returns:
        Dict with path and file content
    """
    await require_job_access(request, postgres_db, job_id)
    content = workspace_service.get_workspace_file(job_id, path)
    if content is None:
        raise HTTPException(
            status_code=404,
            detail=f"File '{path}' not found in workspace for job '{job_id}'",
        )
    return {"path": path, "content": content}


@app.put("/api/jobs/{job_id}/workspace/{path:path}")
async def write_workspace_file(
    request: Request,
    job_id: str,
    path: str,
    body: dict[str, Any] = Body(...),
) -> dict[str, Any]:
    """Write content to a workspace file.

    Sandboxed — directory traversal is blocked, and certain paths
    (todos.yaml, .git/, tools/) are not editable.

    Args:
        job_id: Job UUID
        path: Relative path within the workspace
        body: {"content": "...", "commit_message": "..."}
    """
    await require_job_access(request, postgres_db, job_id)
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


@app.get("/api/jobs/{job_id}/diff")
async def get_job_diff(request: Request, job_id: str) -> dict[str, Any]:
    """Mode A diff summary for a project-attached job.

    Returns ``{baseline_commit, head_commit, files: [{path, status}]}``
    where each ``status`` is ``added`` / ``modified`` / ``deleted``.
    Per-file diff content is served separately via the sibling
    ``/diff/{path}`` endpoint.

    Returns 404 when the job has no baseline (loose job, or a pre-Mode-A
    project job). Empty ``files`` list means no changes under
    ``projects/<slug>/`` — the agent didn't touch the mounted folder.

    See docs/done/job_cloud_export.md §5.
    """
    _, job = await require_job_access(request, postgres_db, job_id)
    if not job.get("cloud_diff_baseline_commit"):
        raise HTTPException(
            status_code=404,
            detail="Job has no Mode A diff baseline.",
        )
    if not gitea_client.is_initialized:
        raise HTTPException(status_code=503, detail="Gitea not available.")
    from services.job_cloud_baseline import get_diff_summary

    summary = await get_diff_summary(job=job, gitea_client=gitea_client)
    if summary is None:
        raise HTTPException(
            status_code=404,
            detail="Diff unavailable (no repo or no head).",
        )
    return {
        "job_id": job_id,
        "diff_status": job.get("diff_status"),
        **summary,
    }


@app.get("/api/jobs/{job_id}/diff/{file_path:path}")
async def get_job_diff_file(
    request: Request, job_id: str, file_path: str
) -> dict[str, Any]:
    """Mode A per-file diff content.

    Returns ``{path, status, old_content, new_content}`` for one file in
    the diff. ``old_content`` is read from the baseline commit;
    ``new_content`` from the head of the job's branch. Either side can
    be ``None`` (added → no old, deleted → no new).

    Only files under ``projects/`` are accepted — the Mode A diff is
    scoped to the project-folder mount.
    """
    _, job = await require_job_access(request, postgres_db, job_id)
    baseline = job.get("cloud_diff_baseline_commit")
    if not baseline:
        raise HTTPException(
            status_code=404,
            detail="Job has no Mode A diff baseline.",
        )
    if not file_path.startswith("projects/"):
        raise HTTPException(
            status_code=400,
            detail="Per-file diff is scoped to projects/<slug>/* paths.",
        )
    if not gitea_client.is_initialized:
        raise HTTPException(status_code=503, detail="Gitea not available.")
    repo_name = job.get("repo_name")
    branch = job.get("branch_name") or "main"
    if not repo_name:
        raise HTTPException(status_code=404, detail="Job repo not found.")

    # Pull the diff summary to learn the file's status (added/modified/deleted).
    from services.job_cloud_baseline import get_diff_summary

    summary = await get_diff_summary(job=job, gitea_client=gitea_client)
    if summary is None:
        raise HTTPException(status_code=404, detail="Diff unavailable for this job.")
    file_entry = next(
        (f for f in summary.get("files", []) if f["path"] == file_path),
        None,
    )
    if file_entry is None:
        raise HTTPException(
            status_code=404,
            detail=f"Path '{file_path}' is not in the diff.",
        )
    status = file_entry["status"]
    old_content = None
    new_content = None
    if status in ("modified", "deleted"):
        old_content = await gitea_client.get_file_content(
            repo_name, file_path, ref=baseline
        )
    if status in ("modified", "added"):
        new_content = await gitea_client.get_file_content(
            repo_name, file_path, ref=branch
        )
    return {
        "job_id": job_id,
        "path": file_path,
        "status": status,
        "old_content": old_content,
        "new_content": new_content,
    }


@app.post("/api/jobs/{job_id}/accept")
async def accept_job_diff(request: Request, job_id: str) -> dict[str, Any]:
    """Mode A accept: apply the job's diff back to the project's cloud folder.

    Gates:

    * Job is ``pending_review`` and project-attached.
    * ``diff_status`` is ``pending`` (the diff capture flagged changes).
    * Backend + Gitea are reachable.
    * No external modifications to the cloud folder since seed
      (etag map captured at seed time vs. fresh PROPFIND at accept). On
      divergence, returns 409 with the diverging path list — user must
      resolve manually and re-accept.

    On success, writes/deletes each diff path back via the cloud
    backend, then transitions ``diff_status='accepted'`` and
    ``status='completed'``.

    See docs/done/job_cloud_export.md §3.5.
    """
    _, job = await require_job_access(request, postgres_db, job_id)

    # --- Gates -------------------------------------------------------
    if job.get("status") != "pending_review":
        raise HTTPException(
            status_code=409,
            detail=(
                f"Job is in status '{job.get('status')}'; "
                "only pending_review jobs can be accepted."
            ),
        )
    if not job.get("project_id"):
        raise HTTPException(
            status_code=409,
            detail="Job has no project attached; nothing to write back to.",
        )
    if job.get("diff_status") != "pending":
        raise HTTPException(
            status_code=409,
            detail=(
                f"Job diff_status is '{job.get('diff_status')}'; "
                "only pending diffs can be accepted."
            ),
        )
    if not job.get("cloud_diff_baseline_commit"):
        raise HTTPException(
            status_code=409,
            detail="Job has no Mode A baseline; nothing to compare against.",
        )

    if not gitea_client.is_initialized:
        raise HTTPException(status_code=503, detail="Gitea not available.")

    project = await postgres_db.get_project(str(job["project_id"]))
    if not project:
        raise HTTPException(status_code=404, detail="Project not found.")
    if not project.get("main_cloud_folder_handle"):
        raise HTTPException(
            status_code=409,
            detail="Project has no cloud folder; cannot apply diff.",
        )
    backend_id = project.get("main_cloud_backend")
    if not backend_id:
        raise HTTPException(
            status_code=409,
            detail="Project has no cloud backend; cannot apply diff.",
        )
    try:
        backend = main_cloud_router.for_backend(backend_id)
    except Exception as e:
        raise HTTPException(
            status_code=503,
            detail=f"Cloud backend '{backend_id}' unavailable: {e}",
        ) from e
    if not backend.is_initialized:
        raise HTTPException(status_code=503, detail="Cloud backend not initialized.")

    # --- External-modification gate ---------------------------------
    from services.job_cloud_baseline import (
        apply_diff_to_cloud,
        detect_external_mods,
    )

    diverged = await detect_external_mods(
        job=job,
        project=project,
        main_cloud_router=main_cloud_router,
    )
    if diverged:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "external_modifications_detected",
                "message": (
                    "Cloud folder was modified externally since the job "
                    "started. Resolve manually before accepting."
                ),
                "diverged": diverged,
            },
        )

    # --- Apply -------------------------------------------------------
    result = await apply_diff_to_cloud(
        job=job,
        project=project,
        gitea_client=gitea_client,
        main_cloud_router=main_cloud_router,
    )
    if result.get("errors"):
        # Partial failure: cloud is now in a mixed state. Surface the
        # errors so the user can see what missed; don't transition the
        # job — user can retry.
        raise HTTPException(
            status_code=502,
            detail={
                "code": "partial_write_failure",
                "applied": result.get("applied", 0),
                "deleted": result.get("deleted", 0),
                "errors": result.get("errors"),
            },
        )

    # --- Status transition ------------------------------------------
    await postgres_db.update_job_cloud_diff(job_id, diff_status="accepted")
    await postgres_db.update_job_status(job_id, status="completed")
    logger.info(
        "Mode A: job %s — diff accepted (%d applied, %d deleted)",
        job_id,
        result.get("applied", 0),
        result.get("deleted", 0),
    )
    return {
        "job_id": job_id,
        "diff_status": "accepted",
        "status": "completed",
        "applied": result.get("applied", 0),
        "deleted": result.get("deleted", 0),
    }


@app.post("/api/jobs/{job_id}/reject")
async def reject_job_diff(request: Request, job_id: str) -> dict[str, Any]:
    """Mode A reject: discard the job's diff, no cloud write.

    Stamps ``diff_status='rejected'`` and ``status='completed'``. The
    Gitea commits stay around as the audit trail of what the agent
    tried to do (cheap; see §3.6).

    See docs/done/job_cloud_export.md §3.6.
    """
    _, job = await require_job_access(request, postgres_db, job_id)

    if job.get("status") != "pending_review":
        raise HTTPException(
            status_code=409,
            detail=(
                f"Job is in status '{job.get('status')}'; "
                "only pending_review jobs can be rejected."
            ),
        )
    if not job.get("project_id"):
        raise HTTPException(
            status_code=409,
            detail="Job has no project attached; no diff to reject.",
        )
    if job.get("diff_status") != "pending":
        raise HTTPException(
            status_code=409,
            detail=(
                f"Job diff_status is '{job.get('diff_status')}'; "
                "only pending diffs can be rejected."
            ),
        )

    await postgres_db.update_job_cloud_diff(job_id, diff_status="rejected")
    await postgres_db.update_job_status(job_id, status="completed")
    logger.info("Mode A: job %s — diff rejected", job_id)
    return {
        "job_id": job_id,
        "diff_status": "rejected",
        "status": "completed",
    }


@app.post("/api/jobs/{job_id}/export-to-shared-folder")
async def export_job_to_shared_folder(request: Request, job_id: str) -> dict[str, Any]:
    """Mode B of the job cloud workflow — copy a job's deliverables into a
    shared cloud folder ("Open cloud folder") and return its browser URL.

    Valid for ``completed`` or ``pending_review`` jobs whose project has **no**
    main-cloud folder (loose jobs and default-project / no-cloud-folder jobs);
    jobs whose project *does* have a cloud folder go through the Mode A
    diff-review flow instead. Copies the agent's declared deliverables (from
    ``freeze_data.deliverables``, preserving their workspace-relative paths;
    falls back to ``output/`` for jobs without a deliverables list) into a
    per-job session-style cloud folder shared with the calling user.

    Re-syncable: the folder id is derived deterministically from the job id, so
    a repeat call overwrites the same folder and re-stamps ``exported_at`` as
    "last synced at" (e.g. after resume-with-feedback). v1 overwrites in place
    and does not prune files removed between syncs.

    See docs/done/job_cloud_export.md §3.2.
    """
    user, job = await require_job_access(request, postgres_db, job_id)

    # Status gate — completed or in-review. In-review (pending_review) export
    # lets the user preview the deliverables in the cloud before approving;
    # failed/cancelled/in-flight jobs have no stable output to copy.
    if job.get("status") not in ("completed", "pending_review"):
        raise HTTPException(
            status_code=409,
            detail=(
                f"Job is in status '{job.get('status')}'; "
                "only completed or in-review jobs can be exported."
            ),
        )

    # Routing gate — only jobs whose project has a main-cloud folder use the
    # Mode A diff flow. Loose jobs AND default-project / no-cloud-folder jobs
    # fall through to Mode B here (mirrors the seed gate in
    # services/job_cloud_baseline.py). ``project_has_cloud_folder`` is computed
    # by the projects LEFT JOIN in postgres.get_job.
    if job.get("project_has_cloud_folder"):
        raise HTTPException(
            status_code=409,
            detail=(
                "Job's project has a cloud folder — use the diff-review "
                "(accept/reject) flow instead of shared-folder export."
            ),
        )

    # No idempotency refusal: this endpoint is re-syncable. The folder id is
    # derived deterministically from the job id below, so a repeat call
    # overwrites the same folder (e.g. after resume-with-feedback) and
    # re-stamps ``exported_at`` as "last synced at".

    # Fresh loose-job export folder — no project/thread row to pin to yet,
    # so resolve via the owner seam (returns the active backend today;
    # per-org under multi-tenancy). Issue 16, docs/issues/main_cloud.md.
    backend = main_cloud_router.for_owner(user)
    if not backend.is_initialized:
        raise HTTPException(status_code=503, detail="Cloud backend not available.")
    if not gitea_client.is_initialized:
        raise HTTPException(status_code=503, detail="Gitea not available.")

    repo_name, branch = await resolve_job_repo(job_id)

    # 1) Provision shared folder. Short stable folder name from the job id
    #    keeps it distinguishable in the user's cloud root.
    folder_session_id = f"job-{job_id.replace('-', '')[:12]}"
    try:
        folder_handle = await backend.ensure_session_folder(
            session_id=folder_session_id
        )
        resolved_user_id = await backend.ensure_user(
            sub=user.get("keycloak_sub") or "",
            issuer=getattr(backend, "_keycloak_issuer", "") or "",
            email=user.get("email"),
            display_name=user.get("display_name"),
            preferred_username=user.get("preferred_username"),
        )
        if resolved_user_id:
            await backend.share_session_folder(folder_handle, resolved_user_id)
    except CloudBackendError as e:
        logger.exception("Mode B export: folder provisioning failed for job %s", job_id)
        raise HTTPException(
            status_code=502,
            detail=f"Cloud folder provisioning failed: {e}",
        ) from e

    # 2) Copy the job's deliverables from Gitea → cloud, bytes-faithful via
    #    get_file_bytes so binary outputs (PDFs, images) survive the round trip
    #    and the declared workspace-relative paths are preserved. The agent's
    #    declared deliverables (validated non-empty at freeze time) are the
    #    curated result set — for code jobs they live under ``repo/`` and the
    #    workspace root, not ``output/``. Jobs without a deliverables list
    #    (older jobs) fall back to copying ``output/`` wholesale. Per-file read
    #    failures in the deliverables path are logged and skipped (fail-soft) so
    #    one missing artifact doesn't sink the whole export; ``files_copied``
    #    reflects what actually landed.
    files_copied = 0

    # Declared deliverables from freeze_data (JSONB may arrive as a str).
    freeze_data = job.get("freeze_data")
    if isinstance(freeze_data, str):
        try:
            freeze_data = json.loads(freeze_data)
        except (json.JSONDecodeError, TypeError):
            freeze_data = None
    deliverables: list[str] = []
    if isinstance(freeze_data, dict) and isinstance(
        freeze_data.get("deliverables"), list
    ):
        deliverables = [
            str(p).strip() for p in freeze_data["deliverables"] if str(p).strip()
        ]

    async def _copy_deliverable(path: str) -> None:
        nonlocal files_copied
        # Declared paths are workspace-root-relative, matching the job's Gitea
        # repo layout. Reject path escapes defensively.
        rel = path.lstrip("/")
        if not rel or ".." in rel.split("/"):
            logger.warning("Mode B export: skipping unsafe deliverable path %r", path)
            return
        file_bytes = await gitea_client.get_file_bytes(repo_name, rel, ref=branch)
        if file_bytes is None:
            # Declared but absent from the repo (e.g. a directory entry or a
            # workspace-only artifact). Skip fail-soft rather than 502.
            logger.warning(
                "Mode B export: deliverable %r not found in repo %s; skipping",
                rel,
                repo_name,
            )
            return
        await backend.put_session_file(folder_handle, path=rel, content=file_bytes)
        files_copied += 1

    async def _copy_tree(src_dir: str) -> None:
        nonlocal files_copied
        entries = await gitea_client.list_contents(repo_name, src_dir, ref=branch)
        if not entries:
            return
        for entry in entries:
            entry_path = entry["path"]
            entry_type = entry.get("type")
            if entry_type == "dir":
                await _copy_tree(entry_path)
                continue
            if entry_type != "file":
                continue
            file_bytes = await gitea_client.get_file_bytes(
                repo_name, entry_path, ref=branch
            )
            if file_bytes is None:
                raise HTTPException(
                    status_code=502,
                    detail=f"Failed to read '{entry_path}' from Gitea.",
                )
            await backend.put_session_file(
                folder_handle,
                path=entry_path,
                content=file_bytes,
            )
            files_copied += 1

    try:
        if deliverables:
            for deliverable_path in deliverables:
                await _copy_deliverable(deliverable_path)
        else:
            # No declared deliverables (older jobs) — copy output/ wholesale.
            await _copy_tree("output")
    except HTTPException:
        raise
    except CloudBackendError as e:
        logger.exception(
            "Mode B export: cloud upload failed for job %s (copied %d)",
            job_id,
            files_copied,
        )
        raise HTTPException(
            status_code=502,
            detail=f"File copy to cloud failed after {files_copied} files: {e}",
        ) from e
    except Exception as e:
        logger.exception("Mode B export: unexpected failure for job %s", job_id)
        raise HTTPException(
            status_code=502,
            detail=f"Export failed: {e}",
        ) from e

    # 3) Stamp the job — only on success so retries are safe.
    await postgres_db.update_job_exported_folder(job_id, handle=folder_handle.to_db())

    return {
        "job_id": job_id,
        "files_copied": files_copied,
        "folder": {
            "name": folder_session_id,
            "browser_url": backend.get_session_folder_browser_url(folder_handle),
            "webdav_url": backend.get_session_folder_webdav_url(folder_handle),
        },
    }


@app.get("/api/jobs/{job_id}/todos")
async def get_job_todos(request: Request, job_id: str) -> dict[str, Any]:
    """Get all todos for a job (current + archives).

    Returns:
        Dict with:
        - job_id: Job UUID
        - current: Current todos from todos.yaml (if exists)
        - archives: List of archived todo files
        - has_workspace: Whether workspace directory exists
    """
    await require_job_access(request, postgres_db, job_id)
    return workspace_service.get_all_todos(job_id)


@app.get("/api/jobs/{job_id}/todos/current")
async def get_current_todos(request: Request, job_id: str) -> dict[str, Any]:
    """Get current active todos from todos.yaml.

    Returns:
        Dict with todos list and metadata, or 404 if not found
    """
    await require_job_access(request, postgres_db, job_id)
    result = workspace_service.get_current_todos(job_id)
    if result is None:
        raise HTTPException(
            status_code=404, detail=f"No current todos found for job '{job_id}'"
        )
    return result


@app.get("/api/jobs/{job_id}/todos/archives")
async def list_todo_archives(request: Request, job_id: str) -> list[dict[str, Any]]:
    """List all archived todo files for a job.

    Returns:
        List of archive metadata (filename, phase_name, timestamp)
    """
    await require_job_access(request, postgres_db, job_id)
    return workspace_service.list_archived_todos(job_id)


@app.get("/api/jobs/{job_id}/todos/archives/{filename}")
async def get_archived_todos(
    request: Request, job_id: str, filename: str
) -> dict[str, Any]:
    """Get parsed content of an archived todo file.

    Args:
        job_id: Job UUID
        filename: Archive filename (e.g., "todos_phase1_20260124_183618.md")

    Returns:
        Dict with parsed todos, summary, and metadata
    """
    await require_job_access(request, postgres_db, job_id)
    result = workspace_service.get_archived_todos(job_id, filename)
    if result is None:
        raise HTTPException(
            status_code=404, detail=f"Archive '{filename}' not found for job '{job_id}'"
        )
    return result


# =============================================================================
# Bulk Fetch Endpoints for Client-Side Caching
# =============================================================================


@app.get("/api/jobs/{job_id}/audit/bulk")
async def get_job_audit_bulk(
    request: Request,
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
    await require_job_access(request, postgres_db, job_id)
    if not audit_reader.is_available:
        return {
            "entries": [],
            "total": 0,
            "offset": offset,
            "limit": limit,
            "hasMore": False,
            "error": "MongoDB not available",
        }

    try:
        return await audit_reader.get_job_audit_bulk(
            job_id=job_id,
            offset=offset,
            limit=limit,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/jobs/{job_id}/chat/bulk")
async def get_job_chat_bulk(
    request: Request,
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
    await require_job_access(request, postgres_db, job_id)
    if not audit_reader.is_available:
        return {
            "entries": [],
            "total": 0,
            "offset": offset,
            "limit": limit,
            "hasMore": False,
            "error": "MongoDB not available",
        }

    try:
        return await audit_reader.get_chat_history_bulk(
            job_id=job_id,
            offset=offset,
            limit=limit,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/jobs/{job_id}/graph/bulk")
async def get_job_graph_bulk(
    request: Request,
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
    await require_job_access(request, postgres_db, job_id)
    if not audit_reader.is_available:
        return {
            "deltas": [],
            "total": 0,
            "offset": offset,
            "limit": limit,
            "hasMore": False,
            "error": "MongoDB not available",
        }

    try:
        return await audit_reader.get_graph_deltas_bulk(
            job_id=job_id,
            offset=offset,
            limit=limit,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/jobs/{job_id}/version")
async def get_job_version(request: Request, job_id: str) -> dict[str, Any] | None:
    """Get job data version info for cache invalidation.

    Returns counts and timestamps that can be compared to cached values
    to determine if the cache needs to be refreshed.

    Returns:
        Dict with version, auditEntryCount, chatEntryCount, graphDeltaCount, lastUpdate
        Returns null if job has no audit data or MongoDB unavailable
    """
    await require_job_access(request, postgres_db, job_id)
    if not audit_reader.is_available:
        return None

    try:
        return await audit_reader.get_job_version(job_id)
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
            "read": ["cypher_query", "get_database_schema"],
            "write": ["cypher_query", "cypher_execute", "get_database_schema"],
        },
        "postgresql": {
            "category": "sql",
            "read": ["sql_query", "sql_schema"],
            "write": ["sql_query", "sql_schema", "sql_execute"],
        },
        "mongodb": {
            "category": "mongodb",
            "read": ["mongo_query", "mongo_aggregate", "mongo_schema"],
            "write": [
                "mongo_query",
                "mongo_aggregate",
                "mongo_schema",
                "mongo_insert",
                "mongo_update",
            ],
        },
        "webdav": {
            "category": "webdav",
            "read": ["webdav_list", "webdav_read", "webdav_info"],
            "write": [
                "webdav_list",
                "webdav_read",
                "webdav_info",
                "webdav_write",
                "webdav_delete",
            ],
        },
    }

    # Group datasources by type
    by_type: dict[str, list[dict[str, Any]]] = {}
    for ds in datasources:
        by_type.setdefault(ds["type"], []).append(ds)

    for ds_type, tool_info in DS_TOOL_MAP.items():
        category = tool_info["category"]
        ds_list = by_type.get(ds_type, [])
        if not ds_list:
            # No datasource attached — strip the category
            tools_override[category] = []
            continue

        # If ANY datasource of this type is read-write, use CLI mode
        # (tools stripped). If ALL are read-only, register read-only tools.
        all_read_only = all(ds.get("project_read_only", False) for ds in ds_list)

        if all_read_only:
            tools_override[category] = tool_info["read"]
        elif ds_type == "webdav":
            # WebDAV always uses tools (no good CLI)
            tools_override[category] = tool_info["write"]
        else:
            # Read-write managed connectors: CLI mode, no custom tools
            tools_override[category] = []

    override["tools"] = tools_override
    return override


def _apply_cloud_storage_override(
    resolved_ds: list[dict[str, Any]], job_context: dict[str, Any]
) -> None:
    """Apply job-level cloud_storage_read_only override to WebDAV datasources.

    If the job's context contains cloud_storage_read_only, it overrides the
    project-level read_only setting on any webdav datasource in the resolved list.
    Mutates resolved_ds in place.
    """
    override = job_context.get("cloud_storage_read_only")
    if override is None:
        return
    for ds in resolved_ds:
        if ds["type"] == "webdav":
            ds["project_read_only"] = bool(override)


def _build_datasources_payload(
    resolved_ds: list[dict[str, Any]],
) -> list[dict[str, Any]] | None:
    """Build the datasources payload for sending to the agent.

    Strips internal fields (id, job_id, created_at, updated_at) that the
    agent doesn't need. For read-only managed connectors, credentials are
    withheld (tools hold them internally).

    Args:
        resolved_ds: List of resolved datasource dicts from the database

    Returns:
        List of datasource dicts for the agent, or None if empty
    """
    if not resolved_ds:
        return None

    managed_types = {"postgresql", "neo4j", "mongodb", "webdav"}
    payload = []
    for ds in resolved_ds:
        creds = ds.get("credentials") or {}
        if isinstance(creds, str):
            import json as json_module

            try:
                creds = json_module.loads(creds)
            except (json.JSONDecodeError, ValueError):
                creds = {}

        is_read_only = ds.get("project_read_only", False)
        ds_type = ds["type"]

        # Read-only managed connectors: withhold credentials (tools hold them)
        if ds_type in managed_types and is_read_only:
            creds = {}

        entry = {
            "type": ds_type,
            "name": ds["name"],
            "description": ds.get("description"),
            "connection_url": ds.get("connection_url"),
            "credentials": creds,
            "project_read_only": is_read_only,
        }
        if ds.get("cli_hint"):
            entry["cli_hint"] = ds["cli_hint"]
        if ds.get("default_branch"):
            entry["default_branch"] = ds["default_branch"]

        payload.append(entry)

    return payload or None


@app.post("/api/jobs/{job_id}/assign/{agent_id}")
async def assign_job_to_agent(
    request: Request, job_id: str, agent_id: str
) -> dict[str, str]:
    """Manually assign a job to an agent.

    **Admin only** (P4c): manual dispatch override bypasses the
    auto-assign dispatcher's queue, so only admins can call it.

    Validates job and agent status, then delegates to the shared dispatch helper.
    Accepts jobs in 'created', 'failed', or 'paused' status.
    """
    await _require_admin(request)
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


def _normalize_datasource_credentials(
    credentials: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Validate and normalize secret fields in a datasource credentials dict.

    Currently this means: if an ``ssh_key`` is present, run it through
    :func:`validate_private_key`, which trims surrounding whitespace,
    normalizes line endings, and ensures the single trailing newline that
    OpenSSL/libcrypto requires. Raises ``HTTPException(400)`` if the key
    fails structural validation.
    """
    if not credentials:
        return credentials
    ssh_key = credentials.get("ssh_key")
    if ssh_key is None:
        return credentials
    try:
        credentials["ssh_key"] = _validate_ssh_private_key(ssh_key)
    except InvalidSSHKeyError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid ssh_key: {exc}") from exc
    return credentials


@app.post(
    "/api/datasources/ssh-keys/generate",
    response_model=SSHKeyGenerateResponse,
)
async def generate_datasource_ssh_key(
    request: Request,
    body: SSHKeyGenerateRequest | None = None,
) -> SSHKeyGenerateResponse:
    """Generate a fresh ed25519 SSH keypair for the user to paste into the form.

    **P4e** — gated to approved users. The keypair is ephemeral (no DB
    write); the gate just blocks anonymous CPU-burn from key generation.

    The private half is returned in OpenSSH PEM format (already normalized
    with a trailing newline so it round-trips through validation) and the
    public half is returned in the single-line authorized_keys format the
    user pastes into their provider's deploy-keys UI. The server does not
    persist the keypair — storage happens when the user submits the
    datasource form, which re-validates the private key via the same
    ssh_key path as a hand-pasted key.
    """
    await require_approved_user(request, postgres_db)
    comment = (body.comment if body else None) or ""
    keypair = _generate_ed25519_keypair(comment=comment)
    return SSHKeyGenerateResponse(
        private_key=keypair.private_key,
        public_key=keypair.public_key,
    )


@app.get("/api/datasources")
async def list_datasources(
    request: Request,
    job_id: str | None = Query(
        default=None, description="Filter by job ID (use 'global' for global-only)"
    ),
    type: str | None = Query(
        default=None, description="Filter by type (postgresql, neo4j, mongodb)"
    ),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict[str, Any]]:
    """List datasources visible to the caller.

    F3: each row is scoped (admin / creator / project member) and the
    `credentials` field is stripped from every row.
    """
    user = await require_approved_user(request, postgres_db)
    try:
        rows = await postgres_db.list_datasources(
            job_id=job_id, ds_type=type, limit=limit
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    visible: list[dict[str, Any]] = []
    for ds in rows:
        if await user_can_access_datasource(user, postgres_db, ds):
            visible.append(ds)
    return redact_datasources(visible)


@app.get("/api/datasources/eligible")
async def list_eligible_datasources(
    request: Request,
    project_id: list[str] | None = Query(
        default=None,
        description="Project(s) to include linked datasources for (repeatable)",
    ),
) -> list[dict[str, Any]]:
    """Datasources the caller may pre-select for a job/session (the picker
    source of truth).

    Returns the union of: datasources the caller created, global datasources,
    and datasources linked to any supplied project. Credentials are stripped.
    Membership is required for each supplied project (403 otherwise). Used to
    seed the create-job / create-session datasource picker; with explicit-only
    resolution the picker is the only way a job gets datasources.
    """
    user = await require_approved_user(request, postgres_db)
    project_ids = project_id or []
    for pid in project_ids:
        await require_project_member(request, postgres_db, pid)
    try:
        rows = await postgres_db.list_eligible_datasources(
            str(user["id"]),
            project_ids,
            is_admin=bool(user.get("is_admin")),
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    return redact_datasources(rows)


@app.get("/api/datasources/{datasource_id}")
async def get_datasource(request: Request, datasource_id: str) -> dict[str, Any]:
    """Get a single datasource by ID. F3: gated + credentials redacted."""
    _, ds = await require_datasource_access(request, postgres_db, datasource_id)
    return redact_datasource(ds)


@app.post("/api/datasources")
async def create_datasource(body: DatasourceCreate, request: Request) -> dict[str, Any]:
    """Create a new datasource owned by the current user."""
    valid_types = {
        "generic",
        "repository",
        "postgresql",
        "neo4j",
        "mongodb",
        "webdav",
        "kubeconfig",
        "ssh_key",
        "generic_file",
    }
    if body.type not in valid_types:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid type '{body.type}'. Must be one of: {', '.join(sorted(valid_types))}",
        )

    user = await require_approved_user(request, postgres_db)
    user_id = str(user["id"])

    credentials = _normalize_datasource_credentials(body.credentials)
    try:
        credentials = normalize_credential_files(body.type, body.name, credentials)
    except CredentialFileValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        created = await postgres_db.create_datasource(
            name=body.name,
            ds_type=body.type,
            connection_url=body.connection_url,
            description=body.description,
            credentials=credentials,
            job_id=body.job_id,
            cli_hint=body.cli_hint,
            default_branch=body.default_branch,
            created_by=user_id,
            is_global=body.is_global,
        )
    except HTTPException:
        raise
    except Exception as e:
        error_msg = str(e)
        if "unique" in error_msg.lower() or "duplicate" in error_msg.lower():
            raise HTTPException(
                status_code=409,
                detail=f"A datasource named '{body.name}' of type '{body.type}' already exists",
            ) from e
        raise HTTPException(status_code=500, detail=error_msg) from e
    return redact_datasource(created)


@app.put("/api/datasources/{datasource_id}")
async def update_datasource(
    request: Request, datasource_id: str, body: DatasourceUpdate
) -> dict[str, str]:
    """Update a datasource. F3: creator/admin only; null/empty credentials preserved."""
    _, existing_ds = await require_datasource_owner(request, postgres_db, datasource_id)
    # F3: if body.credentials is None or {}, do NOT touch the stored value.
    # The cockpit's edit form sends an empty creds dict when the user
    # didn't re-enter; passing that through would clobber the secret.
    raw_creds = _normalize_datasource_credentials(body.credentials)
    credentials = raw_creds if raw_creds else None
    if credentials is not None and existing_ds.get("type") in CREDENTIAL_FILE_TYPES:
        try:
            credentials = normalize_credential_files(
                existing_ds["type"],
                body.name or existing_ds.get("name", ""),
                credentials,
            )
        except CredentialFileValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        success = await postgres_db.update_datasource(
            datasource_id=datasource_id,
            name=body.name,
            description=body.description,
            connection_url=body.connection_url,
            credentials=credentials,
            cli_hint=body.cli_hint,
            default_branch=body.default_branch,
        )
        if not success:
            raise HTTPException(
                status_code=404, detail=f"Datasource '{datasource_id}' not found"
            )

        # Re-sync knowledge entries for all linked projects
        linked_projects = await postgres_db.list_datasource_projects(datasource_id)
        if linked_projects:
            updated_ds = await postgres_db.get_datasource(datasource_id)
            if updated_ds:
                for pid in linked_projects:
                    await _sync_datasource_knowledge(pid, updated_ds)

        return {"status": "updated"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.delete("/api/datasources/{datasource_id}")
async def delete_datasource(request: Request, datasource_id: str) -> dict[str, str]:
    """Delete a datasource. F3: creator/admin only."""
    await require_datasource_owner(request, postgres_db, datasource_id)
    try:
        # Clean up knowledge entries for all linked projects before deletion
        linked_projects = await postgres_db.list_datasource_projects(datasource_id)
        for pid in linked_projects:
            await _delete_datasource_knowledge(pid, datasource_id)

        success = await postgres_db.delete_datasource(datasource_id)
        if not success:
            raise HTTPException(
                status_code=404, detail=f"Datasource '{datasource_id}' not found"
            )
        return {"status": "deleted"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/jobs/{job_id}/datasources")
async def get_job_datasources(request: Request, job_id: str) -> list[dict[str, Any]]:
    """Get resolved datasources for a job.

    F3: gated by `require_job_access`; credentials redacted in the
    response (the agent process gets them via internal dispatch, not via
    this endpoint).
    """
    await require_job_access(request, postgres_db, job_id)
    try:
        rows = await postgres_db.resolve_datasources_for_job(job_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    return redact_datasources(rows)


@app.post("/api/datasources/{datasource_id}/test")
async def test_datasource(request: Request, datasource_id: str) -> dict[str, Any]:
    """Test connectivity to a datasource.

    Attempts to connect using the stored connection details and returns
    the result. Does not modify any data. F3: creator/admin only (test
    uses live credentials and probes the target).
    """
    try:
        _, ds = await require_datasource_owner(request, postgres_db, datasource_id)
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

        elif ds_type == "webdav":
            try:
                from webdav3.client import Client as WebDAVClient

                client = WebDAVClient(
                    {
                        "webdav_hostname": url,
                        "webdav_login": creds.get("username"),
                        "webdav_password": creds.get("password"),
                    }
                )
                client.list("/")
                return {"status": "ok", "message": "Connected to WebDAV"}
            except Exception as e:
                return {"status": "error", "message": str(e)}

        elif ds_type in ("generic", "repository"):
            return {
                "status": "ok",
                "message": f"No connectivity test for {ds_type} datasources",
            }

        else:
            return {"status": "error", "message": f"Unknown datasource type: {ds_type}"}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


# =============================================================================
# Statistics Endpoints
# =============================================================================


async def _visibility_kwargs_for_stats(user: dict[str, Any]) -> dict[str, Any]:
    """Build the visibility kwargs G5 passes through to postgres stats methods.

    Admin without an MCP project: scope → empty dict (full fleet view).
    Admin with project scope → just ``scope_project_id`` (AND-narrowed).
    Non-admin → owner_user_id + visible_project_ids (+ scope_project_id).
    """
    scope_pid = mcp_scope_project_id(user)
    if user.get("is_admin"):
        if scope_pid is None:
            return {}
        return {"scope_project_id": str(scope_pid)}
    visible = await user_visible_project_ids(user, postgres_db)
    project_ids = [str(p) for p in visible] if visible != "all" else []
    return {
        "owner_user_id": str(user["id"]),
        "visible_project_ids": project_ids,
        "scope_project_id": str(scope_pid) if scope_pid else None,
    }


@app.get("/api/stats/jobs")
async def get_job_statistics(request: Request) -> dict[str, int]:
    """Get overall job statistics scoped to the caller's visibility (G5).

    Admins see the full fleet (optionally narrowed by an MCP
    ``project:<uuid>`` scope). Non-admins see only jobs they own or
    are project members of.
    """
    user = await require_approved_user(request, postgres_db)
    vis = await _visibility_kwargs_for_stats(user)
    try:
        return await postgres_db.get_job_statistics(**vis)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/stats/daily")
async def get_daily_statistics(
    request: Request,
    days: int = Query(default=7, ge=1, le=90),
) -> list[dict[str, Any]]:
    """Get daily job statistics for the past N days, scoped to the caller's visibility (G5)."""
    user = await require_approved_user(request, postgres_db)
    vis = await _visibility_kwargs_for_stats(user)
    try:
        return await postgres_db.get_daily_statistics(days=days, **vis)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/stats/agents")
async def get_agent_statistics(request: Request) -> dict[str, Any]:
    """Get agent workforce summary. **Admin only** (G5) — counts the
    fleet by status, which is infra-level data tied to ``/api/agents``."""
    await _require_admin(request)
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
    request: Request,
    threshold_minutes: int = Query(default=60, ge=1, le=1440),
) -> list[dict[str, Any]]:
    """Get jobs that appear to be stuck, scoped to the caller's visibility (G5).

    A job is considered stuck if it's in 'processing' status but hasn't
    been updated within the threshold period. Admins see the full
    fleet; non-admins see only jobs they own or are project members of.
    """
    user = await require_approved_user(request, postgres_db)
    vis = await _visibility_kwargs_for_stats(user)
    try:
        return await postgres_db.detect_stuck_jobs(
            threshold_minutes=threshold_minutes, **vis
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


def _parse_utc_date(s: str) -> datetime:
    """Parse an ISO date/datetime as tz-aware UTC (naive → assumed UTC)."""
    d = datetime.fromisoformat(s)
    return (
        d.replace(tzinfo=timezone.utc)
        if d.tzinfo is None
        else d.astimezone(timezone.utc)
    )


@app.get("/api/usage")
async def get_usage(
    request: Request,
    days: int = Query(default=30, ge=1, le=365),
    from_date: str | None = Query(
        default=None, description="ISO date/datetime (UTC); overrides `days`"
    ),
    to_date: str | None = Query(
        default=None, description="ISO date/datetime (UTC), exclusive"
    ),
    ref_id: str | None = Query(default=None, description="Filter to one job/thread id"),
) -> dict[str, Any]:
    """Aggregate usage (LLM tokens + workspace compute) for the caller (G5).

    Reads the ``usage_events`` ledger (Slice 4). Window defaults to the last
    ``days``; ``from_date``/``to_date`` override it. Admins see the full fleet
    (optionally narrowed by an MCP ``project:`` scope); non-admins see only rows
    they own or can see via project membership. Returns sums by (category, unit)
    plus the headline ``total_cost_usd`` (0 while resources are unpriced).
    ``available=false`` means the audit tier is off — metering disabled.
    """
    user = await require_approved_user(request, postgres_db)
    if usage_ledger is None or not usage_ledger.is_available:
        return {"by_category": [], "total_cost_usd": 0.0, "available": False}
    try:
        now = datetime.now(timezone.utc)
        to_ts = _parse_utc_date(to_date) if to_date else now
        from_ts = (
            _parse_utc_date(from_date) if from_date else (to_ts - timedelta(days=days))
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"invalid date: {e}") from e
    vis = await _visibility_kwargs_for_stats(user)
    try:
        result = await usage_ledger.query_usage(
            from_ts=from_ts,
            to_ts=to_ts,
            owner_user_id=vis.get("owner_user_id"),
            visible_project_ids=vis.get("visible_project_ids"),
            scope_project_id=vis.get("scope_project_id"),
            ref_id=ref_id,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    result["available"] = True
    result["from"] = from_ts.isoformat()
    result["to"] = to_ts.isoformat()
    return result


# =============================================================================
# Citation & Source Library Endpoints
# =============================================================================


@app.get("/api/sources")
async def list_sources(
    request: Request,
    job_id: str | None = Query(default=None, description="Filter by job ID"),
    type: str | None = Query(default=None, description="Filter by source type"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """List sources, optionally filtered by job and/or type.

    Visibility model (G3):
        * With ``?job_id=``: gate on ``require_job_access``. The job's
          sources are returned (visible if the caller can access the job).
        * Without ``?job_id=``: admin-only. Non-admins must filter by a
          specific job they can access. (Cross-job source enumeration
          would require a vector_db ⇆ postgres_db JOIN we deliberately
          don't do; the per-job path covers the legitimate cockpit use
          case without that complexity.)
    """
    if job_id:
        await require_job_access(request, postgres_db, job_id)
    else:
        caller = await require_approved_user(request, postgres_db)
        if not caller.get("is_admin"):
            raise HTTPException(
                status_code=403,
                detail="Cross-job source listing requires admin role; "
                "non-admins must pass ?job_id=",
            )
    try:
        async with vector_db.acquire() as conn:
            conditions = []
            params: list[Any] = []
            idx = 1

            if job_id:
                conditions.append(
                    f"s.id IN (SELECT source_id FROM job_sources WHERE job_id = ${idx}::uuid)"
                )
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
    request: Request,
    source_id: int,
    content_limit: int = Query(default=2000, ge=0, le=100000),
) -> dict[str, Any]:
    """Get full detail for a single source.

    Visibility (G3): the source is visible if the caller can access at
    least one job linked to it via ``job_sources``. Admins (without an
    MCP project: scope) bypass without enumerating links.
    """
    caller = await require_approved_user(request, postgres_db)
    try:
        async with vector_db.acquire() as conn:
            if content_limit > 0:
                row = await conn.fetchrow(
                    """SELECT id, type::text as type, identifier, name, version,
                          LEFT(content, $2) as content, content_hash, metadata, created_at,
                          LENGTH(content) as full_content_length
                    FROM sources WHERE id = $1""",
                    source_id,
                    content_limit,
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
                raise HTTPException(
                    status_code=404, detail=f"Source {source_id} not found"
                )

            result = dict(row)
            result["content_truncated"] = (
                content_limit > 0
                and result.get("full_content_length", 0) > content_limit
            )

            # Include linked job IDs
            job_rows = await conn.fetch(
                "SELECT job_id FROM job_sources WHERE source_id = $1", source_id
            )
            result["job_ids"] = [str(r["job_id"]) for r in job_rows]

            # G3 visibility: caller must be able to access at least one
            # linked job. Admins with no MCP project: scope skip the loop.
            if not await user_can_access_any_job(
                caller, postgres_db, result["job_ids"]
            ):
                raise HTTPException(
                    status_code=403, detail="Not authorized to access this source"
                )

            return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/jobs/{job_id}/citations")
async def list_job_citations(
    request: Request,
    job_id: str,
    source_id: int | None = Query(default=None),
    status: str | None = Query(
        default=None, description="Filter by verification_status"
    ),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """List citations for a job with optional filters."""
    await require_job_access(request, postgres_db, job_id)
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


@app.post("/api/citations/snapshot")
async def store_citation_snapshot(request: Request) -> dict[str, Any]:
    """Persist the original bytes of a cited cloud document (Phase 3, D7).

    **Internal** (P4b) — requires ``X-Internal-Key``; the agent calls this at
    cite-time because it has no S3 credentials of its own. The request body is
    the raw file bytes; ``content_type`` is an optional query param used when
    the blob is later served back. Returns a content-addressed
    ``snapshot_blob_key`` the agent records onto the citation source's
    ``metadata.cloud`` so the original can be retrieved on view.
    """
    await require_internal(request)
    if not snapshot_service.is_available:
        raise HTTPException(status_code=503, detail="Snapshot store unavailable")
    data = await request.body()
    if not data:
        raise HTTPException(status_code=400, detail="Empty body")
    content_type = (
        request.query_params.get("content_type") or "application/octet-stream"
    )
    key = await snapshot_service.save_blob(
        data, prefix="citations", content_type=content_type
    )
    if not key:
        raise HTTPException(status_code=500, detail="Snapshot store write failed")
    return {"snapshot_blob_key": key, "size_bytes": len(data)}


@app.get("/api/citations/{citation_id}")
async def get_citation_detail(request: Request, citation_id: int) -> dict[str, Any]:
    """Get full citation record with source info and verification details.

    **P4e** — visible if the caller can access the citation's linked job
    (mirrors G3's ``get_source_detail`` pattern). Admins without an MCP
    ``project:<uuid>`` scope bypass. Missing/unauthorized → 404 to avoid
    leaking citation existence via probe.
    """
    caller = await require_approved_user(request, postgres_db)
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
                raise HTTPException(
                    status_code=404, detail=f"Citation {citation_id} not found"
                )

            result = dict(row)
            job_id = result.get("job_id")
            if not await user_can_access_job_or_thread(
                caller, postgres_db, str(job_id) if job_id else None
            ):
                # 404 instead of 403 — don't leak that the citation exists.
                raise HTTPException(
                    status_code=404, detail=f"Citation {citation_id} not found"
                )

            return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


def _source_cloud_meta(metadata: Any) -> dict[str, Any]:
    """Extract the ``metadata.cloud`` block from a ``sources.metadata`` value.

    The vector pool may hand back JSONB as a dict (codec) or a JSON string;
    coerce both, and return ``{}`` when there's no cloud snapshot-anchor.
    """
    if isinstance(metadata, (str, bytes)):
        try:
            metadata = json.loads(metadata)
        except (ValueError, TypeError):
            return {}
    if not isinstance(metadata, dict):
        return {}
    cloud = metadata.get("cloud")
    return cloud if isinstance(cloud, dict) else {}


def _home_relative_path(anchor_webdav_url: str, home_webdav_url: str) -> Optional[str]:
    """Path of a cited file relative to the viewing user's home, if it's under it.

    Phase 3c (D7) only re-fetches a cited cloud file for the drift check when it
    is provably inside the *viewing user's own* cloud home — i.e. the anchor's
    WebDAV URL starts with the user's home WebDAV URL. Returns the home-relative
    path (no leading slash) in that case, else ``None`` (→ ``live_state =
    unreachable``: an external datasource or a different cloud the orchestrator
    can't fetch on the user's behalf). This both guards against comparing a
    same-named-but-different file and yields the path for
    ``get_project_folder_file_bytes``.
    """
    if not anchor_webdav_url or not home_webdav_url:
        return None
    base = home_webdav_url.rstrip("/")
    if not anchor_webdav_url.startswith(base):
        return None
    return anchor_webdav_url[len(base) :].lstrip("/") or None


@app.get("/api/citations/{citation_id}/snapshot")
async def get_citation_snapshot(request: Request, citation_id: int) -> Response:
    """Serve the backed-up original bytes of a cited cloud document (Phase 3c, D7).

    Viewing-user auth (same gate as ``get_citation_detail``). Returns the copy
    SRW stored at cite-time (``metadata.cloud.snapshot_blob_key``) so a citation
    can show the exact version cited even when the live source changed or is
    unreachable. 404 if the citation is unknown/unauthorized or has no snapshot
    (404 over 403 so citation existence isn't leaked by probing).
    """
    caller = await require_approved_user(request, postgres_db)
    try:
        async with vector_db.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT c.job_id, s.name AS source_name, s.metadata
                   FROM citations c JOIN sources s ON c.source_id = s.id
                   WHERE c.id = $1""",
                citation_id,
            )
        if not row:
            raise HTTPException(
                status_code=404, detail=f"Citation {citation_id} not found"
            )
        job_id = row["job_id"]
        if not await user_can_access_job_or_thread(
            caller, postgres_db, str(job_id) if job_id else None
        ):
            raise HTTPException(
                status_code=404, detail=f"Citation {citation_id} not found"
            )

        cloud = _source_cloud_meta(row["metadata"])
        key = cloud.get("snapshot_blob_key")
        if not key:
            raise HTTPException(
                status_code=404, detail="No snapshot stored for this citation"
            )
        data = await snapshot_service.get_blob(key)
        if data is None:
            raise HTTPException(status_code=404, detail="Snapshot blob not found")

        media_type = cloud.get("content_type") or "application/octet-stream"
        filename = (row["source_name"] or f"citation-{citation_id}").replace('"', "")
        return Response(
            content=data,
            media_type=media_type,
            headers={"Content-Disposition": f'inline; filename="{filename}"'},
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/citations/{citation_id}/drift")
async def get_citation_drift(request: Request, citation_id: int) -> dict[str, Any]:
    """On-view drift check for a cited cloud document (Phase 3c, D7).

    Viewing-user auth. Compares the live source against what was cited. A
    best-effort re-fetch runs **only** when the cited file is provably inside the
    viewing user's own cloud home (so the credentials are the user's, never the
    agent's expired ones, and we never compare a same-named different file).
    Returns:

    - ``live_state``: ``unchanged`` | ``changed`` | ``unreachable`` | ``unknown``
    - ``snapshot_available``: whether ``/snapshot`` can serve the backed-up copy
    - ``cited``: the drift fingerprint captured at cite-time

    ``unreachable`` (external datasource / different cloud / no access) is the
    spec's "fall back to the snapshot" branch.
    """
    caller = await require_approved_user(request, postgres_db)
    try:
        async with vector_db.acquire() as conn:
            row = await conn.fetchrow(
                """SELECT c.job_id, s.metadata
                   FROM citations c JOIN sources s ON c.source_id = s.id
                   WHERE c.id = $1""",
                citation_id,
            )
        if not row:
            raise HTTPException(
                status_code=404, detail=f"Citation {citation_id} not found"
            )
        job_id = row["job_id"]
        if not await user_can_access_job_or_thread(
            caller, postgres_db, str(job_id) if job_id else None
        ):
            raise HTTPException(
                status_code=404, detail=f"Citation {citation_id} not found"
            )

        cloud = _source_cloud_meta(row["metadata"])
        if not cloud:
            raise HTTPException(
                status_code=400, detail="Citation has no cloud source to drift-check"
            )

        cited_sha = cloud.get("file_sha256")
        result: dict[str, Any] = {
            "citation_id": citation_id,
            "live_state": "unknown",
            "snapshot_available": bool(cloud.get("snapshot_blob_key")),
            "cited": {
                "etag": cloud.get("etag"),
                "file_sha256": cited_sha,
                "captured_at": cloud.get("captured_at"),
                "webdav_url": cloud.get("webdav_url"),
                "backend": cloud.get("backend"),
            },
        }

        # Best-effort live re-fetch via the viewing user's own cloud home.
        try:
            backend = main_cloud_router.for_owner(caller)
            user_id = await backend.resolve_user_identity(
                email=caller.get("email"),
                display_name=caller.get("display_name"),
            )
            home = await backend.get_user_home(user_id) if user_id else None
            rel = (
                _home_relative_path(
                    cloud.get("webdav_url") or "", home.webdav_url or ""
                )
                if home and home.webdav_url
                else None
            )
            if rel is None:
                result["live_state"] = "unreachable"
                result["reason"] = "live source not reachable from your account"
                return result
            live_bytes = await backend.get_project_folder_file_bytes(
                home.handle, path=rel
            )
            live_sha = hashlib.sha256(live_bytes).hexdigest()
            result["live"] = {"file_sha256": live_sha, "size_bytes": len(live_bytes)}
            result["live_state"] = (
                "unchanged" if cited_sha and live_sha == cited_sha else "changed"
            )
            return result
        except Exception as e:
            logger.info("Citation %s drift live-fetch unreachable: %s", citation_id, e)
            result["live_state"] = "unreachable"
            result["reason"] = "live source could not be fetched"
            return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/jobs/{job_id}/sources/{source_id}/annotations")
async def get_source_annotations(
    request: Request,
    job_id: str,
    source_id: int,
    type: str | None = Query(default=None, description="Filter by annotation_type"),
) -> list[dict[str, Any]]:
    """Get annotations for a source within a job."""
    await require_job_access(request, postgres_db, job_id)
    try:
        async with vector_db.acquire() as conn:
            if type:
                rows = await conn.fetch(
                    """SELECT id, annotation_type, content, page_reference, created_at, created_by
                    FROM source_annotations
                    WHERE source_id = $1 AND job_id = $2::uuid AND annotation_type = $3
                    ORDER BY created_at""",
                    source_id,
                    job_id,
                    type,
                )
            else:
                rows = await conn.fetch(
                    """SELECT id, annotation_type, content, page_reference, created_at, created_by
                    FROM source_annotations
                    WHERE source_id = $1 AND job_id = $2::uuid
                    ORDER BY created_at""",
                    source_id,
                    job_id,
                )

            return [dict(r) for r in rows]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/jobs/{job_id}/sources/{source_id}/tags")
async def get_source_tags(request: Request, job_id: str, source_id: int) -> list[str]:
    """Get tags for a source within a job."""
    await require_job_access(request, postgres_db, job_id)
    try:
        async with vector_db.acquire() as conn:
            rows = await conn.fetch(
                "SELECT tag FROM source_tags WHERE source_id = $1 AND job_id = $2::uuid ORDER BY tag",
                source_id,
                job_id,
            )
            return [r["tag"] for r in rows]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/jobs/{job_id}/citations/stats")
async def get_citation_stats(request: Request, job_id: str) -> dict[str, Any]:
    """Get citation statistics for a job."""
    await require_job_access(request, postgres_db, job_id)
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
async def get_memory_stats(request: Request, job_id: str) -> dict[str, Any]:
    """Get memory statistics for a job."""
    await require_job_access(request, postgres_db, job_id)
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
async def get_project_memory_stats(request: Request, project_id: str) -> dict[str, Any]:
    """Get memory statistics for a project (all memories scoped to this project)."""
    await require_project_member(request, postgres_db, project_id)
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
    request: Request,
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
    await require_job_access(request, postgres_db, job_id)
    # Validate sort parameters
    valid_sort_fields = {
        "created_at",
        "importance",
        "access_count",
        "token_count",
        "last_accessed",
    }
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
    request: Request,
    job_id: str,
    query: str = Query(..., description="Search query"),
    mode: str = Query(
        default="keyword", description="Search mode: keyword, semantic, hybrid"
    ),
    source_type: str | None = Query(default=None),
    tags: str | None = Query(
        default=None, description="Comma-separated tags (AND logic)"
    ),
    top_k: int = Query(default=10, ge=1, le=50),
) -> dict[str, Any]:
    """Search a job's source library using keyword search.

    Falls back to SQL keyword search. Semantic/hybrid modes require
    the CitationEngine with pgvector.
    """
    await require_job_access(request, postgres_db, job_id)
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

                results.append(
                    {
                        "source_id": r["id"],
                        "source_name": r["name"],
                        "source_type": r["type"],
                        "identifier": r["identifier"],
                        "evidence_label": evidence,
                        "rank": rank,
                        "snippet": r["snippet"],
                    }
                )

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
async def register_agent(
    request: Request, registration: AgentRegistration
) -> AgentRegistrationResponse:
    """Register a new agent or update existing one. **Internal** (P4b) —
    requires ``X-Internal-Key``. Public ingress also strips this path.

    When an agent starts up, it calls this endpoint to register itself.
    If an agent with the same hostname exists, its pod_ip is updated.

    Returns:
        AgentRegistrationResponse with agent_id and heartbeat_interval_seconds
    """
    await require_internal(request)
    try:
        result = await postgres_db.register_agent(
            config_name=registration.config_name,
            pod_ip=registration.pod_ip,
            hostname=registration.hostname,
            pod_port=registration.pod_port,
            pid=registration.pid,
            agent_mode=registration.agent_mode,
            thread_id=registration.thread_id,
            build_sha=registration.build_sha,
            pod_uid=registration.pod_uid,
        )
        # Bind persistent agent to its thread. Defense-in-depth against the
        # double-provisioning race (docs/issues/persistent_thread_double_provisioning_race.md):
        # take the per-thread advisory lock and refuse the bind if a *different*
        # live agent already owns this thread — turns a silent overwrite into
        # a loud 409 the orphan pod can react to by shutting down.
        if registration.agent_mode == "persistent" and registration.thread_id:
            new_id = str(result["agent_id"])
            async with postgres_db.thread_advisory_lock(registration.thread_id):
                thread = await postgres_db.get_thread(registration.thread_id)
                existing_id = thread.get("agent_id") if thread else None
                if existing_id and str(existing_id) != new_id:
                    existing = await postgres_db.get_agent(str(existing_id))
                    existing_status = (existing or {}).get("status")
                    if existing and existing_status not in (
                        None,
                        "offline",
                        "failed",
                    ):
                        logger.warning(
                            "register_agent: duplicate persistent registration "
                            "for thread %s; winner=%s loser=%s — refusing.",
                            registration.thread_id,
                            existing_id,
                            new_id,
                        )
                        try:
                            await postgres_db.delete_agent(new_id)
                        except Exception as del_err:
                            logger.warning(
                                "register_agent: failed to roll back loser %s: %s",
                                new_id,
                                del_err,
                            )
                        raise HTTPException(
                            status_code=409,
                            detail="thread already bound to another live agent",
                        )
                try:
                    await postgres_db.update_thread_agent(
                        registration.thread_id, new_id
                    )
                except Exception as bind_err:
                    logger.warning(f"Thread binding failed (non-fatal): {bind_err}")
        return AgentRegistrationResponse(**result)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


# --- Agent-facing thread endpoints (no auth, same as /api/agents/register) ---


class AgentThreadCreateRequest(BaseModel):
    """Request from agent to create its own thread on startup."""

    config_name: str = Field("persistent_defaults", description="Agent config name")
    permission_mode: str = Field("supervised", description="Permission mode")
    title: str = Field("Local Session", description="Session title")


class AgentThreadMessageRequest(BaseModel):
    """Request from agent to save a message."""

    role: str
    content: str | None = None
    tool_calls: list[dict] | None = None
    turn_number: int | None = None
    metrics: dict | None = None
    # Links a role='tool' row back to its originating tool_calls[].id.
    tool_call_id: str | None = None
    # Reasoning content captured from role='ai' rows. See migration 0011.
    thinking: str | None = None
    # Component columns added in migration 0019 — all optional/nullable.
    reasoning: Any | None = None
    tool_results: Any | None = None
    provider: str | None = None
    provider_raw: Any | None = None
    additional_kwargs: dict | None = None
    response_metadata: dict | None = None


@app.post("/api/agents/threads")
async def agent_create_thread(
    request: Request, body: AgentThreadCreateRequest
) -> dict[str, Any]:
    """Agent creates its own thread on startup. **Internal** (P4b) —
    requires ``X-Internal-Key``. Ingress strips this path.

    Used by persistent agents starting with ORCHESTRATOR_URL set.
    Creates a thread with user_id=NULL (visible to all cockpit users).
    """
    await require_internal(request)
    try:
        thread_id = await postgres_db.create_thread(
            user_id=None,
            config_name=body.config_name,
            permission_mode=body.permission_mode,
            title=body.title,
        )

        # Inject system-default model pins so the standalone agent boots
        # against the catalog (with resolved base_url + api_key) instead of
        # falling through to its YAML default. Same shape as the cockpit
        # create_thread path above, just without user prefs since this
        # endpoint has no user context.
        config_override: dict[str, Any] = {}
        chat_model = await postgres_db.resolve_default_for_capability("chat")
        if chat_model:
            llm_section: dict[str, Any] = {"model": chat_model}
            await _inject_model_credentials(
                section=llm_section,
                model_id=chat_model,
                user_id=None,
                resolved_keys=None,
            )
            config_override["llm"] = llm_section
        aux_model = await postgres_db.resolve_default_for_capability("auxiliary")
        if aux_model:
            aux_section: dict[str, Any] = {"model": aux_model}
            await _inject_model_credentials(
                section=aux_section,
                model_id=aux_model,
                user_id=None,
                resolved_keys=None,
                capability="auxiliary",
            )
            config_override["auxiliary"] = aux_section
        embedding_model = await postgres_db.resolve_default_for_capability("embedding")
        if embedding_model:
            env_keys_block: dict[str, Any] = {"EMBEDDING_MODEL": embedding_model}
            await _inject_env_key_credentials(
                env_keys=env_keys_block,
                prefix="EMBEDDING",
                model_id=embedding_model,
                user_id=None,
                resolved_keys=None,
                capability="embedding",
            )
            config_override["env_keys"] = env_keys_block
        if config_override:
            async with postgres_db.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE threads
                    SET metadata = COALESCE(metadata, '{}') || $2::jsonb
                    WHERE id = $1
                    """,
                    thread_id,
                    json.dumps({"config_override": config_override}),
                )

        # Create Gitea repo for workspace versioning
        if not gitea_client.is_initialized and gitea_client.is_configured:
            await gitea_client.ensure_initialized()
        if gitea_client.is_initialized:
            repo_name = f"thread-{thread_id[:8]}"
            git_remote_url = await gitea_client.create_repo(repo_name)
            if git_remote_url:
                await postgres_db.merge_thread_workspace_context(
                    thread_id,
                    {"git_remote_url": git_remote_url, "repo_name": repo_name},
                )

        # Provision workspace container in background if K8s is available
        # (in-cluster only) — unless this is a lite (virtual/none) session, which
        # runs with no workspace pod at all (no_workspace_agent_mode.md §4).
        if (
            container_provisioner.is_available
            and container_provisioner.in_cluster
            and _backend_from_override(config_override) not in LITE_BACKENDS
        ):
            await postgres_db.merge_thread_workspace_context(
                thread_id, {"status": "pending"}
            )
            asyncio.create_task(
                container_provisioner.create_workspace(
                    WorkspaceOwner.session(thread_id)
                )
            )

        return {"thread_id": thread_id, "status": "created"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


def _slugify_mount_name(name: str) -> str:
    """Workspace-safe slug for a mount's target_path."""
    out = "".join(ch.lower() if ch.isalnum() else "_" for ch in name).strip("_")
    return out or "project"


def _cloud_workspace_driver() -> str:
    return os.getenv("CLOUD_WORKSPACE_DRIVER", "sync").strip().lower() or "sync"


def _should_skip_session_folder(mounts: list[dict[str, Any]]) -> bool:
    """Phase 4 (cloud_collaboration_model.md §9): is the legacy per-session
    cloud folder redundant for this thread?

    If at least one ``thread_mounts`` row has a working ``webdav_url`` —
    any kind (``project``, ``project_default``, ``repo``) — the thread
    already has a user-visible cloud surface. Provisioning a per-session
    folder on top would create a parallel sync target the user has no
    reason to use.

    Returns False when no mount can be observed (no rows, or every row
    failed to resolve a transport). That falls through to legacy
    session-folder provisioning so the thread never ends up with zero
    cloud surfaces — important for unattached sessions and for transient
    backend failures during mount resolution.
    """
    if _cloud_workspace_driver() == "rclone_mount":
        # The rclone driver falls back to mounting the regular session folder
        # when a user-home/project mount cannot be represented safely. Keep that
        # fallback provisioned instead of treating a WebDAV URL as proof that
        # the runtime can mount the surface.
        return False

    for m in mounts:
        if m.get("webdav_url"):
            return True
    return False


def _project_ids_from_mounts(mounts: list[dict[str, Any]]) -> list[str]:
    """Pick out project ``source_ref``s from a list of mount rows.

    Both ``project`` (non-default, mounted under ``projects/<slug>/``) and
    ``project_default`` (default project, mounted at workspace root via the
    user's cloud home) rows contribute — the default project is still a
    project attachment for datasource resolution and visibility.
    """
    out: list[str] = []
    for m in mounts:
        if m.get("mount_kind") not in {"project", "project_default"}:
            continue
        ref = m.get("source_ref")
        if ref:
            out.append(str(ref))
    return out


async def _thread_project_ids(thread_id: str) -> list[str]:
    """Derive the project-attachment list for a thread from ``thread_mounts``.

    Replaces the legacy ``threads.metadata.project_ids`` JSONB read. Phase 1
    of cloud_collaboration_model.md §9. Only ``mount_kind='project'`` rows
    contribute — ``project_default`` and ``repo`` rows are different shapes
    on the agent side.

    **Lazy backfill (transitional):** threads that predate the migration
    have ``metadata.project_ids`` set but no ``thread_mounts`` rows. When
    we see that combination we materialize the missing rows on the spot,
    so the rest of the codebase observes ``thread_mounts`` as the single
    source of truth from the first access onwards. Remove this fallback
    one release after Phase 1 ships — by then every active thread has
    been touched at least once and the JSONB key is dead.
    """
    mounts = await postgres_db.list_thread_mounts(thread_id)
    ids = _project_ids_from_mounts(mounts)
    if ids:
        return ids

    # ---- lazy backfill from metadata.project_ids ----
    thread = await postgres_db.get_thread(thread_id)
    if not thread:
        return []
    metadata = thread.get("metadata") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (json.JSONDecodeError, TypeError):
            metadata = {}
    legacy_ids = metadata.get("project_ids") or []
    if not legacy_ids:
        return []
    try:
        rows = await _build_thread_mount_rows([str(p) for p in legacy_ids])
        if rows:
            await postgres_db.replace_thread_mounts(thread_id, rows)
            logger.info(
                "Thread %s: backfilled %d thread_mounts row(s) from "
                "legacy metadata.project_ids",
                thread_id,
                len(rows),
            )
            return _project_ids_from_mounts(
                await postgres_db.list_thread_mounts(thread_id)
            )
    except Exception as e:
        # Don't let a backfill failure prevent the caller from getting the
        # project_ids — fall back to the legacy list so behavior matches
        # the pre-migration era. The next access retries the backfill.
        logger.warning(
            "Thread %s: thread_mounts backfill failed (%s); "
            "returning legacy project_ids",
            thread_id,
            e,
        )
    return [str(p) for p in legacy_ids]


async def _build_default_project_mount_row(
    project_id: str, project: dict[str, Any]
) -> Optional[dict[str, Any]]:
    """Shape a ``project_default`` mount row for a default project.

    Resolves the project's owner on the cloud backend and queries their
    personal home Space. The mount targets the workspace root (``target_path
    = ""``) — the agent's workspace and the user's cloud home become two
    views of the same surface. Phase 2 of cloud_collaboration_model.md §9.

    Also stashes the owner's Keycloak ``sub`` on the row (``target_user_sub``)
    so the agent can mint a user-scoped token via RFC 8693 token-exchange
    at WebDAV-call time. Without that, the agent's service-account
    bearer token gets a 404 on PROPFIND against the user's Personal Space
    (owned by exactly one user, not shared with the service account).

    Returns ``None`` when the user-home can't be resolved (no owner, owner
    missing from the cloud backend, no webdav URL, no keycloak_sub on the
    owner, backend down). The caller treats ``None`` as "fall back to the
    legacy session folder" so a transient resolution failure never leaves
    the thread with zero mounts.
    """
    backend = main_cloud_router.for_project(project)
    if not backend.is_initialized:
        return None
    members = await postgres_db.get_project_members(project_id)
    owner = next((m for m in members if m.get("role") == "owner"), None)
    if not owner:
        return None
    owner_email = owner.get("email")
    if not owner_email:
        return None
    owner_user = await postgres_db.get_user(str(owner["user_id"]))
    target_user_sub = (owner_user or {}).get("keycloak_sub")
    if not target_user_sub:
        # Owner has never completed an SSO login, so we don't have their
        # Keycloak sub yet. Token-exchange impersonation needs a real sub —
        # bail out and let the caller fall back to the legacy session folder.
        return None
    owner_display = (owner.get("display_name") or "").lower()
    resolved = await backend.resolve_user_identity(owner_email, owner_display)
    if not resolved:
        return None
    home = await backend.get_user_home(resolved)
    if not home or not home.webdav_url:
        return None
    return {
        "mount_kind": "project_default",
        "target_path": "",
        "source_kind": "user_home",
        "source_ref": project_id,
        "backend_id": backend.backend_id,
        "cloud_handle": home.handle.to_db(),
        "webdav_url": home.webdav_url,
        "target_user_sub": target_user_sub,
    }


async def _build_thread_mount_rows(
    project_ids: list[str],
) -> list[dict[str, Any]]:
    """Resolve mount-row payloads for the given project_ids.

    Each row carries everything ``replace_thread_mounts`` needs. Default
    projects (Phase 2) produce a ``project_default`` row that mounts the
    owner's cloud home at the workspace root; non-default projects produce
    a ``project`` row that mounts at ``projects/<slug>/``. Projects whose
    cloud transport can't be resolved are skipped — the mount-row entry is
    not partially filled, the caller observes a missing row.

    Multi-project collisions (two attached projects whose slugified names
    are identical, including case-insensitive matches since the slugifier
    lowercases) are resolved by suffixing ``-2``, ``-3``, ... on the
    target_path so the ``UNIQUE (thread_id, target_path)`` constraint at
    persistence time always holds.
    """
    rows: list[dict[str, Any]] = []
    used_paths: set[str] = set()
    seen_project_ids: set[str] = set()
    for project_id in project_ids:
        if project_id in seen_project_ids:
            continue
        seen_project_ids.add(project_id)
        project = await postgres_db.get_project(project_id)
        if not project:
            continue
        if project.get("is_default"):
            try:
                default_row = await _build_default_project_mount_row(
                    project_id, project
                )
            except Exception as e:
                logger.warning(
                    "Project %s (default): failed to resolve user-home mount row: %s",
                    project_id,
                    e,
                )
                continue
            if default_row:
                rows.append(default_row)
                used_paths.add(default_row.get("target_path", ""))
            continue
        backend_id = project.get("main_cloud_backend")
        handle_str = project.get("main_cloud_folder_handle")
        webdav_url: str | None = None
        if backend_id and handle_str:
            try:
                backend = main_cloud_router.for_backend(backend_id)
                if backend.is_initialized:
                    handle = ProjectFolderHandle.from_db(
                        handle_str, backend=backend.backend_id
                    )
                    webdav_url = backend.get_project_folder_webdav_url(handle)
            except Exception as e:
                logger.warning(
                    "Project %s: failed to resolve webdav URL for thread mount: %s",
                    project_id,
                    e,
                )
        base_path = f"projects/{_slugify_mount_name(project.get('name', ''))}"
        target_path = base_path
        suffix = 2
        while target_path in used_paths:
            target_path = f"{base_path}-{suffix}"
            suffix += 1
        used_paths.add(target_path)
        rows.append(
            {
                "mount_kind": "project",
                "target_path": target_path,
                "source_kind": "project_folder",
                "source_ref": project_id,
                "backend_id": backend_id,
                "cloud_handle": handle_str,
                "webdav_url": webdav_url,
            }
        )
    return rows


async def _resolve_thread_datasources(
    metadata: dict[str, Any],
    *,
    project_ids: list[str] | None = None,
) -> list[dict[str, Any]] | None:
    """Resolve and build datasource payload for a thread.

    ``project_ids`` is the canonical input — derived from ``thread_mounts``
    by the caller. ``metadata`` still carries the explicit ``datasource_ids``
    list.
    """
    ds_ids = metadata.get("datasource_ids")
    if not ds_ids and not project_ids:
        return None
    resolved = await postgres_db.resolve_datasources_for_thread(
        datasource_ids=ds_ids, project_ids=project_ids
    )
    return _build_datasources_payload(resolved)


@app.get("/api/agents/threads/{thread_id}/workspace")
async def agent_get_thread_workspace(
    request: Request, thread_id: str
) -> dict[str, Any]:
    """Agent polls workspace container status for a thread. **Internal**
    (P4b) — requires ``X-Internal-Key``. Ingress strips this path.

    Returns workspace_container metadata from the thread,
    allowing the agent to wait for the workspace to be ready.
    """
    await require_internal(request)
    thread = await postgres_db.get_thread(thread_id)
    if not thread:
        raise HTTPException(status_code=404, detail="Thread not found")
    metadata = thread.get("metadata") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (json.JSONDecodeError, TypeError):
            metadata = {}
    ws = metadata.get("workspace_container") or {}
    vm = metadata.get("vm") or {}
    # Phase 1: project attachment + cloud mounts now live on thread_mounts.
    project_ids = await _thread_project_ids(thread_id)
    mount_rows = await postgres_db.list_thread_mounts(thread_id)
    cloud_mount_cfg = await _build_agent_cloud_mount(
        thread,
        mount_rows=mount_rows,
        metadata=metadata,
    )
    if cloud_mount_cfg:
        cloud_sync_cfg = None
    elif _cloud_workspace_driver() == "rclone_mount":
        # rclone requested but unavailable/unsupported: fall back to the
        # regular session folder only. Do not eagerly clone thread_mounts such
        # as a default user home; that is the startup failure this driver is
        # meant to avoid.
        cloud_sync_cfg = _build_agent_cloud_sync(thread, mount_rows=[])
    else:
        cloud_sync_cfg = _build_agent_cloud_sync(thread, mount_rows=mount_rows)
    # Issue 13 follow-up: if the main cloud is up but this thread resolved NO
    # sync target (session-folder provisioning failed upstream, or user-home /
    # project-mount resolution produced nothing usable), the agent would
    # otherwise run unsynced with no signal. Flag it so the agent surfaces the
    # same degraded-sync state it shows for a failed initial pull, instead of
    # silently skipping cloud sync for the session's whole life.
    try:
        _cloud_up = main_cloud_router.active.is_initialized
    except Exception:
        _cloud_up = False
    cloud_sync_degraded = bool(
        _cloud_up
        and not cloud_mount_cfg
        and not cloud_sync_cfg
        and not thread.get("nc_session_folder")
    )
    # Re-inject credentials in-flight: the persisted config_override is stripped
    # of secrets (redact_config_override at create/hot-swap). This endpoint is
    # the agent's key source on resume — its attach fallback in persistent_app.py
    # reads ``config_override`` from here. require_internal + ingress-stripped, so
    # plaintext stays on the agent trust boundary. Models/providers survive
    # stripping, so user_settings isn't needed to repopulate the keys.
    co = metadata.get("config_override") or {}
    if co:
        co = await _inject_thread_dispatch_credentials(
            co,
            user_id=str(thread["user_id"]) if thread.get("user_id") else None,
            project_id=str(thread["project_id"]) if thread.get("project_id") else None,
        )
    # Orchestrator-resolved config for cold/dedicated attach: the agent prefers
    # this fully-resolved, credential-injected blob over the config_override
    # merge above (which stays for the fallback). None when experts are off.
    # Session dispatch PEP (fail closed): a grant denial or resolve error must not
    # fall through to the unvetted config_override — refuse the attach (403).
    _sess_status: dict[str, Any] = {}
    try:
        session_resolved = await _resolve_session_config(
            thread, metadata, status=_sess_status
        )
    except GrantDenied as gd:
        raise HTTPException(
            status_code=403, detail=_grant_violations_detail(gd.violations)
        )
    if _sess_status.get("state") == "error":
        raise HTTPException(
            status_code=403,
            detail="capability grants could not be verified for this session config",
        )
    # Lite (virtual/none) sessions run with no workspace pod. Attach the
    # object-store mounts in-flight here — the same enrichment
    # _send_session_attach does for the idle-pool path — so a DEDICATED session
    # agent (provisioned when no pool agent is free) can build its lite backend
    # from this response. Without it _attach_session would poll for a workspace
    # pod that never exists and the agent would exit cleanly (the lite session
    # boot gap, no_workspace_agent_mode). No-op for sandbox/vm.
    try:
        co = _inject_lite_workspace_config(co, prefix=f"threads/{thread_id}/") or co
        # The resolved blob is the agent's PREFERRED hydration source, loaded via
        # load_agent_config_from_dict(resolved["agent"]) WITHOUT a config_override
        # merge (persistent_app._attach_session). Its agent.workspace already
        # carries the lite backend but NOT the in-flight object-store mounts —
        # attach them there, else create_lite_backend raises "requires
        # workspace.mounts" and the lite session can't boot.
        if isinstance(session_resolved, dict) and _backend_from_override(co) in (
            LITE_BACKENDS
        ):
            agent_ws = session_resolved.setdefault("agent", {}).setdefault(
                "workspace", {}
            )
            agent_ws.update(co.get("workspace") or {})
    except LiteWorkspaceConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {
        "status": ws.get("status", "none"),
        # K8s provisioner uses pod_ip; Docker provisioner uses host — normalize
        "pod_ip": ws.get("pod_ip") or ws.get("host"),
        "pod_name": ws.get("pod_name"),
        "pod_port": ws.get("pod_port") or ws.get("port"),
        "namespace": ws.get("namespace"),
        "git_remote_url": ws.get("git_remote_url"),
        # SSH key path (set by Docker provisioner in dev mode)
        "ssh_key_path": os.environ.get("SSH_KEY_PATH"),
        # VM fields (take precedence when present)
        "vm_status": vm.get("status"),
        "vm_ssh_host": vm.get("ssh_host"),
        "vm_ssh_port": vm.get("ssh_port"),
        "vm_name": vm.get("vm_name"),
        # Config overrides (model, temperature, etc.) — secrets re-injected above
        "config_override": co,
        # Orchestrator-resolved config blob (preferred over config_override when present)
        "resolved_config": session_resolved,
        # Project scoping
        "project_ids": project_ids,
        # Resolved datasources for the thread
        "datasources": await _resolve_thread_datasources(
            metadata, project_ids=project_ids
        ),
        # Nextcloud session folder (legacy; preserved one release for back-compat)
        "nc_session_folder": thread.get("nc_session_folder"),
        # Structured cloud-sync config (backend + webdav URL + auth).
        # Agent consumes this via ``src.services.cloud_sync.build_workspace_sync``.
        "cloud_sync": cloud_sync_cfg,
        # Structured lazy cloud mount config. Mutually exclusive with
        # cloud_sync for the same thread response.
        "cloud_mount": cloud_mount_cfg,
        # True when cloud is up but no sync target resolved (Issue 13 follow-up).
        "cloud_sync_degraded": cloud_sync_degraded,
    }


@app.get("/api/agents/threads/{thread_id}/lifecycle")
async def agent_get_thread_lifecycle(
    request: Request, thread_id: str
) -> dict[str, Any]:
    """Return lifecycle fields the agent needs for self-cleanup polling.
    **Internal** (P4b) — requires ``X-Internal-Key``. Ingress strips.

    Minimal projection so the agent's thread-status watchdog (PR 2) can
    decide whether to exit without dragging in the full thread payload.
    """
    await require_internal(request)
    thread = await postgres_db.get_thread(thread_id)
    if not thread:
        raise HTTPException(status_code=404, detail="Thread not found")
    return {
        "status": thread.get("status"),
        "agent_id": str(thread.get("agent_id")) if thread.get("agent_id") else None,
        "ended_at": thread.get("ended_at").isoformat()
        if thread.get("ended_at")
        else None,
    }


class AgentThreadStatusRequest(BaseModel):
    status: str


@app.put("/api/agents/threads/{thread_id}/status")
async def agent_update_thread_status(
    request: Request,
    thread_id: str,
    body: AgentThreadStatusRequest,
) -> dict[str, str]:
    """Update thread status. **Internal** (P4b) — requires ``X-Internal-Key``.
    Ingress strips this path.

    Lifecycle transitions:
      created → active, active → ended (existing).
      active → awaiting_user (Phase 5: agent reached natural pause, no WS
        subscriber). Idempotent — repeated awaiting_user writes preserve
        the original awaiting_user_since so the attention-sleep watchdog's
        clock keeps ticking.
      awaiting_user → active (Phase 5: subscriber reattached). Clears
        awaiting_user_since and extend_count.

    'suspended' is reserved for the attention-sleep watchdog and is not
    writable from agent path — would create a race where an agent flips
    the thread back to active while the orchestrator is mid-suspend.
    """
    await require_internal(request)
    valid_statuses = {"active", "ended", "awaiting_user"}
    if body.status not in valid_statuses:
        raise HTTPException(
            status_code=400,
            detail=f"Status must be one of: {valid_statuses}",
        )
    try:
        if body.status == "ended":
            # Guarded end (mirrors end_thread, which stays unguarded for
            # user-intent call sites): a late agent-side 'ended' — e.g. the
            # SIGTERM shutdown handler of a pod deleted mid-suspend, or the
            # drain-suspend fallback racing a lost suspend response — must
            # never clobber an orchestrator-driven 'suspended' thread.
            async with postgres_db.acquire() as conn:
                updated = await conn.fetchval(
                    "UPDATE threads "
                    "SET status = 'ended', ended_at = CURRENT_TIMESTAMP "
                    "WHERE id = $1 AND status <> 'suspended' "
                    "RETURNING id",
                    thread_id,
                )
            if updated:
                # Agent-initiated `ended` (idle timeout, watchdog, WS
                # disconnect) is almost always recoverable, not a user-intent
                # delete — preserve the workspace via S3 snapshot so /resume
                # can restore it. The user-facing DELETE handler still uses
                # _release_thread_resources for true destruction.
                # See docs/issues/persistent_session_permission_check_race.md.
                asyncio.create_task(_suspend_thread_resources(thread_id))
            else:
                logger.info(
                    "Ignored agent 'ended' for thread %s — already suspended",
                    thread_id,
                )
        elif body.status == "awaiting_user":
            # Idempotent: preserve awaiting_user_since on repeated writes
            # (the agent's loop calls this on every untethered turn-complete
            # in eager mode; resetting the timestamp would let the
            # attention-sleep watchdog never fire). extend_count is also
            # preserved across repeated writes within the same session;
            # only the active→awaiting_user transition resets it.
            async with postgres_db.acquire() as conn:
                await conn.execute(
                    "UPDATE threads "
                    "SET status = 'awaiting_user', "
                    "    awaiting_user_since = CASE "
                    "        WHEN status = 'awaiting_user' "
                    "             THEN awaiting_user_since "
                    "        ELSE now() "
                    "    END, "
                    "    extend_count = CASE "
                    "        WHEN status = 'awaiting_user' THEN extend_count "
                    "        ELSE 0 "
                    "    END, "
                    "    last_activity = CURRENT_TIMESTAMP "
                    "WHERE id = $1",
                    thread_id,
                )
        else:  # active
            # On revert from awaiting_user (or any other source), clear the
            # attention-sleep timer fields so the watchdog re-arms cleanly
            # on the next natural-pause transition.
            async with postgres_db.acquire() as conn:
                await conn.execute(
                    "UPDATE threads "
                    "SET status = $2, "
                    "    awaiting_user_since = NULL, "
                    "    extend_count = 0, "
                    "    last_activity = CURRENT_TIMESTAMP "
                    "WHERE id = $1",
                    thread_id,
                    body.status,
                )
        return {"status": body.status}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/api/agents/threads/{thread_id}/suspend")
async def agent_suspend_thread(request: Request, thread_id: str) -> dict[str, Any]:
    """Clean drain-suspend requested by the thread's own agent. **Internal**
    (P4b) — requires ``X-Internal-Key``. Ingress strips this path.

    Called by a persistent agent that received ``intents.should_drain``
    (stale build) while its loop is parked between turns. Converges on the
    attention-sleep terminal state: workspace snapshotted to S3, workspace +
    agent pods deleted, thread ``suspended`` with the agent binding cleared
    so the next user input provisions a fresh (new-build) agent and walks
    the existing suspended-restore path.

    Returns ``{"suspended": bool, "status": <thread status>}``. The agent
    falls back to the legacy 'ended' detach when ``suspended`` is false —
    e.g. suspension service disabled, snapshot failure, or a thread already
    past the point of suspending.
    """
    await require_internal(request)
    thread = await postgres_db.get_thread(thread_id)
    if not thread:
        raise HTTPException(status_code=404, detail="Thread not found")
    status = thread.get("status")
    if status == "suspended":
        # Idempotent — a retried call after a lost response must not fail.
        return {"suspended": True, "status": "suspended"}
    if status not in ("created", "active", "awaiting_user"):
        return {"suspended": False, "status": status}
    if not workspace_suspension_service.is_enabled:
        return {"suspended": False, "status": status, "reason": "disabled"}

    ok = await workspace_suspension_service.suspend_thread_workspace(thread_id)
    if not ok:
        return {"suspended": False, "status": status, "reason": "snapshot_failed"}

    # CAS like the attention-sleep sweeper: don't clobber a state that moved
    # under us mid-snapshot. agent_id is cleared because the requesting agent
    # pod is already being deleted by suspend_thread_workspace — a stale
    # binding would wedge the next attach on a dead agent.
    async with postgres_db.acquire() as conn:
        updated = await conn.fetchval(
            "UPDATE threads "
            "SET status = 'suspended', agent_id = NULL "
            "WHERE id = $1 AND status IN ('created', 'active', 'awaiting_user') "
            "RETURNING id",
            thread_id,
        )
    if updated:
        logger.info("Drain-suspend complete for thread %s", thread_id)
    else:
        logger.info(
            "Drain-suspend for thread %s: workspace suspended but status "
            "moved concurrently — restore path will handle wake",
            thread_id,
        )
    return {"suspended": True, "status": "suspended" if updated else status}


@app.post("/api/agents/threads/{thread_id}/release-agent")
async def agent_release_thread_agent(
    request: Request, thread_id: str
) -> dict[str, str]:
    """Clear threads.agent_id. **Internal** (P4b) — requires
    ``X-Internal-Key``. Ingress strips this path.

    Called by an agent whose /session/attach background task failed (e.g.
    workspace SSH polling timed out before the workspace pod's image pull
    completed). Without this, the thread stays bound to a session-less agent
    and the next WS reconnect re-targets the same broken agent.
    """
    await require_internal(request)
    thread = await postgres_db.get_thread(thread_id)
    if not thread:
        raise HTTPException(status_code=404, detail="Thread not found")
    await postgres_db.resume_thread(thread_id)
    return {"status": "released"}


class AgentThreadConfigUpdateRequest(BaseModel):
    config_override: dict[str, Any]


@app.patch("/api/agents/threads/{thread_id}/config")
async def agent_update_thread_config(
    request: Request,
    thread_id: str,
    body: AgentThreadConfigUpdateRequest,
) -> dict[str, Any]:
    """Persist runtime config changes for a thread. **Internal** (P4b) —
    requires ``X-Internal-Key``. Ingress strips this path.

    Deep-merges the provided config_override into the existing
    ``threads.metadata.config_override``.  If the override includes
    ``interactive.permission_mode``, the top-level ``permission_mode``
    column is updated as well for query consistency.

    Returns the enriched ``config_override`` so the agent can rebuild its
    LLM with the resolved ``base_url``/``api_key`` instead of sending the
    next request to api.openai.com with ``not-needed``.
    """
    await require_internal(request)
    try:
        # Enrich endpoint-backed model swaps with base_url + api_key so the
        # persisted override is complete. Without this, a hot-swap to a
        # custom-endpoint model leaves the next session attach pointing at
        # the default OpenAI base.
        config_override = dict(body.config_override or {})
        llm_section = config_override.get("llm")
        if llm_section and llm_section.get("model"):
            thread_row = await postgres_db.get_thread(thread_id)
            if thread_row:
                user_id = (
                    str(thread_row["user_id"]) if thread_row.get("user_id") else None
                )
                project_id = (
                    str(thread_row["project_id"])
                    if thread_row.get("project_id")
                    else None
                )
                resolved_keys = await postgres_db.resolve_api_keys_for_job(
                    user_id=user_id, project_id=project_id
                )
                llm_section = dict(llm_section)
                await _inject_model_credentials(
                    section=llm_section,
                    model_id=llm_section["model"],
                    user_id=user_id,
                    resolved_keys=resolved_keys,
                )
                # A model swap must fully determine its transport. Any field
                # resolution didn't set becomes an explicit None so the
                # agent-side deep_merge CLEARS the previous model's value
                # instead of inheriting it (e.g. swapping off an
                # endpoint-backed model must not keep its base_url).
                for transport_key in ("provider", "base_url", "api_key"):
                    llm_section.setdefault(transport_key, None)
                config_override["llm"] = llm_section

        # Persist WITHOUT secrets — the agent rebuilds its LLM from the enriched
        # dict returned below, and resume re-injects from source. The explicit
        # None transport sentinels stay in the stored copy so the deep-merge
        # clears the previous model's transport; resume re-injection treats them
        # as absent (see _inject_thread_dispatch_credentials).
        ok = await postgres_db.merge_thread_config_override(
            thread_id, redact_config_override(config_override)
        )
        if not ok:
            raise HTTPException(status_code=404, detail="Thread not found")

        # Keep top-level permission_mode column in sync
        pm = (config_override.get("interactive") or {}).get("permission_mode")
        if pm and pm in ("supervised", "auto_accept", "autonomous"):
            async with postgres_db.acquire() as conn:
                await conn.execute(
                    "UPDATE threads SET permission_mode = $1 WHERE id = $2",
                    pm,
                    thread_id,
                )

        return {"status": "updated", "config_override": config_override}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/api/agents/threads/{thread_id}/upgrade-to-vm")
async def agent_upgrade_thread_to_vm(
    request: Request, thread_id: str
) -> dict[str, Any]:
    """Request VM provisioning for a persistent thread. **Internal** (P4b) —
    requires ``X-Internal-Key``. Ingress strips this path.

    Called by the persistent agent when a sudo command is detected and the
    user approves a VM upgrade via WebSocket.
    """
    await require_internal(request)
    thread = await postgres_db.get_thread(thread_id)
    if not thread:
        raise HTTPException(status_code=404, detail="Thread not found")

    # Sec-1 — authorize BEFORE provisioning (fail-closed). This endpoint is the
    # target of both the sandbox→VM sudo path and the lite→vm delegation from
    # /upgrade-to-workspace; it previously ran ungated. The shared gate enforces
    # the global vm_workspaces kill-switch + per-user can_use_vm + the
    # vm_workspace PDP grant (workspace_tier_upgrade.md §4.4 Sec-1 / Phase 2).
    await _enforce_workspace_upgrade_grants(thread, target_tier="vm")

    if not vm_provisioner.is_available:
        raise HTTPException(
            status_code=503,
            detail="VM provisioning not available (no NATS or K8s)",
        )

    # Check if a VM is already provisioned or in progress
    metadata = thread.get("metadata") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (json.JSONDecodeError, TypeError):
            metadata = {}
    vm_ctx = metadata.get("vm") or {}
    if vm_ctx.get("status") in ("provisioning", "created", "ready"):
        return {
            "status": vm_ctx["status"],
            "thread_id": thread_id,
            "message": "VM already provisioned or in progress",
        }

    ok = await vm_provisioner.create_thread_vm(
        thread_id=thread_id,
        agent_config=thread.get("config_name", "defaults"),
    )
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to request VM provisioning")

    return {
        "status": "provisioning",
        "thread_id": thread_id,
        "vm_provisioner_mode": vm_provisioner.mode,
    }


@app.post("/api/agents/threads/{thread_id}/abort-vm-upgrade")
async def agent_abort_thread_vm_upgrade(
    request: Request, thread_id: str
) -> dict[str, Any]:
    """Tear down a thread's VM after a failed/timed-out live upgrade.
    **Internal** (P4b) — requires ``X-Internal-Key``. Ingress strips this path.

    Called by the persistent agent when ``_poll_vm_ready`` gives up: a cold CDI
    registry import can outrun the poll budget, leaving a half-provisioned VM +
    DataVolume + importer pod with nobody attached. This deletes the VM and
    marks ``metadata.vm.status='aborted'`` so the provisioning-in-progress guard
    (``status in provisioning/created/ready``) doesn't wedge a later retry
    (workspace_tier_upgrade.md Q7). Idempotent — safe to call when no VM exists.
    """
    await require_internal(request)
    thread = await postgres_db.get_thread(thread_id)
    if not thread:
        raise HTTPException(status_code=404, detail="Thread not found")

    deleted = False
    if vm_provisioner.is_available:
        try:
            deleted = await vm_provisioner.delete_thread_vm(thread_id)
        except Exception as e:
            logger.warning(
                "abort-vm-upgrade: delete_thread_vm failed for %s: %s", thread_id, e
            )
    # Clear the in-progress marker regardless of delete outcome so a retry isn't
    # blocked by the idempotency guard.
    await postgres_db.merge_thread_vm_context(thread_id, {"status": "aborted"})
    return {"status": "aborted", "thread_id": thread_id, "vm_deleted": deleted}


class ThreadWorkspaceUpgradeRequest(BaseModel):
    """Body for ``POST /api/agents/threads/{id}/upgrade-to-workspace``."""

    target_tier: str = "sandbox"


@app.post("/api/agents/threads/{thread_id}/upgrade-to-workspace")
async def agent_upgrade_thread_to_workspace(
    request: Request,
    thread_id: str,
    body: ThreadWorkspaceUpgradeRequest | None = None,
) -> dict[str, Any]:
    """Provision a real workspace container for a lite (``virtual``/``none``)
    thread, upgrading it to the ``sandbox`` tier. **Internal** (P4b) — requires
    ``X-Internal-Key``. Ingress strips this path.

    The session-side counterpart to the live ``swap_backend()`` hot-swap
    (workspace_tier_upgrade.md §4.2 S2): the agent calls this when a ``virtual``
    session needs a real environment (the user starts coding / the agent
    requests an upgrade), then polls ``/workspace`` for readiness via
    ``_poll_workspace_ready`` and swaps its backend in place — the conversation
    never drops. Idempotent: a second call while a container is already
    provisioning/ready is a no-op.

    ``vm`` targets (workspace_tier_upgrade.md Phase 2) are delegated to the
    operator-gated VM path (``/upgrade-to-vm``): same grant gate, but provisions
    a KubeVirt VM and records ``metadata.vm``. The agent polls vm readiness and
    hot-swaps in place just like the container tier.
    """
    await require_internal(request)
    target_tier = (body.target_tier if body else "sandbox") or "sandbox"
    if target_tier not in ("sandbox", "vm"):
        raise HTTPException(
            status_code=400,
            detail=(
                f"upgrade-to-workspace supports target_tier 'sandbox' or 'vm'; "
                f"got {target_tier!r}"
            ),
        )

    # vm targets reuse the operator-gated VM provisioning path: it runs the same
    # _enforce_workspace_upgrade_grants gate, provisions the VM, and records
    # metadata.vm. The agent then polls vm readiness and hot-swaps in place
    # exactly like the sandbox path — the swap handler (_handle_workspace_upgrade)
    # is tier-agnostic and sets sudo_action="allow" for a vm backend
    # (workspace_tier_upgrade.md Phase 2). Keeping a single client method +
    # endpoint means the agent stays uniform across tiers.
    if target_tier == "vm":
        return await agent_upgrade_thread_to_vm(request, thread_id)

    thread = await postgres_db.get_thread(thread_id)
    if not thread:
        raise HTTPException(status_code=404, detail="Thread not found")

    # Sec-1 — authorize the upgrade against the owner's capability grants BEFORE
    # provisioning (fail-closed). sandbox passes by default; a shell-restricted
    # owner (or a vm target without vm_workspace) is refused with 403.
    await _enforce_workspace_upgrade_grants(thread, target_tier=target_tier)

    if not (container_provisioner.is_available and container_provisioner.in_cluster):
        raise HTTPException(
            status_code=503,
            detail="Workspace container provisioning not available (no in-cluster K8s)",
        )

    # Idempotency: short-circuit if a container is already in flight or ready.
    metadata = thread.get("metadata") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (json.JSONDecodeError, TypeError):
            metadata = {}
    wc = metadata.get("workspace_container") or {}
    if wc.get("status") in ("pending", "creating", "created", "ready"):
        return {
            "status": wc["status"],
            "thread_id": thread_id,
            "target_tier": "sandbox",
            "message": "Workspace container already provisioned or in progress",
        }

    # Mark pending, then provision in the background: create_workspace blocks
    # up to ~120s waiting for the pod IP and updates
    # metadata.workspace_container to ready/failed itself. The agent polls
    # /workspace (-> _poll_workspace_ready) for the ready connection block.
    # Mirrors the eager-session provisioning path in agent_create_thread.
    await postgres_db.merge_thread_workspace_context(thread_id, {"status": "pending"})
    asyncio.create_task(
        container_provisioner.create_workspace(WorkspaceOwner.session(thread_id))
    )

    return {
        "status": "provisioning",
        "thread_id": thread_id,
        "target_tier": "sandbox",
    }


class JobWorkspaceUpgradeRequest(BaseModel):
    target_tier: str = "sandbox"


@app.post("/api/jobs/{job_id}/provision-workspace")
async def provision_job_workspace(
    request: Request,
    job_id: str,
    body: JobWorkspaceUpgradeRequest | None = None,
) -> dict[str, Any]:
    """Provision a real workspace container for a RUNNING lite (``virtual``/
    ``none``) worker job, upgrading it to the ``sandbox`` tier IN PLACE.
    **Internal** (P4b) — requires ``X-Internal-Key``.

    The worker-side counterpart to the session ``upgrade-to-workspace`` endpoint
    (workspace_tier_upgrade.md §4.3 W2). Unlike the operator-gated
    ``/api/jobs/{id}/upgrade-to-vm`` (which freezes → re-dispatches), this never
    pauses or re-dispatches: the job stays ``processing`` and the SAME running
    agent provisions, polls ``/workspace-status`` for readiness, seeds the
    virtual files into the new pod, and hot-swaps its ``WorkspaceManager``
    backend — re-``ainvoke``-ing from the local checkpoint. That sidesteps the
    non-portable pod-local LangGraph checkpoint entirely (§2.3). Idempotent: a
    second call while a container is already provisioning/ready is a no-op.

    ``vm`` is intentionally NOT accepted here: VM is operator-gated and must
    pause for approval (it can't stay in-process), so it keeps the existing
    ``/upgrade-to-vm`` freeze→approve→re-dispatch path (§4.3 W3).
    """
    await require_internal(request)
    target_tier = (body.target_tier if body else "sandbox") or "sandbox"
    if target_tier != "sandbox":
        raise HTTPException(
            status_code=400,
            detail=(
                f"provision-workspace supports target_tier 'sandbox' only for a "
                f"running job; vm upgrades go through /upgrade-to-vm "
                f"(operator-gated). Got {target_tier!r}"
            ),
        )

    job = await postgres_db.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    # Sec-1 — authorize against the owner's grants BEFORE provisioning
    # (fail-closed), via the shared gate. sandbox passes by default; a
    # shell-restricted owner is refused 403.
    await _enforce_job_workspace_upgrade_grants(job, target_tier=target_tier)

    if not (container_provisioner.is_available and container_provisioner.in_cluster):
        raise HTTPException(
            status_code=503,
            detail="Workspace container provisioning not available (no in-cluster K8s)",
        )

    # Idempotency: short-circuit if a container is already in flight or ready.
    context = job.get("context") or {}
    if isinstance(context, str):
        try:
            context = json.loads(context)
        except (json.JSONDecodeError, TypeError):
            context = {}
    wc = context.get("workspace_container") or {}
    if wc.get("status") in ("pending", "creating", "created", "ready"):
        return {
            "status": wc["status"],
            "job_id": job_id,
            "target_tier": "sandbox",
            "message": "Workspace container already provisioned or in progress",
        }

    # Mark pending, then provision in the background: create_workspace blocks up
    # to ~120s waiting for the pod IP and updates context.workspace_container to
    # ready/failed itself. The agent polls /workspace-status
    # (-> _poll_job_workspace_ready) for the ready connection block, then swaps
    # in place. No status change, no _trigger_dispatch — the running agent owns
    # the swap (the whole point of the in-process design, §4.3 W1).
    await postgres_db.merge_workspace_container_context(job_id, {"status": "pending"})
    asyncio.create_task(
        container_provisioner.create_workspace(WorkspaceOwner.job(job_id))
    )

    return {
        "status": "provisioning",
        "job_id": job_id,
        "target_tier": "sandbox",
    }


@app.get("/api/jobs/{job_id}/workspace-status")
async def get_job_workspace_status(request: Request, job_id: str) -> dict[str, Any]:
    """Return a running job's workspace-container connection details for the
    agent's in-process upgrade poller. **Internal** — requires ``X-Internal-Key``.

    The job-side analogue of ``GET /api/agents/threads/{id}/workspace``: surfaces
    ``context.workspace_container`` (status + pod IP/port) so the agent's
    ``_poll_job_workspace_ready`` can build the upgraded ``RemoteBackend``
    (workspace_tier_upgrade.md §4.3 W1). The provisioner writes ``pod_ip``/
    ``port``; map ``port`` → ``pod_port`` to match the session poller's shape.
    """
    await require_internal(request)
    job = await postgres_db.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    context = job.get("context") or {}
    if isinstance(context, str):
        try:
            context = json.loads(context)
        except (json.JSONDecodeError, TypeError):
            context = {}
    wc = context.get("workspace_container") or {}

    return {
        "status": wc.get("status", "none"),
        "pod_ip": wc.get("pod_ip") or wc.get("host"),
        "pod_port": wc.get("pod_port") or wc.get("port"),
        "pod_name": wc.get("pod_name"),
        "namespace": wc.get("namespace"),
        "ssh_key_path": os.environ.get("SSH_KEY_PATH"),
        "git_remote_url": wc.get("git_remote_url"),
    }


@app.post("/api/agents/threads/{thread_id}/messages")
async def agent_save_message(
    request: Request,
    thread_id: str,
    body: AgentThreadMessageRequest,
) -> dict[str, Any]:
    """Agent saves a message to thread history. **Internal** (P4b) —
    requires ``X-Internal-Key``. Ingress strips this path.

    Fire-and-forget safe — agents call this after each turn.
    """
    await require_internal(request)
    try:
        message_id = await postgres_db.save_thread_message(
            thread_id=thread_id,
            role=body.role,
            content=body.content,
            tool_calls=body.tool_calls,
            turn_number=body.turn_number,
            metrics=body.metrics,
            tool_call_id=body.tool_call_id,
            thinking=body.thinking,
            reasoning=body.reasoning,
            tool_results=body.tool_results,
            provider=body.provider,
            provider_raw=body.provider_raw,
            additional_kwargs=body.additional_kwargs,
            response_metadata=body.response_metadata,
        )
        return {"message_id": message_id, "status": "saved"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/api/agents/{agent_id}/heartbeat")
async def agent_heartbeat(
    request: Request, agent_id: str, heartbeat: AgentHeartbeat
) -> dict[str, Any]:
    """Update agent heartbeat and status. **Internal** (P4b) — requires
    ``X-Internal-Key``. Ingress strips this path.

    Agents call this every 60 seconds to report their status.
    The orchestrator uses this to track agent health and current job state.
    """
    await require_internal(request)
    try:
        # Surface auxiliary-model health (aux Phase 2): the agent folds a
        # compact AuxHealth summary into metrics.aux; persist its degraded flag
        # on the agent row so the admin view can badge it. Absent on older
        # agent builds / before the aux LLM is wired → None, which leaves the
        # persisted flag untouched.
        aux = (heartbeat.metrics or {}).get("aux")
        aux_degraded = bool(aux.get("degraded")) if isinstance(aux, dict) else None
        result = await postgres_db.heartbeat(
            agent_id=agent_id,
            status=heartbeat.status,
            current_job_id=heartbeat.current_job_id,
            metrics=heartbeat.metrics,
            aux_degraded=aux_degraded,
        )
        if result is None:
            raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")

        # If agent transitioned to ready, trigger the dispatcher
        # (will be wired up in the dispatcher task). Use effective_status —
        # the orchestrator may preserve 'draining' against an agent-reported
        # 'ready', and we must not dispatch in that case.
        prev_status = result.get("previous_status")
        effective_status = result.get("effective_status", heartbeat.status)
        if (
            prev_status
            and prev_status != effective_status
            and effective_status == "ready"
        ):
            logger.info(f"Agent {agent_id} transitioned {prev_status} → ready")
            _trigger_dispatch()

        # Track workspace container activity for idle suspension
        if heartbeat.current_job_id and heartbeat.status == "working":
            try:
                await postgres_db.merge_workspace_container_context(
                    heartbeat.current_job_id,
                    {"last_activity": datetime.now(timezone.utc).isoformat()},
                )
            except Exception:
                pass  # Non-critical — don't fail heartbeat

        # Surface orchestrator-set intents (drain, version-upgrade hints)
        # so the agent can react on the next heartbeat tick. Keeping the
        # legacy {"status": "ok"} key for back-compat with older agent
        # builds that don't read intents.
        return {"status": "ok", "intents": result.get("intents") or {}}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/agents")
async def list_agents(
    request: Request,
    status: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict[str, Any]]:
    """List all registered agents. **Admin only** (G4) — exposes pod IPs,
    hostnames, and full fleet metadata. Non-admins must use
    `/api/me/active-jobs` for a stripped, per-user projection of their
    in-flight work.

    Args:
        status: Optional status filter (booting, ready, working, completed, failed, offline)
        limit: Maximum agents to return
    """
    await _require_admin(request)
    try:
        return await postgres_db.list_agents(status=status, limit=limit)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/agents/{agent_id}")
async def get_agent(request: Request, agent_id: str) -> dict[str, Any]:
    """Get agent details by ID. **Admin only** (G4)."""
    await _require_admin(request)
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
async def get_agent_system_info(request: Request, agent_id: str) -> dict[str, Any]:
    """Proxy system info request to an agent's /system/info endpoint.
    **Admin only** (G4) — proxies host-level CPU/memory/process/port
    inventory from the agent container.

    Returns CPU, memory, disk, processes, listening ports, and network
    connections from the agent's container.
    """
    await _require_admin(request)
    try:
        agent = await postgres_db.get_agent(agent_id)
        if not agent:
            raise HTTPException(status_code=404, detail=f"Agent '{agent_id}' not found")

        if agent["status"] == "offline":
            raise HTTPException(status_code=400, detail="Agent is offline")

        pod_ip = agent.get("pod_ip")
        if not pod_ip:
            raise HTTPException(
                status_code=400, detail="Agent has no pod IP configured"
            )

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


# =============================================================================
# Persistent Agent — Thread CRUD + WebSocket Proxy
# =============================================================================


class ThreadCreateRequest(BaseModel):
    """Request body for creating a persistent thread."""

    # Sessions run the persistent base config — every other session-config
    # fallback in this file already says "persistent_defaults". The old
    # "defaults" default silently put bare API threads on the WORKER yaml
    # (docs/issues/session_config_name_plumbing.md, hole A).
    config_name: str = Field("persistent_defaults", description="Agent config to use")
    project_id: str | None = Field(None, description="(Legacy) Single project to scope")
    project_ids: list[str] | None = Field(
        None, description="List of project UUIDs to scope"
    )
    datasource_ids: list[str] | None = Field(
        None, description="Explicit datasource IDs to attach to this thread"
    )
    permission_mode: str | None = Field(
        None,
        description=(
            "Per-session permission mode override. Omit to inherit the user's "
            "saved default, then the config default ('supervised')."
        ),
    )
    title: str = Field("Untitled Session", description="Session title")
    expert_id: str | None = Field(
        None,
        description=(
            "DB-backed expert UUID for this session. Preferred over config_name "
            "for expert selection — stored in metadata.expert_id and resolved "
            "into the session config at attach. config_name stays the base."
        ),
    )
    model: str | None = Field(
        None,
        description="LLM model override (e.g. RedHatAI/gemma-4-31B-it-FP8-Dynamic)",
    )
    temperature: float | None = Field(None, description="Temperature override")
    config_override: dict[str, Any] | None = Field(
        None,
        description=(
            "Per-session config overrides from the New Session 'Advanced' form. "
            "Only the workspace sub-dict is honored at create time: "
            "workspace.backend selects the tier (sandbox | virtual | none) and "
            "MUST be set here because the workspace is provisioned at creation. "
            "vm is not creatable directly — start on a lite tier and upgrade."
        ),
    )


@app.post("/api/persistent/threads")
async def create_thread(
    request_body: ThreadCreateRequest, request: Request
) -> dict[str, Any]:
    """Create a new persistent thread (auth required).

    Merges user's persistent_agent settings into thread metadata.config_override
    so the agent can apply them via the existing deep_merge path.
    """
    await _enforce_readiness_gate()
    try:
        user = await require_approved_user(request, postgres_db)

        # Build config_override: user settings as base, request fields win
        config_override = {}
        user_settings = (user.get("settings") or {}).get("persistent_agent", {})
        if user_settings:
            if user_settings.get("model"):
                config_override["llm"] = {"model": user_settings["model"]}
            if user_settings.get("permission_mode"):
                config_override["interactive"] = {
                    "permission_mode": user_settings["permission_mode"]
                }
            if user_settings.get("greeting"):
                config_override.setdefault("interactive", {})["greeting"] = (
                    user_settings["greeting"]
                )
            if user_settings.get("idle_timeout_minutes"):
                config_override.setdefault("interactive", {})[
                    "idle_timeout_minutes"
                ] = user_settings["idle_timeout_minutes"]
            if user_settings.get("command_allowlist"):
                config_override["command_allowlist"] = user_settings[
                    "command_allowlist"
                ]
            # Phase 6: headless behavior (polite/eager + attention-sleep TTL +
            # notification channels). Carried under config_override.headless
            # so the agent's loader maps it onto AgentConfig.headless.
            headless_override: dict[str, Any] = {}
            if user_settings.get("headless_mode"):
                headless_override["mode"] = user_settings["headless_mode"]
            if user_settings.get("headless_attention_sleep_minutes") is not None:
                headless_override["attention_sleep_minutes"] = int(
                    user_settings["headless_attention_sleep_minutes"]
                )
            if user_settings.get("notification_channels"):
                headless_override["notification_channels"] = list(
                    user_settings["notification_channels"]
                )
            if headless_override:
                config_override["headless"] = headless_override

        # Per-session overrides from request (take priority over user defaults)
        if request_body.model:
            config_override.setdefault("llm", {})["model"] = request_body.model
        if request_body.temperature is not None:
            config_override.setdefault("llm", {})["temperature"] = (
                request_body.temperature
            )
        # The agent reads its permission mode from config.interactive.permission_mode
        # (src/api/persistent_session.py), NOT from the threads.permission_mode
        # column — so a per-session choice only reaches the agent if it lands in
        # config_override, exactly like the model/temperature bridges above and the
        # runtime PATCH path (agent_update_thread_config). Without this, picking a
        # non-default mode in the New Session form was silently dropped and every
        # session booted "supervised". Field is str|None: omitted → keep the user
        # default applied above; present → it wins.
        if request_body.permission_mode:
            config_override.setdefault("interactive", {})["permission_mode"] = (
                request_body.permission_mode
            )

        # Per-session WORKSPACE TIER from the New Session "Backend" selector.
        # The cockpit sends it nested under request_body.config_override
        # ({"workspace": {"backend": ..., "max_read_words": ..., ...}}).
        # ThreadCreateRequest historically declared no config_override field, so
        # Pydantic dropped it and create_thread rebuilt the override only from
        # model/temperature/permission_mode — every session booted the default
        # (sandbox) regardless of the dropdown, because the provisioning fork
        # below keys off _backend_from_override(config_override). Honor ONLY the
        # validated workspace sub-dict (no creds, no tool grants); the backend
        # must land in config_override now because the workspace is provisioned
        # synchronously at create — unlike the other Advanced settings it can't
        # be a runtime PATCH.
        req_workspace = _validated_session_workspace_override(
            request_body.config_override
        )
        if req_workspace:
            config_override.setdefault("workspace", {}).update(req_workspace)

        # Normalize project_ids (backward compat: project_id → [project_id])
        effective_project_ids = request_body.project_ids or (
            [request_body.project_id] if request_body.project_id else []
        )

        # Resolve + inject LLM / auxiliary / embedding credentials so the agent
        # gets the right base_url + api_key. Mirrors the worker-job dispatch
        # injection. Secrets are injected in-flight here (and re-injected at
        # session attach/resume) but stripped before the row is persisted (see
        # redact_config_override below) — the threads table never stores keys.
        config_override = await _inject_thread_dispatch_credentials(
            config_override,
            user_id=str(user["id"]),
            project_id=effective_project_ids[0] if effective_project_ids else None,
            user_settings=user_settings,
        )

        # Keep the threads.permission_mode column in sync with the mode the
        # agent will actually load from config_override (request > user default >
        # "supervised"). Mirrors the column sync in agent_update_thread_config.
        effective_permission_mode = (config_override.get("interactive") or {}).get(
            "permission_mode"
        ) or "supervised"

        thread_id = await postgres_db.create_thread(
            user_id=str(user["id"]),
            project_id=effective_project_ids[0] if effective_project_ids else None,
            config_name=request_body.config_name
            or user_settings.get("config_name", "persistent_defaults"),
            permission_mode=effective_permission_mode,
            title=request_body.title,
        )

        # Store config_override + datasource_ids in thread metadata. Project
        # attachment is the canonical concern of ``thread_mounts`` (Phase 1
        # of cloud_collaboration_model.md §9) — the legacy
        # ``metadata.project_ids`` JSONB key is no longer written.
        metadata_patch = {}
        if config_override:
            # Persist WITHOUT secrets: keys are injected in-flight at attach
            # (provision_or_assign / _assign_pool_agent below pass the enriched
            # in-memory copy) and re-injected on resume (workspace endpoint +
            # resume dispatcher). The threads row never stores plaintext keys.
            metadata_patch["config_override"] = redact_config_override(config_override)
        if request_body.expert_id:
            # DB expert selection: resolved into the session config at attach
            # (_resolve_session_config reads metadata.expert_id).
            metadata_patch["expert_id"] = request_body.expert_id
        if request_body.datasource_ids:
            metadata_patch["datasource_ids"] = request_body.datasource_ids
        if metadata_patch:
            async with postgres_db.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE threads
                    SET metadata = COALESCE(metadata, '{}') || $2::jsonb
                    WHERE id = $1
                    """,
                    thread_id,
                    json.dumps(metadata_patch),
                )

        # Seed thread_mounts for the attached projects.
        if effective_project_ids:
            try:
                mount_rows = await _build_thread_mount_rows(effective_project_ids)
                if mount_rows:
                    await postgres_db.replace_thread_mounts(thread_id, mount_rows)
            except Exception as e:
                logger.warning(
                    "Thread %s: failed to seed thread_mounts: %s", thread_id, e
                )

        # Provision workspace container + agent pod FIRST (non-blocking).
        # Start image pull / pod creation immediately so it runs in parallel
        # with the Gitea + Nextcloud setup below.
        # Same priority as dispatcher: in-cluster K8s → Docker Compose → kubeconfig K8s
        # Lite (virtual/none) sessions run with no workspace pod — skip every
        # provisioning path below (no_workspace_agent_mode.md §4). The session
        # agent builds its lite backend from the mounts injected at attach.
        lite_session = _backend_from_override(config_override) in LITE_BACKENDS
        use_k8s = (
            not lite_session
            and container_provisioner.is_available
            and (
                container_provisioner.in_cluster or not docker_provisioner.is_available
            )
        )
        if lite_session:
            logger.info(
                "Thread %s: lite workspace backend — no workspace pod provisioned",
                thread_id,
            )
        elif use_k8s:
            # Signal that workspace provisioning is starting so the agent
            # keeps polling instead of falling back to local mode immediately.
            await postgres_db.merge_thread_workspace_context(
                thread_id, {"status": "pending"}
            )

            # Kubernetes mode: create pod on demand
            async def _provision_thread_workspace(tid: str) -> None:
                ok = await container_provisioner.create_workspace(
                    WorkspaceOwner.session(tid)
                )
                if not ok:
                    logger.error(
                        "Thread %s: workspace container provisioning failed. "
                        "Check image availability, RBAC, and node resources.",
                        tid,
                    )

            asyncio.create_task(_provision_thread_workspace(thread_id))
        elif docker_provisioner.is_available:
            await postgres_db.merge_thread_workspace_context(
                thread_id, {"status": "pending"}
            )

            # Docker Compose mode: assign from static pool
            async def _assign_thread_workspace(tid: str) -> None:
                result = await docker_provisioner.assign_thread_workspace(tid)
                if not result:
                    logger.warning(
                        "Thread %s: no free workspace in Docker pool. "
                        "All containers occupied.",
                        tid,
                    )

            asyncio.create_task(_assign_thread_workspace(thread_id))
        else:
            logger.warning(
                "Thread %s: workspace container not provisioned — "
                "no provisioner available. "
                "Start the agent manually: python agent.py --mode persistent "
                "--thread-id %s",
                thread_id,
                thread_id,
            )

        # Run Gitea + Nextcloud setup in parallel, and AWAIT both before
        # assigning an agent. The workspace container is already provisioning
        # in the background above. If we fired the agent-attach first, the
        # agent could see `status=ready` on the workspace before _setup_gitea
        # had written `git_remote_url`, so WorkspaceManager would init a
        # local-only repo with no origin — commits would accumulate but
        # never push. Blocking on gather here is cheap (Gitea create_repo is
        # ~50ms) and makes the workspace→remote wiring race-free.
        async def _setup_gitea() -> None:
            if not gitea_client.is_initialized and gitea_client.is_configured:
                await gitea_client.ensure_initialized()
            if not gitea_client.is_initialized:
                return
            repo_name = f"thread-{thread_id[:8]}"
            git_remote_url = await gitea_client.create_repo(repo_name)
            if git_remote_url:
                await postgres_db.merge_thread_workspace_context(
                    thread_id,
                    {"git_remote_url": git_remote_url, "repo_name": repo_name},
                )
                if user.get("email"):
                    try:
                        # Pass username + full_name + sub so grant_user_repo_access
                        # can pre-provision the Gitea user if they haven't
                        # visited Gitea directly yet. sub is used as login_name
                        # so Gitea's OIDC matches this account on first direct
                        # login instead of creating a duplicate.
                        email_local = user["email"].split("@")[0]
                        await gitea_client.grant_user_repo_access(
                            user["email"],
                            repo_name,
                            username=user.get("preferred_username") or email_local,
                            full_name=user.get("display_name"),
                            sub=user.get("keycloak_sub"),
                        )
                    except Exception as e:
                        logger.warning(
                            "Failed to grant Gitea access for thread %s: %s",
                            thread_id,
                            e,
                        )

        async def _setup_main_cloud() -> None:
            # Fresh session folder for a new thread — resolve via the owner
            # seam (active today). The thread row is stamped with this
            # backend's id below, so resume/delete later dispatch via
            # for_thread. Issue 16, docs/issues/main_cloud.md.
            backend = main_cloud_router.for_owner(user)
            if not backend.is_initialized and backend.is_configured:
                await backend.ensure_initialized()
            if not backend.is_initialized:
                return

            # Phase 4 (cloud_collaboration_model.md §9): if the thread
            # already has any mount with a working webdav_url — project,
            # project_default, or repo — the legacy session folder would
            # be a redundant second sync target. Skip provisioning it.
            # The gate is observable-state: failed mount resolution
            # leaves no usable row, the legacy folder is still
            # provisioned as fallback, the thread never ends up with
            # zero cloud surfaces.
            try:
                existing_mounts = await postgres_db.list_thread_mounts(thread_id)
            except Exception as e:
                existing_mounts = []
                logger.warning(
                    "Thread %s: failed to read thread_mounts before session "
                    "folder provisioning (%s); proceeding with legacy folder.",
                    thread_id,
                    e,
                )
            if _should_skip_session_folder(existing_mounts):
                logger.info(
                    "Thread %s: skipping legacy session folder — at least "
                    "one mount with a working webdav_url is observable.",
                    thread_id,
                )
                return

            try:
                session_handle = await backend.ensure_session_folder(
                    session_id=thread_id[:8]
                )
                share_handle = None
                # ensure_user (not just resolve_user_identity) so we synchronously
                # provision the cloud user record here — otherwise we race the
                # fire-and-forget JIT task from auth.get_current_user and the
                # share gets skipped on a user's very first session.
                resolved_user_id = await backend.ensure_user(
                    sub=user.get("keycloak_sub") or "",
                    issuer=getattr(backend, "_keycloak_issuer", "") or "",
                    email=user.get("email"),
                    display_name=user.get("display_name"),
                    preferred_username=user.get("preferred_username"),
                )
                if resolved_user_id:
                    share_handle = await backend.share_session_folder(
                        session_handle, resolved_user_id
                    )
                await postgres_db.update_thread_main_cloud(
                    thread_id,
                    backend_id=backend.backend_id,
                    session_handle=session_handle.to_db(),
                    share_handle=share_handle.to_db() if share_handle else None,
                )
            except Exception as e:
                logger.warning(
                    "Failed to provision main-cloud session folder for thread %s: %s",
                    thread_id,
                    e,
                )

        await asyncio.gather(_setup_gitea(), _setup_main_cloud())

        # Provision agent pod / assign from pool (fires AFTER Gitea setup so
        # the agent's workspace-readiness poll sees git_remote_url).
        # Priority: unified provisioner (K8s) → Docker Compose pool → manual
        use_k8s_agent = agent_provisioner.is_available and (
            agent_provisioner.in_cluster or not docker_provisioner.is_available
        )
        if use_k8s_agent:
            # Kubernetes mode: create agent pod on demand, with pool fallback
            effective_config = request_body.config_name or user_settings.get(
                "config_name", "persistent_defaults"
            )

            from services.provision_or_assign import provision_or_assign

            asyncio.create_task(
                provision_or_assign(
                    str(user["id"]),
                    thread_id,
                    effective_config,
                    config_override,
                    effective_project_ids,
                    request_body.datasource_ids,
                )
            )
        elif docker_provisioner.is_available:
            # Docker Compose mode: find an idle pool agent and attach the thread
            async def _assign_pool_agent(
                tid: str,
                co: dict,
                pids: list,
                ds_ids: list[str] | None,
                cfg_name: str | None = None,
            ) -> None:
                # Resolve datasources for the thread (explicit + project + global)
                resolved_ds = await postgres_db.resolve_datasources_for_thread(
                    datasource_ids=ds_ids, project_ids=pids
                )
                ds_payload = _build_datasources_payload(resolved_ds)
                if resolved_ds:
                    co = _build_datasource_tool_override(resolved_ds, co)

                idle_agent = await _find_idle_persistent_agent()
                if idle_agent:
                    await _send_session_attach(
                        idle_agent,
                        tid,
                        co,
                        pids,
                        datasources=ds_payload,
                        config_name=cfg_name,
                    )
                else:
                    logger.warning(
                        "Thread %s: no idle agents in pool. "
                        "Increase AGENT_REPLICAS or wait for a session to end.",
                        tid,
                    )

            asyncio.create_task(
                _assign_pool_agent(
                    thread_id,
                    config_override,
                    effective_project_ids,
                    request_body.datasource_ids,
                    request_body.config_name,
                )
            )
        else:
            logger.warning(
                "Thread %s: agent pod not provisioned — "
                "no provisioner available. Start the agent manually: "
                "python agent.py --mode persistent --thread-id %s",
                thread_id,
                thread_id,
            )

        return {"thread_id": thread_id, "status": "created"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/persistent/threads")
async def list_threads(
    request: Request,
    project_id: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    """List persistent threads for the authenticated user."""
    try:
        user = await require_approved_user(request, postgres_db)
        threads = await postgres_db.list_threads(
            user_id=str(user["id"]),
            project_id=project_id,
            status=status,
        )
        for t in threads:
            # Phase 2: default-project threads have no legacy session folder,
            # so the cloud-button URL has to come from the project_default
            # mount row instead. Per-thread mounts lookup is N+1 but the
            # list endpoint is bounded by the user's own thread count.
            mount_rows = await postgres_db.list_thread_mounts(str(t["id"]))
            t["cloud_session_url"] = _resolve_cloud_session_url(t, mount_rows)
        return {"threads": [_redact_thread_metadata(t) for t in threads]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


def _resolve_cloud_session_url(
    thread: dict[str, Any],
    mount_rows: list[dict[str, Any]] | None = None,
) -> Optional[str]:
    """Compute a backend-agnostic browser URL for a thread's cloud folder.

    Looks at the legacy session-folder handle first (Phase 1 and earlier).
    When that's empty — as happens for Phase 2 default-project threads
    where the session folder is intentionally skipped in favor of the
    user-home mount — fall back to the first ``project_default`` mount
    row's cloud handle. The Cockpit's "Cloud" button (sessions-page line
    179: ``thread.cloud_session_url || thread.nc_session_folder``) depends
    on this; without the fallback, default-project threads show no
    button even though sync is working fine.
    """
    handle_str = thread.get("main_cloud_session_handle") or thread.get(
        "nc_session_folder"
    )
    if handle_str:
        backend_id = thread.get("main_cloud_backend") or None
        backend = main_cloud_router.for_backend(backend_id)
        if not backend.is_initialized:
            return None
        try:
            handle = SessionFolderHandle.from_db(handle_str, backend=backend.backend_id)
            return backend.get_session_folder_browser_url(handle)
        except Exception:
            return None

    # No legacy folder — try the project_default mount.
    for m in mount_rows or []:
        if m.get("mount_kind") != "project_default":
            continue
        row_backend_id = m.get("backend_id")
        row_handle_str = m.get("cloud_handle")
        if not row_backend_id or not row_handle_str:
            continue
        backend = main_cloud_router.for_backend(row_backend_id)
        if not backend.is_initialized:
            continue
        try:
            handle = ProjectFolderHandle.from_db(
                row_handle_str, backend=backend.backend_id
            )
            return backend.get_project_folder_browser_url(handle)
        except Exception:
            continue
    return None


def _backend_cloud_cfg(
    backend,
    webdav_url: str,
    *,
    target_user_sub: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Build a single ``{backend, webdav_url, auth}`` cfg for the agent.

    Shape matches ``src/services/cloud_sync/__init__.py::build_workspace_sync``'s
    expected payload. Returns ``None`` when credentials aren't resolvable.
    Never logs the auth payload.

    When ``target_user_sub`` is set (Phase 2 user-home mount, only on
    OpenCloud), the auth shape becomes ``keycloak_user_impersonation``: the
    agent first fetches its service-account token via client_credentials,
    then exchanges it for a user-scoped token impersonating the target
    user (their Keycloak ``sub``) via RFC 8693 token-exchange. The exchanged
    token is what authenticates WebDAV calls — required because OpenCloud
    Personal Spaces are owned by exactly one user and the service account
    has no WebDAV access of its own to them.
    """
    if backend.backend_id == "nextcloud":
        creds = backend.webdav_credentials or {}
        if not creds.get("username") or not creds.get("password"):
            return None
        return {
            "backend": "nextcloud",
            "webdav_url": webdav_url,
            "auth": {
                "type": "basic",
                "username": creds["username"],
                "password": creds["password"],
            },
        }
    if backend.backend_id == "opencloud":
        settings = getattr(backend, "_settings", None)
        if settings is None:
            return None
        try:
            client_secret = settings.keycloak_client_secret.get_secret_value()
        except Exception:
            return None
        auth: dict[str, Any] = {
            "issuer": str(settings.keycloak_issuer).rstrip("/"),
            "client_id": settings.keycloak_client_id,
            "client_secret": client_secret,
        }
        if target_user_sub:
            auth["type"] = "keycloak_user_impersonation"
            auth["target_user_sub"] = target_user_sub
        else:
            auth["type"] = "keycloak_client_credentials"
        return {
            "backend": "opencloud",
            "webdav_url": webdav_url,
            "auth": auth,
        }
    return None


def _build_agent_cloud_sync(
    thread: dict[str, Any],
    *,
    mount_rows: list[dict[str, Any]] | None = None,
) -> Optional[dict[str, Any]]:
    """Build the ``cloud_sync`` payload the agent uses to push/pull workspace files.

    Phase 1 of ``docs/features/cloud_collaboration_model.md`` §9 introduces the
    multi-mount model. Payload shape is now ``version: 2``::

        {
          "version": 2,
          "session_folder": {backend, webdav_url, auth} | None,
          "mounts": [
              {mount_id, mount_kind, target_path, backend, webdav_url, auth},
              ...
          ],
        }

    ``session_folder`` is the legacy per-thread session folder cfg (kept in
    parallel for back-compat until Phase 4). ``mounts`` are the project /
    user-home / repo mounts derived from ``thread_mounts``. Returns ``None``
    when neither a session folder nor any mount could be resolved — in that
    case there's nothing for the agent to sync. Never logs auth payloads.
    """
    # ---- legacy session folder (still provisioned in v1)
    session_folder_cfg: Optional[dict[str, Any]] = None
    handle_str = thread.get("main_cloud_session_handle") or thread.get(
        "nc_session_folder"
    )
    if handle_str:
        backend_id = thread.get("main_cloud_backend") or None
        backend = main_cloud_router.for_backend(backend_id)
        if backend.is_initialized:
            try:
                handle = SessionFolderHandle.from_db(
                    handle_str, backend=backend.backend_id
                )
                webdav_url = backend.get_session_folder_webdav_url(handle)
                if webdav_url:
                    session_folder_cfg = _backend_cloud_cfg(backend, webdav_url)
            except Exception:
                session_folder_cfg = None

    # ---- new-style mounts from thread_mounts
    mounts_out: list[dict[str, Any]] = []
    for row in mount_rows or []:
        row_backend_id = row.get("backend_id")
        webdav_url = row.get("webdav_url")
        if not row_backend_id or not webdav_url:
            # The orchestrator couldn't resolve this mount's transport
            # details — skip rather than ship a half-built entry. Agent
            # will surface "no mount available" via the raise-and-block
            # policy at the next turn boundary if anything depended on it.
            continue
        backend = main_cloud_router.for_backend(row_backend_id)
        if not backend.is_initialized:
            continue
        cfg = _backend_cloud_cfg(
            backend,
            webdav_url,
            target_user_sub=row.get("target_user_sub"),
        )
        if not cfg:
            continue
        mounts_out.append(
            {
                "mount_id": str(row.get("id", "")),
                "mount_kind": row.get("mount_kind"),
                "target_path": row.get("target_path", ""),
                **cfg,
            }
        )

    if not session_folder_cfg and not mounts_out:
        return None

    return {
        "version": 2,
        "session_folder": session_folder_cfg,
        "mounts": mounts_out,
    }


def _runtime_supports_rclone_mount(metadata: dict[str, Any]) -> bool:
    """Whether this thread's current workspace runtime may receive cloud_mount."""
    if _cloud_workspace_driver() != "rclone_mount":
        return False
    vm_ctx = metadata.get("vm") or {}
    if vm_ctx.get("status") == "ready" and vm_ctx.get("ssh_host"):
        return True
    allow_container = os.getenv("CLOUD_RCLONE_ALLOW_CONTAINER", "true").lower()
    if allow_container in {"0", "false", "no", "off"}:
        return False
    ws_ctx = metadata.get("workspace_container") or {}
    return ws_ctx.get("status") == "ready" and bool(
        ws_ctx.get("pod_ip") or ws_ctx.get("host")
    )


def _cloud_mount_name(row: dict[str, Any], used: set[str]) -> str:
    if row.get("mount_kind") == "project_default":
        base = "home"
    else:
        target = str(row.get("target_path") or "").strip("/")
        base = target.rsplit("/", 1)[-1] if target else "cloud"
        base = _slugify_mount_name(base)
    name = base
    suffix = 2
    while name in used:
        name = f"{base}-{suffix}"
        suffix += 1
    used.add(name)
    return name


async def _build_rclone_mount_from_row(
    row: dict[str, Any],
    *,
    workspace_name: str,
    runtime_is_vm: bool = False,
) -> Optional[dict[str, Any]]:
    backend_id = row.get("backend_id")
    handle_str = row.get("cloud_handle")
    if not backend_id or not handle_str:
        return None
    backend = main_cloud_router.for_backend(backend_id)
    if not backend.is_initialized or not isinstance(backend, SupportsRcloneMount):
        return None
    try:
        handle = ProjectFolderHandle.from_db(handle_str, backend=backend.backend_id)
        subject = CloudMountSubject(
            user_sub=row.get("target_user_sub"),
            username=handle.vendor_meta.get("username"),
        )
        target_path = f"/cloud/{workspace_name}"
        # vm tier = root → read-only by default (root + FUSE over the whole
        # Space is a real blast radius); see
        # docs/issues/workspace_upgrade_drops_cloud_mount.md § Security.
        access = "read_only" if runtime_is_vm else "read_write"
        spec = await backend.build_rclone_mount_spec(
            handle=handle,
            mount_kind=str(row.get("mount_kind") or "project"),
            target_path=target_path,
            access=access,
            subject=subject,
            prefer_public_url=runtime_is_vm,
        )
    except CloudBackendError as e:
        logger.info(
            "Thread mount %s cannot use rclone (%s); considering fallback.",
            row.get("id") or row.get("source_ref") or row.get("mount_kind"),
            e.kind.value,
        )
        return None
    except Exception as e:
        logger.warning(
            "Thread mount %s: failed to build rclone spec: %s",
            row.get("id") or row.get("source_ref") or row.get("mount_kind"),
            e,
        )
        return None

    return {
        "mount_id": str(row.get("id") or row.get("source_ref") or workspace_name),
        "mount_kind": row.get("mount_kind"),
        "backend": backend.backend_id,
        "target_path": target_path,
        "workspace_name": workspace_name,
        "access": access,
        "source_ref": str(row.get("source_ref")) if row.get("source_ref") else None,
        **spec.to_payload(),
    }


async def _build_rclone_session_mount(
    thread: dict[str, Any],
    *,
    runtime_is_vm: bool = False,
) -> Optional[dict[str, Any]]:
    handle_str = thread.get("main_cloud_session_handle") or thread.get(
        "nc_session_folder"
    )
    if not handle_str:
        return None
    backend_id = thread.get("main_cloud_backend") or None
    backend = main_cloud_router.for_backend(backend_id)
    if not backend.is_initialized or not isinstance(backend, SupportsRcloneMount):
        return None
    try:
        handle = SessionFolderHandle.from_db(handle_str, backend=backend.backend_id)
        access = "read_only" if runtime_is_vm else "read_write"
        spec = await backend.build_rclone_mount_spec(
            handle=handle,
            mount_kind="session_folder",
            target_path="/cloud/home",
            access=access,
            subject=None,
            prefer_public_url=runtime_is_vm,
        )
    except Exception as e:
        logger.warning(
            "Thread %s: failed to build rclone session-folder fallback: %s",
            thread.get("id"),
            e,
        )
        return None
    return {
        "mount_id": "legacy-session",
        "mount_kind": "session_folder",
        "backend": backend.backend_id,
        "target_path": "/cloud/home",
        "workspace_name": "home",
        "access": access,
        **spec.to_payload(),
    }


async def _build_agent_cloud_mount(
    thread: dict[str, Any],
    *,
    mount_rows: list[dict[str, Any]] | None,
    metadata: dict[str, Any],
) -> Optional[dict[str, Any]]:
    """Build the rclone ``cloud_mount`` payload for capable runtimes.

    v1 is all-or-fallback: if every requested thread_mount can be represented
    safely, mount those. If any requested mount is unsupported, mount the
    regular session folder instead when it exists. This prevents a default
    user-home row from falling back to the eager clone path.
    """
    if not _runtime_supports_rclone_mount(metadata):
        return None

    # A cross-cluster VM runtime needs the public WebDAV URL (it can't reach the
    # internal service DNS) and defaults to a read-only mount (root tier). A
    # same-cluster workspace pod keeps the internal URL + read-write.
    vm_ctx = metadata.get("vm") or {}
    runtime_is_vm = vm_ctx.get("status") == "ready" and bool(vm_ctx.get("ssh_host"))

    rows = [
        row
        for row in (mount_rows or [])
        if row.get("backend_id") and row.get("cloud_handle")
    ]
    used_names: set[str] = set()
    mounted_rows: list[dict[str, Any]] = []
    for row in rows:
        workspace_name = _cloud_mount_name(row, used_names)
        mounted = await _build_rclone_mount_from_row(
            row, workspace_name=workspace_name, runtime_is_vm=runtime_is_vm
        )
        if mounted is None:
            mounted_rows = []
            break
        mounted_rows.append(mounted)

    fallback = False
    mounts_out = mounted_rows
    if not mounts_out or len(mounts_out) != len(rows):
        session_mount = await _build_rclone_session_mount(
            thread, runtime_is_vm=runtime_is_vm
        )
        if session_mount:
            mounts_out = [session_mount]
            fallback = bool(rows)

    if not mounts_out:
        return None

    return {
        "version": 1,
        "driver": "rclone",
        "cloud_root": "/cloud",
        "workspace_entry": "cloud",
        "fallback": fallback,
        "mounts": mounts_out,
    }


@app.get("/api/persistent/threads/{thread_id}")
async def get_thread(thread_id: str, request: Request) -> dict[str, Any]:
    """Get thread status and metadata (auth: owner only).

    Phase 1 of cloud_collaboration_model.md §9 surfaces the thread's
    attached mounts here so the Cockpit "Project files" panel can render
    them without a second round-trip. ``project_ids`` is the derived
    list-of-strings view kept stable for callers that only need scoping.
    """
    user, thread = await require_thread_owner(request, postgres_db, thread_id)
    result = _redact_thread_metadata(dict(thread))
    mounts = await postgres_db.list_thread_mounts(thread_id)
    result["cloud_session_url"] = _resolve_cloud_session_url(thread, mounts)
    result["mounts"] = [
        {
            "id": str(m["id"]),
            "mount_kind": m["mount_kind"],
            "target_path": m["target_path"],
            "source_kind": m["source_kind"],
            "source_ref": str(m["source_ref"]) if m.get("source_ref") else None,
            "backend_id": m.get("backend_id"),
        }
        for m in mounts
    ]
    result["project_ids"] = [
        str(m["source_ref"])
        for m in mounts
        if m.get("mount_kind") == "project" and m.get("source_ref")
    ]
    return result


async def _thread_turn_in_flight(thread: dict) -> bool:
    """Best-effort probe: is the thread's agent currently executing a turn?

    Asks the bound agent's ``/session/status``. Any failure (no agent, pod
    gone, timeout) returns False — ending a dead or unreachable session must
    always be possible.
    """
    agent_id = thread.get("agent_id")
    if not agent_id:
        return False
    try:
        agent = await postgres_db.get_agent(str(agent_id))
        if not agent or not agent.get("pod_ip"):
            return False
        url = f"http://{agent['pod_ip']}:{agent['pod_port']}/session/status"
        async with httpx.AsyncClient(timeout=3.0) as client:
            response = await client.get(url)
        if response.status_code != 200:
            return False
        return bool(response.json().get("turn_in_flight"))
    except Exception:
        return False


@app.delete("/api/persistent/threads/{thread_id}")
async def end_thread(
    thread_id: str,
    request: Request,
    permanent: bool = False,
    force: bool = False,
) -> dict[str, str]:
    """End (or permanently delete) a persistent thread (auth: owner only).

    Query params:
        permanent: If true, delete the thread row and all associated messages
                   from the database. If false (default), just mark as ended.
        force: Required to end a session whose agent is mid-turn. Without it
               a live turn returns 409 — a sessions-list cleanup sweep tore
               down an active session mid-turn, destroying its in-memory
               input queue (docs/issues/session_silent_failure_audit.md #11).
    """
    user, thread = await require_thread_owner(request, postgres_db, thread_id)

    if not force and await _thread_turn_in_flight(thread):
        raise HTTPException(
            status_code=409,
            detail=(
                "turn_in_flight: the agent is mid-turn on this session. "
                "Retry with ?force=true to end it anyway."
            ),
        )

    metadata = thread.get("metadata") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (json.JSONDecodeError, TypeError):
            metadata = {}
    ws_ctx = metadata.get("workspace_container") or {}

    # Snapshot + tear down workspace container/VM and agent pod
    await _release_thread_resources(thread_id)

    # Destructive cleanup of user-visible resources runs ONLY on permanent
    # delete. A soft "end" keeps the Gitea repo and the cloud session folder
    # around so resume restores the thread with its data intact — the workspace
    # container is already snapshotted to S3 above, so the file tree survives
    # there either way.
    if permanent:
        repo_name = ws_ctx.get("repo_name")
        if repo_name and gitea_client.is_initialized:
            await gitea_client.delete_repo(repo_name)

        session_handle_str = thread.get("main_cloud_session_handle") or thread.get(
            "nc_session_folder"
        )
        if session_handle_str:
            backend = main_cloud_router.for_thread(thread)
            if backend.is_initialized:
                try:
                    session_handle = SessionFolderHandle.from_db(
                        session_handle_str, backend=backend.backend_id
                    )
                    await backend.delete_session_folder(session_handle)
                except Exception as e:
                    logger.warning(
                        f"Failed to delete main-cloud session folder for "
                        f"thread {thread_id}: {e}"
                    )

        await postgres_db.delete_thread(thread_id)
        return {"status": "deleted"}

    await postgres_db.end_thread(thread_id)
    return {"status": "ended"}


@app.post("/api/persistent/threads/{thread_id}/resume")
async def resume_thread(
    thread_id: str,
    request: Request,
) -> dict[str, Any]:
    """Resume an ended thread (auth: owner only).

    Resets thread status to 'created' and clears the stale agent_id so that
    a new agent can pick it up. The frontend navigates to the chat page after
    calling this, where the orchestrator will provision or wait for an agent.
    """
    user, thread = await require_thread_owner(request, postgres_db, thread_id)
    if thread.get("status") != "ended":
        raise HTTPException(
            status_code=409, detail=f"Thread is already {thread.get('status')}"
        )

    await postgres_db.resume_thread(thread_id)

    # Provision cloud session folder if missing (e.g. session was created
    # before the cloud backend was initialized), or retry the share alone
    # if the folder exists but no share_handle was recorded — this unstucks
    # threads where folder creation raced the user's first browser login.
    existing_session_handle = thread.get("main_cloud_session_handle") or thread.get(
        "nc_session_folder"
    )
    needs_share_only = bool(existing_session_handle) and not thread.get(
        "main_cloud_share_handle"
    )
    # Phase 4: if the thread already has a working mount, skip the
    # late-provision branch — the mount is the user-visible cloud surface
    # and a session folder on top would be redundant. The share-retry
    # branch is untouched: it only fires on existing folders, so it
    # remains the recovery path for old session folders that lost their
    # share record.
    try:
        existing_mounts = await postgres_db.list_thread_mounts(thread_id)
    except Exception as e:
        existing_mounts = []
        logger.warning(
            "Thread %s: failed to read thread_mounts during resume late-provision "
            "check (%s); proceeding with default policy.",
            thread_id,
            e,
        )
    needs_full_provision = (
        not existing_session_handle
        and not thread.get("nc_session_folder")
        and not _should_skip_session_folder(existing_mounts)
    )

    if needs_full_provision or needs_share_only:

        async def _late_cloud_setup(
            tid: str, usr: dict, existing_handle: str | None
        ) -> None:
            # Pinned: this thread already exists and carries its origin
            # backend in main_cloud_backend. Dispatch via for_thread so a
            # later active-backend swap can't re-provision it on the wrong
            # cloud (Issue 16). Mirrors the delete path above (~:12339).
            backend = main_cloud_router.for_thread(thread)
            if not backend.is_initialized and backend.is_configured:
                await backend.ensure_initialized()
            if not backend.is_initialized:
                return
            try:
                if existing_handle:
                    session_handle = SessionFolderHandle.from_db(
                        existing_handle, backend=backend.backend_id
                    )
                else:
                    session_handle = await backend.ensure_session_folder(
                        session_id=tid[:8]
                    )
                share_handle = None
                resolved_user_id = await backend.resolve_user_identity(
                    usr.get("email"),
                    usr.get("display_name", "").lower(),
                )
                if resolved_user_id:
                    share_handle = await backend.share_session_folder(
                        session_handle, resolved_user_id
                    )
                if not share_handle and existing_handle:
                    # Share still failed (user hasn't signed into cloud yet).
                    # Don't persist — leaving share_handle NULL lets the next
                    # resume retry once autoprovision has materialised them.
                    return
                await postgres_db.update_thread_main_cloud(
                    tid,
                    backend_id=backend.backend_id,
                    session_handle=session_handle.to_db(),
                    share_handle=share_handle.to_db() if share_handle else None,
                )
                logger.info(
                    "Thread %s: %s cloud session folder",
                    tid,
                    "shared previously-unshared"
                    if existing_handle
                    else "late-provisioned",
                )
            except Exception as e:
                logger.warning(
                    "Thread %s: late cloud folder provisioning failed: %s", tid, e
                )

        asyncio.create_task(_late_cloud_setup(thread_id, user, existing_session_handle))

    # Re-provision agent pod and restore workspace if suspended
    if agent_provisioner.is_available:
        config_name = thread.get("config_name", "persistent_defaults")

        async def _reprovision(tid: str, cfg: str) -> None:
            # Serialise concurrent provisioning attempts for the same
            # thread (docs/issues/persistent_thread_double_provisioning_race.md).
            # A concurrent /prepare or /resume on the same thread blocks
            # here; the second arrival observes the binding written by
            # the first and exits. Lifecycle SSE events for the cockpit's
            # resume progress card come from /api/sessions/{tid}/prepare,
            # which the cockpit drives in parallel with this endpoint.
            async with postgres_db.thread_advisory_lock(tid):
                cur = await postgres_db.get_thread(tid)
                if cur and cur.get("agent_id"):
                    logger.info(
                        "Thread %s: already bound to agent %s — "
                        "skipping duplicate reprovision.",
                        tid,
                        cur["agent_id"],
                    )
                    return

                # Try idle pool agent first (instant attach, no pod boot).
                idle_agent = await _find_idle_persistent_agent()
                if idle_agent:
                    # config_override lives in metadata (no top-level column) and
                    # is stripped of secrets at rest — re-inject from source so the
                    # attach payload carries the agent's keys. Needed in addition
                    # to the workspace-endpoint re-inject because datasource
                    # sessions make `co` truthy, suppressing the agent's
                    # fetch-fallback (persistent_app.py).
                    md = cur.get("metadata") or {}
                    if isinstance(md, str):
                        try:
                            md = json.loads(md)
                        except (json.JSONDecodeError, TypeError):
                            md = {}
                    co = (
                        (md.get("config_override") or {})
                        if isinstance(md, dict)
                        else {}
                    )
                    co = await _inject_thread_dispatch_credentials(
                        co,
                        user_id=str(cur["user_id"]) if cur.get("user_id") else None,
                        project_id=str(cur["project_id"])
                        if cur.get("project_id")
                        else None,
                    )
                    # Pass the thread's explicit datasource selection (persisted
                    # in metadata) — without it, explicit-only resolution returns
                    # nothing on idle-pool resume (this path previously dropped
                    # it). Read from the freshly-locked row (cur). NOTE: project_ids
                    # here is the legacy thread field; deriving it from
                    # thread_mounts is a separate latent fix.
                    meta = cur.get("metadata") or {}
                    if isinstance(meta, str):
                        try:
                            meta = json.loads(meta)
                        except (json.JSONDecodeError, TypeError):
                            meta = {}
                    pids = thread.get("project_ids") or []
                    resolved_ds = await postgres_db.resolve_datasources_for_thread(
                        datasource_ids=meta.get("datasource_ids"),
                        project_ids=pids,
                    )
                    ds_payload = _build_datasources_payload(resolved_ds)
                    if resolved_ds:
                        co = _build_datasource_tool_override(resolved_ds, co)
                    ok = await _send_session_attach(
                        idle_agent,
                        tid,
                        co,
                        pids,
                        datasources=ds_payload,
                        config_name=cfg,
                    )
                    if ok:
                        logger.info(
                            "Thread %s: resumed via idle pool agent %s",
                            tid,
                            idle_agent["hostname"],
                        )
                        return

                # No idle agent — create a dedicated session pod.
                pod_name = await agent_provisioner.provision_agent(
                    purpose="session", thread_id=tid, config_name=cfg
                )
                if pod_name:
                    return

                logger.error(
                    "Thread %s: resume failed — no idle agents and pod provisioning failed",
                    tid,
                )

        asyncio.create_task(_reprovision(thread_id, config_name))
    elif persistent_provisioner.is_available:
        config_name = thread.get("config_name", "persistent_defaults")
        asyncio.create_task(
            persistent_provisioner.create_agent_pod(thread_id, config_name=config_name)
        )

    # Ensure the session workspace is provisioned/restored (idempotent): restores
    # a suspended workspace, recreates a failed/missing one. Fire-and-forget so
    # resume stays fast — the agent tolerates a not-yet-ready workspace and the
    # periodic reconcile retries on failure.
    asyncio.create_task(
        ensure_session_workspace(
            thread_id,
            db=postgres_db,
            provisioner=container_provisioner,
            suspension=workspace_suspension_service,
        )
    )

    return {"status": "created", "thread_id": thread_id}


@app.get("/api/persistent/threads/{thread_id}/citations")
async def get_thread_citations(
    thread_id: str,
    request: Request,
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """List citations created in a persistent session, for inline ``[N]`` rendering.

    The citation engine stores a session's citations with ``job_id = thread_id``
    (it maps ``CitationContext.session_id`` → ``job_id``), so the thread UUID *is*
    the ``job_id`` — there is no separate thread column. Owner-only (the by-job
    endpoint 404s for a thread since no ``jobs`` row exists). The marker the agent
    emits is the citation ``id``; the cockpit renumbers for display and resolves
    each ``[id]`` to a row returned here.
    """
    await require_thread_owner(request, postgres_db, thread_id)
    try:
        async with vector_db.acquire() as conn:
            count_row = await conn.fetchrow(
                "SELECT COUNT(*) AS total FROM citations WHERE job_id = $1::uuid",
                thread_id,
            )
            total = count_row["total"] if count_row else 0
            rows = await conn.fetch(
                """SELECT c.id, LEFT(c.claim, 300) AS claim, c.source_id,
                       s.name AS source_name, s.type::text AS source_type,
                       s.identifier AS source_identifier,
                       c.verification_status::text AS verification_status,
                       c.confidence::text AS confidence,
                       c.created_at, s.metadata
                FROM citations c
                JOIN sources s ON c.source_id = s.id
                WHERE c.job_id = $1::uuid
                ORDER BY c.id ASC
                LIMIT $2 OFFSET $3""",
                thread_id,
                limit,
                offset,
            )
            citations = []
            for r in rows:
                d = dict(r)
                # Cloud-document citations (cite_document with a snapshot-anchor)
                # can offer "view original" (/snapshot) + on-view drift (/drift);
                # web citations have neither. Surface the two flags so the cockpit
                # only renders those controls where they apply. The raw metadata
                # isn't returned (internal blob keys / anchor URLs).
                cloud = _source_cloud_meta(d.pop("metadata", None))
                d["has_cloud_anchor"] = bool(cloud)
                d["has_snapshot"] = bool(cloud.get("snapshot_blob_key"))
                citations.append(d)
            return {
                "citations": citations,
                "total": total,
                "thread_id": thread_id,
            }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/persistent/threads/{thread_id}/messages")
async def get_thread_messages_history(
    thread_id: str,
    request: Request,
    limit: Optional[int] = None,
    before: Optional[str] = None,
    after: Optional[str] = None,
    offset: int = 0,
) -> dict[str, Any]:
    """Load message history for a persistent thread, ascending (chronological).

    Default (no params) returns the **entire** conversation — the cockpit caches
    the full thread client-side and windows the render itself, so the display
    must not be truncated. Cursor paging (mutually exclusive, ISO-8601):

    - ``before=<ts>``: backfill — newest messages at-or-before the cursor, up to
      ``limit``.
    - ``after=<ts>``:  catch-up — messages at-or-after the cursor, up to ``limit``.

    A bare ``limit`` with no cursor keeps the legacy oldest-first paged read
    (``offset`` honored) used by the MCP inspection tool. Returns
    ``{messages, total, has_more, thread_id}``.
    """
    user, thread = await require_thread_owner(request, postgres_db, thread_id)

    def _parse_cursor(value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(
                status_code=400, detail=f"Invalid ISO-8601 timestamp: {value!r}"
            )

    before_dt = _parse_cursor(before)
    after_dt = _parse_cursor(after)
    if before_dt is not None and after_dt is not None:
        raise HTTPException(
            status_code=400, detail="Pass at most one of 'before' / 'after'"
        )

    capped_limit = min(limit, 500) if limit is not None else None

    if before_dt is not None or after_dt is not None:
        messages, has_more = await postgres_db.get_thread_messages_page(
            thread_id=thread_id,
            before=before_dt,
            after=after_dt,
            limit=capped_limit,
        )
    else:
        messages = await postgres_db.get_thread_messages_history(
            thread_id=thread_id,
            limit=capped_limit,
            offset=offset,
        )
        # Legacy paged read: a full page implies there may be more.
        has_more = capped_limit is not None and len(messages) == capped_limit

    total = await postgres_db.get_thread_message_count(thread_id)
    return {
        "messages": messages,
        "total": total,
        "has_more": has_more,
        "thread_id": thread_id,
    }


# =============================================================================
# Headless persistent sessions — Phase 2 SSE + REST transport
# =============================================================================
#
# SSE replaces the WebSocket as the primary server→client path; the existing
# /ws/persistent/{thread_id} stays as a fallback. Per
# docs/features/headless_persistent_sessions.md.
#
# The per-turn input lock guards against duplicate POSTs from concurrent
# cockpit tabs racing on the same turn. Single-instance orchestrator, so a
# module-level dict is enough; entries auto-clean 5 min after release.

_thread_turn_locks: dict[tuple[str, int], asyncio.Lock] = {}
_thread_turn_inflight: dict[str, int] = {}


def _ensure_thread_turn_lock(thread_id: str, turn_id: int) -> asyncio.Lock:
    """Get or create the lock for (thread_id, turn_id). Concurrent callers
    landing on the same tuple share the same Lock object."""
    key = (thread_id, turn_id)
    lock = _thread_turn_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _thread_turn_locks[key] = lock
    return lock


def _schedule_turn_lock_cleanup(thread_id: str, turn_id: int) -> None:
    """Remove the lock entry 5 minutes after release. Memory-leak guard
    for long-lived sessions accumulating per-turn locks."""

    async def _later() -> None:
        await asyncio.sleep(300)
        _thread_turn_locks.pop((thread_id, turn_id), None)
        if _thread_turn_inflight.get(thread_id) == turn_id:
            _thread_turn_inflight.pop(thread_id, None)

    asyncio.create_task(_later(), name=f"turn-lock-cleanup-{thread_id[:8]}")


async def _resolve_thread_for_forwarding(
    thread_id: str, user: dict
) -> tuple[dict, dict]:
    """Look up thread + bound agent for orchestrator → agent forwarding.

    Returns (thread, agent). Raises HTTPException on auth or routing failures.
    Restores a suspended workspace if needed (same pattern as the WS proxy).
    """
    thread = await postgres_db.get_thread(thread_id)
    if not thread:
        raise HTTPException(status_code=404, detail="Thread not found")
    # Fail-closed for orphans (user_id IS NULL); admins bypass.
    if not user.get("is_admin") and str(thread.get("user_id") or "") != str(user["id"]):
        raise HTTPException(status_code=403, detail="Not your thread")

    # Restore suspended workspace before forwarding (mirrors persistent_ws_proxy)
    metadata = thread.get("metadata") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (json.JSONDecodeError, TypeError):
            metadata = {}
    ws_ctx = metadata.get("workspace_container") or {}
    if ws_ctx.get("status") == "suspended" and workspace_suspension_service.is_enabled:
        logger.info("Restoring suspended workspace for thread %s", thread_id)
        ok = await workspace_suspension_service.restore_thread_workspace(thread_id)
        if not ok:
            raise HTTPException(
                status_code=503,
                detail="Failed to restore suspended workspace",
            )
        thread = await postgres_db.get_thread(thread_id)

    agent_id = thread.get("agent_id") if thread else None
    if not agent_id:
        raise HTTPException(
            status_code=503,
            detail="No agent bound to thread — open the SSE stream first",
        )
    agent = await postgres_db.get_agent(str(agent_id))
    if not agent or not agent.get("pod_ip"):
        raise HTTPException(
            status_code=503,
            detail="Agent unreachable (no pod_ip)",
        )
    return thread, agent


async def _forward_to_agent(
    agent: dict, path: str, payload: dict, timeout: float = 30.0
) -> dict[str, Any]:
    """POST `payload` to the agent pod's REST endpoint at `path`. Returns
    parsed JSON body. Raises HTTPException on transport/status errors."""
    agent_url = f"http://{agent['pod_ip']}:{agent['pod_port']}{path}"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(agent_url, json=payload)
    except Exception as e:
        logger.warning("Agent forward failed: %s %s -> %s", path, agent.get("id"), e)
        raise HTTPException(status_code=503, detail=f"Agent unreachable: {e}") from e
    if response.status_code >= 500:
        raise HTTPException(
            status_code=502,
            detail=f"Agent error: {response.status_code} {response.text[:200]}",
        )
    if response.status_code >= 400:
        raise HTTPException(
            status_code=response.status_code,
            detail=response.text[:200],
        )
    try:
        return response.json()
    except Exception:
        return {"raw": response.text[:500]}


async def _no_cursor_replay_start(conn, thread_id: str, epoch: int) -> int:
    """Replay floor (exclusive) for an SSE attach that carries no cursor.

    A fresh client — opening the session on a second device, or any client
    with no cached cursor for this thread — has already painted the thread's
    completed turns from REST history. Replaying the whole epoch from seq 0
    would re-deliver each completed turn as a *live* copy the cockpit reducer
    can't reconcile (history turns are keyed by message id, replayed turns by
    turn_id), so the last assistant turn renders twice, split by a spurious
    "SESSION RESUMED" divider — the cold-attach twin of the gone_beyond_horizon
    duplicate render.

    Anchor instead just past the last turn-terminal event (``turn.completed`` /
    ``turn.error``, both of which persist their turn to ``thread_messages``), so
    the replay carries only the in-flight, not-yet-persisted turn. Returns 0
    when no turn has finished yet (first turn still streaming) so that turn —
    absent from REST history — still replays from the start.
    """
    anchor = await conn.fetchval(
        "SELECT COALESCE(MAX(seq), 0) FROM thread_events "
        "WHERE thread_id = $1 AND epoch = $2 "
        "AND kind IN ('turn.completed', 'turn.error')",
        thread_id,
        epoch,
    )
    return int(anchor or 0)


@app.get("/api/persistent/threads/{thread_id}/stream")
async def thread_event_stream(thread_id: str, request: Request) -> StreamingResponse:
    """SSE: stream this thread's event log with replay-from-cursor.

    The client sends `Last-Event-ID: <epoch>:<seq>` to resume from a known
    point. If the cursor's epoch doesn't match the server, or its seq is
    older than retention, the server emits a single `gone_beyond_horizon`
    event and closes — the client must drop its cursor and re-sync.

    Otherwise: replay everything since the cursor, then switch to live
    mode (200ms poll, adaptive backoff to 1s after 5 empty polls).
    """
    user, thread = await require_thread_owner(request, postgres_db, thread_id)

    server_epoch = int(thread.get("events_epoch") or 0)

    # Parse Last-Event-ID. Format: "<epoch>:<seq>". Missing/malformed → no
    # cursor, so the replay floor is computed by _no_cursor_replay_start below
    # (anchored past the last completed turn, not seq 0).
    #
    # EventSource doesn't let the browser set custom request headers, so the
    # cockpit hands us the cached cursor via `?last_event_id=` for the
    # initial connection. On automatic reconnect, the browser appends the
    # `Last-Event-ID` header from the latest `id:` line we yielded — that
    # path is fully native and doesn't need the query param.
    last_event_id = (
        request.headers.get("Last-Event-ID")
        or request.headers.get("last-event-id")
        or request.query_params.get("last_event_id")
    )
    cursor_epoch: Optional[int] = None
    cursor_seq: Optional[int] = None
    if last_event_id:
        try:
            e_str, s_str = last_event_id.split(":", 1)
            cursor_epoch = int(e_str)
            cursor_seq = int(s_str)
        except (ValueError, AttributeError):
            cursor_epoch = None
            cursor_seq = None

    async def event_stream():
        # Kickstart: flush a comment immediately so the browser EventSource
        # fires `onopen` at once and buffering intermediaries (Cloudflare
        # Tunnel, Traefik) don't hold the response headers / idle-timeout the
        # connection waiting for the first body byte. Without this, a connect
        # whose cursor is already at the tail sends nothing until the ~20s
        # keepalive ping below — stalling the SSE receive path ~20s. Comments
        # (lines starting with `:`) are ignored by EventSource, so this is
        # side-effect-free on the client.
        yield ": open\n\n"

        # Mismatched epoch → force re-sync.
        if cursor_epoch is not None and cursor_epoch != server_epoch:
            async with postgres_db.acquire() as conn:
                tail = await conn.fetchval(
                    "SELECT COALESCE(MAX(seq), 0) FROM thread_events "
                    "WHERE thread_id = $1 AND epoch = $2",
                    thread_id,
                    server_epoch,
                )
            payload = json.dumps(
                {
                    "method": "gone_beyond_horizon",
                    "params": {
                        "epoch": server_epoch,
                        "server_seq": int(tail or 0),
                        "reason": "epoch_mismatch",
                    },
                }
            )
            yield f"id: {server_epoch}:0\nevent: gone_beyond_horizon\ndata: {payload}\n\n"
            return

        # Retention floor for the current epoch.
        async with postgres_db.acquire() as conn:
            min_seq = await conn.fetchval(
                "SELECT MIN(seq) FROM thread_events "
                "WHERE thread_id = $1 AND epoch = $2",
                thread_id,
                server_epoch,
            )
        min_seq = int(min_seq) if min_seq is not None else 0

        # Cursor older than retention → also force re-sync.
        if cursor_seq is not None and min_seq > 0 and cursor_seq < min_seq - 1:
            async with postgres_db.acquire() as conn:
                tail = await conn.fetchval(
                    "SELECT COALESCE(MAX(seq), 0) FROM thread_events "
                    "WHERE thread_id = $1 AND epoch = $2",
                    thread_id,
                    server_epoch,
                )
            payload = json.dumps(
                {
                    "method": "gone_beyond_horizon",
                    "params": {
                        "epoch": server_epoch,
                        "server_seq": int(tail or 0),
                        "retention_min_seq": min_seq,
                        "reason": "cursor_older_than_retention",
                    },
                }
            )
            yield f"id: {server_epoch}:0\nevent: gone_beyond_horizon\ndata: {payload}\n\n"
            return

        # Replay floor. With a cursor, resume right after it. Without one, a
        # fresh attach has already loaded completed turns from REST history, so
        # anchor past the last completed turn instead of replaying the whole
        # epoch from 0 (which doubles the last assistant turn + shows a spurious
        # "SESSION RESUMED" divider — see _no_cursor_replay_start).
        if cursor_seq is not None:
            last_sent_seq = cursor_seq
        else:
            async with postgres_db.acquire() as conn:
                last_sent_seq = await _no_cursor_replay_start(
                    conn, thread_id, server_epoch
                )
        empty_polls = 0
        idle_keepalive_at = 0.0
        cancelled = False
        try:
            while not cancelled:
                if await request.is_disconnected():
                    break
                async with postgres_db.acquire() as conn:
                    rows = await conn.fetch(
                        "SELECT seq, kind, payload "
                        "FROM thread_events "
                        "WHERE thread_id = $1 AND epoch = $2 AND seq > $3 "
                        "ORDER BY seq ASC "
                        "LIMIT 500",
                        thread_id,
                        server_epoch,
                        last_sent_seq,
                    )
                if rows:
                    empty_polls = 0
                    for row in rows:
                        seq = int(row["seq"])
                        # row["payload"] is a JSONB column — asyncpg may
                        # return it as str or already-parsed dict depending
                        # on codec registration.
                        raw_payload = row["payload"]
                        if isinstance(raw_payload, str):
                            payload_obj = json.loads(raw_payload)
                        else:
                            payload_obj = raw_payload
                        frame = {
                            "method": row["kind"],
                            "params": payload_obj,
                        }
                        body = json.dumps(frame)
                        yield f"id: {server_epoch}:{seq}\ndata: {body}\n\n"
                        last_sent_seq = seq
                    idle_keepalive_at = 0.0
                else:
                    # Adaptive backoff: 200ms × 5 empty polls, then 1s.
                    empty_polls += 1
                    wait = 1.0 if empty_polls >= 5 else 0.2
                    # Typed `ping` event every ~20s of idle. A bare `:`
                    # comment would keep the socket warm but never fire
                    # `onmessage` in the browser, leaving silent network
                    # drops undetectable client-side. A typed event with no
                    # `id:` line lets the cockpit watchdog observe liveness
                    # without advancing the replay cursor.
                    idle_keepalive_at += wait
                    if idle_keepalive_at >= 20.0:
                        yield "event: ping\ndata: {}\n\n"
                        idle_keepalive_at = 0.0
                    try:
                        await asyncio.sleep(wait)
                    except asyncio.CancelledError:
                        cancelled = True
                        break
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.warning("thread_event_stream error (thread=%s): %s", thread_id, e)
            return

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


class ThreadInputRequest(BaseModel):
    """Body for POST /api/persistent/threads/{thread_id}/input."""

    content: str
    turn_id: Optional[int] = None


@app.post("/api/persistent/threads/{thread_id}/input")
async def thread_input(
    thread_id: str, body: ThreadInputRequest, request: Request
) -> dict[str, Any]:
    """Submit user input to a thread. Per-turn lock returns 409 on dupes."""
    user = await require_approved_user(request, postgres_db)
    thread, agent = await _resolve_thread_for_forwarding(thread_id, user)

    if not body.content or not isinstance(body.content, str):
        raise HTTPException(
            status_code=400, detail="content must be a non-empty string"
        )

    # Turn id defaults to the thread's current total_turns + 1. Reject
    # arbitrarily-large values to bound the lock dict.
    total_turns = int(thread.get("total_turns") or 0)
    if body.turn_id is None:
        turn_id = total_turns + 1
    else:
        turn_id = body.turn_id
        if turn_id < 0 or turn_id > total_turns + 5:
            raise HTTPException(
                status_code=400,
                detail=f"turn_id out of range "
                f"(thread at turn {total_turns}, max accepted "
                f"{total_turns + 5})",
            )

    lock = _ensure_thread_turn_lock(thread_id, turn_id)
    if lock.locked():
        in_flight = _thread_turn_inflight.get(thread_id, turn_id)
        return JSONResponse(
            status_code=409,
            content={
                "error": "turn_in_flight",
                "turn_id": in_flight,
                "thread_id": thread_id,
            },
        )
    async with lock:
        _thread_turn_inflight[thread_id] = turn_id
        try:
            result = await _forward_to_agent(
                agent,
                "/api/input",
                {"content": body.content, "turn_id": turn_id},
            )
        finally:
            _schedule_turn_lock_cleanup(thread_id, turn_id)
    return {
        "accepted": True,
        "turn_id": turn_id,
        "agent": result,
    }


@app.post("/api/persistent/threads/{thread_id}/interrupt")
async def thread_interrupt(thread_id: str, request: Request) -> dict[str, Any]:
    """Interrupt the in-flight turn. Mode (hard/graceful) is decided by
    the agent based on whether a tool is currently mid-`ainvoke`."""
    user = await require_approved_user(request, postgres_db)
    _, agent = await _resolve_thread_for_forwarding(thread_id, user)
    result = await _forward_to_agent(agent, "/api/interrupt", {})
    return {"accepted": True, "agent": result}


class ThreadApproveRequest(BaseModel):
    """Body for POST /api/persistent/threads/{id}/approve/{approval_id}."""

    decision: str  # "approve" or "deny"


@app.post("/api/persistent/threads/{thread_id}/approve/{approval_id}")
async def thread_approve(
    thread_id: str,
    approval_id: str,
    body: ThreadApproveRequest,
    request: Request,
) -> dict[str, Any]:
    """Resolve a pending permission gate by updating thread_permission_requests
    directly. The DB trigger fires NOTIFY → the agent's LISTEN wakes its
    permission_check. No agent forwarding hop — this endpoint is the
    canonical resolution path for magic-link approvals and MCP clients
    alike. The cockpit WS approve method does the same UPDATE inside the
    agent for back-compat.

    Returns:
        200 — request resolved (status flipped)
        400 — invalid decision
        403 — not thread owner
        404 — approval_id not found, or wrong thread, or no pending request
        409 — request already decided (idempotent re-clicks land here)
    """
    user, thread = await require_thread_owner(request, postgres_db, thread_id)

    if body.decision == "approve":
        new_status = "approved"
    elif body.decision == "deny":
        new_status = "denied"
    else:
        raise HTTPException(
            status_code=400,
            detail="decision must be 'approve' or 'deny'",
        )

    decided_by = str(user.get("id") or user.get("sub") or "rest_client")

    async with postgres_db.acquire() as conn:
        # Lookup-then-update so we can distinguish 404 (wrong id/thread)
        # from 409 (already decided).
        existing = await conn.fetchrow(
            "SELECT id, status, tool_call_id FROM thread_permission_requests "
            "WHERE id = $1 AND thread_id = $2",
            approval_id,
            thread_id,
        )
        if existing is None:
            raise HTTPException(
                status_code=404,
                detail="Permission request not found for this thread",
            )
        if existing["status"] != "pending":
            raise HTTPException(
                status_code=409,
                detail=f"Already {existing['status']}",
            )
        row = await conn.fetchrow(
            "UPDATE thread_permission_requests "
            "SET status = $2, decided_at = now(), decided_by = $3 "
            "WHERE id = $1 AND status = 'pending' "
            "RETURNING id, status, tool_call_id",
            approval_id,
            new_status,
            decided_by,
        )
    if row is None:
        # Lost the race — somebody else just decided this. Idempotency.
        raise HTTPException(
            status_code=409,
            detail="Already decided (race lost)",
        )
    return {
        "accepted": True,
        "decision": body.decision,
        "approval_id": str(row["id"]),
        "status": row["status"],
        "tool_call_id": row["tool_call_id"],
    }


async def thread_events_prune_sweeper(
    shutdown_event: asyncio.Event,
) -> None:
    """Background task that prunes the thread_events log on retention.

    Runs every THREAD_EVENTS_PRUNE_INTERVAL_S (default 300s). Two queries:
      - DELETE rows for threads in 'ended' status older than 24h.
      - DELETE rows for threads NOT in 'ended' older than 7 days.

    Best-effort. Survives transient DB errors by logging and continuing.
    """
    interval_s = int(os.environ.get("THREAD_EVENTS_PRUNE_INTERVAL_S", "300"))
    logger.info("Thread-events prune sweeper started (interval=%ds)", interval_s)
    while not shutdown_event.is_set():
        try:
            async with postgres_db.acquire() as conn:
                ended_deleted = await conn.fetchval(
                    "WITH deleted AS ("
                    "  DELETE FROM thread_events "
                    "  WHERE thread_id IN ("
                    "    SELECT id FROM threads WHERE status = 'ended'"
                    "  ) "
                    "  AND created_at < now() - interval '24 hours' "
                    "  RETURNING 1"
                    ") SELECT COUNT(*) FROM deleted"
                )
                active_deleted = await conn.fetchval(
                    "WITH deleted AS ("
                    "  DELETE FROM thread_events "
                    "  WHERE thread_id IN ("
                    "    SELECT id FROM threads WHERE status <> 'ended'"
                    "  ) "
                    "  AND created_at < now() - interval '7 days' "
                    "  RETURNING 1"
                    ") SELECT COUNT(*) FROM deleted"
                )
            if (ended_deleted or 0) + (active_deleted or 0) > 0:
                logger.info(
                    "thread_events prune: ended=%d active=%d",
                    int(ended_deleted or 0),
                    int(active_deleted or 0),
                )
        except Exception as e:
            logger.warning("thread_events prune error (non-fatal): %s", e)
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=float(interval_s))
            break
        except asyncio.TimeoutError:
            pass
    logger.info("Thread-events prune sweeper stopped")


async def security_events_prune_sweeper(shutdown_event: asyncio.Event) -> None:
    """Background task that prunes the security_events audit log on retention.

    Runs hourly (SECURITY_EVENTS_PRUNE_INTERVAL_S, default 3600). Deletes
    rows older than SECURITY_EVENTS_RETENTION_DAYS (default 90). Bounds
    table growth — writes happen on the post-auth 403 path, so any flood
    is tied to a real account, but retention still caps the worst case.
    Best-effort: survives transient DB errors by logging and continuing.
    """
    interval_s = int(os.environ.get("SECURITY_EVENTS_PRUNE_INTERVAL_S", "3600"))
    retention_days = int(os.environ.get("SECURITY_EVENTS_RETENTION_DAYS", "90"))
    logger.info(
        "Security-events prune sweeper started (interval=%ds, retention=%dd)",
        interval_s,
        retention_days,
    )
    while not shutdown_event.is_set():
        try:
            deleted = await postgres_db.prune_security_events(retention_days)
            if deleted:
                logger.info("security_events prune: deleted=%d", deleted)
        except Exception as e:
            logger.warning("security_events prune error (non-fatal): %s", e)
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=float(interval_s))
            break
        except asyncio.TimeoutError:
            pass
    logger.info("Security-events prune sweeper stopped")


# =============================================================================
# Headless persistent sessions — Phase 4 magic-link routes + watcher
# =============================================================================
#
# Email magic-links land at /magic/approve/{token}. GET renders a
# confirmation page (read-only, prefetch-safe). POST consumes the token
# and UPDATEs thread_permission_requests via the same trigger path as
# the cockpit WS approve handler.
#
# Background watcher (thread_permission_notify_sweeper) detects pending
# requests older than 30s with no notification on record and dispatches
# the email via services.headless_notifications.


# Phase 5: per-thread cap on /magic/extend clicks. 4 × 60min = 4h total
# awaiting_user before unconditional suspension. Configurable via env for
# ops tuning during incident response.
_MAGIC_EXTEND_CAP: int = int(os.environ.get("HEADLESS_EXTEND_CAP", "4"))


def _magic_link_confirmation_page(
    *,
    tool_name: str,
    tool_args_preview: str,
    intended_decision: Optional[str],
    token: str,
    extend_status: Optional[str] = None,
    extends_remaining: Optional[int] = None,
) -> str:
    """Render the GET landing page. Single button POSTs back to the same
    URL with the actual decision; this is what prevents email-link
    prefetchers (Outlook Safe Links, Gmail) from auto-consuming tokens.

    Phase 5: a second form lets the user POST /magic/extend/{token} to
    bump the attention-sleep clock by 60 min without consuming the
    approval token. extend_status (when set) drives an inline toast:
    'extended' on success, 'cap_reached' when extend_count >= cap,
    'not_awaiting' when the thread is no longer in awaiting_user.
    """
    safe_args = (
        tool_args_preview.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    safe_tool = tool_name.replace("&", "&amp;").replace("<", "&lt;")
    if intended_decision == "approved":
        button_label = "Confirm: Approve"
        button_color = "#a6e3a1"
    elif intended_decision == "denied":
        button_label = "Confirm: Deny"
        button_color = "#f38ba8"
    else:
        button_label = "Confirm decision"
        button_color = "#cba6f7"

    quoted_token = urllib.parse.quote(token, safe="")

    # Extend banner copy — friendly, action-specific.
    extend_banner_html = ""
    if extend_status == "extended":
        remaining_str = (
            f" — {extends_remaining} extends remaining"
            if extends_remaining is not None
            else ""
        )
        extend_banner_html = (
            '<div style="background: #1e2030; border: 1px solid #a6e3a1; '
            "border-radius: 6px; padding: 10px 12px; margin: 0 0 12px 0; "
            f'color: #a6e3a1; font-size: 13px;">Window extended by 60 minutes'
            f"{remaining_str}.</div>"
        )
    elif extend_status == "cap_reached":
        extend_banner_html = (
            '<div style="background: #1e2030; border: 1px solid #f9e2af; '
            "border-radius: 6px; padding: 10px 12px; margin: 0 0 12px 0; "
            'color: #f9e2af; font-size: 13px;">Extend limit reached — please '
            "approve, deny, or open the cockpit.</div>"
        )
    elif extend_status == "not_awaiting":
        extend_banner_html = (
            '<div style="background: #1e2030; border: 1px solid #89b4fa; '
            "border-radius: 6px; padding: 10px 12px; margin: 0 0 12px 0; "
            'color: #89b4fa; font-size: 13px;">No extend needed — the agent '
            "is already active.</div>"
        )

    # Disable the extend button if we already know the cap was hit.
    extend_disabled_attr = (
        ' disabled style="opacity: 0.5; cursor: not-allowed;"'
        if extend_status == "cap_reached"
        else ""
    )

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>SRW — Confirm Decision</title></head>
<body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #1e1e2e; color: #cdd6f4; padding: 40px 20px;">
  <div style="max-width: 600px; margin: 0 auto; border: 1px solid #313244; border-radius: 12px; overflow: hidden;">
    <div style="background: #181825; padding: 16px 20px; border-bottom: 1px solid #313244;">
      <h2 style="margin: 0; color: #cba6f7; font-size: 16px;">Confirm tool decision</h2>
    </div>
    <div style="padding: 20px; font-size: 14px; line-height: 1.6;">
      {extend_banner_html}
      <p>The agent wants to call <code style="background: #181825; padding: 2px 6px; border-radius: 4px;">{safe_tool}</code> with these arguments:</p>
      <pre style="background: #181825; padding: 12px; border-radius: 6px; overflow-x: auto; font-size: 12px; color: #a6e3a1;">{safe_args}</pre>
    </div>
    <div style="background: #181825; padding: 16px 20px; border-top: 1px solid #313244; text-align: center;">
      <form method="POST" action="/magic/approve/{quoted_token}" style="display: inline;">
        <button type="submit" style="background: {button_color}; color: #1e1e2e; padding: 10px 28px; border: 0; border-radius: 6px; cursor: pointer; font-weight: 600; font-size: 14px;">{button_label}</button>
      </form>
      <form method="POST" action="/magic/extend/{quoted_token}" style="display: inline; margin-left: 8px;">
        <button type="submit"{extend_disabled_attr} style="background: transparent; color: #89b4fa; padding: 10px 20px; border: 1px solid #89b4fa; border-radius: 6px; cursor: pointer; font-weight: 600; font-size: 14px;">I'm reviewing — extend 60min</button>
      </form>
      <p style="margin: 16px 0 0 0; color: #6c7086; font-size: 12px;">Approve link is single-use and expires in 30 minutes.</p>
    </div>
  </div>
</body></html>"""


def _magic_link_result_page(
    *,
    title: str,
    body: str,
    cockpit_url: str,
    is_error: bool = False,
) -> str:
    accent = "#f38ba8" if is_error else "#a6e3a1"
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>SRW — {title}</title></head>
<body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #1e1e2e; color: #cdd6f4; padding: 40px 20px;">
  <div style="max-width: 600px; margin: 0 auto; border: 1px solid #313244; border-radius: 12px; overflow: hidden;">
    <div style="background: #181825; padding: 16px 20px; border-bottom: 1px solid #313244;">
      <h2 style="margin: 0; color: {accent}; font-size: 16px;">{title}</h2>
    </div>
    <div style="padding: 20px; font-size: 14px; line-height: 1.6;">
      <p>{body}</p>
      <p style="margin-top: 16px;"><a href="{cockpit_url}" style="color: #cba6f7;">Open the cockpit</a></p>
    </div>
  </div>
</body></html>"""


@app.get("/magic/approve/{token}")
async def magic_link_get(token: str) -> HTMLResponse:
    """Show a confirmation page for the magic-link token.

    Does NOT consume the token (POST does). This separation is critical:
    email link previewers (Outlook Safe Links, Gmail) auto-fetch URLs
    server-side; a GET-executes link would be consumed by a bot before
    the human ever clicks.
    """
    cockpit_external_url = email_service.cockpit_url or "http://localhost:4200"

    row = await headless_notifications.validate_magic_link(postgres_db, token)
    if row is None:
        return HTMLResponse(
            _magic_link_result_page(
                title="Link expired or already used",
                body=(
                    "This approval link is no longer valid. It may have "
                    "expired, been used already, or been invalidated by a "
                    "newer approval. Open the cockpit to see the current "
                    "state."
                ),
                cockpit_url=cockpit_external_url,
                is_error=True,
            ),
            status_code=404,
        )

    # Fetch tool details for the confirmation page.
    async with postgres_db.acquire() as conn:
        permission_row = await conn.fetchrow(
            "SELECT id, tool_name, tool_args, status "
            "FROM thread_permission_requests WHERE id = $1",
            row["approval_id"],
        )

    if permission_row is None or permission_row["status"] != "pending":
        return HTMLResponse(
            _magic_link_result_page(
                title="Already decided",
                body=(
                    "The agent's request has already been resolved. No "
                    "further action is needed."
                ),
                cockpit_url=cockpit_external_url,
            ),
            status_code=409,
        )

    tool_args = permission_row["tool_args"]
    if isinstance(tool_args, str):
        try:
            tool_args = json.loads(tool_args)
        except Exception:
            tool_args = {}
    elif tool_args is None:
        tool_args = {}
    args_preview = json.dumps(tool_args, indent=2, default=str)
    if len(args_preview) > 600:
        args_preview = args_preview[:600] + "\n… (truncated)"

    page = _magic_link_confirmation_page(
        tool_name=permission_row["tool_name"],
        tool_args_preview=args_preview,
        intended_decision=row.get("intended_decision"),
        token=token,
    )
    return HTMLResponse(page)


@app.post("/magic/approve/{token}")
async def magic_link_post(token: str) -> HTMLResponse:
    """Consume the token and resolve the permission request.

    CAS UPDATE on magic_link_tokens (single-use) + a second UPDATE on
    thread_permission_requests (which the agent's LISTEN picks up via
    the existing trigger). Distinguishes 404 (invalid) from 409 (token
    already used or request already decided) for clean UX on double-clicks.
    """
    cockpit_external_url = email_service.cockpit_url or "http://localhost:4200"

    row = await headless_notifications.validate_magic_link(postgres_db, token)
    if row is None:
        return HTMLResponse(
            _magic_link_result_page(
                title="Link expired or already used",
                body=(
                    "This approval link is no longer valid. It may have "
                    "expired or been used already."
                ),
                cockpit_url=cockpit_external_url,
                is_error=True,
            ),
            status_code=404,
        )

    decision = row.get("intended_decision") or "approved"

    consumed = await headless_notifications.consume_magic_link(
        postgres_db, str(row["id"]), decision
    )
    if consumed is None:
        return HTMLResponse(
            _magic_link_result_page(
                title="Already used",
                body=(
                    "This link has already been used. The agent's request "
                    "is being processed."
                ),
                cockpit_url=cockpit_external_url,
            ),
            status_code=409,
        )

    # Resolve the permission request. CAS-style UPDATE so we don't race
    # with the cockpit having already decided it.
    decided_by_label = "magic_link"
    if consumed.get("user_id"):
        decided_by_label = f"user:{consumed['user_id']}"
    async with postgres_db.acquire() as conn:
        permission_row = await conn.fetchrow(
            "UPDATE thread_permission_requests "
            "SET status = $2, decided_at = now(), decided_by = $3 "
            "WHERE id = $1 AND status = 'pending' "
            "RETURNING id, status, tool_call_id, tool_name, thread_id",
            consumed["approval_id"],
            decision,
            decided_by_label,
        )

    if permission_row is None:
        return HTMLResponse(
            _magic_link_result_page(
                title="Already decided",
                body=(
                    "The agent's request was already resolved by another "
                    "approval path (cockpit click, REST, or expired). "
                    "Your action was not needed."
                ),
                cockpit_url=cockpit_external_url,
            ),
            status_code=409,
        )

    # Phase 5: if the thread is suspended (attention-sleep watchdog fired
    # since the email was sent), kick off restore + agent-pod re-creation.
    # The agent's wake path (_loop_permission_check select-first guard)
    # will pick up this UPDATE's decision once the new pod is alive — no
    # second click required.
    asyncio.create_task(
        _phase5_wake_if_suspended(str(permission_row["thread_id"])),
        name=f"phase5-wake-{str(permission_row['thread_id'])[:8]}",
    )

    pretty = "approved" if decision == "approved" else "denied"
    return HTMLResponse(
        _magic_link_result_page(
            title=f"Tool {pretty}",
            body=(
                f"The agent's request to call "
                f"<code>{permission_row['tool_name']}</code> has been "
                f"{pretty}. The agent will resume shortly."
            ),
            cockpit_url=cockpit_external_url,
        )
    )


@app.post("/magic/extend/{token}")
async def magic_link_extend(token: str) -> HTMLResponse:
    """Extend the attention-sleep window for the thread bound to this token.

    Validates the token (same hash + expiry + single-use checks as
    /magic/approve) but does NOT consume it — the user is signaling
    "I'm still reviewing" without making the approve decision. Bumps
    threads.awaiting_user_since forward by 60 minutes per click, capped
    at HEADLESS_EXTEND_CAP (default 4 = 4h total ceiling).

    Re-renders the confirmation page with a toast so the user can still
    click approve/deny on the same screen. Status_code 200 throughout —
    the page itself carries the success/cap/not-awaiting signal.

    Why a separate route and not "extend ↔ approve same POST": the
    approve handler consumes the token (single-use CAS). If extend
    shared that path, every extend click would burn the approval token
    and the user couldn't approve afterward.
    """
    cockpit_external_url = email_service.cockpit_url or "http://localhost:4200"

    row = await headless_notifications.validate_magic_link(postgres_db, token)
    if row is None:
        return HTMLResponse(
            _magic_link_result_page(
                title="Link expired or already used",
                body=(
                    "This link is no longer valid. Open the cockpit to "
                    "review the agent's current state."
                ),
                cockpit_url=cockpit_external_url,
                is_error=True,
            ),
            status_code=404,
        )

    thread_id = row.get("thread_id")
    if thread_id is None:
        return HTMLResponse(
            _magic_link_result_page(
                title="Cannot extend",
                body="This link is not bound to a thread.",
                cockpit_url=cockpit_external_url,
                is_error=True,
            ),
            status_code=400,
        )

    # Bump awaiting_user_since iff the thread is still in awaiting_user
    # and extend_count < cap. The CAS UPDATE returns the new row state so
    # we can show the right banner. status='active' or 'suspended' means
    # there's nothing to extend — the agent has either woken up already
    # or moved beyond awaiting_user.
    async with postgres_db.acquire() as conn:
        updated = await conn.fetchrow(
            "UPDATE threads "
            "SET awaiting_user_since = now(), "
            "    extend_count = extend_count + 1 "
            "WHERE id = $1 "
            "  AND status = 'awaiting_user' "
            "  AND extend_count < $2 "
            "RETURNING extend_count",
            str(thread_id),
            _MAGIC_EXTEND_CAP,
        )

    if updated is None:
        # Distinguish cap_reached from not_awaiting for the banner copy.
        async with postgres_db.acquire() as conn:
            row_state = await conn.fetchrow(
                "SELECT status, extend_count FROM threads WHERE id = $1",
                str(thread_id),
            )
        if row_state is None:
            extend_status = "not_awaiting"
        elif row_state["status"] != "awaiting_user":
            extend_status = "not_awaiting"
        elif row_state["extend_count"] >= _MAGIC_EXTEND_CAP:
            extend_status = "cap_reached"
        else:
            # Edge case — concurrent change between our UPDATE and SELECT.
            # Render not_awaiting which is the gentler banner.
            extend_status = "not_awaiting"
        extends_remaining = None
    else:
        extend_status = "extended"
        extends_remaining = max(0, _MAGIC_EXTEND_CAP - int(updated["extend_count"]))

    # Re-render the confirmation page with the banner. Load the permission
    # row again (status may have changed underneath us).
    approval_id = row.get("approval_id")
    if approval_id is not None:
        async with postgres_db.acquire() as conn:
            permission_row = await conn.fetchrow(
                "SELECT tool_name, tool_args, status FROM "
                "thread_permission_requests WHERE id = $1",
                approval_id,
            )
    else:
        permission_row = None

    if permission_row is None or permission_row["status"] != "pending":
        return HTMLResponse(
            _magic_link_result_page(
                title="Already decided",
                body=(
                    "The agent's request has been resolved. No further "
                    "action is needed."
                ),
                cockpit_url=cockpit_external_url,
            ),
            status_code=200,
        )

    tool_args = permission_row["tool_args"]
    if isinstance(tool_args, str):
        try:
            tool_args = json.loads(tool_args)
        except Exception:
            tool_args = {}
    elif tool_args is None:
        tool_args = {}
    args_preview = json.dumps(tool_args, indent=2, default=str)
    if len(args_preview) > 600:
        args_preview = args_preview[:600] + "\n… (truncated)"

    page = _magic_link_confirmation_page(
        tool_name=permission_row["tool_name"],
        tool_args_preview=args_preview,
        intended_decision=row.get("intended_decision"),
        token=token,
        extend_status=extend_status,
        extends_remaining=extends_remaining,
    )
    return HTMLResponse(page)


async def _phase5_wake_if_suspended(thread_id: str) -> None:
    """Restore a suspended thread's workspace + agent pod after a magic-link
    decision. Fire-and-forget — the HTTP response has already returned.

    Pattern mirrors resume_persistent_thread (main.py:10640) — restore the
    workspace from S3, then spawn the agent pod if the persistent
    provisioner is wired. Idempotent: status checks short-circuit when the
    workspace is already alive (e.g. a cockpit tab is open and the click
    came from email anyway).
    """
    try:
        thread = await postgres_db.get_thread(thread_id)
        if not thread:
            return
        metadata = thread.get("metadata") or {}
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except (json.JSONDecodeError, TypeError):
                metadata = {}
        ws_ctx = metadata.get("workspace_container") or {}
        ws_status = ws_ctx.get("status")
        if ws_status == "suspended" and workspace_suspension_service.is_enabled:
            logger.info(
                "magic-link wake: restoring suspended workspace for thread %s",
                thread_id,
            )
            ok = await workspace_suspension_service.restore_thread_workspace(thread_id)
            if not ok:
                logger.warning(
                    "magic-link wake: workspace restore failed for thread %s",
                    thread_id,
                )
                return
            # Reflect on the thread row that we're awake again. The agent
            # pod's _attach_session will set this to 'active' too, but
            # writing here closes the window where the attention-sleep
            # watchdog could re-fire before the agent boots.
            async with postgres_db.acquire() as conn:
                await conn.execute(
                    "UPDATE threads "
                    "SET status = 'active', "
                    "    awaiting_user_since = NULL, "
                    "    extend_count = 0 "
                    "WHERE id = $1 AND status IN ('suspended', 'awaiting_user')",
                    thread_id,
                )

        # Agent pod may also have been deleted on suspension
        # (workspace_suspension.py:502-504). Re-provision if a persistent
        # provisioner is configured. fire-and-forget — the agent's boot
        # will restore the LangGraph checkpoint and re-enter permission_check
        # for the same tool_call_id, where the select-first guard picks up
        # the decision we just UPDATEd.
        if persistent_provisioner is not None and not thread.get("agent_id"):
            config_name = thread.get("config_name", "persistent_defaults")
            asyncio.create_task(
                persistent_provisioner.create_agent_pod(
                    thread_id, config_name=config_name
                ),
                name=f"phase5-create-agent-{thread_id[:8]}",
            )
    except Exception as e:
        logger.warning(
            "magic-link wake task failed for thread %s: %s",
            thread_id,
            e,
        )


async def thread_permission_notify_sweeper(
    shutdown_event: asyncio.Event,
) -> None:
    """Background task: scan for permission requests waiting >N seconds and
    dispatch the magic-link email if not yet notified.

    Runs every HEADLESS_NOTIFY_INTERVAL_S (default 30s). Idempotent: the
    send function dedup-skips rows already in thread_notifications.

    Best-effort. Survives transient errors by logging and continuing.
    """
    interval_s = int(os.environ.get("HEADLESS_NOTIFY_INTERVAL_S", "30"))
    age_threshold_s = int(os.environ.get("HEADLESS_NOTIFY_AGE_S", "30"))
    logger.info(
        "Headless permission-notify sweeper started (interval=%ds, age_threshold=%ds)",
        interval_s,
        age_threshold_s,
    )
    cockpit_external_url = email_service.cockpit_url or "http://localhost:4200"

    while not shutdown_event.is_set():
        try:
            async with postgres_db.acquire() as conn:
                # Suppress requests with terminal-or-permanent outcomes
                # ('sent', 'failed', 'skipped_no_email',
                # 'skipped_already_resolved') forever. Suppress
                # transient outcomes ('skipped_rate_limit',
                # 'skipped_smtp') only inside a recency window of
                # 2 × sweeper interval, so they can re-try once the
                # transient condition clears.
                rows = await conn.fetch(
                    "SELECT id, thread_id "
                    "FROM thread_permission_requests "
                    "WHERE status = 'pending' "
                    "  AND requested_at < now() - "
                    "      ($1 || ' seconds')::interval "
                    "  AND NOT EXISTS ("
                    "    SELECT 1 FROM thread_notifications tn "
                    "    WHERE tn.request_id = thread_permission_requests.id "
                    "      AND tn.kind = 'permission_pending' "
                    "      AND ("
                    "        tn.delivery_status IN ("
                    "          'sent', 'failed', "
                    "          'skipped_no_email', "
                    "          'skipped_already_resolved'"
                    "        ) "
                    "        OR ("
                    "          tn.delivery_status IN ("
                    "            'skipped_rate_limit', 'skipped_smtp'"
                    "          ) "
                    "          AND tn.sent_at > now() - "
                    "              make_interval(secs => $2)"
                    "        )"
                    "      )"
                    "  ) "
                    "ORDER BY requested_at ASC "
                    "LIMIT 50",
                    str(age_threshold_s),
                    interval_s * 2,
                )
            for row in rows:
                try:
                    result = await headless_notifications.send_permission_pending_email(
                        postgres_db,
                        email_service,
                        thread_id=str(row["thread_id"]),
                        approval_id=str(row["id"]),
                        cockpit_external_url=cockpit_external_url,
                    )
                    if result.get("status") == "sent":
                        logger.info(
                            "Sent permission-pending email (thread=%s req=%s)",
                            str(row["thread_id"])[:8],
                            str(row["id"])[:8],
                        )
                except Exception as e:
                    logger.warning(
                        "Permission-pending email failed (req=%s): %s",
                        str(row["id"])[:8],
                        e,
                    )
        except Exception as e:
            logger.warning("headless permission-notify sweep error: %s", e)
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=float(interval_s))
            break
        except asyncio.TimeoutError:
            pass
    logger.info("Headless permission-notify sweeper stopped")


# =============================================================================
# Phase 5 — Attention sleep watchdog
# =============================================================================
#
# Suspends thread workspaces (and the bound agent pod) when the agent has
# been in `awaiting_user` for longer than HEADLESS_ATTENTION_SLEEP_MINUTES.
# State machine:
#   active ─→ awaiting_user (agent: natural pause + no WS subscriber)
#   awaiting_user ─→ suspended (this watchdog after TTL)
#   awaiting_user ─→ active (agent: subscriber reattach, clears timer)
#   suspended ─→ active (magic-link wake or REST reattach restores workspace)
#
# Magic-link "extend window" POSTs bump awaiting_user_since forward so the
# watchdog re-arms; threads.extend_count caps the bumps at 4 (4h total).
#
# Today's "tethered" signal is WS-only — Phase 5 v1 ships before the
# cockpit migrates from WS to SSE. SSE-only consumers (MCP, curl) do not
# block suspension; they should rely on magic-link wake to bring the
# session back. When cockpit moves to SSE, this watchdog will need to
# consult the orchestrator's in-process SSE attach registry too.


_ATTENTION_SLEEP_INTERVAL_S: int = int(
    os.environ.get("HEADLESS_ATTENTION_SLEEP_INTERVAL_S", "60")
)
_ATTENTION_SLEEP_MINUTES: int = int(
    os.environ.get("HEADLESS_ATTENTION_SLEEP_MINUTES", "60")
)


async def attention_sleep_sweeper(shutdown_event: asyncio.Event) -> None:
    """Background task: suspend threads stuck in awaiting_user past their TTL.

    Runs every HEADLESS_ATTENTION_SLEEP_INTERVAL_S (default 60s). For each
    qualifying thread:
      1. Call workspace_suspension_service.suspend_thread_workspace() —
         snapshots filesystem to S3, deletes workspace pod/VM, also deletes
         the bound agent pod (workspace_suspension.py:502-504).
      2. CAS UPDATE thread.status from 'awaiting_user' → 'suspended'. The
         CAS guards against the user re-attaching mid-suspend: if status
         flipped back to 'active' between the SELECT and the UPDATE, we
         don't clobber it.

    Best-effort: a transient failure (DB unavailable, suspend service
    error) is logged and retried on the next tick.
    """
    interval_s = _ATTENTION_SLEEP_INTERVAL_S
    ttl_minutes = _ATTENTION_SLEEP_MINUTES
    logger.info(
        "Attention-sleep sweeper started (interval=%ds, ttl=%dmin)",
        interval_s,
        ttl_minutes,
    )

    while not shutdown_event.is_set():
        try:
            if workspace_suspension_service.is_enabled:
                async with postgres_db.acquire() as conn:
                    # Phase 6: per-thread TTL resolution. Priority order is
                    # (1) thread.metadata.config_override.headless overrides,
                    # (2) users.settings.persistent_agent overrides,
                    # (3) the global HEADLESS_ATTENTION_SLEEP_MINUTES default.
                    # ttl <= 0 disables the watchdog for that thread, matching
                    # the cockpit UX of "Never auto-suspend".
                    rows = await conn.fetch(
                        "SELECT t.id "
                        "FROM threads t "
                        "LEFT JOIN users u ON u.id = t.user_id "
                        "WHERE t.status = 'awaiting_user' "
                        "  AND t.awaiting_user_since IS NOT NULL "
                        "  AND COALESCE("
                        "    NULLIF(t.metadata->'config_override'->'headless'->>'attention_sleep_minutes', '')::int, "
                        "    NULLIF(u.settings->'persistent_agent'->>'headless_attention_sleep_minutes', '')::int, "
                        "    $1::int"
                        "  ) > 0 "
                        "  AND t.awaiting_user_since < now() - make_interval(mins => COALESCE("
                        "    NULLIF(t.metadata->'config_override'->'headless'->>'attention_sleep_minutes', '')::int, "
                        "    NULLIF(u.settings->'persistent_agent'->>'headless_attention_sleep_minutes', '')::int, "
                        "    $1::int"
                        "  )) "
                        "ORDER BY t.awaiting_user_since ASC "
                        "LIMIT 50",
                        int(ttl_minutes),
                    )

                for row in rows:
                    thread_id = str(row["id"])
                    try:
                        ok = (
                            await workspace_suspension_service.suspend_thread_workspace(
                                thread_id
                            )
                        )
                        if not ok:
                            logger.info(
                                "attention-sleep: suspend declined for thread %s "
                                "(workspace not ready or already suspending)",
                                thread_id,
                            )
                            continue
                        async with postgres_db.acquire() as conn:
                            updated = await conn.fetchval(
                                "UPDATE threads "
                                "SET status = 'suspended' "
                                "WHERE id = $1 AND status = 'awaiting_user' "
                                "RETURNING id",
                                thread_id,
                            )
                        if updated:
                            logger.info(
                                "attention-sleep: thread %s suspended (was "
                                "awaiting_user >%dm)",
                                thread_id,
                                ttl_minutes,
                            )
                        else:
                            # Concurrent reattach won the race — workspace
                            # is suspended but the restore path will pick it
                            # up on the next reattach.
                            logger.info(
                                "attention-sleep: status flipped during "
                                "suspend for thread %s; restore path will "
                                "handle wake",
                                thread_id,
                            )
                    except Exception as e:
                        logger.warning(
                            "attention-sleep: suspend failed for thread %s: %s",
                            thread_id,
                            e,
                        )
        except Exception as e:
            logger.warning("attention-sleep sweep error: %s", e)

        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=float(interval_s))
            break
        except asyncio.TimeoutError:
            pass

    logger.info("Attention-sleep sweeper stopped")


@app.get("/api/persistent/threads/{thread_id}/ide")
async def get_thread_ide_status(thread_id: str, request: Request) -> dict[str, Any]:
    """Get IDE session status for a persistent thread's workspace.

    Returns the workspace container or VM status with a code-server URL
    when the workspace is ready. The proxy path uses the thread_id in
    place of job_id: ``/api/ide/{thread_id}/proxy/``.
    """
    user, thread = await require_thread_owner(request, postgres_db, thread_id)

    metadata = thread.get("metadata") or {}
    if isinstance(metadata, str):
        import json as _json

        try:
            metadata = _json.loads(metadata)
        except (ValueError, TypeError):
            metadata = {}

    # Build Gitea web URL if repo exists
    ws_ctx = metadata.get("workspace_container", {})
    repo_name = ws_ctx.get("repo_name")
    gitea_url = None
    if repo_name:
        gitea_base = os.environ.get("GITEA_URL", "").rstrip("/")
        gitea_user = os.environ.get("GITEA_ADMIN_USER", "srw")
        if gitea_base:
            gitea_url = f"{gitea_base}/{gitea_user}/{repo_name}"

    # Check VM first (takes precedence over container)
    vm_ctx = metadata.get("vm", {})
    if vm_ctx.get("status") == "ready":
        ssh_host = vm_ctx.get("ssh_host") or vm_ctx.get("pod_ip")
        if ssh_host:
            proxy_base = os.environ.get("IDE_PROXY_BASE_URL", "http://localhost:8085")
            return {
                "status": "active",
                "code_server_url": f"{proxy_base}/api/ide/{thread_id}/proxy/?folder=/home/agent-host/workspace",
                "source": "live_vm",
                "gitea_url": gitea_url,
            }

    # Check workspace container (K8s pod_ip or Docker Compose ide_host)
    if ws_ctx.get("status") == "ready" and (
        ws_ctx.get("pod_ip") or ws_ctx.get("ide_host")
    ):
        proxy_base = os.environ.get("IDE_PROXY_BASE_URL", "http://localhost:8085")
        return {
            "status": "active",
            "code_server_url": f"{proxy_base}/api/ide/{thread_id}/proxy/?folder=/home/agent-host/workspace",
            "source": "live_workspace",
            "gitea_url": gitea_url,
        }

    # Workspace is provisioning (includes "pending" from pre-provision signal)
    if ws_ctx.get("status") in ("provisioning", "pending") or vm_ctx.get("status") in (
        "provisioning",
        "pending",
    ):
        return {"status": "restoring", "code_server_url": None, "gitea_url": gitea_url}

    return {"status": "unavailable", "code_server_url": None, "gitea_url": gitea_url}


@app.post("/api/persistent/threads/{thread_id}/uploads")
async def upload_files_to_thread(
    thread_id: str,
    request: Request,
    files: list[UploadFile] = File(...),
) -> dict[str, Any]:
    """Push files into a persistent thread workspace's ``uploads/`` directory.

    Files are SFTP'd into ``<workspace_path>/uploads/`` on the live
    workspace container (or VM). The cockpit then appends an
    ``Attached files: …`` hint to the user's next message so the agent
    can find them. See ``services/thread_uploads.py`` for SSH details.

    Returns:
        ``{"thread_id": "...", "files": [{name, size, mime_type, path}, ...]}``
    """
    from services.thread_uploads import (
        ThreadUploadError,
        upload_files_to_thread_workspace,
    )

    user, thread = await require_thread_owner(request, postgres_db, thread_id)

    if not files:
        raise HTTPException(status_code=400, detail="No files provided")

    payloads: list[tuple[str, bytes, str]] = []
    for f in files:
        contents = await f.read()
        payloads.append(
            (
                f.filename or "unnamed",
                contents,
                f.content_type or "application/octet-stream",
            )
        )

    try:
        results = await upload_files_to_thread_workspace(thread, payloads)
    except ThreadUploadError as e:
        logger.warning(
            "Thread upload failed for %s: %d %s", thread_id, e.status_code, e.detail
        )
        raise HTTPException(status_code=e.status_code, detail=e.detail) from e

    return {
        "thread_id": thread_id,
        "files": [
            {"name": r.name, "size": r.size, "mime_type": r.mime_type, "path": r.path}
            for r in results
        ],
    }


@app.post("/api/persistent/threads/{thread_id}/tts")
async def synthesize_thread_message_tts(
    thread_id: str,
    request: Request,
    body: dict[str, Any] = Body(...),
) -> Response:
    """Generate speech audio for a chat message.

    Body:
        ``content`` (str, required) — text to speak (typically an assistant
        message).
        ``reformulate`` (bool, default ``True``) — when true, runs an
        auxiliary LLM pass to rewrite the text for natural narration
        (strips markdown, summarizes code blocks, etc.).
        ``language`` (str, default ``"en"``) — selects the TTS voice.

    Returns:
        JSON ``{"text": <spoken text>, "audio": <base64 MP3>}`` on success —
        ``text`` is the formulation-rewritten version actually read aloud, so
        the UI can surface it. ``204 No Content`` when no TTS model is
        configured (the cockpit treats this as "feature off"). ``502`` when a
        model is configured but synthesis fails — so the button shows an error
        instead of silently doing nothing.
    """
    import base64

    from services.tts import TtsSynthesisError, generate_message_tts

    user, thread = await require_thread_owner(request, postgres_db, thread_id)

    content = (body.get("content") or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="Missing 'content' in request body")
    reformulate = bool(body.get("reformulate", True))
    language = (body.get("language") or "en").strip() or "en"

    try:
        result = await generate_message_tts(
            content=content,
            language=language,
            reformulate=reformulate,
            user_id=str(user["id"]),
            postgres_db=postgres_db,
        )
    except TtsSynthesisError as exc:
        raise HTTPException(status_code=502, detail="Speech synthesis failed") from exc

    if result is None:
        # 204: TTS disabled / not configured. The cockpit treats this as a
        # disabled-feature signal rather than an error.
        return Response(status_code=204)
    spoken_text, audio = result
    return JSONResponse(
        {"text": spoken_text, "audio": base64.b64encode(audio).decode("ascii")}
    )


@app.post("/api/persistent/threads/{thread_id}/tts/plan")
async def plan_thread_message_tts(
    thread_id: str,
    request: Request,
    body: dict[str, Any] = Body(...),
) -> Response:
    """Plan a (possibly long) message into ordered, speakable chunks.

    The client synthesizes each chunk via ``POST …/tts`` with
    ``reformulate=false`` (chunks are already cleaned) and plays them as a
    progressive playlist — so a long message reads start-to-finish without a
    single multi-minute request and without truncation.

    Body:
        ``content`` (str, required) — the message text to read aloud.

    Returns:
        JSON ``{"chunks": [str, ...]}`` — one entry for a short message, several
        (each ≤ 4096 chars, split at natural breakpoints) for a long one.
        ``204`` when no TTS model is configured. ``502`` only on an unexpected
        planner error (the planner has deterministic fallbacks, so this is rare).
    """
    from services.tts import plan_tts_chunks

    user, thread = await require_thread_owner(request, postgres_db, thread_id)

    content = (body.get("content") or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="Missing 'content' in request body")

    try:
        chunks = await plan_tts_chunks(
            content=content, user_id=str(user["id"]), postgres_db=postgres_db
        )
    except Exception as exc:
        logger.exception("TTS chunk planning failed for thread %s", thread_id)
        raise HTTPException(status_code=502, detail="TTS planning failed") from exc

    if chunks is None:
        return Response(status_code=204)
    return JSONResponse({"chunks": chunks})


@app.post("/api/persistent/threads/{thread_id}/transcribe")
async def transcribe_thread_audio_endpoint(
    thread_id: str,
    request: Request,
    audio: UploadFile = File(...),
) -> Response:
    """Transcribe a recorded voice message to text (speech-to-text).

    The cockpit composer POSTs the recorded blob here when the user stops
    recording; the returned text is dropped into the message input (editable)
    while the audio is also kept as an attachment. Transcription is server-side
    via the user's configured Whisper model, with auto-detected language.

    Returns:
        ``{"text": "..."}`` on success. ``204 No Content`` when no STT model is
        configured for the user (or transcription failed) — the cockpit then
        just attaches the audio. ``400`` for empty audio; ``413`` when the clip
        exceeds 25 MB (Whisper's hard limit).
    """
    from services.transcribe import transcribe_thread_audio

    user, thread = await require_thread_owner(request, postgres_db, thread_id)

    data = await audio.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty audio upload")
    if len(data) > 25 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Audio too large (max 25 MB)")

    text = await transcribe_thread_audio(
        audio_bytes=data,
        filename=audio.filename or "voice.webm",
        user_id=str(user["id"]),
        postgres_db=postgres_db,
    )
    if text is None:
        # 204: STT disabled / not configured, or transcription failed. The
        # cockpit treats this as "attach audio only" rather than an error.
        return Response(status_code=204)
    return JSONResponse({"text": text})


@app.get("/api/jobs/{job_id}/logs")
async def get_job_logs(
    request: Request,
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

    await require_job_access(request, postgres_db, job_id)

    log_path = workspace_service.base_path / "logs" / f"job_{job_id}.log"
    if not log_path.exists():
        raise HTTPException(
            status_code=404, detail=f"Log file not found for job {job_id}"
        )

    try:
        all_lines = log_path.read_text().splitlines()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read log file: {e}")

    filtered = False

    # Level filter: match lines starting with timestamp pattern followed by level
    if level:
        level_upper = level.upper()
        if level_upper not in ("DEBUG", "INFO", "WARNING", "ERROR"):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid level: {level}. Must be DEBUG, INFO, WARNING, or ERROR",
            )
        pattern = re.compile(
            rf"^\d{{4}}-\d{{2}}-\d{{2}}\s+\d{{2}}:\d{{2}}:\d{{2}}\s+-\s+\S+\s+-\s+{level_upper}\s+-"
        )
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
    request: Request,
    job_id: str,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    call_type: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> dict[str, Any]:
    """List LLM requests for a job with summary fields.

    Returns model, timestamp, token usage, tool call names, call_type, and
    iteration for each request. Use the _id with GET /api/requests/{doc_id} to
    get the full request/response.

    Query params:
        call_type: filter by call type; ``all``/omitted returns main +
            auxiliary calls, or pass an exact type (e.g. ``memory_extraction``).
        status: pass ``error`` to return only failed calls (auxiliary failures
            carry ``status="error"``).
    """
    await require_job_access(request, postgres_db, job_id)
    if not audit_reader.is_available:
        raise HTTPException(status_code=503, detail="MongoDB not available")

    try:
        UUID(job_id)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid job_id format: {job_id}")

    try:
        data = await audit_reader.list_llm_requests(
            job_id,
            limit=limit,
            offset=offset,
            call_type=call_type,
            status=status,
        )
        return data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/jobs/{job_id}/shell-state")
async def get_job_shell_state(request: Request, job_id: str) -> dict[str, Any]:
    """Proxy shell state request to the agent processing a job.

    Resolves job -> assigned agent -> pod IP, then proxies to
    the agent's GET /system/shell-state endpoint.
    """
    import httpx as _httpx

    _, job = await require_job_access(request, postgres_db, job_id)
    try:
        if job.get("status") != "processing":
            raise HTTPException(
                status_code=400,
                detail=f"Job is not processing (status: {job.get('status')})",
            )

        assigned_agent_id = job.get("assigned_agent_id")
        if not assigned_agent_id:
            raise HTTPException(status_code=400, detail="Job has no assigned agent")

        agent = await postgres_db.get_agent(str(assigned_agent_id))
        if not agent:
            raise HTTPException(status_code=404, detail="Assigned agent not found")

        pod_ip = agent.get("pod_ip")
        if not pod_ip:
            raise HTTPException(
                status_code=400, detail="Agent has no pod IP configured"
            )

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
async def delete_agent(request: Request, agent_id: str) -> dict[str, str]:
    """Deregister an agent. **Admin only** (G4).

    Used by the cockpit's agent-list admin tool. Agents may also call
    this on graceful shutdown, but Track B (agent ↔ orchestrator auth)
    will give them a proper bearer-credentialled path — for now the
    heartbeat timeout (3min) handles agent crashes without needing
    this endpoint to be open.
    """
    await _require_admin(request)
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
# Per-User Projections — safe replacement for non-admin agent visibility (G4)
# =============================================================================


_ME_ACTIVE_JOB_STATUSES = {"created", "processing", "paused", "pending_review"}


@app.get("/api/me/active-jobs")
async def list_my_active_jobs(
    request: Request,
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict[str, Any]]:
    """Caller's in-flight jobs — the non-admin replacement for `/api/agents`.

    Returns jobs visible to the caller (G1 visibility OR — own jobs OR
    project-member jobs) in any of the active statuses (created,
    processing, paused, pending_review). The underlying ``get_jobs`` /
    ``get_visible_jobs`` SELECT already excludes pod IPs and hostnames,
    so this is safe to expose to non-admins. Admins still get the full
    fleet via `/api/agents`; they can use this endpoint too if they want
    a personal in-flight summary.

    Respects MCP ``project:<uuid>`` scope narrowing.
    """
    user = await require_approved_user(request, postgres_db)
    is_admin = bool(user.get("is_admin"))
    scope_pid = mcp_scope_project_id(user)
    try:
        if is_admin:
            jobs = await postgres_db.get_jobs(
                status=None,
                user_id=str(user["id"]),
                limit=limit,
                scope_project_id=str(scope_pid) if scope_pid else None,
            )
        else:
            visible = await user_visible_project_ids(user, postgres_db)
            project_ids = [str(p) for p in visible] if visible != "all" else []
            jobs = await postgres_db.get_visible_jobs(
                owner_user_id=str(user["id"]),
                visible_project_ids=project_ids,
                status=None,
                scope_project_id=str(scope_pid) if scope_pid else None,
                limit=limit,
            )
        return [j for j in jobs if j.get("status") in _ME_ACTIVE_JOB_STATUSES]
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
                description = (
                    f"Agent with {', '.join(tool_categories)} tools."
                    if tool_categories
                    else "Custom agent configuration."
                )

            experts.append(
                ExpertInfo(
                    id=entry.name,
                    display_name=data.get(
                        "display_name", entry.name.replace("_", " ").title()
                    ),
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
async def list_experts(
    request: Request, type: str | None = None
) -> list[dict[str, Any]]:
    """List experts: bundled (disk) + DB rows visible to the caller (owned +
    project-linked + global), each tagged with ``source``. **P4e** — approved
    users only. ``type`` narrows the DB rows (worker/session); bundled experts
    are unfiltered (they carry no type), preserving today's behavior.

    Read-only surface. Expert CRUD (create/update/delete/import/export) is the
    deferred fast-follow — the orchestrator-resolved config feature only needs
    the catalog visible + selectable.
    """
    user = await require_approved_user(request, postgres_db)
    global _experts_cache
    if _experts_cache is None:
        _experts_cache = _scan_experts()
    result = [{**e.model_dump(), "source": "bundled"} for e in _experts_cache]
    if _is_experts_db_enabled():
        visible = await user_visible_project_ids(user, postgres_db)
        pids = [] if visible == "all" else [str(p) for p in visible]
        rows = await postgres_db.list_experts_visible(
            user_id=str(user["id"]), project_ids=pids, expert_type=type
        )
        result += [
            {
                "id": str(r["id"]),
                "display_name": r["display_name"],
                "description": r.get("description") or "",
                "icon": r["icon"],
                "color": r["color"],
                "tags": r.get("tags") or [],
                "expert_type": r["expert_type"],
                "source": "global" if r["is_global"] else "user",
            }
            for r in rows
        ]
    return result


@app.post("/api/experts/reload")
async def reload_experts(request: Request) -> dict[str, Any]:
    """Force reload of expert configurations cache. **Admin only** (P4d) —
    reloads expert YAML from disk."""
    await _require_admin(request)
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


_settings_matrix_cache: dict[str, Any] | None = None


def _project_settings_subsection(parsed: dict[str, Any]) -> dict[str, Any]:
    """Project a parsed model_config_matrix to the legacy settings shape.

    Returns ``{family: settings_dict}`` (drops families without a settings
    block) so callers that pre-date the unified file see the same shape
    they used to read from ``settings_matrix.yaml``.
    """
    out: dict[str, Any] = {}
    for family, sections in (parsed or {}).items():
        if not isinstance(sections, dict):
            continue
        # Tolerate both legacy flat shape and unified subsection shape — the
        # unified loader downstream is the source of truth, but per-expert
        # files written before chunk 1 may still arrive flat in tests.
        if "settings" in sections and isinstance(sections["settings"], dict):
            out[family] = sections["settings"]
        elif {"prompts", "instructions"}.isdisjoint(sections.keys()):
            # Pure legacy settings-only block (no `settings:` wrapper) — keep
            # whatever scalar/dict children it carries.
            out[family] = sections
    return out


def _load_settings_matrix(config_dir: Path) -> dict[str, Any]:
    """Load and cache the settings subsection of ``model_config_matrix.yaml``.

    Returns the legacy ``{family: settings_dict}`` shape so existing callers
    (`/api/admin/families`, expert detail endpoints) keep working without
    rewrites. Per-expert overlays are handled by the callers that need
    them; this function returns the base file only.
    """
    global _settings_matrix_cache
    if _settings_matrix_cache is None:
        matrix_path = config_dir / "model_config_matrix.yaml"
        if matrix_path.exists():
            with open(matrix_path) as f:
                parsed = yaml.safe_load(f) or {}
            _settings_matrix_cache = _project_settings_subsection(parsed)
        else:
            _settings_matrix_cache = {}
    return _settings_matrix_cache


async def _load_expert_detail(expert_id: str) -> dict[str, Any]:
    """Load full expert detail: merged config + instructions content. DB-backed
    experts (UUID) resolve their fragment onto the expert_type base; bundled
    experts resolve from disk as before."""
    if _is_experts_db_enabled() and _looks_like_uuid(expert_id):
        row = await postgres_db.get_expert_by_id(expert_id)
        if not row:
            return {}
        base_name = (
            "defaults" if row["expert_type"] == "worker" else "persistent_defaults"
        )
        base_path = _get_config_dir() / f"{base_name}.yaml"
        base = yaml.safe_load(base_path.read_text()) if base_path.exists() else {}
        cfg = row.get("config") or {}
        if isinstance(cfg, str):
            cfg = json.loads(cfg)
        merged = _deep_merge(base, cfg)
        merged.pop("connections", None)
        prompts = row.get("prompts") or {}
        if isinstance(prompts, str):
            prompts = json.loads(prompts)
        return {
            "id": str(row["id"]),
            "display_name": row["display_name"],
            "description": row.get("description") or "",
            "icon": row["icon"],
            "color": row["color"],
            "tags": row.get("tags") or [],
            "expert_type": row["expert_type"],
            "source": "user",
            "config": merged,
            "instructions": prompts.get("instructions"),
            "persona": prompts.get("persona"),
        }
    config_dir = _get_config_dir()

    # Load expert config
    if expert_id == "defaults":
        defaults_path = config_dir / "defaults.yaml"
        if defaults_path.exists():
            with open(defaults_path) as f:
                defaults = yaml.safe_load(f) or {}
        else:
            defaults = {}
        merged = dict(defaults)
        expert_config_dir = config_dir
    else:
        expert_dir = config_dir / "experts" / expert_id
        config_path = expert_dir / "config.yaml"
        if not expert_dir.is_dir() or not config_path.exists():
            return {}
        with open(config_path) as f:
            expert_data = yaml.safe_load(f) or {}

        # Resolve $extends to load the correct base config
        # (e.g. persistent_defaults for interactive, defaults for worker experts)
        extends_name = expert_data.pop("$extends", "defaults")
        base_path = config_dir / f"{extends_name}.yaml"
        if not base_path.exists():
            base_path = config_dir / "defaults.yaml"
        if base_path.exists():
            with open(base_path) as f:
                defaults = yaml.safe_load(f) or {}
        else:
            defaults = {}

        merged = _deep_merge(defaults, expert_data)
        expert_config_dir = expert_dir

    # Load the raw settings_matrix for the client to resolve per-model defaults.
    # Do NOT apply it to merged — the client resolves based on the user's model selection.
    raw_matrix = _load_settings_matrix(config_dir)
    if expert_config_dir and expert_config_dir != config_dir:
        expert_matrix_path = expert_config_dir / "model_config_matrix.yaml"
        if expert_matrix_path.exists():
            with open(expert_matrix_path) as f:
                expert_parsed = yaml.safe_load(f) or {}
            expert_settings = _project_settings_subsection(expert_parsed)
            raw_matrix = _deep_merge(raw_matrix, expert_settings)

    # Load instructions content
    instructions_content = None
    # Check for expert-specific instructions.md first
    instr_path = expert_config_dir / "instructions.md"
    if expert_id != "defaults" and instr_path.exists():
        instructions_content = instr_path.read_text(encoding="utf-8")
    else:
        # Fall back to template referenced in config
        template_name = merged.get("workspace", {}).get(
            "instructions_template", "instructions.md"
        )
        template_path = config_dir / "prompts" / template_name
        if template_path.exists():
            instructions_content = template_path.read_text(encoding="utf-8")

    # Remove internal/sensitive keys from merged config
    for key in ("$extends", "connections"):
        merged.pop(key, None)

    # Expose the defaults' tool lists so the cockpit can re-enable
    # categories that an expert disabled (e.g., scholar sets citation: []).
    defaults_tools = defaults.get("tools", {})

    return {
        "config": merged,
        "instructions": instructions_content,
        "defaults_tools": defaults_tools,
        "settings_matrix": raw_matrix,
    }


@app.get("/api/experts/{expert_id}")
async def get_expert(request: Request, expert_id: str) -> dict[str, Any]:
    """Get full expert detail including merged config and instructions content.

    **P4e** — gated to approved users (shared catalog metadata, not per-user).

    Returns the expert's configuration (merged with defaults) and the raw
    instructions.md content, enabling the cockpit to pre-populate the job
    creation form.
    """
    await require_approved_user(request, postgres_db)

    # DB-backed expert (UUID): the detail payload is self-contained.
    if _is_experts_db_enabled() and _looks_like_uuid(expert_id):
        detail = await _load_expert_detail(expert_id)
        if not detail:
            raise HTTPException(
                status_code=404, detail=f"Expert not found: {expert_id}"
            )
        return detail

    # Verify bundled expert exists
    global _experts_cache
    if _experts_cache is None:
        _experts_cache = _scan_experts()

    if expert_id == "defaults":
        # "defaults" is a virtual expert representing framework defaults
        detail = await _load_expert_detail(expert_id)
        if not detail:
            raise HTTPException(status_code=404, detail="Defaults config not found")
        return detail

    expert_info = next((e for e in _experts_cache if e.id == expert_id), None)
    if not expert_info:
        raise HTTPException(status_code=404, detail=f"Expert not found: {expert_id}")

    detail = await _load_expert_detail(expert_id)
    if not detail:
        raise HTTPException(
            status_code=404, detail=f"Expert config not found: {expert_id}"
        )

    return {
        **expert_info.model_dump(),
        **detail,
    }


# =============================================================================
# User-Defined Experts: DB-backed CRUD + import/export (Slice 1)
# =============================================================================
# Restored from 8334fb3c (removed by 6f8c635e). WRITE surface only — config
# resolution stays orchestrator-side in services/config_resolver.py (the agent
# is a pure executor). The save-time hard-deny scan is the credential boundary;
# per-user grants are Slice 2. Gated by EXPERTS_DB_ENABLED (on in dev, off prod).


class ExpertCreate(BaseModel):
    """Create a DB-backed expert (Slice 1: hard-deny validated; grants in S2)."""

    name: str = Field(..., pattern=r"^[a-z][a-z0-9_-]*$", max_length=100)
    display_name: str = Field(..., min_length=1, max_length=200)
    expert_type: Literal["worker", "session"]
    description: str | None = None
    icon: str = "smart_toy"
    color: str = Field("#6B7280", pattern=r"^#[0-9A-Fa-f]{6}$")
    tags: list[str] = []
    config: dict[str, Any] = {}
    prompts: dict[str, Any] = {}


class ExpertUpdate(BaseModel):
    """Patch a DB expert; expert_type is immutable (decision 3) so it is absent."""

    display_name: str | None = Field(None, min_length=1, max_length=200)
    description: str | None = None
    icon: str | None = None
    color: str | None = Field(None, pattern=r"^#[0-9A-Fa-f]{6}$")
    tags: list[str] | None = None
    config: dict[str, Any] | None = None
    prompts: dict[str, Any] | None = None


class SkillInfo(BaseModel):
    """Skill catalog metadata for discovery (the L1 'menu' entry)."""

    id: str
    name: str
    display_name: str
    description: str
    icon: str = "extension"
    color: str = "#6B7280"
    tags: list[str] = []


class SkillCreate(BaseModel):
    """Create a DB-backed skill from its file tree (must include SKILL.md).

    name + description are parsed from SKILL.md frontmatter, not sent separately."""

    files: dict[str, str]
    display_name: str | None = Field(None, max_length=200)
    icon: str = "extension"
    color: str = Field("#6B7280", pattern=r"^#[0-9A-Fa-f]{6}$")
    tags: list[str] = []


class SkillUpdate(BaseModel):
    """Patch a DB skill; name is immutable (derived from SKILL.md) so it is absent."""

    files: dict[str, str] | None = None
    display_name: str | None = Field(None, min_length=1, max_length=200)
    icon: str | None = None
    color: str | None = Field(None, pattern=r"^#[0-9A-Fa-f]{6}$")
    tags: list[str] | None = None
    is_global: bool | None = None


def _require_experts_db() -> None:
    """The DB-experts feature is fully behind EXPERTS_DB_ENABLED."""
    if not _is_experts_db_enabled():
        raise HTTPException(status_code=404, detail="DB-backed experts are not enabled")


def _validate_expert_fragment(config: dict[str, Any]) -> None:
    """Reject credential sections in a user fragment (decision 10, hard-deny)."""
    from src.core.expert_resolution import hard_deny_scan

    offending = hard_deny_scan(config)
    if offending:
        raise HTTPException(
            status_code=422,
            detail="config may not set credential sections: "
            + ", ".join(sorted(offending)),
        )


# ── Skills (Agent Skills, Slice 1) ────────────────────────────────────────
# Cache bundled skills at startup (mirrors _experts_cache).
_skills_cache: list[SkillInfo] | None = None


def _require_skills_db() -> None:
    """The DB-skills feature is fully behind SKILLS_DB_ENABLED."""
    if not _is_skills_db_enabled():
        raise HTTPException(status_code=404, detail="DB-backed skills are not enabled")


def _validate_skill_frontmatter(frontmatter: dict[str, Any]) -> None:
    """Reject credential sections in SKILL.md frontmatter (reuses expert deny-scan)."""
    from src.core.expert_resolution import hard_deny_scan

    offending = hard_deny_scan(frontmatter)
    if offending:
        raise HTTPException(
            status_code=422,
            detail="SKILL.md frontmatter may not set credential sections: "
            + ", ".join(sorted(offending)),
        )


def _parse_skill_bundle(files: dict[str, str]) -> tuple[str, str, dict[str, str]]:
    """Validate paths, parse SKILL.md, deny-scan. Returns (name, description, files)."""
    from src.core.skill_format import (
        SkillFormatError,
        parse_skill_md,
        skill_identity,
        validate_skill_files,
    )

    try:
        validate_skill_files(files)
        fm, _body = parse_skill_md(files["SKILL.md"])
        name, description = skill_identity(fm)
    except SkillFormatError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    _validate_skill_frontmatter(fm)
    return name, description, files


def _skill_row_to_meta(row: dict[str, Any]) -> dict[str, Any]:
    """Project a skills row into the catalog metadata shape."""
    return {
        "id": str(row["id"]),
        "name": row["name"],
        "display_name": row["display_name"],
        "description": row.get("description") or "",
        "icon": row["icon"],
        "color": row["color"],
        "tags": row.get("tags") or [],
        "version": row.get("version"),
        "owner_id": str(row["owner_id"]) if row.get("owner_id") else None,
    }


def _scan_skills() -> list[SkillInfo]:
    """Scan config/skills/<name>/SKILL.md for bundled skills."""
    from src.core.skill_format import SkillFormatError, parse_skill_md, skill_identity

    skills_dir = _get_config_dir() / "skills"
    skills: list[SkillInfo] = []
    if not skills_dir.is_dir():
        return skills
    for entry in sorted(skills_dir.iterdir()):
        skill_md = entry / "SKILL.md"
        if not entry.is_dir() or not skill_md.exists():
            continue
        try:
            fm, _ = parse_skill_md(skill_md.read_text(encoding="utf-8"))
            name, description = skill_identity(fm)
            skills.append(
                SkillInfo(
                    id=entry.name,
                    name=name,
                    display_name=fm.get("display_name", name.replace("-", " ").title()),
                    description=description,
                    icon=fm.get("icon", "extension"),
                    color=fm.get("color", "#6B7280"),
                    tags=fm.get("tags", []),
                )
            )
        except (SkillFormatError, OSError, ValueError) as e:
            logger.warning(f"Failed to parse bundled skill {skill_md}: {e}")
    return skills


def _bundled_skill_bundle(skill_name: str) -> dict[str, Any] | None:
    """Read a bundled skill's full directory into a metadata + files dict."""
    from src.core.skill_format import (
        parse_skill_md,
        skill_identity,
        validate_skill_path,
    )

    skill_dir = _get_config_dir() / "skills" / skill_name
    skill_md = skill_dir / "SKILL.md"
    if not skill_dir.is_dir() or not skill_md.exists():
        return None
    files: dict[str, str] = {}
    for fp in sorted(skill_dir.rglob("*")):
        if not fp.is_file():
            continue
        rel = str(fp.relative_to(skill_dir))
        try:
            validate_skill_path(rel)
            files[rel] = fp.read_text(encoding="utf-8")
        except (ValueError, UnicodeDecodeError):
            continue
    fm, _ = parse_skill_md(files["SKILL.md"])
    name, description = skill_identity(fm)
    return {
        "id": skill_name,
        "name": name,
        "display_name": fm.get("display_name", name.replace("-", " ").title()),
        "description": description,
        "icon": fm.get("icon", "extension"),
        "color": fm.get("color", "#6B7280"),
        "tags": fm.get("tags", []),
        "files": files,
    }


async def _gather_in_scope_skills(
    user_id: str | None, project_ids: list[str] | None = None
) -> dict[str, Any]:
    """Build the resolved-blob skills payload: the precedence-deduped menu plus
    the file tree for each winning skill. Bundled (disk) + DB (owned + global).
    Returns {} when skills are disabled or there is no user. Slice 2."""
    from src.core.skill_resolution import resolve_skill_menu

    if not _is_skills_db_enabled() or not user_id:
        return {}

    global _skills_cache
    if _skills_cache is None:
        _skills_cache = _scan_skills()

    rows: list[dict[str, Any]] = []
    for s in _skills_cache:
        rows.append(
            {
                **s.model_dump(),
                "owner_id": None,
                "is_global": False,
                "created_at": "",
                "_source": "bundled",
                "_ref": s.id,  # bundled dir name
            }
        )
    for r in await postgres_db.list_skills_visible(user_id=str(user_id)):
        rows.append(
            {
                **_skill_row_to_meta(r),
                "owner_id": str(r["owner_id"]) if r.get("owner_id") else None,
                "is_global": r["is_global"],
                "created_at": str(r.get("created_at", "")),
                "_source": "global" if r["is_global"] else "user",
                "_ref": str(r["id"]),
            }
        )

    menu_rows = resolve_skill_menu(
        rows, user_id=str(user_id), project_ids=set(project_ids or [])
    )

    menu: list[dict[str, Any]] = []
    files: dict[str, dict[str, str]] = {}
    for row in menu_rows:
        menu.append(
            {
                "id": row.get("id"),
                "name": row["name"],
                "display_name": row.get("display_name"),
                "description": row.get("description") or "",
                "icon": row.get("icon"),
                "color": row.get("color"),
                "tags": row.get("tags") or [],
            }
        )
        if row["_source"] == "bundled":
            bundle = _bundled_skill_bundle(row["_ref"])
            if bundle:
                files[row["name"]] = bundle["files"]
        else:
            files[row["name"]] = await postgres_db.get_skill_files(row["_ref"])

    return {"menu": menu, "files": files}


async def _create_forked_skill(
    src: dict[str, Any],
    owner_id: str,
    suffix: str = "copy",
    *,
    prefer_original: bool = False,
) -> dict[str, Any]:
    """Create an owned skill from a source dict. ``prefer_original`` (import) tries
    the source name first and only suffixes on collision, storing the SKILL.md
    verbatim so a clean import->export round-trips byte-for-byte; duplicate always
    suffixes ``-copy``. The SKILL.md 'name' is rewritten only when the slug changes."""
    from src.core.skill_format import set_skill_name

    base_name = src["name"]
    candidates = [base_name] if prefer_original else []
    candidates.append(f"{base_name}-{suffix}")
    candidates += [f"{base_name}-{suffix}-{i}" for i in range(2, 8)]
    for cand in candidates:
        name = cand[:100]
        renamed = name != base_name
        files = dict(src["files"])
        if renamed:
            files["SKILL.md"] = set_skill_name(src["files"]["SKILL.md"], name)
        display = (
            f"{src['display_name']} ({suffix})" if renamed else src["display_name"]
        )
        try:
            return await postgres_db.create_skill(
                name=name,
                display_name=display[:200],
                description=src.get("description"),
                icon=src.get("icon", "extension"),
                color=src.get("color", "#6B7280"),
                tags=src.get("tags") or [],
                owner_id=owner_id,
                files=files,
            )
        except HTTPException:
            raise
        except Exception as e:
            if "uq_skills_name_owner" in str(e):
                continue
            raise
    raise HTTPException(status_code=409, detail="No free name for the copy")


def _bundled_expert_bundle(expert_id: str) -> dict[str, Any] | None:
    """Portable bundle from a bundled (disk) expert: raw config.yaml fragment
    (minus $extends/connections) + persona/instructions files + cache metadata.
    None if not found. expert_type is inferred from $extends."""
    global _experts_cache
    if _experts_cache is None:
        _experts_cache = _scan_experts()
    info = next((e for e in _experts_cache if e.id == expert_id), None)
    if not info:
        return None
    expert_dir = _get_config_dir() / "experts" / expert_id
    config_path = expert_dir / "config.yaml"
    if not config_path.exists():
        return None
    raw = yaml.safe_load(config_path.read_text()) or {}
    extends = raw.pop("$extends", "defaults")
    raw.pop("connections", None)
    prompts: dict[str, Any] = {}
    for key, fname in (("persona", "persona.txt"), ("instructions", "instructions.md")):
        fp = expert_dir / fname
        if fp.exists():
            prompts[key] = fp.read_text(encoding="utf-8")
    return {
        "name": expert_id,
        "display_name": info.display_name,
        "description": info.description,
        "icon": info.icon,
        "color": info.color,
        "tags": info.tags,
        "expert_type": "session" if extends == "persistent_defaults" else "worker",
        "config": raw,
        "prompts": prompts,
    }


def _db_expert_to_bundle_src(row: dict[str, Any]) -> dict[str, Any]:
    """Normalize a DB expert row into the bundle-source shape (JSONB str-tolerant)."""
    cfg = row.get("config") or {}
    if isinstance(cfg, str):
        cfg = json.loads(cfg)
    prm = row.get("prompts") or {}
    if isinstance(prm, str):
        prm = json.loads(prm)
    return {
        "name": row["name"],
        "display_name": row["display_name"],
        "expert_type": row["expert_type"],
        "description": row.get("description"),
        "icon": row["icon"],
        "color": row["color"],
        "tags": row.get("tags") or [],
        "config": cfg,
        "prompts": prm,
    }


async def _create_forked_expert(
    src: dict[str, Any], owner_id: str, suffix: str = "copy"
) -> dict[str, Any]:
    """Create an owned expert from a bundle dict, suffixing the name on collision
    (decision 4/27 fork-on-copy)."""
    base_name = src["name"]
    name = f"{base_name}-{suffix}"[:100]
    for attempt in range(6):
        try:
            return await postgres_db.create_expert(
                name=name,
                display_name=f"{src['display_name']} ({suffix})"[:200],
                expert_type=src["expert_type"],
                owner_id=owner_id,
                description=src.get("description"),
                icon=src.get("icon", "smart_toy"),
                color=src.get("color", "#6B7280"),
                tags=src.get("tags") or [],
                config=src.get("config") or {},
                prompts=src.get("prompts") or {},
            )
        except HTTPException:
            raise
        except Exception as e:
            if "uq_experts_name_owner" in str(e):
                name = f"{base_name}-{suffix}-{attempt + 1}"[:100]
                continue
            raise
    raise HTTPException(status_code=409, detail="No free name for the copy")


@app.post("/api/experts")
async def create_expert(request: Request, body: ExpertCreate) -> dict[str, Any]:
    """Create an owned DB expert. Slice 1: hard-deny validated, no grants yet."""
    _require_experts_db()
    user = await require_approved_user(request, postgres_db)
    if body.config:
        _validate_expert_fragment(body.config)
    await _enforce_expert_save(request, body.config or {}, user=user)
    try:
        return await postgres_db.create_expert(
            name=body.name,
            display_name=body.display_name,
            expert_type=body.expert_type,
            owner_id=str(user["id"]),
            description=body.description,
            icon=body.icon,
            color=body.color,
            tags=body.tags,
            config=body.config,
            prompts=body.prompts,
        )
    except HTTPException:
        raise
    except Exception as e:
        if "uq_experts_name_owner" in str(e):
            raise HTTPException(
                status_code=409,
                detail=f"You already have an expert named '{body.name}'",
            ) from e
        raise


@app.put("/api/experts/{expert_id}")
async def update_expert(
    request: Request, expert_id: str, body: ExpertUpdate
) -> dict[str, Any]:
    """Update an owned DB expert (owner or admin). Bundled experts have no row."""
    _require_experts_db()
    user = await require_approved_user(request, postgres_db)
    if not _looks_like_uuid(expert_id):
        raise HTTPException(status_code=403, detail="Bundled experts are read-only")
    existing = await postgres_db.get_expert_by_id(expert_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Expert not found")
    if str(existing["owner_id"]) != str(user["id"]) and not user.get("is_admin"):
        raise HTTPException(
            status_code=403, detail="Only the owner may edit this expert"
        )
    if body.config is not None:
        _validate_expert_fragment(body.config)
    await _enforce_expert_save(request, body.config or {}, user=user)
    fields = body.model_dump(exclude_unset=True)
    return await postgres_db.update_expert(
        expert_id, updated_by=str(user["id"]), **fields
    )


@app.delete("/api/experts/{expert_id}")
async def delete_expert(request: Request, expert_id: str) -> dict[str, Any]:
    """Delete an owned DB expert (owner or admin). Blocks (409) while
    live-referenced (decision 15)."""
    _require_experts_db()
    user = await require_approved_user(request, postgres_db)
    if not _looks_like_uuid(expert_id):
        raise HTTPException(status_code=403, detail="Bundled experts cannot be deleted")
    existing = await postgres_db.get_expert_by_id(expert_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Expert not found")
    if str(existing["owner_id"]) != str(user["id"]) and not user.get("is_admin"):
        raise HTTPException(
            status_code=403, detail="Only the owner may delete this expert"
        )
    blockers = await postgres_db.expert_delete_blockers(expert_id)
    if blockers:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Expert is in use; repoint or remove these first",
                "blockers": blockers,
            },
        )
    await postgres_db.delete_expert(expert_id)
    return {"deleted": True}


@app.post("/api/experts/{expert_id}/duplicate")
async def duplicate_expert(request: Request, expert_id: str) -> dict[str, Any]:
    """Fork any visible expert (bundled or DB) into an owned copy — 'start from
    scholar' (decision 4: copy, not live link)."""
    _require_experts_db()
    user = await require_approved_user(request, postgres_db)
    if _looks_like_uuid(expert_id):
        row = await postgres_db.get_expert_by_id(expert_id)
        if not row:
            raise HTTPException(status_code=404, detail="Expert not found")
        src = _db_expert_to_bundle_src(row)
    else:
        src = _bundled_expert_bundle(expert_id)
        if not src:
            raise HTTPException(status_code=404, detail="Expert not found")
    return await _create_forked_expert(src, str(user["id"]), suffix="copy")


@app.get("/api/experts/{expert_id}/export")
async def export_expert(request: Request, expert_id: str) -> dict[str, Any]:
    """Serialize an expert to a portable bundle (decision 27). DB experts export
    their raw fragment; bundled experts export their on-disk config."""
    from src.core.expert_resolution import to_export_bundle

    _require_experts_db()
    await require_approved_user(request, postgres_db)
    if _looks_like_uuid(expert_id):
        row = await postgres_db.get_expert_by_id(expert_id)
        if not row:
            raise HTTPException(status_code=404, detail="Expert not found")
        return to_export_bundle(_db_expert_to_bundle_src(row))
    bundle = _bundled_expert_bundle(expert_id)
    if not bundle:
        raise HTTPException(status_code=404, detail="Expert not found")
    return to_export_bundle(bundle)


@app.post("/api/experts/import")
async def import_expert(request: Request, body: ExpertCreate) -> dict[str, Any]:
    """Create an owned expert from a posted bundle (decision 27). Same validation
    as create; fork-on-import (name collision -> suffix)."""
    _require_experts_db()
    user = await require_approved_user(request, postgres_db)
    if body.config:
        _validate_expert_fragment(body.config)
    await _enforce_expert_save(request, body.config or {}, user=user)
    name = body.name
    for attempt in range(6):
        try:
            return await postgres_db.create_expert(
                name=name,
                display_name=body.display_name,
                expert_type=body.expert_type,
                owner_id=str(user["id"]),
                description=body.description,
                icon=body.icon,
                color=body.color,
                tags=body.tags,
                config=body.config,
                prompts=body.prompts,
            )
        except HTTPException:
            raise
        except Exception as e:
            if "uq_experts_name_owner" in str(e):
                name = (
                    f"{body.name}-import"
                    if attempt == 0
                    else f"{body.name}-import-{attempt}"
                )
                continue
            raise
    raise HTTPException(status_code=409, detail="No free name for the import")


# =============================================================================
# Skill Endpoints (Agent Skills, Slice 1)
# =============================================================================


@app.post("/api/skills")
async def create_skill(request: Request, body: SkillCreate) -> dict[str, Any]:
    """Create an owned DB skill from its file tree (Slice 1: deny-scan validated)."""
    _require_skills_db()
    user = await require_approved_user(request, postgres_db)
    name, description, files = _parse_skill_bundle(body.files)
    try:
        return await postgres_db.create_skill(
            name=name,
            display_name=body.display_name or name,
            description=description,
            icon=body.icon,
            color=body.color,
            tags=body.tags,
            owner_id=str(user["id"]),
            files=files,
        )
    except HTTPException:
        raise
    except Exception as e:
        if "uq_skills_name_owner" in str(e):
            raise HTTPException(
                status_code=409, detail=f"You already have a skill named '{name}'"
            ) from e
        raise


@app.get("/api/skills")
async def list_skills(request: Request) -> list[dict[str, Any]]:
    """List skills: bundled (disk) + DB rows visible to the caller (owned + global),
    each tagged with ``source``. Read-only; tags-and-concatenates (precedence is a
    Slice-2 resolver concern)."""
    user = await require_approved_user(request, postgres_db)
    global _skills_cache
    if _skills_cache is None:
        _skills_cache = _scan_skills()
    result = [{**s.model_dump(), "source": "bundled"} for s in _skills_cache]
    if _is_skills_db_enabled():
        rows = await postgres_db.list_skills_visible(user_id=str(user["id"]))
        result += [
            {
                **_skill_row_to_meta(r),
                "source": "global" if r["is_global"] else "user",
            }
            for r in rows
        ]
    return result


@app.post("/api/skills/reload")
async def reload_skills(request: Request) -> dict[str, Any]:
    """Force reload of bundled skill cache. **Admin only**."""
    await _require_admin(request)
    global _skills_cache
    _skills_cache = _scan_skills()
    return {"status": "reloaded", "count": len(_skills_cache)}


@app.get("/api/skills/{skill_id}")
async def get_skill(request: Request, skill_id: str) -> dict[str, Any]:
    """Full skill detail (metadata + file tree). DB skill by UUID, else bundled."""
    await require_approved_user(request, postgres_db)
    if _is_skills_db_enabled() and _looks_like_uuid(skill_id):
        row = await postgres_db.get_skill_by_id(skill_id)
        if not row:
            raise HTTPException(status_code=404, detail=f"Skill not found: {skill_id}")
        files = await postgres_db.get_skill_files(skill_id)
        return {
            **_skill_row_to_meta(row),
            "source": "global" if row["is_global"] else "user",
            "files": files,
        }
    bundle = _bundled_skill_bundle(skill_id)
    if not bundle:
        raise HTTPException(status_code=404, detail=f"Skill not found: {skill_id}")
    return {**bundle, "source": "bundled"}


@app.put("/api/skills/{skill_id}")
async def update_skill(
    request: Request, skill_id: str, body: SkillUpdate
) -> dict[str, Any]:
    """Update an owned DB skill (owner or admin). Bundled skills are read-only.
    ``name`` is immutable — an edited SKILL.md whose frontmatter name differs is
    rejected (rename = create a new skill)."""
    _require_skills_db()
    user = await require_approved_user(request, postgres_db)
    if not _looks_like_uuid(skill_id):
        raise HTTPException(status_code=403, detail="Bundled skills are read-only")
    existing = await postgres_db.get_skill_by_id(skill_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Skill not found")
    if str(existing["owner_id"]) != str(user["id"]) and not user.get("is_admin"):
        raise HTTPException(
            status_code=403, detail="Only the owner may edit this skill"
        )
    fields = body.model_dump(exclude_unset=True, exclude={"files"})
    files = body.files
    if files is not None:
        name, description, files = _parse_skill_bundle(files)
        if name != existing["name"]:
            raise HTTPException(
                status_code=422,
                detail=f"SKILL.md name '{name}' must match the skill's name "
                f"'{existing['name']}'; create a new skill to rename",
            )
        fields["description"] = description
    return await postgres_db.update_skill(
        skill_id, updated_by=str(user["id"]), files=files, **fields
    )


@app.delete("/api/skills/{skill_id}")
async def delete_skill(request: Request, skill_id: str) -> dict[str, Any]:
    """Delete an owned DB skill (owner or admin). Files cascade away."""
    _require_skills_db()
    user = await require_approved_user(request, postgres_db)
    if not _looks_like_uuid(skill_id):
        raise HTTPException(status_code=403, detail="Bundled skills cannot be deleted")
    existing = await postgres_db.get_skill_by_id(skill_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Skill not found")
    if str(existing["owner_id"]) != str(user["id"]) and not user.get("is_admin"):
        raise HTTPException(
            status_code=403, detail="Only the owner may delete this skill"
        )
    await postgres_db.delete_skill(skill_id)
    return {"deleted": True}


@app.post("/api/skills/{skill_id}/duplicate")
async def duplicate_skill(request: Request, skill_id: str) -> dict[str, Any]:
    """Fork any visible skill (bundled or DB) into an owned copy."""
    _require_skills_db()
    user = await require_approved_user(request, postgres_db)
    if _looks_like_uuid(skill_id):
        row = await postgres_db.get_skill_by_id(skill_id)
        if not row:
            raise HTTPException(status_code=404, detail="Skill not found")
        src = {
            **_skill_row_to_meta(row),
            "files": await postgres_db.get_skill_files(skill_id),
        }
    else:
        src = _bundled_skill_bundle(skill_id)
        if not src:
            raise HTTPException(status_code=404, detail="Skill not found")
    return await _create_forked_skill(src, str(user["id"]))


@app.get("/api/skills/{skill_id}/export")
async def export_skill(request: Request, skill_id: str) -> Response:
    """Serialize a skill to a native zipped directory (drops into .claude/skills)."""
    from src.core.skill_format import pack_skill_zip

    _require_skills_db()
    await require_approved_user(request, postgres_db)
    if _looks_like_uuid(skill_id):
        row = await postgres_db.get_skill_by_id(skill_id)
        if not row:
            raise HTTPException(status_code=404, detail="Skill not found")
        name, files = row["name"], await postgres_db.get_skill_files(skill_id)
    else:
        bundle = _bundled_skill_bundle(skill_id)
        if not bundle:
            raise HTTPException(status_code=404, detail="Skill not found")
        name, files = bundle["name"], bundle["files"]
    return Response(
        content=pack_skill_zip(name, files),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{name}.zip"'},
    )


@app.post("/api/skills/import")
async def import_skill(
    request: Request, file: UploadFile = File(...)
) -> dict[str, Any]:
    """Create an owned skill from an uploaded skill zip (fork-on-name-collision)."""
    from src.core.skill_format import SkillFormatError, unpack_skill_zip

    _require_skills_db()
    user = await require_approved_user(request, postgres_db)
    try:
        files = unpack_skill_zip(await file.read())
    except SkillFormatError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    name, description, files = _parse_skill_bundle(files)
    src = {
        "name": name,
        "display_name": name.replace("-", " ").title(),
        "description": description,
        "files": files,
    }
    return await _create_forked_skill(
        src, str(user["id"]), suffix="import", prefer_original=True
    )


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
async def list_project_experts(
    request: Request, project_id: str
) -> list[dict[str, Any]]:
    """List expert configurations from a project's jobs repo.

    Scans the experts/ directory in the project's Gitea jobs repo and returns
    metadata for each expert configuration found.
    """
    await require_project_member(request, postgres_db, project_id)
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
    request: Request, project_id: str, expert_name: str
) -> dict[str, Any]:
    """Get full detail for a project expert including merged config and instructions."""
    await require_project_member(request, postgres_db, project_id)
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
        raise HTTPException(status_code=404, detail=f"Expert not found: {expert_name}")

    try:
        expert_data = yaml.safe_load(config_content) or {}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Invalid YAML: {e}") from e

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

    # Load the raw settings_matrix for the client to resolve per-model defaults.
    # Do NOT apply it to merged — the client resolves based on the user's model selection.
    raw_matrix = _load_settings_matrix(config_dir)

    # Read instructions
    instructions_content = await gitea_client.get_file_content(
        repo_name, f"experts/{expert_name}/instructions.md"
    )

    return {
        **info.model_dump(),
        "config": merged,
        "instructions": instructions_content,
        "settings_matrix": raw_matrix,
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
        "default_project_id": str(user["default_project_id"])
        if user.get("default_project_id")
        else None,
        "is_admin": user.get("is_admin", False),
        "is_approved": user.get("is_approved", False),
        "can_use_vm": bool(user.get("can_use_vm", False)),
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


# nosec: public auth-bootstrap (Bearer-required, intentionally serves pending-approval users)
@app.get("/api/auth/me")
async def auth_me(request: Request) -> dict[str, Any]:
    """Get current user from Bearer token (OIDC).

    Always returns the user record (even if not yet approved) so the cockpit
    can display a "pending approval" message instead of a blank screen.
    """
    user = await get_current_user(request, postgres_db)
    return {"user": _user_dict(user)}


# =============================================================================
# MCP Token Endpoints
# =============================================================================

# Note: ``MCP_INTERNAL_KEY`` is read in ``security/access.py`` (helpers
# ``require_internal`` / ``is_internal_call``) and used by every Track B
# (P4b) endpoint. This module-level constant is no longer needed here.


@app.post("/api/mcp-tokens")
async def create_mcp_token(request: Request, body: McpTokenCreate) -> dict[str, Any]:
    """Generate a new MCP API token. Returns the plaintext token once."""
    user = await require_approved_user(request, postgres_db)

    # Validate scope
    scope = body.scope.strip()
    if scope not in ("user", "all") and not scope.startswith("project:"):
        raise HTTPException(
            status_code=400,
            detail="Invalid scope. Use 'user', 'all', or 'project:<uuid>'",
        )
    if scope == "all" and not user.get("real_is_admin", False):
        raise HTTPException(
            status_code=403, detail="Only admins can create full-access tokens"
        )
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

    result = {
        k: str(v) if isinstance(v, (UUID, datetime)) else v for k, v in row.items()
    }
    result["token"] = token  # Plaintext returned once only
    return result


@app.get("/api/mcp-tokens")
async def list_mcp_tokens(request: Request) -> list[dict[str, Any]]:
    """List the current user's MCP tokens (no plaintext or hashes)."""
    user = await require_approved_user(request, postgres_db)
    rows = await postgres_db.list_mcp_tokens(str(user["id"]))
    return [
        {k: str(v) if isinstance(v, (UUID, datetime)) else v for k, v in r.items()}
        for r in rows
    ]


@app.delete("/api/mcp-tokens/{token_id}")
async def revoke_mcp_token(request: Request, token_id: str) -> dict[str, str]:
    """Revoke an MCP token (soft delete)."""
    user = await require_approved_user(request, postgres_db)
    revoked = await postgres_db.revoke_mcp_token(token_id, str(user["id"]))
    if not revoked:
        raise HTTPException(
            status_code=404, detail="Token not found or already revoked"
        )
    return {"status": "revoked"}


@app.post("/api/internal/mcp-token-verify")
async def internal_mcp_token_verify(
    request: Request, body: McpTokenVerifyRequest
) -> dict[str, Any]:
    """Internal endpoint for MCP server to verify a token hash.
    **Internal** (P4b) — requires ``X-Internal-Key``. Ingress strips
    this path.
    """
    await require_internal(request)
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


@app.post("/api/internal/mcp-token-create")
async def internal_mcp_token_create(
    request: Request, body: McpTokenCreateInternal
) -> dict[str, Any]:
    """Internal endpoint for OAuth bridge to create an srw_* token.
    **Internal** (P4b) — requires ``X-Internal-Key``. Ingress strips
    this path.

    Looks up the user by Keycloak subject (JIT-creates if needed),
    then creates a token with the given hash, scope, and origin.
    """
    await require_internal(request)
    # Look up or JIT-create user by Keycloak sub
    user = await postgres_db.get_user_by_keycloak_sub(body.user_sub)
    if not user:
        # JIT-create via upsert (same as cockpit OIDC login)
        user = await postgres_db.upsert_user_from_oidc(
            sub=body.user_sub,
            email=body.user_email,
            display_name=body.user_email.split("@")[0]
            if body.user_email
            else "OAuth User",
        )
    if not user:
        raise HTTPException(status_code=400, detail="Could not resolve user")

    # Parse expiry
    expires_at = None
    if body.expires_at:
        expires_at = datetime.fromisoformat(body.expires_at)

    row = await postgres_db.create_mcp_token(
        user_id=str(user["id"]),
        name=body.name,
        token_hash=body.token_hash,
        token_prefix=body.token_prefix,
        scope=body.scope,
        expires_at=expires_at,
        origin=body.origin,
    )

    result = {
        k: str(v) if isinstance(v, (UUID, datetime)) else v for k, v in row.items()
    }
    return result


# =============================================================================
# Personal Access Token (PAT) Endpoints — see auth_bff_and_api_tokens.md §3
# =============================================================================
#
# PATs live in the consolidated `auth_tokens` table with kind='api'. The
# legacy MCP-token endpoints (above) keep working unchanged on the same
# table with kind='mcp'. Validator path is shared (see security.auth
# `_resolve_pat`, `_resolve_legacy_mcp_token`).


def _serialize_api_key_row(row: dict[str, Any]) -> dict[str, Any]:
    """Coerce UUID / datetime values to strings so they JSON-serialise."""
    return {k: str(v) if isinstance(v, (UUID, datetime)) else v for k, v in row.items()}


@app.post("/api/api-keys")
async def create_api_key(request: Request, body: ApiKeyCreate) -> dict[str, Any]:
    """Generate a new Personal Access Token. Plaintext returned once."""
    user = await require_approved_user(request, postgres_db)

    # Validate scope set. `admin` is gated on the user's admin flag.
    requested = set(body.scopes)
    if not requested:
        raise HTTPException(status_code=400, detail="At least one scope required")
    bad = requested - VALID_PAT_SCOPES
    if bad:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown scopes: {sorted(bad)}",
        )
    if "admin" in requested and not user.get("real_is_admin", False):
        raise HTTPException(
            status_code=403, detail="Only admins can issue admin-scoped tokens"
        )

    token = "ak_" + secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
    token_prefix = token[:12]
    last_four = token[-4:]

    expires_at = None
    if body.expires_in_days:
        expires_at = datetime.now(timezone.utc) + timedelta(days=body.expires_in_days)

    row = await postgres_db.create_api_key(
        user_id=str(user["id"]),
        name=body.name,
        token_hash=token_hash,
        token_prefix=token_prefix,
        last_four=last_four,
        scopes=sorted(requested),
        expires_at=expires_at,
    )
    result = _serialize_api_key_row(row)
    result["token"] = token  # Plaintext — caller must store immediately
    return result


@app.get("/api/api-keys")
async def list_api_keys(request: Request) -> list[dict[str, Any]]:
    """List the current user's PATs (no hashes, no plaintext)."""
    user = await require_approved_user(request, postgres_db)
    rows = await postgres_db.list_api_keys(str(user["id"]))
    return [_serialize_api_key_row(r) for r in rows]


@app.delete("/api/api-keys/{token_id}")
async def revoke_api_key(request: Request, token_id: str) -> dict[str, str]:
    """Soft-revoke a PAT."""
    user = await require_approved_user(request, postgres_db)
    revoked = await postgres_db.revoke_api_key(token_id, str(user["id"]))
    if not revoked:
        raise HTTPException(
            status_code=404, detail="Token not found or already revoked"
        )
    return {"status": "revoked"}


@app.post("/api/api-keys/{token_id}/rotate")
async def rotate_api_key(request: Request, token_id: str) -> dict[str, Any]:
    """Issue a successor PAT.

    Same name + scopes + expiry as the source token. The old row stays
    valid for 24h (cleanup loop revokes it) so an automation can roll
    over without an outage.
    """
    user = await require_approved_user(request, postgres_db)
    token = "ak_" + secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
    token_prefix = token[:12]
    last_four = token[-4:]

    row = await postgres_db.rotate_api_key(
        old_id=token_id,
        user_id=str(user["id"]),
        token_hash=token_hash,
        token_prefix=token_prefix,
        last_four=last_four,
    )
    if not row:
        raise HTTPException(
            status_code=404,
            detail="Token not found, already revoked, or not yours",
        )
    result = _serialize_api_key_row(row)
    result["token"] = token
    return result


# =============================================================================
# User Settings & API Key Endpoints
# =============================================================================


@app.get("/api/settings/api-keys")
async def list_user_api_keys(request: Request) -> list[dict[str, Any]]:
    """List the current user's API keys (prefix only, no full keys)."""
    user = await require_approved_user(request, postgres_db)
    rows = await postgres_db.list_user_api_keys(str(user["id"]))
    return [
        {k: str(v) if isinstance(v, (UUID, datetime)) else v for k, v in r.items()}
        for r in rows
    ]


@app.put("/api/settings/api-keys/{provider}")
async def set_user_api_key(
    request: Request, provider: str, body: ApiKeySet
) -> dict[str, Any]:
    """Set (create or replace) an API key for a provider."""
    user = await require_approved_user(request, postgres_db)
    if provider not in VALID_API_KEY_PROVIDERS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid provider '{provider}'. Valid: {sorted(VALID_API_KEY_PROVIDERS)}",
        )

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
    user = await require_approved_user(request, postgres_db)
    deleted = await postgres_db.delete_user_api_key(str(user["id"]), provider)
    if not deleted:
        raise HTTPException(
            status_code=404, detail=f"No API key for provider '{provider}'"
        )
    return {"status": "deleted"}


# =============================================================================
# User-defined LLM Endpoints
# OpenAI-compatible endpoints a user has registered (vLLM, Ollama, private
# gateways). Models served by these endpoints appear in every model picker
# and are routed to via dispatcher injection of base_url + api_key.
# =============================================================================


def _validate_llm_endpoint_url(base_url: str, allow_insecure: bool) -> str:
    """Basic URL sanity check. Raises HTTPException(400) on malformed input.

    Rejects non-http(s) schemes (file://, javascript:), empty hosts, and
    http:// URLs unless the caller explicitly opts in via allow_insecure.
    """
    from urllib.parse import urlparse

    try:
        parsed = urlparse(base_url.strip())
    except Exception:
        raise HTTPException(status_code=400, detail="Malformed base_url")

    if parsed.scheme not in ("http", "https"):
        raise HTTPException(
            status_code=400,
            detail=f"base_url scheme must be http or https, got {parsed.scheme!r}",
        )
    if not parsed.netloc:
        raise HTTPException(status_code=400, detail="base_url must include a host")
    if parsed.scheme == "http" and not allow_insecure:
        raise HTTPException(
            status_code=400,
            detail=(
                "base_url uses http:// — set allow_insecure=true to override "
                "(not recommended outside local development)."
            ),
        )
    return parsed.geturl()


def _serialize_endpoint(row: dict[str, Any]) -> dict[str, Any]:
    """Shape an endpoint row for the API response (key_prefix only, no full key).

    The ``models`` key is kept on the response shape for Cockpit
    compatibility but is always empty after the catalog flip — model
    offerings live in the admin-curated ``models`` table now.
    """
    return {
        "id": str(row["id"]),
        "label": row["label"],
        "base_url": row["base_url"],
        "key_prefix": row.get("key_prefix"),
        "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
        "updated_at": row["updated_at"].isoformat() if row.get("updated_at") else None,
        "models": [],
    }


@app.get("/api/settings/llm-endpoints")
async def list_llm_endpoints(request: Request) -> list[dict[str, Any]]:
    """List the user's registered LLM endpoints with their model rows."""
    user = await require_approved_user(request, postgres_db)
    rows = await postgres_db.list_user_llm_endpoints(str(user["id"]))
    return [_serialize_endpoint(r) for r in rows]


@app.post("/api/settings/llm-endpoints")
async def create_llm_endpoint(
    request: Request, body: LlmEndpointCreate
) -> dict[str, Any]:
    user = await require_approved_user(request, postgres_db)
    base_url = _validate_llm_endpoint_url(body.base_url, body.allow_insecure)
    key_prefix = body.api_key[:8] if body.api_key else None
    try:
        row = await postgres_db.create_user_llm_endpoint(
            user_id=str(user["id"]),
            label=body.label,
            base_url=base_url,
            api_key=body.api_key,
            key_prefix=key_prefix,
        )
    except Exception as e:
        # UniqueViolation on (user_id, label) — mirror the 409 style elsewhere
        if "uq_llm_endpoint_label" in str(e):
            raise HTTPException(
                status_code=409,
                detail=f"An endpoint labeled {body.label!r} already exists.",
            )
        raise
    row["models"] = []
    return _serialize_endpoint(row)


@app.patch("/api/settings/llm-endpoints/{endpoint_id}")
async def update_llm_endpoint(
    request: Request, endpoint_id: str, body: LlmEndpointUpdate
) -> dict[str, Any]:
    user = await require_approved_user(request, postgres_db)
    base_url = None
    if body.base_url is not None:
        base_url = _validate_llm_endpoint_url(body.base_url, body.allow_insecure)
    key_prefix = body.api_key[:8] if body.api_key else None

    row = await postgres_db.update_user_llm_endpoint(
        endpoint_id=endpoint_id,
        user_id=str(user["id"]),
        label=body.label,
        base_url=base_url,
        api_key=body.api_key,
        key_prefix=key_prefix,
        clear_api_key=body.clear_api_key and body.api_key is None,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Endpoint not found")
    row["models"] = []
    return _serialize_endpoint(row)


@app.delete("/api/settings/llm-endpoints/{endpoint_id}")
async def delete_llm_endpoint(request: Request, endpoint_id: str) -> dict[str, str]:
    user = await require_approved_user(request, postgres_db)
    deleted = await postgres_db.delete_user_llm_endpoint(
        endpoint_id=endpoint_id, user_id=str(user["id"])
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="Endpoint not found")
    return {"status": "deleted"}


@app.post("/api/settings/llm-endpoints/{endpoint_id}/test")
async def test_llm_endpoint(request: Request, endpoint_id: str) -> dict[str, Any]:
    """Probe an endpoint by calling ``GET {base_url}/models`` server-side.

    Runs from the orchestrator pod (not the browser) so the api_key is
    never exposed to the client. Returns the HTTP status plus the first
    error message if the probe fails.
    """
    user = await require_approved_user(request, postgres_db)
    endpoint = await postgres_db.get_user_llm_endpoint(
        endpoint_id=endpoint_id, user_id=str(user["id"])
    )
    if endpoint is None:
        raise HTTPException(status_code=404, detail="Endpoint not found")

    result = await probe_endpoint_models(
        base_url=endpoint["base_url"],
        api_key=endpoint.get("api_key"),
    )
    return {
        "ok": result.ok,
        "status": result.status,
        "error": result.error,
        "probe_url": result.probe_url,
    }


@app.post("/api/settings/llm-endpoints/{endpoint_id}/discover")
async def discover_llm_endpoint_models(
    request: Request, endpoint_id: str
) -> dict[str, Any]:
    """Return the model list served by ``GET {base_url}/models``.

    Read-only — the user-side surface only exposes discovery for browsing;
    actual catalog authoring is admin-only via Admin → Models.
    """
    user = await require_approved_user(request, postgres_db)
    endpoint = await postgres_db.get_user_llm_endpoint(
        endpoint_id=endpoint_id, user_id=str(user["id"])
    )
    if endpoint is None:
        raise HTTPException(status_code=404, detail="Endpoint not found")

    result = await probe_endpoint_models(
        base_url=endpoint["base_url"],
        api_key=endpoint.get("api_key"),
    )

    return {
        "ok": result.ok,
        "status": result.status,
        "error": result.error,
        "probe_url": result.probe_url,
        "models": result.models,
    }


# =============================================================================
# Admin Provider Endpoints
# System-scoped provider keys, LLM endpoints, and default-model settings.
# Gated by the srw-admin role via _require_admin.
# =============================================================================


def _serialize_system_api_key(row: dict[str, Any]) -> dict[str, Any]:
    """Shape a system_api_keys row for API responses (prefix only)."""
    return {
        "id": str(row["id"]),
        "provider": row["provider"],
        "key_prefix": row.get("key_prefix"),
        "label": row.get("label"),
        "seeded_from": row.get("seeded_from"),
        "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
        "updated_at": row["updated_at"].isoformat() if row.get("updated_at") else None,
    }


@app.get("/api/admin/providers/keys")
async def admin_list_provider_keys(request: Request) -> list[dict[str, Any]]:
    """List system-scoped provider API keys (prefix only, no full keys)."""
    await _require_admin(request)
    rows = await postgres_db.list_system_api_keys()
    return [_serialize_system_api_key(r) for r in rows]


@app.put("/api/admin/providers/keys/{provider}")
async def admin_set_provider_key(
    request: Request, provider: str, body: ApiKeySet
) -> dict[str, Any]:
    """Set or rotate the system-level API key for a provider.

    On success, schedules a non-blocking discovery probe for providers
    we know how to enumerate (see ``discovery_service.DISCOVERABLE_PROVIDERS``).
    The discovery cache is cleared inline before the probe fires so the
    cockpit never shows stale candidates from a previous key. When the
    ``admin.discovery_enabled`` flag is set to ``false``, no probe runs.
    """
    await _require_admin(request)
    if provider not in VALID_SYSTEM_API_KEY_PROVIDERS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid provider '{provider}'. Valid: "
                f"{sorted(VALID_SYSTEM_API_KEY_PROVIDERS)}"
            ),
        )
    row = await postgres_db.upsert_system_api_key(
        provider=provider,
        api_key=body.api_key,
        key_prefix=body.api_key[:8],
        label=body.label,
    )
    await _maybe_schedule_discovery(provider, body.api_key)
    return _serialize_system_api_key(row)


@app.delete("/api/admin/providers/keys/{provider}")
async def admin_delete_provider_key(request: Request, provider: str) -> dict[str, str]:
    """Remove the system-level key for a provider."""
    await _require_admin(request)
    deleted = await postgres_db.delete_system_api_key(provider)
    if not deleted:
        raise HTTPException(
            status_code=404, detail=f"No system key for provider '{provider}'"
        )
    return {"status": "deleted"}


@app.get("/api/admin/providers/keys/{provider}/discovery")
async def admin_get_provider_discovery(
    request: Request, provider: str
) -> dict[str, Any]:
    """Return the cached discovery payload for a provider key.

    Powers the post-save confirmation dialog on Admin → Providers. The
    response is ``{ready, fresh, payload, cached_at}``:

    - ``ready=False`` when no probe has completed yet (e.g. the async
      probe scheduled by the PUT side-effect is still running).
    - ``fresh`` reflects the 24h TTL — the cockpit can prompt for an
      explicit rediscover when stale.
    - ``payload`` is the cockpit-ready candidate list shaped by
      :func:`discovery_service.build_cache_payload`.
    """
    await _require_admin(request)
    if provider not in VALID_SYSTEM_API_KEY_PROVIDERS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid provider '{provider}'. Valid: "
                f"{sorted(VALID_SYSTEM_API_KEY_PROVIDERS)}"
            ),
        )
    cached = await postgres_db.get_system_api_key_discovery_cache(provider)
    if cached is None:
        return {"ready": False, "fresh": False, "payload": None, "cached_at": None}
    cached_at_dt = (
        datetime.fromisoformat(cached["cached_at"]) if cached.get("cached_at") else None
    )
    return {
        "ready": True,
        "fresh": discovery_service.is_discovery_cache_fresh(cached_at_dt),
        "payload": cached.get("payload"),
        "cached_at": cached.get("cached_at"),
    }


@app.post("/api/admin/providers/keys/{provider}/rediscover")
async def admin_rediscover_provider_models(
    request: Request, provider: str
) -> dict[str, Any]:
    """Force-refresh the discovery cache for a provider key.

    Useful when the provider released new models since the cache was
    populated, or when the admin wants to retry after a transient probe
    failure. Returns the freshly-cached payload (synchronous probe — the
    button blocks until results come back, like an explicit "test" click).
    """
    await _require_admin(request)
    if provider not in VALID_SYSTEM_API_KEY_PROVIDERS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid provider '{provider}'. Valid: "
                f"{sorted(VALID_SYSTEM_API_KEY_PROVIDERS)}"
            ),
        )
    if provider not in discovery_service.DISCOVERABLE_PROVIDERS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Provider '{provider}' has no discovery source. "
                "Add models manually via Admin → Models."
            ),
        )
    if not await _discovery_enabled():
        raise HTTPException(
            status_code=409,
            detail=(
                "Auto-discovery is disabled "
                "(admin.discovery_enabled = false in system_settings)."
            ),
        )
    api_key = await postgres_db.get_system_api_key(provider)
    if not api_key:
        raise HTTPException(
            status_code=404, detail=f"No system key for provider '{provider}'"
        )
    candidates = await discovery_service.discover_models(provider, api_key)
    payload = discovery_service.build_cache_payload(provider, candidates)
    await postgres_db.set_system_api_key_discovery_cache(provider, payload)
    return {
        "ready": True,
        "fresh": True,
        "payload": payload,
        "cached_at": payload["fetched_at"],
    }


async def _discovery_enabled() -> bool:
    """Return whether admin auto-discovery is enabled (system setting,
    default True). Operators flip this off when they want catalog growth
    to be a deliberate manual action."""
    row = await postgres_db.get_system_setting("admin.discovery_enabled")
    if row is None:
        return True
    value = row.get("value")
    if isinstance(value, dict):
        return bool(value.get("enabled", True))
    return bool(value)


async def _maybe_schedule_discovery(provider: str, api_key: str) -> None:
    """Clear the cache for a key and fire an async probe if applicable.

    The probe runs as a fire-and-forget task so the PUT route returns as
    quickly as today; the cockpit polls ``GET .../discovery`` to render
    the confirmation dialog when results land. Errors are swallowed by
    ``discover_models`` so a failed probe never blocks key-save.
    """
    if provider not in discovery_service.DISCOVERABLE_PROVIDERS:
        return
    if not await _discovery_enabled():
        return
    await postgres_db.set_system_api_key_discovery_cache(provider, None)

    async def _probe() -> None:
        candidates = await discovery_service.discover_models(provider, api_key)
        payload = discovery_service.build_cache_payload(provider, candidates)
        await postgres_db.set_system_api_key_discovery_cache(provider, payload)

    asyncio.create_task(_probe())


# =============================================================================
# Admin -> Prompts (DB-backed prompt overrides, v1)
# =============================================================================


def load_config_catalog() -> list[dict[str, Any]]:
    """Human-facing descriptions for editable prompt keys.

    Read from config/prompts/catalog.yaml (shipped with the image). Missing
    file -> empty list.
    """
    import yaml

    from src.core.loader import get_project_root

    path = get_project_root() / "config" / "prompts" / "catalog.yaml"
    if not path.exists():
        return []
    return yaml.safe_load(path.read_text()) or []


def _config_catalog_entry(kind: str, name: str) -> dict[str, Any] | None:
    for entry in load_config_catalog():
        if entry.get("kind") == kind and entry.get("name") == name:
            return entry
    return None


def validate_override_value(kind: str, name: str, value: Any) -> None:
    """Validate a structured (settings/guardrails) override value against the
    catalog. Raises HTTPException(422) on unknown key, wrong type, or out-of-bounds.

    Text kinds are validated by the Pydantic model (min_length) and are a no-op
    here. Fail-closed on write; reads stay fail-open (see the loader).
    """
    if kind not in ("settings", "guardrails"):
        return
    entry = _config_catalog_entry(kind, name)
    if entry is None:
        raise HTTPException(status_code=422, detail=f"unknown {kind} key: {name!r}")
    vtype = entry.get("type")
    if vtype in ("number", "integer"):
        # bool is a subclass of int — reject it for numeric leaves.
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise HTTPException(status_code=422, detail=f"{name} must be a {vtype}")
        if vtype == "integer" and isinstance(value, float) and not value.is_integer():
            raise HTTPException(status_code=422, detail=f"{name} must be an integer")
        lo, hi = entry.get("min"), entry.get("max")
        if lo is not None and value < lo:
            raise HTTPException(status_code=422, detail=f"{name} must be >= {lo}")
        if hi is not None and value > hi:
            raise HTTPException(status_code=422, detail=f"{name} must be <= {hi}")
    elif vtype == "boolean":
        if not isinstance(value, bool):
            raise HTTPException(status_code=422, detail=f"{name} must be a boolean")
    elif vtype == "enum":
        choices = entry.get("enum", [])
        if value not in choices:
            raise HTTPException(
                status_code=422, detail=f"{name} must be one of {choices}"
            )
    elif vtype == "json":
        if not isinstance(value, dict):
            raise HTTPException(status_code=422, detail=f"{name} must be a JSON object")
    # Unknown/absent type -> accept (forward-compatible with new catalog types).


def read_bundled_config(kind: str, family: str | None, name: str) -> Any:
    """Read the shipped (bundled) value for (kind, family, name), bypassing overrides.

    Returns text for prompts/instructions; the file-resolved value for settings
    (a single leaf) and guardrails (the {tool_examples, nudges} dict).
    """
    from src.core.loader import (
        InstructionMatrixResolver,
        PromptMatrixResolver,
        bundled_guardrails_for_family,
        bundled_settings_for_family,
    )

    if kind == "settings":
        return bundled_settings_for_family(family or "default", name)
    if kind == "guardrails":
        return bundled_guardrails_for_family(family or "default")

    resolver_cls = {
        "prompts": PromptMatrixResolver,
        "instructions": InstructionMatrixResolver,
    }.get(kind)
    if resolver_cls is None:
        raise HTTPException(status_code=400, detail=f"unknown kind: {kind!r}")
    resolver = resolver_cls(None, family or "default")
    try:
        return resolver.load(name, bundled_only=True)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="no bundled default for that key")


@app.get("/api/admin/config/overrides")
async def admin_list_config_overrides(request: Request) -> list[dict[str, Any]]:
    """List all prompt overrides (system-wide)."""
    await _require_admin(request)
    return await postgres_db.list_config_overrides()


@app.get("/api/admin/config/overrides/{override_id}")
async def admin_get_config_override(
    request: Request, override_id: str
) -> dict[str, Any]:
    """Fetch a single prompt override by id."""
    await _require_admin(request)
    row = await postgres_db.get_config_override(override_id)
    if not row:
        raise HTTPException(status_code=404, detail="override not found")
    return row


@app.post("/api/admin/config/overrides")
async def admin_create_config_override(
    request: Request, body: ConfigOverrideCreate
) -> dict[str, Any]:
    """Create or replace the override for (family, kind, name)."""
    user = await _require_admin(request)
    is_text = body.kind in ("prompts", "instructions")
    if not is_text:
        validate_override_value(body.kind, body.name, body.value_json)
    return await postgres_db.upsert_config_override(
        family=body.family,
        kind=body.kind,
        name=body.name,
        content=body.content,
        content_format=body.content_format if is_text else None,
        value_json=body.value_json,
        notes=body.notes,
        user_id=user.get("id"),
    )


@app.put("/api/admin/config/overrides/{override_id}")
async def admin_update_config_override(
    request: Request, override_id: str, body: ConfigOverrideUpdate
) -> dict[str, Any]:
    """Update an existing override's payload (family/kind/name are immutable)."""
    user = await _require_admin(request)
    existing = await postgres_db.get_config_override(override_id)
    if not existing:
        raise HTTPException(status_code=404, detail="override not found")
    kind = existing["kind"]
    if kind in ("prompts", "instructions"):
        if body.content is None:
            raise HTTPException(
                status_code=422, detail="content is required for this kind"
            )
        content, content_format, value_json = body.content, body.content_format, None
    else:  # settings, guardrails
        if body.value_json is None:
            raise HTTPException(
                status_code=422, detail="value_json is required for this kind"
            )
        validate_override_value(kind, existing["name"], body.value_json)
        content, content_format, value_json = None, None, body.value_json
    return await postgres_db.upsert_config_override(
        family=existing["family"],
        kind=kind,
        name=existing["name"],
        content=content,
        content_format=content_format,
        value_json=value_json,
        notes=body.notes,
        user_id=user.get("id"),
    )


@app.delete("/api/admin/config/overrides/{override_id}")
async def admin_delete_config_override(
    request: Request, override_id: str
) -> dict[str, Any]:
    """Delete an override (reset to the bundled default)."""
    await _require_admin(request)
    if not await postgres_db.delete_config_override(override_id):
        raise HTTPException(status_code=404, detail="override not found")
    return {"deleted": True}


@app.get("/api/admin/config/catalog")
async def admin_config_catalog(request: Request) -> list[dict[str, Any]]:
    """List the editable prompt keys with human descriptions."""
    await _require_admin(request)
    return load_config_catalog()


@app.get("/api/admin/config/bundled/{family}/{kind}/{name}")
async def admin_get_bundled_config(
    request: Request, family: str, kind: str, name: str
) -> dict[str, Any]:
    """Return the bundled (shipped) default for a key, plus its catalog entry.

    ``family='_'`` stands in for the global/default family.
    """
    await _require_admin(request)
    fam = None if family == "_" else family
    return {
        "family": fam,
        "kind": kind,
        "name": name,
        "content": read_bundled_config(kind, fam, name),
        "catalog": _config_catalog_entry(kind, name),
    }


@app.get("/api/admin/providers/endpoints")
async def admin_list_provider_endpoints(request: Request) -> list[dict[str, Any]]:
    """List system-scoped LLM endpoints with their models."""
    await _require_admin(request)
    rows = await postgres_db.list_system_llm_endpoints()
    return [_serialize_endpoint(r) for r in rows]


@app.post("/api/admin/providers/endpoints")
async def admin_create_provider_endpoint(
    request: Request, body: LlmEndpointCreate
) -> dict[str, Any]:
    """Create a new system-scoped LLM endpoint (visible to every user)."""
    await _require_admin(request)
    base_url = _validate_llm_endpoint_url(body.base_url, body.allow_insecure)
    key_prefix = body.api_key[:8] if body.api_key else None
    try:
        row = await postgres_db.create_system_llm_endpoint(
            label=body.label,
            base_url=base_url,
            api_key=body.api_key,
            key_prefix=key_prefix,
        )
    except Exception as e:
        if "uq_llm_endpoint_label_system" in str(e):
            raise HTTPException(
                status_code=409,
                detail=f"A system endpoint labeled {body.label!r} already exists.",
            )
        raise
    row["models"] = []
    return _serialize_endpoint(row)


@app.patch("/api/admin/providers/endpoints/{endpoint_id}")
async def admin_update_provider_endpoint(
    request: Request, endpoint_id: str, body: LlmEndpointUpdate
) -> dict[str, Any]:
    await _require_admin(request)
    base_url = None
    if body.base_url is not None:
        base_url = _validate_llm_endpoint_url(body.base_url, body.allow_insecure)
    key_prefix = body.api_key[:8] if body.api_key else None

    row = await postgres_db.update_system_llm_endpoint(
        endpoint_id=endpoint_id,
        label=body.label,
        base_url=base_url,
        api_key=body.api_key,
        key_prefix=key_prefix,
        clear_api_key=body.clear_api_key and body.api_key is None,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="System endpoint not found")
    row["models"] = []
    return _serialize_endpoint(row)


@app.delete("/api/admin/providers/endpoints/{endpoint_id}")
async def admin_delete_provider_endpoint(
    request: Request, endpoint_id: str
) -> dict[str, str]:
    await _require_admin(request)
    deleted = await postgres_db.delete_system_llm_endpoint(endpoint_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="System endpoint not found")
    return {"status": "deleted"}


@app.post("/api/admin/providers/endpoints/{endpoint_id}/test")
async def admin_test_provider_endpoint(
    request: Request, endpoint_id: str
) -> dict[str, Any]:
    """Probe a system endpoint by calling ``GET {base_url}/models`` server-side."""
    await _require_admin(request)
    endpoint = await postgres_db.get_system_llm_endpoint(endpoint_id)
    if endpoint is None:
        raise HTTPException(status_code=404, detail="System endpoint not found")

    result = await probe_endpoint_models(
        base_url=endpoint["base_url"],
        api_key=endpoint.get("api_key"),
    )
    return {
        "ok": result.ok,
        "status": result.status,
        "error": result.error,
        "probe_url": result.probe_url,
    }


@app.post("/api/admin/providers/endpoints/{endpoint_id}/discover")
async def admin_discover_provider_endpoint_models(
    request: Request, endpoint_id: str
) -> dict[str, Any]:
    """Return the model list served by ``GET {base_url}/models`` (admin).

    Discovery is read-only — admins author catalog rows via Admin → Models
    using the endpoint as the transport reference. The legacy
    ``already_registered`` field is gone (the endpoint itself no longer
    owns model rows after the catalog flip).
    """
    await _require_admin(request)
    endpoint = await postgres_db.get_system_llm_endpoint(endpoint_id)
    if endpoint is None:
        raise HTTPException(status_code=404, detail="System endpoint not found")

    result = await probe_endpoint_models(
        base_url=endpoint["base_url"],
        api_key=endpoint.get("api_key"),
    )

    return {
        "ok": result.ok,
        "status": result.status,
        "error": result.error,
        "probe_url": result.probe_url,
        "models": result.models,
    }


@app.get("/api/admin/providers/codex/availability")
async def admin_codex_availability(request: Request) -> dict[str, Any]:
    """Report whether the codex proxy is configured AND has an active account.

    Used by Admin → Models to decide whether to surface "Codex proxy" as a
    model source. ``available`` is true only when the proxy is reachable
    AND at least one auth file is active (not disabled, not unavailable).
    Catalog wiring is deliberate: when ``available`` is true and
    ``endpoint_id`` is set, the frontend can route Discover/Add flows to
    the existing /api/admin/providers/endpoints/{id} routes — no codex-
    specific UI plumbing required.
    """
    await _require_admin(request)
    # Same fallback as _codex_proxy_request and ensure_codex_proxy_endpoint —
    # a local stack with CODEX_PROXY_URL unset is a fully supported config.
    proxy_url = os.getenv("CODEX_PROXY_URL", "http://localhost:8317")

    # Locate the seeded codex-proxy endpoint (init.py creates it when
    # CODEX_PROXY_URL is set; admins may also have deleted it).
    endpoint_id: str | None = None
    for row in await postgres_db.list_system_llm_endpoints():
        if row.get("label") == CODEX_PROXY_ENDPOINT_LABEL:
            endpoint_id = str(row["id"])
            break

    try:
        auth_resp = await _codex_proxy_request("GET", "/v0/management/auth-files")
        auth_files = auth_resp.json()
    except HTTPException:
        return {
            "available": False,
            "account_count": 0,
            "models": [],
            "proxy_url": proxy_url,
            "endpoint_id": endpoint_id,
        }

    accounts = (
        auth_files if isinstance(auth_files, list) else auth_files.get("files", [])
    )
    active = [a for a in accounts if not a.get("disabled") and not a.get("unavailable")]

    models: list[str] = []
    if active:
        try:
            models_resp = await _codex_proxy_request("GET", "/v1/models")
            data = models_resp.json()
            models = [
                m["id"] if isinstance(m, dict) else m for m in data.get("data", [])
            ]
        except HTTPException:
            pass

    # Self-heal: a live subscription with no transport row means a CLI login
    # (or a previously-deleted row) bypassed the cockpit's wire-up path.
    # Create the row now so the next Admin → Models render lists the proxy.
    if endpoint_id is None and len(active) > 0:
        await ensure_codex_proxy_endpoint(postgres_db, proxy_url=proxy_url)
        for row in await postgres_db.list_system_llm_endpoints():
            if row.get("label") == CODEX_PROXY_ENDPOINT_LABEL:
                endpoint_id = str(row["id"])
                break

    return {
        "available": len(active) > 0,
        "account_count": len(active),
        "models": models,
        "proxy_url": proxy_url,
        "endpoint_id": endpoint_id,
    }


@app.get("/api/admin/providers/defaults")
async def admin_list_provider_defaults(request: Request) -> dict[str, str | None]:
    """Return the currently-configured default model IDs for each workload kind."""
    await _require_admin(request)
    return {
        kind: await postgres_db.get_default_llm_model(kind)
        for kind in sorted(VALID_DEFAULT_MODEL_KINDS)
    }


@app.put("/api/admin/providers/defaults/{kind}")
async def admin_set_provider_default(
    request: Request, kind: str, body: AdminDefaultModelSet
) -> dict[str, str | None]:
    """Set or clear (empty string) the default model for a workload kind."""
    admin = await _require_admin(request)
    if kind not in VALID_DEFAULT_MODEL_KINDS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid kind '{kind}'. Valid: {sorted(VALID_DEFAULT_MODEL_KINDS)}"
            ),
        )
    await postgres_db.set_default_llm_model(
        kind, body.model or None, updated_by=str(admin.get("id"))
    )
    return {"kind": kind, "model": await postgres_db.get_default_llm_model(kind)}


# =============================================================================
# Admin Models Catalog API (Admin → Models)
# Reads/writes the ``models`` table — the curated list of LLMs the application
# offers. Each row is provider-anchored (system_api_keys or system-scoped
# llm_endpoints). User authoring is not exposed.
# =============================================================================


def _serialize_catalog_model(row: dict[str, Any]) -> dict[str, Any]:
    """Shape a ``models`` row for API responses.

    Single-column source of truth: only the ``capabilities`` array is on
    the wire. Cockpit clients have been migrated to the array form.
    """
    return {
        "id": str(row["id"]),
        "provider_kind": row["provider_kind"],
        "provider_ref": row["provider_ref"],
        "model_id": row["model_id"],
        "display_label": row["display_label"],
        "capabilities": list(row.get("capabilities") or []),
        "family": row["family"],
        "context_window": row.get("context_window"),
        "reasoning_level": row.get("reasoning_level"),
        "params_json": row.get("params_json"),
        "enabled": row.get("enabled", True),
        "seeded_from": row.get("seeded_from"),
        "notes": row.get("notes"),
        "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
        "updated_at": row["updated_at"].isoformat() if row.get("updated_at") else None,
    }


async def _validate_catalog_provider_ref(provider_kind: str, provider_ref: str) -> None:
    """Reject catalog inserts/updates pointing at a transport that doesn't
    exist. Keeps the catalog from referencing stale rows after the admin
    deletes a provider key or system endpoint.
    """
    if provider_kind == "system":
        if provider_ref not in VALID_SYSTEM_API_KEY_PROVIDERS:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Invalid system provider '{provider_ref}'. Valid: "
                    f"{sorted(VALID_SYSTEM_API_KEY_PROVIDERS)}"
                ),
            )
        existing = await postgres_db.get_system_api_key(provider_ref)
        if not existing:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"No system_api_keys row for provider '{provider_ref}'. "
                    "Configure via Admin → Providers first."
                ),
            )
    elif provider_kind == "endpoint":
        endpoint = await postgres_db.get_system_llm_endpoint(provider_ref)
        if endpoint is None:
            raise HTTPException(
                status_code=400,
                detail=f"No system endpoint with id '{provider_ref}'.",
            )
    else:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid provider_kind '{provider_kind}'. Valid: "
                f"{list(VALID_CATALOG_PROVIDER_KINDS)}"
            ),
        )


def _normalize_catalog_model_id(
    provider_kind: str, provider_ref: str, model_id: str
) -> str:
    """Prepend the ``openrouter/`` routing prefix for system-anchored
    OpenRouter rows.

    OpenRouter routing in the agent keys off the ``openrouter/`` model-ID
    prefix: ``_create_openrouter_llm`` strips it back to the gateway slug and
    targets ``openrouter.ai``. A system-anchored OpenRouter row whose ID lacks
    the prefix routes to the OpenAI factory default (``api.openai.com``) and
    rejects the ``sk-or-v1`` key. Mirrors the seed convention
    (``db_backed_model_catalog.md``) and ``discovery.py``'s auto-prepend.
    No-op for endpoint rows (routed by their inline base_url) and any
    non-OpenRouter provider.
    """
    if (
        provider_kind == "system"
        and provider_ref == "openrouter"
        and not model_id.lower().startswith("openrouter/")
    ):
        return f"openrouter/{model_id}"
    return model_id


@app.get("/api/admin/providers/models")
async def admin_list_catalog_models(
    request: Request,
    capability: str | None = None,
    provider_kind: str | None = None,
    provider_ref: str | None = None,
    enabled_only: bool = False,
) -> list[dict[str, Any]]:
    """List catalog rows with optional filters.

    The ``capability`` query param narrows by membership — a row matches
    iff its ``capabilities[]`` contains the requested value. Returns full
    row shape.
    """
    await _require_admin(request)
    if capability is not None and capability not in VALID_CATALOG_CAPABILITIES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid capability '{capability}'. "
                f"Valid: {list(VALID_CATALOG_CAPABILITIES)}"
            ),
        )
    capability_filter = [capability] if capability else None
    rows = await postgres_db.list_models(
        capabilities=capability_filter,
        provider_kind=provider_kind,
        provider_ref=provider_ref,
        enabled_only=enabled_only,
    )
    return [_serialize_catalog_model(r) for r in rows]


@app.post("/api/admin/providers/models")
async def admin_create_catalog_model(
    request: Request, body: CatalogModelCreate
) -> dict[str, Any]:
    """Insert a new catalog row.

    Validates that ``provider_ref`` resolves to an existing transport before
    insert. Returns the created row; raises 409 on
    ``(provider_kind, provider_ref, model_id, capability)`` collision.
    """
    await _require_admin(request)
    await _validate_catalog_provider_ref(body.provider_kind, body.provider_ref)
    model_id = _normalize_catalog_model_id(
        body.provider_kind, body.provider_ref, body.model_id
    )
    try:
        row = await postgres_db.create_model(
            provider_kind=body.provider_kind,
            provider_ref=body.provider_ref,
            model_id=model_id,
            display_label=body.display_label,
            capabilities=body.capabilities,
            family=body.family,
            context_window=body.context_window,
            reasoning_level=body.reasoning_level,
            params_json=body.params_json,
            enabled=body.enabled,
            notes=body.notes,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        if "uq_model_provider" in str(e):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Catalog row for ({body.provider_kind}/{body.provider_ref}, "
                    f"{model_id}) already exists."
                ),
            )
        raise
    if row is None:
        raise HTTPException(status_code=500, detail="Catalog insert returned no row.")
    return _serialize_catalog_model(row)


@app.patch("/api/admin/providers/models/{catalog_id}")
async def admin_update_catalog_model(
    request: Request, catalog_id: str, body: CatalogModelUpdate
) -> dict[str, Any]:
    """Patch a catalog row. Only fields present in the body are written.

    Pass ``null`` to clear an optional column. The validator re-checks the
    transport when ``provider_kind`` or ``provider_ref`` changes.
    """
    await _require_admin(request)
    fields = body.model_dump(exclude_unset=True)
    existing = None
    if "provider_kind" in fields or "provider_ref" in fields:
        existing = await postgres_db.get_model(catalog_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="Catalog row not found")
        new_kind = fields.get("provider_kind", existing["provider_kind"])
        new_ref = fields.get("provider_ref", existing["provider_ref"])
        await _validate_catalog_provider_ref(new_kind, new_ref)
    # Apply the same openrouter/ prefix normalization as create when the
    # model_id is being (re)written. The effective provider_kind/ref may come
    # from this patch or fall back to the existing row.
    if "model_id" in fields:
        if existing is None:
            existing = await postgres_db.get_model(catalog_id)
            if existing is None:
                raise HTTPException(status_code=404, detail="Catalog row not found")
        fields["model_id"] = _normalize_catalog_model_id(
            fields.get("provider_kind", existing["provider_kind"]),
            fields.get("provider_ref", existing["provider_ref"]),
            fields["model_id"],
        )
    try:
        row = await postgres_db.update_model(catalog_id, **fields)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        if "uq_model_provider" in str(e):
            raise HTTPException(
                status_code=409,
                detail="Update would collide with an existing catalog row.",
            )
        raise
    if row is None:
        raise HTTPException(status_code=404, detail="Catalog row not found")
    return _serialize_catalog_model(row)


@app.delete("/api/admin/providers/models/{catalog_id}")
async def admin_delete_catalog_model(
    request: Request, catalog_id: str
) -> dict[str, Any]:
    """Hard-delete a catalog row. Returns a warning when the row's model_id
    is currently referenced by a ``default_llm_models`` pointer (the pin
    becomes a dangling reference; the resolver's first-enabled-alphabetical
    fallback handles it gracefully).
    """
    await _require_admin(request)
    existing = await postgres_db.get_model(catalog_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Catalog row not found")

    referencing_kinds: list[str] = []
    for kind in sorted(VALID_DEFAULT_MODEL_KINDS):
        pin = await postgres_db.get_default_llm_model(kind)
        if pin == existing["model_id"]:
            referencing_kinds.append(kind)

    deleted = await postgres_db.delete_model(catalog_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Catalog row not found")
    return {
        "status": "deleted",
        "id": catalog_id,
        "warning": (
            f"Default-model pin(s) referenced this row: {referencing_kinds}. "
            "Resolver falls back to first-enabled-alphabetical until repinned."
            if referencing_kinds
            else None
        ),
    }


@app.post("/api/admin/providers/models/{catalog_id}/test")
async def admin_test_catalog_model(request: Request, catalog_id: str) -> dict[str, Any]:
    """Probe a catalog row's transport.

    For ``provider_kind='endpoint'``, calls ``GET {endpoint.base_url}/models``
    via the existing endpoint-probe helper. For ``provider_kind='system'``,
    confirms the ``system_api_keys`` row exists and returns ``ok=True``
    without round-tripping the provider — vendor-specific health probes
    are out of scope for v1.
    """
    await _require_admin(request)
    row = await postgres_db.get_model(catalog_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Catalog row not found")

    if row["provider_kind"] == "endpoint":
        endpoint = await postgres_db.get_system_llm_endpoint(row["provider_ref"])
        if endpoint is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Catalog row references missing endpoint "
                    f"'{row['provider_ref']}' — repoint or delete."
                ),
            )
        result = await probe_endpoint_models(
            base_url=endpoint["base_url"], api_key=endpoint.get("api_key")
        )
        return {
            "ok": result.ok,
            "status": result.status,
            "error": result.error,
            "probe_url": result.probe_url,
        }

    key = await postgres_db.get_system_api_key(row["provider_ref"])
    if not key:
        return {
            "ok": False,
            "status": None,
            "error": (f"No system_api_keys row for provider '{row['provider_ref']}'."),
            "probe_url": None,
        }
    return {"ok": True, "status": 200, "error": None, "probe_url": None}


@app.get("/api/admin/families")
async def admin_list_families(request: Request) -> dict[str, list[str]]:
    """Return the family keys defined in ``model_config_matrix.yaml``.

    Powers the family dropdown on the *Admin → Models* form so adding a
    family in the YAML doesn't require a frontend rebuild.
    """
    await _require_admin(request)
    matrix = _load_settings_matrix(_get_config_dir())
    families = sorted(k for k in matrix.keys() if isinstance(k, str))
    return {"families": families}


@app.get("/api/admin/families/detect")
async def admin_detect_family(request: Request, model_id: str) -> dict[str, str]:
    """Suggest a family for ``model_id`` via the regex matcher.

    Pre-fills the family dropdown on the *Admin → Models* add form and the
    discovery confirmation dialog so admins don't have to memorize the
    mapping. ``source`` is ``"matched"`` for a regex hit and ``"fallback"``
    when no rule matched (the result is ``default`` — works, but quality is
    on the model). Admin can override before saving either way.
    """
    await _require_admin(request)
    if not model_id or not model_id.strip():
        raise HTTPException(status_code=400, detail="model_id is required")
    detection = family_matcher.detect_family(model_id.strip())
    return {
        "family": detection.family,
        "source": detection.source,
    }


# nosec: public auth-bootstrap (Bearer-required, intentionally pre-approval — onboarding first paint)
@app.get("/api/system/readiness")
async def system_readiness(request: Request) -> dict[str, Any]:
    """Return the cockpit-facing readiness signal.

    Authenticated, but not admin-gated — the onboarding screen calls this
    on first paint. Auth-required because the response leaks details
    about whether catalog rows exist (a low-stakes leak, but still
    user-scoped). See ``readiness_service.compute_readiness`` for the
    payload shape.
    """
    await get_current_user(request, postgres_db)
    return await readiness_service.compute_readiness(postgres_db)


async def _enforce_readiness_gate() -> None:
    """Raise 503 when the LLM stack isn't ready.

    Called from ``POST /api/jobs`` and ``POST /api/persistent/threads``
    so dispatch hard-fails rather than silently routing to a chat model
    that doesn't exist. The error body carries the same ``missing_*``
    fields the cockpit reads from ``/api/system/readiness`` so the UI
    can deep-link to the right admin page from either source.
    """
    readiness = await readiness_service.compute_readiness(postgres_db)
    if readiness.get("ready"):
        return
    raise HTTPException(
        status_code=503,
        detail=readiness_service.gate_error_detail(readiness),
    )


def _resolve_preference_defaults() -> dict[str, Any]:
    """Compute resolved default values for all user preference fields.

    Reads framework defaults from defaults.yaml / persistent_defaults.yaml
    and env-var defaults for helper models. This lets the UI show the actual
    effective value instead of "Not set" / "Server default".
    """
    config_dir = _get_config_dir()

    # Worker defaults (defaults.yaml)
    defaults_path = config_dir / "defaults.yaml"
    if defaults_path.exists():
        with open(defaults_path) as f:
            worker_cfg = yaml.safe_load(f) or {}
    else:
        worker_cfg = {}

    # Persistent defaults (persistent_defaults.yaml)
    persistent_path = config_dir / "persistent_defaults.yaml"
    if persistent_path.exists():
        with open(persistent_path) as f:
            persistent_cfg = yaml.safe_load(f) or {}
    else:
        persistent_cfg = {}

    llm = worker_cfg.get("llm", {})
    aux = worker_cfg.get("auxiliary", {})
    p_llm = persistent_cfg.get("llm", {})

    return {
        "default_model": llm.get("model"),
        "default_autonomy": worker_cfg.get("autonomy"),
        "default_reasoning_level": llm.get("reasoning_level"),
        "default_auxiliary_model": aux.get("model") or llm.get("model"),
        # Helper-model defaults match the env-var fallbacks in the agent code
        # (src/services/vision_helper.py, audio_helper.py, embedding_service.py)
        "default_vision_model": os.environ.get("VISION_MODEL", "gpt-4o"),
        "default_whisper_model": os.environ.get("WHISPER_MODEL", "whisper-1"),
        "default_tts_model": os.environ.get("TTS_MODEL", "tts-1"),
        "default_embedding_model": os.environ.get(
            "EMBEDDING_MODEL", "qwen3-embedding-8b"
        ),
        "embedding_provider": os.environ.get("EMBEDDING_PROVIDER", "local"),
        # Admin "View as" default — fleet-wide visibility unless the admin
        # has explicitly narrowed to their own data.
        "admin_view_mode": "all",
        "persistent_agent": {
            "model": p_llm.get("model"),
            "permission_mode": "supervised",
            "idle_timeout_minutes": 30,
            "config_name": "",
        },
    }


@app.get("/api/settings/preferences")
async def get_user_preferences(request: Request) -> dict[str, Any]:
    """Get the current user's preference settings.

    The response includes a ``_resolved`` key containing the effective
    default for every preference field (derived from framework YAML configs
    and environment variables). The UI uses this to display the actual
    value behind "Server default" / "Not set".
    """
    user = await require_approved_user(request, postgres_db)
    prefs = await postgres_db.get_user_settings(str(user["id"]))
    prefs["_resolved"] = _resolve_preference_defaults()
    return prefs


@app.patch("/api/settings/preferences")
async def update_user_preferences(
    request: Request, body: UserSettingsUpdate
) -> dict[str, str]:
    """Update the current user's preference settings (patch-merge)."""
    user = await require_approved_user(request, postgres_db)
    settings = {
        k: v
        for k, v in body.model_dump().items()
        if v is not None or k in body.model_fields_set
    }
    if not settings:
        raise HTTPException(status_code=400, detail="No settings provided")
    await postgres_db.update_user_settings(str(user["id"]), settings)
    return {"status": "updated"}


# =============================================================================
# Project API Key Endpoints
# =============================================================================


@app.get("/api/projects/{project_id}/api-keys")
async def list_project_api_keys(
    request: Request, project_id: str
) -> list[dict[str, Any]]:
    """List a project's API keys (prefix only). Requires project membership."""
    user = await require_approved_user(request, postgres_db)
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
    user = await require_approved_user(request, postgres_db)
    if provider not in VALID_API_KEY_PROVIDERS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid provider '{provider}'. Valid: {sorted(VALID_API_KEY_PROVIDERS)}",
        )

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
    user = await require_approved_user(request, postgres_db)
    members = await postgres_db.get_project_members(project_id)
    member = next((m for m in members if str(m["user_id"]) == str(user["id"])), None)
    if not member or member["role"] not in ("owner", "editor"):
        raise HTTPException(status_code=403, detail="Requires owner or editor role")

    deleted = await postgres_db.delete_project_api_key(project_id, provider)
    if not deleted:
        raise HTTPException(
            status_code=404, detail=f"No API key for provider '{provider}'"
        )
    return {"status": "deleted"}


# =============================================================================
# Global Models API
# =============================================================================

# Provider → env var mapping for system-level key detection
_PROVIDER_ENV_KEYS: dict[str, list[str]] = {
    "openai": ["OPENAI_API_KEY"],
    "anthropic": ["ANTHROPIC_API_KEY"],
    "google": ["GOOGLE_API_KEY"],
    "groq": ["GROQ_API_KEY"],
    "openrouter": ["OPENROUTER_API_KEY"],
    "mistral": ["MISTRAL_API_KEY"],
    "codex": ["CODEX_API_KEY"],
}


def _get_system_providers() -> set[str]:
    """Detect which providers have API keys set via environment variables."""
    providers: set[str] = set()
    for provider, env_vars in _PROVIDER_ENV_KEYS.items():
        if any(os.getenv(var) for var in env_vars):
            providers.add(provider)
    return providers


# Well-known label for the seeded codex-proxy llm_endpoints row. Kept in sync
# with orchestrator.init._seed_codex_proxy_endpoint — duplicated here to avoid
# importing init at runtime.
CODEX_PROXY_ENDPOINT_LABEL = "codex-proxy"


async def _get_codex_subscription_models() -> set[str]:
    """Fetch model IDs available through the Codex proxy subscription.

    Returns an empty set if the proxy is unreachable or has no active account.
    Uses a short timeout to avoid slowing down /api/models.
    """
    proxy_url = os.getenv("CODEX_PROXY_URL", "http://localhost:8317")
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(f"{proxy_url}/v1/models")
        if resp.status_code == 200:
            data = resp.json()
            return {m["id"] if isinstance(m, dict) else m for m in data.get("data", [])}
    except Exception:
        pass
    return set()


@app.get("/api/models")
async def list_available_models(
    request: Request,
    project_id: str | None = None,
) -> dict[str, Any]:
    """List all models from the admin-curated catalog.

    Returns catalog rows grouped by provider/capability:

    - ``groups`` (chat-capability rows, grouped by provider)
    - ``auxiliary_models`` / ``vision_models`` / ``embedding_models`` /
      ``whisper_models`` / ``tts_models`` (one helper list per capability)

    Every row carries ``configured: true`` because the catalog only
    contains rows whose transport (system_api_keys row or system endpoint)
    is admin-managed. The legacy strategic+tactical preset bundle was
    removed in chunk 7 of the models_yaml_removal work — the job-create
    UX picks strategic and tactical models individually now.

    Query params:
        project_id: kept for backward compatibility — no longer affects the
            response shape now that the catalog is the source of truth.
    """
    await require_approved_user(request, postgres_db)
    _ = project_id  # accepted but unused post-flip

    # Catalog rows joined to transport (only enabled rows surface).
    catalog_rows = await postgres_db.list_models(enabled_only=True)
    system_endpoints = await postgres_db.list_system_llm_endpoints()
    endpoint_label_by_id: dict[str, str] = {
        str(e["id"]): e["label"] for e in system_endpoints
    }

    # Build (provider_kind, provider_ref) → group payload.
    groups_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    auxiliary: list[dict[str, Any]] = []
    vision: list[dict[str, Any]] = []
    embedding: list[dict[str, Any]] = []
    whisper: list[dict[str, Any]] = []
    tts: list[dict[str, Any]] = []

    configured_providers: set[str] = set()

    for row in catalog_rows:
        kind = row["provider_kind"]
        ref = row["provider_ref"]
        # Fan-out: under the array model one row contributes to every
        # capability bucket it claims. A multimodal chat row registered as
        # ['chat','auxiliary','vision'] surfaces in the chat groups AND
        # auxiliary_models AND vision_models simultaneously — which is
        # exactly the operator intent (one physical model serves all three).
        capabilities_set = set(row.get("capabilities") or [])
        helper_entry = {
            "id": row["model_id"],
            "label": row["display_label"],
            "configured": True,
        }
        if "auxiliary" in capabilities_set:
            auxiliary.append(helper_entry)
        if "vision" in capabilities_set:
            vision.append(helper_entry)
        if "embedding" in capabilities_set:
            embedding.append(helper_entry)
        if "whisper" in capabilities_set:
            whisper.append(helper_entry)
        if "tts" in capabilities_set:
            tts.append(helper_entry)
        # Chat-only path: register the row in its provider group. Embedding-/
        # whisper-/tts-only rows skip this path so the chat dropdowns don't
        # show non-chat models.
        if "chat" not in capabilities_set:
            continue
        key = (kind, ref)
        group = groups_by_key.get(key)
        if group is None:
            if kind == "system":
                group_name = ref.title() if ref.islower() else ref
                provider_tag = ref
                configured_providers.add(ref)
                group = {
                    "group": group_name,
                    "provider": provider_tag,
                    "configured": True,
                    "models": [],
                }
            else:  # endpoint
                label = endpoint_label_by_id.get(ref, f"endpoint:{ref[:8]}")
                group = {
                    "group": f"System: {label}",
                    "provider": "system",
                    "endpoint_id": ref,
                    "configured": True,
                    "models": [],
                }
            groups_by_key[key] = group
        group["models"].append(row["model_id"])

    groups = list(groups_by_key.values())

    return {
        "groups": groups,
        "auxiliary_models": auxiliary,
        "vision_models": vision,
        "whisper_models": whisper,
        "tts_models": tts,
        "embedding_models": embedding,
        "configured_providers": sorted(configured_providers),
    }


@app.post("/api/models/reload")
async def reload_model_catalog(request: Request) -> dict[str, str]:
    """No-op kept for backward compat with cockpit clients that still POST.

    Catalog rows live in the DB and ``/api/models`` queries them fresh on
    every call — there is no cache to invalidate. The YAML fallback
    registry that this endpoint used to bounce was deleted in chunk 6;
    the legacy YAML projection cache it then bounced was deleted in
    chunk 7.
    """
    await _require_admin(request)
    return {"status": "reloaded"}


# =============================================================================
# Codex Proxy Management Endpoints (Admin-only)
# =============================================================================


async def _require_admin(request: Request) -> dict[str, Any]:
    """Require authenticated admin user.

    Checks ``real_is_admin`` (the un-shadowed privilege flag set by
    ``require_approved_user``) so admin-only endpoints stay reachable when
    the caller is in "view as user" mode. ``is_admin`` on the returned
    dict still reflects the shadow, so any downstream visibility check
    on the returned user dict honours the toggle.
    """
    user = await require_approved_user(request, postgres_db)
    if not user.get("real_is_admin", False):
        # A non-admin reaching an admin endpoint is the strongest single
        # probe signal we have — always leaves a security_events row.
        await log_security_event(
            postgres_db,
            event_type="admin_denied",
            user=user,
            resource_type="admin_endpoint",
            resource_id=getattr(getattr(request, "url", None), "path", None),
            detail="Admin access required",
            request=request,
        )
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


async def _codex_proxy_request(
    method: str,
    path: str,
    **kwargs: Any,
) -> httpx.Response:
    """Make a request to the CLIProxyAPI management API.

    Reads CODEX_PROXY_URL and CODEX_MANAGEMENT_KEY from environment.
    Raises HTTPException on connection or upstream errors.
    """
    proxy_url = os.getenv("CODEX_PROXY_URL", "http://localhost:8317")
    mgmt_key = os.getenv("CODEX_MANAGEMENT_KEY", "")

    headers = kwargs.pop("headers", {})
    if mgmt_key:
        headers["Authorization"] = f"Bearer {mgmt_key}"

    timeout = kwargs.pop("timeout", 10.0)

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.request(
                method,
                f"{proxy_url}{path}",
                headers=headers,
                **kwargs,
            )
        if response.status_code >= 400:
            raise HTTPException(
                status_code=502,
                detail=f"Codex proxy returned {response.status_code}: {response.text[:200]}",
            )
        return response
    except HTTPException:
        raise
    except httpx.RequestError as e:
        raise HTTPException(
            status_code=502,
            detail=f"Codex proxy unreachable at {proxy_url}: {e}",
        ) from e


@app.get("/api/codex/status")
async def codex_status(request: Request) -> dict[str, Any]:
    """Get Codex proxy health and authentication status (admin-only)."""
    await _require_admin(request)

    try:
        auth_resp = await _codex_proxy_request("GET", "/v0/management/auth-files")
        auth_files = auth_resp.json()
    except HTTPException:
        # Proxy unreachable — the codex-proxy deployment is disabled or down.
        # `reachable: False` lets the cockpit show an "enable it" disclaimer
        # instead of a Connect button that would 502 on /api/codex/login.
        return {
            "connected": False,
            "reachable": False,
            "accounts": [],
            "model_count": 0,
        }

    # Normalize: auth-files may return a list or a dict with a key
    accounts = (
        auth_files if isinstance(auth_files, list) else auth_files.get("files", [])
    )
    active = [a for a in accounts if not a.get("disabled") and not a.get("unavailable")]

    model_count = 0
    try:
        models_resp = await _codex_proxy_request("GET", "/v1/models")
        models_data = models_resp.json()
        model_count = len(models_data.get("data", []))
    except HTTPException:
        pass

    # Wire-up after a local login: the proxy's callback runs on localhost:1455
    # so the orchestrator never sees /api/codex/callback for this flow. The
    # cockpit hits /api/codex/status as soon as polling reports success, so
    # ensure the transport row here when we see ≥1 active subscription.
    if len(active) > 0:
        try:
            await ensure_codex_proxy_endpoint(postgres_db)
        except Exception:
            logger.warning(
                "codex_status: ensure_codex_proxy_endpoint failed", exc_info=True
            )

    return {
        "connected": len(active) > 0,
        "reachable": True,
        "accounts": [
            {
                "name": a.get("name", "unknown"),
                "status": a.get("status", "unknown"),
                "status_message": a.get("status_message"),
            }
            for a in accounts
        ],
        "model_count": model_count,
    }


@app.get("/api/codex/models")
async def codex_models(request: Request) -> dict[str, Any]:
    """List models available through the Codex proxy (admin-only)."""
    await _require_admin(request)

    resp = await _codex_proxy_request("GET", "/v1/models")
    data = resp.json()
    models = [m.get("id", m) for m in data.get("data", [])]
    return {"models": models}


@app.post("/api/codex/login")
async def codex_login(request: Request) -> dict[str, Any]:
    """Initiate Codex OAuth login flow (admin-only).

    Returns an auth URL to open in the browser and a state token for polling.
    """
    await _require_admin(request)

    resp = await _codex_proxy_request(
        "GET",
        "/v0/management/codex-auth-url",
        params={"is_webui": "true"},
        timeout=15.0,
    )
    data = resp.json()
    # Proxy returns "url"; cockpit expects "auth_url"
    if "url" in data and "auth_url" not in data:
        data["auth_url"] = data.pop("url")
    return data


@app.get("/api/codex/login/poll")
async def codex_login_poll(request: Request, state: str) -> dict[str, Any]:
    """Poll Codex OAuth login status (admin-only)."""
    await _require_admin(request)

    resp = await _codex_proxy_request(
        "GET",
        "/v0/management/get-auth-status",
        params={"state": state},
    )
    return resp.json()


@app.post("/api/codex/callback")
async def codex_callback(
    body: CodexCallbackRequest, request: Request
) -> dict[str, Any]:
    """Relay an OAuth callback to the Codex proxy (admin-only).

    Accepts the full localhost callback URL (or explicit code+state).
    Parses the authorization code and state, then relays the callback
    to the proxy internally so no port-forward is needed.
    """
    await _require_admin(request)

    code = body.code
    state = body.state

    if body.url:
        parsed = urlparse(body.url)
        qs = parse_qs(parsed.query)
        code = code or (qs.get("code", [None])[0])
        state = state or (qs.get("state", [None])[0])

    if not code or not state:
        raise HTTPException(
            status_code=422,
            detail="Could not extract 'code' and 'state' from the provided URL. "
            "Please paste the complete URL from your browser address bar.",
        )

    resp = await _codex_proxy_request(
        "GET",
        "/codex/callback",
        params={"code": code, "state": state},
        timeout=15.0,
    )

    # OAuth completed — wire the codex-proxy as a system endpoint so the
    # subscription is immediately selectable in Admin → Models. Idempotent;
    # best-effort: a wiring failure must not block the callback response.
    try:
        await ensure_codex_proxy_endpoint(postgres_db)
    except Exception:
        logger.warning(
            "codex_callback: ensure_codex_proxy_endpoint failed", exc_info=True
        )

    if resp.content:
        return resp.json()
    return {"status": "ok"}


@app.delete("/api/codex/credentials/{name}")
async def codex_delete_credential(name: str, request: Request) -> dict[str, str]:
    """Remove a Codex proxy credential file (admin-only)."""
    await _require_admin(request)

    await _codex_proxy_request(
        "DELETE",
        "/v0/management/auth-files",
        params={"name": name},
    )
    return {"status": "deleted"}


# =============================================================================
# System Settings — Main Cloud (Phase 4, Admin-only)
# =============================================================================
# These endpoints drive the cockpit admin "Cloud Storage" panel. GETs
# return the current effective config with secrets stripped, PUT persists
# a new config to `system_settings.main_cloud` and triggers a reload, and
# POST /test does a dry-run connection check with a proposed config
# without persisting.
#
# Secret handling: non-secret fields (URLs, usernames, quota) are stored
# in the `value` JSONB column. Secret fields (passwords, client secrets)
# are referenced via `credentials_ref` — a pointer like
# `env:OPENCLOUD_KEYCLOAK_CLIENT_SECRET` that the loader resolves against
# the orchestrator's own environment. This keeps secrets in Vault/ESO/.env
# and lets the UI manage only the non-secret knobs.

_MAIN_CLOUD_NONSECRET_FIELDS_BY_BACKEND: dict[str, list[str]] = {
    "nextcloud": [
        "base_url",
        "public_url",
        "admin_user",
        "agent_user",
    ],
    "opencloud": [
        "base_url",
        "public_url",
        "keycloak_issuer",
        "keycloak_client_id",
        "admin_role_claim_value",
        "default_quota_bytes",
    ],
}

_MAIN_CLOUD_SECRET_FIELDS_BY_BACKEND: dict[str, list[str]] = {
    "nextcloud": ["admin_password", "agent_password", "oidc_client_secret"],
    "opencloud": ["keycloak_client_secret"],
}

_MAIN_CLOUD_ALLOWED_BACKENDS = {"nextcloud", "opencloud"}


def _sanitize_main_cloud_value(
    backend_id: str, raw_value: dict[str, Any]
) -> dict[str, Any]:
    """Strip unknown keys + secret fields from an incoming overlay body.

    Secrets are never stored in JSONB — they always come from env vars
    resolved via `credentials_ref`. The sanitizer drops any secret-field
    key from the incoming dict so a careless UI PUT cannot accidentally
    persist a client secret into `system_settings.value`.
    """
    nonsecret = _MAIN_CLOUD_NONSECRET_FIELDS_BY_BACKEND.get(backend_id, [])
    secret = set(_MAIN_CLOUD_SECRET_FIELDS_BY_BACKEND.get(backend_id, []))
    clean: dict[str, Any] = {"backend_id": backend_id}
    for key in nonsecret:
        if key in raw_value and raw_value[key] not in (None, ""):
            clean[key] = raw_value[key]
    # Record which fields are secret-credential-sourced so the loader
    # knows to resolve them via credentials_ref at read time.
    if secret:
        clean["__secret_fields__"] = sorted(secret)
    return clean


def _current_effective_config() -> dict[str, Any]:
    """Read the active backend's current config shape for GET responses."""
    active = main_cloud_router.active
    backend_id = active.backend_id
    result: dict[str, Any] = {
        "backend_id": backend_id,
        "is_initialized": active.is_initialized,
        "is_configured": active.is_configured,
    }

    # Non-secret fields come directly from the backend's settings where
    # possible. NextcloudBackend reads env vars into private attrs;
    # OpenCloudBackend holds a full settings dataclass.
    if backend_id == "nextcloud":
        # Attributes follow the adapter's internal names.
        result.update(
            {
                "base_url": getattr(active, "_base_url", None),
                "public_url": getattr(active, "_public_url", None),
                "admin_user": getattr(active, "_admin_user", None),
                "agent_user": getattr(active, "_agent_user", None),
            }
        )
    elif backend_id == "opencloud":
        settings = getattr(active, "_settings", None)
        if settings is not None:
            result.update(
                {
                    "base_url": str(settings.base_url),
                    "public_url": str(settings.public_url),
                    "keycloak_issuer": str(settings.keycloak_issuer),
                    "keycloak_client_id": settings.keycloak_client_id,
                    "admin_role_claim_value": settings.admin_role_claim_value,
                    "default_quota_bytes": settings.default_quota_bytes,
                }
            )
    return result


def _env_var_provenance(env_name: str) -> dict[str, Any]:
    """Report whether a secret env var is set, without leaking its value."""
    val = os.getenv(env_name)
    return {
        "env_var": env_name,
        "set": bool(val),
        "length": len(val) if val else 0,
    }


@app.get("/api/admin/system-settings/main_cloud")
async def get_main_cloud_settings(request: Request) -> dict[str, Any]:
    """Return the current effective main-cloud config + persisted overlay.

    Admin-only. The response is safe to log: every secret field is
    replaced with its env-var provenance (name + set/unset flag + length).
    """
    await _require_admin(request)
    effective = _current_effective_config()
    backend_id = effective["backend_id"]

    try:
        row = await postgres_db.get_system_setting("main_cloud")
    except Exception:
        row = None

    overlay_value: dict[str, Any] = {}
    overlay_updated_at: Optional[str] = None
    overlay_updated_by: Optional[str] = None
    credentials_ref: Optional[str] = None
    if row:
        raw = row.get("value") or {}
        if isinstance(raw, dict):
            overlay_value = raw
        credentials_ref = row.get("credentials_ref")
        ua = row.get("updated_at")
        overlay_updated_at = ua.isoformat() if ua is not None else None
        overlay_updated_by = row.get("updated_by")

    # Secret provenance: the adapter-specific env var names.
    secret_env_by_backend = {
        "nextcloud": {
            "admin_password": "NEXTCLOUD_ADMIN_PASSWORD",
            "agent_password": "NEXTCLOUD_AGENT_PASSWORD",
            "oidc_client_secret": "NEXTCLOUD_OIDC_CLIENT_SECRET",
        },
        "opencloud": {
            "keycloak_client_secret": "OPENCLOUD_KEYCLOAK_CLIENT_SECRET",
        },
    }
    secret_provenance: dict[str, dict[str, Any]] = {}
    for field, env_name in secret_env_by_backend.get(backend_id, {}).items():
        # credentials_ref can override the env var name per-field.
        effective_env = env_name
        if (
            credentials_ref
            and credentials_ref.startswith("env:")
            and field in overlay_value.get("__secret_fields__", [])
        ):
            effective_env = credentials_ref[4:]
        secret_provenance[field] = _env_var_provenance(effective_env)

    return {
        "effective": effective,
        "overlay": {
            "present": bool(row),
            "value": overlay_value,
            "credentials_ref": credentials_ref,
            "updated_at": overlay_updated_at,
            "updated_by": overlay_updated_by,
        },
        "secrets": secret_provenance,
        "allowed_backends": sorted(_MAIN_CLOUD_ALLOWED_BACKENDS),
    }


@app.put("/api/admin/system-settings/main_cloud")
async def put_main_cloud_settings(
    body: dict[str, Any], request: Request
) -> dict[str, Any]:
    """Persist a new main-cloud config and hot-reload the router.

    Admin-only. The request body is ``{"value": {...}, "credentials_ref": "env:..."}``:

    * ``value.backend_id`` must be one of ``allowed_backends``.
    * Secret fields in ``value`` are silently dropped by the sanitizer —
      never persist secrets in the DB. Rotate via the secret store.
    * ``credentials_ref`` is an optional pointer (e.g. ``env:NEW_VAR``)
      that the loader resolves for secret fields at read time.
    * The handler performs a synchronous reload on the local replica
      after persisting, so the caller gets a success/failure response
      reflecting the actual new backend state. Other replicas pick up
      the change via the pg_notify LISTEN task.
    """
    admin = await _require_admin(request)

    value_in = body.get("value") or {}
    if not isinstance(value_in, dict):
        raise HTTPException(status_code=400, detail="`value` must be an object")
    backend_id = value_in.get("backend_id")
    if backend_id not in _MAIN_CLOUD_ALLOWED_BACKENDS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"unknown backend_id {backend_id!r}; "
                f"must be one of {sorted(_MAIN_CLOUD_ALLOWED_BACKENDS)}"
            ),
        )
    credentials_ref = body.get("credentials_ref")
    if credentials_ref is not None and not isinstance(credentials_ref, str):
        raise HTTPException(
            status_code=400, detail="`credentials_ref` must be a string or null"
        )

    clean_value = _sanitize_main_cloud_value(backend_id, value_in)

    # Validate the proposed config before persisting — raises on
    # missing required fields so we fail fast with a 422-style message.
    from services.cloud.config import load_main_cloud_config, missing_secret_envs

    probe_overlay = {
        "value": clean_value,
        "credentials_ref": credentials_ref,
    }
    try:
        load_main_cloud_config(db_overlay=probe_overlay)
    except Exception as e:
        raise HTTPException(
            status_code=400, detail=f"invalid main cloud config: {e}"
        ) from e

    # Fail loud if the backend's real secrets are not wired (Issue 5). The
    # loader above validates the *shape* but silently substitutes built-in dev
    # defaults for missing secrets, so a config that could only ever connect
    # with `admin` / `agent-service-dev` would otherwise persist + activate and
    # fail much later at the first cloud call (surviving restarts). Refuse here,
    # naming the exact env var(s) to set.
    missing = missing_secret_envs(backend_id, probe_overlay)
    if missing:
        names = ", ".join(sorted({m["env_var"] for m in missing}))
        raise HTTPException(
            status_code=400,
            detail=(
                f"secret env not set for backend {backend_id!r}: {names}. "
                "Wire the secret(s) into the orchestrator env (Helm/Vault) or "
                "point `credentials_ref` at a set env var, then retry. Refusing "
                "to activate a backend that would fall back to built-in dev "
                "credentials."
            ),
        )

    try:
        stored = await postgres_db.upsert_system_setting(
            "main_cloud",
            clean_value,
            credentials_ref=credentials_ref,
            updated_by=str(admin.get("id") or admin.get("email") or "admin"),
        )
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"failed to persist main cloud setting: {e}"
        ) from e

    # Local synchronous reload so the PUT response reflects the new state.
    reload_ok = await main_cloud_router.reload_from_db(stored)
    if not reload_ok:
        raise HTTPException(
            status_code=500,
            detail=(
                "main cloud setting persisted but the new backend failed to "
                "initialize — check orchestrator logs. The active backend is "
                "unchanged; rollback the setting or fix the config and retry."
            ),
        )

    # Fan-out to other replicas via pg_notify. Best-effort.
    await fire_reload(postgres_db)
    return {
        "status": "ok",
        "backend_id": backend_id,
        "reloaded": True,
    }


@app.post("/api/admin/system-settings/main_cloud/test")
async def test_main_cloud_settings(
    body: dict[str, Any], request: Request
) -> dict[str, Any]:
    """Dry-run a proposed main-cloud config without persisting.

    Builds a backend from the proposed overlay, calls
    ``ensure_initialized()``, and tears it down. Returns whether the
    probe succeeded plus a short detail string. Useful for "Test"
    buttons in the admin UI before the operator commits to saving.
    """
    await _require_admin(request)

    value_in = body.get("value") or {}
    backend_id = value_in.get("backend_id")
    if backend_id not in _MAIN_CLOUD_ALLOWED_BACKENDS:
        raise HTTPException(
            status_code=400,
            detail=f"unknown backend_id {backend_id!r}",
        )
    credentials_ref = body.get("credentials_ref")

    clean_value = _sanitize_main_cloud_value(backend_id, value_in)
    probe_overlay = {
        "value": clean_value,
        "credentials_ref": credentials_ref,
    }

    # Issue 5: surface unwired secrets as the precise reason, instead of letting
    # the probe connect with built-in dev credentials and report a cryptic
    # upstream-auth failure.
    from services.cloud.config import missing_secret_envs

    missing = missing_secret_envs(backend_id, probe_overlay)
    if missing:
        names = ", ".join(sorted({m["env_var"] for m in missing}))
        return {
            "ok": False,
            "detail": (
                f"secret env not set for backend {backend_id!r}: {names} "
                "(would fall back to built-in dev credentials). Wire the "
                "secret(s) or set `credentials_ref` before testing."
            ),
        }

    try:
        probe_backend = build_backend(db_overlay=probe_overlay)
    except Exception as e:
        return {"ok": False, "detail": f"build_backend failed: {e}"}

    try:
        try:
            ok = await probe_backend.ensure_initialized()
        except Exception as e:
            return {"ok": False, "detail": f"ensure_initialized raised: {e}"}
        if not ok:
            return {
                "ok": False,
                "detail": "backend reported not initialized — check config + upstream",
            }
        health = await probe_backend.health_check()
        return {
            "ok": health.ok,
            "detail": health.detail or "",
            "latency_ms": health.latency_ms,
        }
    finally:
        try:
            await probe_backend.close()
        except Exception:
            pass


@app.post("/api/admin/system-settings/main_cloud/reload")
async def reload_main_cloud_settings(request: Request) -> dict[str, Any]:
    """Force a local reload from the current persisted overlay.

    Admin-only. Useful when an operator has rotated a secret out-of-band
    (new Keycloak client secret in .env) and wants this orchestrator
    replica to pick up the new env var without a restart. Does not
    re-broadcast — other replicas keep their current state and can be
    reloaded individually or via a rolling restart.
    """
    await _require_admin(request)
    try:
        overlay = await postgres_db.get_system_setting("main_cloud")
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"failed to read setting: {e}"
        ) from e
    ok = await main_cloud_router.reload_from_db(overlay)
    if not ok:
        raise HTTPException(
            status_code=500,
            detail="reload failed — new backend did not initialize",
        )
    return {"status": "ok", "backend_id": main_cloud_router.active.backend_id}


@app.delete("/api/admin/system-settings/main_cloud")
async def delete_main_cloud_settings(request: Request) -> dict[str, Any]:
    """Remove the persisted overlay and reload from env vars only.

    Admin-only. Semantically a "reset to defaults" button.
    """
    await _require_admin(request)
    existed = await postgres_db.delete_system_setting("main_cloud")
    if not existed:
        return {"status": "noop", "existed": False}
    ok = await main_cloud_router.reload_from_db(None)
    if not ok:
        raise HTTPException(
            status_code=500,
            detail="overlay cleared but env-var rebuild failed",
        )
    await fire_reload(postgres_db)
    return {
        "status": "ok",
        "existed": True,
        "backend_id": main_cloud_router.active.backend_id,
    }


def _vm_workspaces_response(row: dict | None) -> dict[str, Any]:
    """Shape the vm_workspaces system_settings row for API responses."""
    value = (row or {}).get("value") or {}
    enabled = True
    if isinstance(value, dict) and value.get("enabled") is False:
        enabled = False
    updated_at = (row or {}).get("updated_at")
    return {
        "enabled": enabled,
        "updated_at": updated_at.isoformat() if updated_at is not None else None,
        "updated_by": (row or {}).get("updated_by"),
    }


@app.get("/api/admin/system-settings/vm_workspaces")
async def get_vm_workspaces_settings(request: Request) -> dict[str, Any]:
    """Return the global VM-workspaces kill-switch.

    Admin-only. Absent row is reported as enabled (fail-open default).
    """
    await _require_admin(request)
    try:
        row = await postgres_db.get_system_setting("vm_workspaces")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    return _vm_workspaces_response(row)


@app.put("/api/admin/system-settings/vm_workspaces")
async def put_vm_workspaces_settings(
    body: dict[str, Any], request: Request
) -> dict[str, Any]:
    """Toggle the global VM-workspaces kill-switch.

    Admin-only. When disabled, every VM workspace request is denied,
    including those from admin users. Already-running VMs are not torn
    down — the switch only blocks new dispatches.
    """
    admin = await _require_admin(request)
    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        raise HTTPException(status_code=400, detail="`enabled` must be a boolean")
    try:
        row = await postgres_db.upsert_system_setting(
            "vm_workspaces",
            {"enabled": enabled},
            updated_by=admin.get("email") or str(admin.get("id", "")),
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    return _vm_workspaces_response(row)


# =============================================================================
# Capability grants — admin CRUD + audit + kill-switch + self-introspection
# (User-Defined Experts, Slice 2; decisions 8, 9, 23). The PEPs live near
# _check_vm_permission; these are the management/read surfaces.
# =============================================================================


@app.get("/api/admin/system-settings/user_experts")
async def get_user_experts_settings(request: Request) -> dict[str, Any]:
    """Return the global user-defined-experts kill-switch (decision 8).
    Admin-only. Absent row is reported as enabled (fail-open default)."""
    await _require_admin(request)
    row = await postgres_db.get_system_setting("user_experts")
    value = (row or {}).get("value") or {}
    return {
        "enabled": not (isinstance(value, dict) and value.get("enabled") is False),
        "updated_by": (row or {}).get("updated_by"),
    }


@app.put("/api/admin/system-settings/user_experts")
async def put_user_experts_settings(
    body: dict[str, Any], request: Request
) -> dict[str, Any]:
    """Toggle the user-defined-experts kill-switch. Admin-only. When disabled,
    DB-expert creation and grant enforcement are off (decision 8)."""
    admin = await _require_admin(request)
    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        raise HTTPException(status_code=400, detail="`enabled` must be a boolean")
    await postgres_db.upsert_system_setting(
        "user_experts",
        {"enabled": enabled},
        updated_by=admin.get("email") or str(admin.get("id", "")),
    )
    return {"enabled": enabled}


class GrantSet(BaseModel):
    """Request body for setting a capability grant."""

    value_json: Any
    reason: str | None = None


def _validate_grant_value(key: str, value: Any) -> None:
    """Reject a grant value that doesn't match the catalog type, so a malformed
    enum can't later crash meet()/resolve_grants at dispatch time."""
    from src.core.capability_grants import CATALOG

    spec = CATALOG[key]
    t = spec["type"]
    if t == "bool" and not isinstance(value, bool):
        raise HTTPException(
            status_code=400, detail=f"{key}: value_json must be a boolean"
        )
    if t == "enum" and value not in spec["order"]:
        raise HTTPException(
            status_code=400, detail=f"{key}: value_json must be one of {spec['order']}"
        )
    if t == "list" and not (value is None or isinstance(value, list)):
        raise HTTPException(
            status_code=400, detail=f"{key}: value_json must be a list or null"
        )


@app.get("/api/admin/grants")
async def list_grants_endpoint(
    request: Request, scope_kind: str, scope_id: str | None = None
) -> dict:
    """List the grants set on one scope, plus the catalog. Admin-only."""
    await _require_admin(request)
    if scope_kind not in ("user", "project", "global"):
        raise HTTPException(status_code=400, detail="bad scope_kind")
    from src.core.capability_grants import CATALOG

    return {
        "grants": await postgres_db.list_grants(
            scope_kind=scope_kind,
            scope_id=(None if scope_kind == "global" else scope_id),
        ),
        "catalog": CATALOG,
    }


@app.put("/api/admin/grants/{scope_kind}/{scope_id}/{key}")
async def set_grant_endpoint(
    scope_kind: str, scope_id: str, key: str, body: GrantSet, request: Request
) -> dict:
    """Set/update one capability grant (audited). Admin-only."""
    admin = await _require_admin(request)
    from src.core.capability_grants import CATALOG

    if key not in CATALOG or scope_kind not in ("user", "project", "global"):
        raise HTTPException(status_code=400, detail="unknown key or scope_kind")
    _validate_grant_value(key, body.value_json)
    return {
        "grant": await postgres_db.set_grant(
            scope_kind=scope_kind,
            scope_id=(None if scope_kind == "global" else scope_id),
            key=key,
            value_json=body.value_json,
            actor=str(admin["id"]),
            reason=body.reason,
        )
    }


@app.delete("/api/admin/grants/{scope_kind}/{scope_id}/{key}")
async def delete_grant_endpoint(
    scope_kind: str, scope_id: str, key: str, request: Request
) -> dict:
    """Revoke one capability grant (audited). Admin-only."""
    admin = await _require_admin(request)
    if scope_kind not in ("user", "project", "global"):
        raise HTTPException(status_code=400, detail="bad scope_kind")
    return {
        "deleted": await postgres_db.delete_grant(
            scope_kind=scope_kind,
            scope_id=(None if scope_kind == "global" else scope_id),
            key=key,
            actor=str(admin["id"]),
        )
    }


@app.get("/api/users/me/capabilities")
async def my_capabilities(request: Request) -> dict:
    """The caller's effective resolved grants + the catalog (drives editor greying
    in the fast-follow). Admins get null grants (unrestricted)."""
    user = await require_approved_user(request, postgres_db)
    from src.core.capability_grants import CATALOG

    if user.get("is_admin"):
        return {"is_admin": True, "grants": None, "catalog": CATALOG}
    from services.grants_service import resolve_grants_for

    grants = await resolve_grants_for(
        postgres_db, user_id=str(user["id"]), project_ids=await _grant_project_ids(user)
    )
    return {"is_admin": False, "grants": grants, "catalog": CATALOG}


# =============================================================================
# User Endpoints
# =============================================================================


@app.get("/api/users")
async def list_users(request: Request) -> list[dict[str, Any]]:
    """List all users (requires authentication)."""
    await require_approved_user(request, postgres_db)
    try:
        return await postgres_db.list_users()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/users/{user_id}")
async def get_user(user_id: str, request: Request) -> dict[str, Any]:
    """Get a single user by ID (requires authentication)."""
    await require_approved_user(request, postgres_db)
    user = await postgres_db.get_user(user_id)
    if not user:
        raise HTTPException(status_code=404, detail=f"User '{user_id}' not found")
    return user


@app.post("/api/users")
async def create_user(body: UserCreate, request: Request) -> dict[str, Any]:
    """Create a new user with a default project. Admin-only.

    Real users are JIT-provisioned via Keycloak OIDC login (see
    upsert_user_from_oidc). This endpoint is for admin user management
    and tests.
    """
    admin = await _require_admin(request)
    try:
        user, project = await postgres_db.create_user_with_default_project(
            display_name=body.display_name,
            avatar_color=body.avatar_color or "#89b4fa",
            email=body.email,
        )
        # Admin creation *is* approval — admit immediately and stamp the
        # approving admin so the audit trail and cockpit status are correct.
        await postgres_db.update_user(
            user_id=str(user["id"]),
            is_approved=True,
            approved_at=datetime.now(timezone.utc),
            approved_by=str(admin.get("id")),
        )
        user["is_approved"] = True
        await _create_gitea_repo_for_project(user, project)

        # Create personal WebDAV datasource for the default project.
        # Fresh owner provisioning — resolve via the owner seam (Issue 16).
        backend = main_cloud_router.for_owner(user)
        if backend.is_initialized and body.email:
            try:
                # Phase 1: the Nextcloud adapter treats the email as the
                # username (legacy behavior — OIDC-backed setups where the NC
                # username differs from the email are broken here, inherited
                # bug from the pre-refactor code). Phase 2 resolves via
                # resolve_user_identity + get_user_home().handle.
                home = await backend.get_user_home(UserId(body.email))
                webdav_url = home.webdav_url if home else None
                if webdav_url:
                    ds = await postgres_db.create_datasource(
                        name="Cloud Storage (Personal)",
                        ds_type="webdav",
                        connection_url=webdav_url,
                        description="Personal cloud storage",
                        credentials=backend.webdav_credentials,
                    )
                    await postgres_db.link_datasource_to_project(
                        project_id=str(project["id"]),
                        datasource_id=str(ds["id"]),
                        read_only=False,
                    )
            except Exception as e:
                logger.warning(
                    f"Failed to create personal cloud storage for user "
                    f"{user['id']}: {e}"
                )

        return user
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.put("/api/users/{user_id}")
async def update_user(
    user_id: str, body: UserUpdate, request: Request
) -> dict[str, str]:
    """Update a user (requires authentication)."""
    await require_approved_user(request, postgres_db)
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
    """Delete a user. Admin-only.

    Self-service deletion isn't exposed yet — it needs Keycloak sync and
    explicit handling of orphaned jobs/threads/project_members. Add a
    separate endpoint if/when the cockpit needs it.
    """
    await _require_admin(request)
    success = await postgres_db.delete_user(user_id)
    if not success:
        raise HTTPException(status_code=404, detail=f"User '{user_id}' not found")
    return {"status": "deleted"}


@app.get("/api/admin/users")
async def admin_list_users(request: Request) -> list[dict[str, Any]]:
    """List all users including admin/VM flags (admin-only)."""
    await _require_admin(request)
    try:
        return await postgres_db.list_users()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.patch("/api/admin/users/{user_id}")
async def admin_patch_user(
    user_id: str, body: AdminUserUpdate, request: Request
) -> dict[str, str]:
    """Toggle privileged user flags (admin-only).

    Accepts partial updates of ``is_admin``, ``can_use_vm`` and
    ``is_approved``. Setting ``is_approved=True`` admits the user and stamps
    ``approved_at``/``approved_by``; setting it False is suspension — the flag
    flips off (effective on the user's next request) while ``approved_at`` is
    kept as history. Refuses to let an admin clear their own ``is_admin``
    flag — lockout prevention.
    """
    admin = await _require_admin(request)
    if body.is_admin is False and str(admin.get("id")) == user_id:
        raise HTTPException(
            status_code=400,
            detail="Admins cannot clear their own is_admin flag",
        )
    approved_at = None
    approved_by = None
    if body.is_approved is True:
        approved_at = datetime.now(timezone.utc)
        approved_by = str(admin.get("id"))
    success = await postgres_db.update_user(
        user_id=user_id,
        is_admin=body.is_admin,
        can_use_vm=body.can_use_vm,
        is_approved=body.is_approved,
        approved_at=approved_at,
        approved_by=approved_by,
    )
    if not success:
        raise HTTPException(status_code=404, detail=f"User '{user_id}' not found")
    if body.is_approved is True:
        # Admission just granted — provision cloud/Gitea from the row. The JIT
        # ensures are gated on approval and never re-fire for an app-side
        # approved user, so this is their provisioning path. Idempotent.
        row = await postgres_db.get_user(user_id)
        if row:
            await ensure_user_provisioned(row)
    return {"status": "updated"}


@app.post("/api/admin/users/approve")
async def admin_bulk_approve_users(
    body: AdminBulkApprove, request: Request
) -> dict[str, Any]:
    """Bulk-approve pending users (admin-only).

    Stamps approval on every id that resolves to a real row in a single
    transaction and reports per-id status. This is the workflow Keycloak's
    console can't do (no bulk role assignment). Ids that don't match an
    existing user come back as ``not_found`` rather than failing the batch.
    """
    admin = await _require_admin(request)
    approved_ids = await postgres_db.approve_users(
        body.user_ids, approved_by=str(admin.get("id"))
    )
    # Provision cloud/Gitea for each newly-approved user (idempotent; the JIT
    # ensures never re-fire for app-side approvals).
    for uid in approved_ids:
        row = await postgres_db.get_user(uid)
        if row:
            await ensure_user_provisioned(row)
    approved_set = set(approved_ids)
    results = [
        {"id": uid, "status": "approved" if uid in approved_set else "not_found"}
        for uid in body.user_ids
    ]
    return {"approved_count": len(approved_set), "results": results}


@app.get("/api/admin/security-events")
async def admin_list_security_events(
    request: Request,
    limit: int = 100,
    user_id: Optional[str] = None,
    event_type: Optional[str] = None,
    since: Optional[str] = None,
) -> dict[str, Any]:
    """List denied-access security events, newest first (admin-only).

    The read path for the cross-user 403 audit log — every 403 raised by
    a ``security/access.py`` gate (plus admin-gate and IDE-proxy denials)
    lands in ``security_events``. Filters: ``user_id`` (the denied
    caller), ``event_type`` (``access_denied`` / ``admin_denied``),
    ``since`` (ISO 8601). Rows are pruned on retention
    (``SECURITY_EVENTS_RETENTION_DAYS``, default 90). Design:
    docs/features/security_event_log.md.
    """
    await _require_admin(request)
    since_dt: Optional[datetime] = None
    if since:
        try:
            since_dt = datetime.fromisoformat(since)
        except ValueError:
            raise HTTPException(
                status_code=400, detail="'since' must be an ISO 8601 timestamp"
            )
    events = await postgres_db.list_security_events(
        limit=limit,
        user_id=user_id,
        event_type=event_type,
        since=since_dt,
    )
    return {"events": events, "count": len(events)}


# =============================================================================
# Project Endpoints
# =============================================================================


# Per-project locks prevent concurrent heal attempts from creating duplicate
# Spaces (ensure_project_folder is not idempotent — each call makes a new drive).
_project_heal_locks: dict[str, asyncio.Lock] = {}


async def _sync_project_member_to_groups(
    project_id_str: str,
    group_name: str,
    user: dict[str, Any],
    backend: Any,
) -> None:
    """Add one member to the Keycloak group and the LibreGraph group.

    Both writes are needed: Keycloak carries the `groups` token claim (durable
    across OpenCloud re-logins), the direct backend add makes the Space visible
    in the user's currently-active OpenCloud session without waiting for them
    to re-authenticate.
    """
    if keycloak_groups.is_initialized and user.get("keycloak_sub"):
        await keycloak_groups.add_user_to_project_group(
            user["keycloak_sub"], project_id_str
        )
    if backend.is_initialized:
        try:
            resolved = await backend.resolve_user_identity(
                user.get("email"),
                (user.get("display_name") or "").lower(),
            )
            if resolved:
                await backend.add_user_to_group(resolved, group_name)
        except Exception as e:
            logger.debug(
                f"Failed to add user {user.get('id')} to backend group "
                f"{group_name}: {e}"
            )


async def _ensure_project_cloud_resources(
    project: dict[str, Any],
) -> dict[str, Any]:
    """Ensure a project has its Keycloak group, main-cloud Space and WebDAV
    datasource — and that every current member is in both groups.

    Two conditional steps:

    * **Folder creation** runs only when `main_cloud_folder_handle` is missing
      (protected by a per-project lock because `ensure_project_folder` is not
      idempotent — each call creates a new Space).
    * **Member sync** runs unconditionally (the Keycloak and LibreGraph adds
      are idempotent). This repairs the common case where the Space already
      exists but the user was never added to the groups — e.g. project created
      while Keycloak admin auth was broken, or a Space adopted out-of-band.

    Default projects skip both: they piggyback on the owner's personal home
    Space (already attached as a datasource by the user-creation flow), so a
    separate project Space + `project-<id>` group would be dead state. The
    cloud_storage_url branch in `get_project` resolves to the user's home
    browser URL for default projects.

    Returns the (possibly updated) project dict.
    """
    if project.get("is_default"):
        return project

    project_id_str = str(project["id"])
    project_name = project["name"]
    group_name = f"project-{project_id_str}"
    backend = main_cloud_router.for_project(project)

    handle_str = project.get("main_cloud_folder_handle")
    legacy_folder_id = project.get("nextcloud_folder_id")
    needs_folder = not (handle_str or legacy_folder_id)

    if needs_folder and backend.is_initialized:
        lock = _project_heal_locks.setdefault(project_id_str, asyncio.Lock())
        async with lock:
            fresh = await postgres_db.get_project(project_id_str)
            if fresh:
                project = fresh
            if not (
                project.get("main_cloud_folder_handle")
                or project.get("nextcloud_folder_id")
            ):
                try:
                    if keycloak_groups.is_initialized:
                        await keycloak_groups.ensure_project_group(
                            project_id_str, project_name
                        )
                    await backend.ensure_group(group_name)
                    folder_handle = await backend.ensure_project_folder(
                        project_name=project_name,
                        group_id=group_name,
                    )
                    legacy_id: int | None = None
                    if backend.backend_id == "nextcloud":
                        try:
                            legacy_id = int(folder_handle.native_id)
                        except ValueError:
                            legacy_id = None
                    await postgres_db.update_project(
                        project_id_str,
                        main_cloud_backend=backend.backend_id,
                        main_cloud_folder_handle=folder_handle.to_db(),
                        nextcloud_folder_id=legacy_id,
                    )
                    project["main_cloud_backend"] = backend.backend_id
                    project["main_cloud_folder_handle"] = folder_handle.to_db()
                    project["nextcloud_folder_id"] = legacy_id

                    # The project working folder is intentionally NOT attached
                    # as a `webdav` datasource: job/session workspaces get the
                    # folder cloned in (Mode-A baseline for jobs, the `projects/`
                    # sync mount for sessions), so attaching it here would
                    # double-expose the same files through the webdav_* tools.
                    # webdav_* tools are reserved for clouds that are NOT cloned
                    # (the personal home cloud + externally-attached WebDAV).
                    # See docs/issues/main_cloud.md (Issue 1 / Issue 8).
                except Exception as e:
                    logger.warning(
                        f"Failed to create cloud resources for project "
                        f"{project_id_str}: {e}"
                    )

    # Member sync — always runs. Idempotent add in both Keycloak and the
    # backend; cheap enough to include in every GET and essential for
    # self-healing projects whose Space exists but whose members were never
    # added to the groups.
    if keycloak_groups.is_initialized or backend.is_initialized:
        try:
            if keycloak_groups.is_initialized:
                await keycloak_groups.ensure_project_group(project_id_str, project_name)
            members = await postgres_db.get_project_members(project_id_str)
            if members:
                users = await asyncio.gather(
                    *[postgres_db.get_user(str(m["user_id"])) for m in members],
                    return_exceptions=False,
                )
                await asyncio.gather(
                    *[
                        _sync_project_member_to_groups(
                            project_id_str, group_name, u, backend
                        )
                        for u in users
                        if u
                    ],
                    return_exceptions=True,
                )
        except Exception as e:
            logger.debug(f"Member sync failed for project {project_id_str}: {e}")

    return project


@app.post("/api/projects")
async def create_project(body: ProjectCreate, request: Request) -> dict[str, Any]:
    """Create a new project with the requesting user as owner."""
    # H5: pre-fix, this endpoint had no auth at all and trusted body.user_id,
    # so any unauthenticated caller could create projects owned by anyone.
    # Admins keep the ability to create on behalf of others (legitimate
    # setup flow); regular users get bound to themselves.
    user = await require_approved_user(request, postgres_db)
    owner_id = body.user_id if user.get("is_admin") else str(user["id"])
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
            user_id=owner_id,
            role="owner",
        )

        # Create Gitea jobs repo and grant creator access
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
                # Grant creator read access
                if owner_id:
                    try:
                        creator = await postgres_db.get_user(owner_id)
                        if creator and creator.get("email"):
                            await gitea_client.grant_user_repo_access(
                                creator["email"], repo_name
                            )
                    except Exception as e:
                        logger.warning(
                            f"Failed to grant Gitea access for project creator: {e}"
                        )

        # Provision Keycloak group + main-cloud Space + WebDAV datasource,
        # then sync the creator into both the Keycloak and LibreGraph groups.
        # The dual group write is intentional: Keycloak carries the `groups`
        # token claim (durable — OpenCloud's proxy reconciles LibreGraph
        # memberships from it on every login), while the direct backend add
        # makes the Space visible in the creator's currently-active OpenCloud
        # session without waiting for them to re-authenticate.
        project = await _ensure_project_cloud_resources(project)

        return project
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/projects")
async def list_projects(
    request: Request,
    user_id: str | None = Query(default=None),
) -> list[dict[str, Any]]:
    """List projects visible to the caller.

    Visibility model (G2):
        * Admins see the full list, optionally narrowed by ``?user_id=`` or
          by an MCP ``project:<uuid>`` token scope.
        * Non-admins see only the projects they're a member of
          (``get_projects_for_user(caller)``), narrowed by any MCP scope.
        * A non-admin passing ``?user_id=`` for anyone other than themselves
          is rejected (403). Self-query is allowed but redundant.
    """
    caller = await require_approved_user(request, postgres_db)
    is_admin = bool(caller.get("is_admin"))
    scope_pid = mcp_scope_project_id(caller)

    if user_id is not None and not is_admin and str(user_id) != str(caller["id"]):
        raise HTTPException(
            status_code=403,
            detail="Not authorized to query other users' projects",
        )

    try:
        if is_admin and user_id is None:
            async with postgres_db.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT * FROM projects WHERE status != 'deleted' "
                    "ORDER BY updated_at DESC LIMIT 100"
                )
                projects = [dict(r) for r in rows]
        elif user_id is not None:
            projects = await postgres_db.get_projects_for_user(user_id)
        else:
            projects = await postgres_db.get_projects_for_user(str(caller["id"]))

        if scope_pid:
            projects = [p for p in projects if str(p.get("id", "")) == str(scope_pid)]

        return projects
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/projects/{project_id}")
async def get_project(request: Request, project_id: str) -> dict[str, Any]:
    """Get a single project by ID."""
    _, project = await require_project_member(request, postgres_db, project_id)

    # Lazy-heal: projects created before the cloud-resource fix have no
    # folder handle. The helper is a no-op if the Space already exists.
    project = await _ensure_project_cloud_resources(project)

    # Compute cloud_storage_url for cockpit deep-links
    project["cloud_storage_url"] = None
    backend = main_cloud_router.for_project(project)
    if backend.is_initialized:
        if project.get("is_default"):
            # Default projects piggyback on the owner's personal home Space —
            # the deep-link must resolve to that home, not to a project Space
            # (which we no longer provision for defaults — see
            # `_ensure_project_cloud_resources`). A stale handle from older
            # deployments is intentionally ignored here.
            try:
                members = await postgres_db.get_project_members(project_id)
                owner = next((m for m in members if m.get("role") == "owner"), None)
                owner_email = (owner or {}).get("email")
                owner_display = (owner or {}).get("display_name") or ""
                if owner_email:
                    resolved = await backend.resolve_user_identity(
                        owner_email, owner_display.lower()
                    )
                    if resolved:
                        home = await backend.get_user_home(resolved)
                        if home and home.browser_url:
                            project["cloud_storage_url"] = home.browser_url
            except Exception as e:
                logger.warning(
                    f"Failed to resolve user-home URL for default project "
                    f"{project_id}: {e}"
                )
            if not project["cloud_storage_url"]:
                project["cloud_storage_url"] = backend.get_default_home_browser_url()
        else:
            handle_str = project.get("main_cloud_folder_handle")
            legacy_folder_id = project.get("nextcloud_folder_id")
            if handle_str or legacy_folder_id:
                handle = ProjectFolderHandle.from_db(
                    handle_str or str(legacy_folder_id),
                    backend=project.get("main_cloud_backend") or backend.backend_id,
                )
                # Legacy Nextcloud handles were backfilled without vendor_meta;
                # re-attach the mountpoint from the project name so the URL
                # builder has what it needs.
                if not handle.vendor_meta.get("mountpoint"):
                    handle = ProjectFolderHandle(
                        backend=handle.backend,
                        native_id=handle.native_id,
                        vendor_meta={
                            **handle.vendor_meta,
                            "mountpoint": project["name"],
                        },
                    )
                project["cloud_storage_url"] = backend.get_project_folder_browser_url(
                    handle
                )

    return project


@app.patch("/api/projects/{project_id}")
async def update_project(
    project_id: str, body: ProjectUpdate, request: Request
) -> dict[str, str]:
    """Update a project. Caller must be a project owner or admin."""
    # H5: pre-fix, anyone could rename any project, change its goal, or
    # toggle cloud-storage settings.
    await require_project_owner(request, postgres_db, project_id)
    kwargs = {k: v for k, v in body.model_dump().items() if v is not None}
    if not kwargs:
        raise HTTPException(status_code=400, detail="No fields to update")
    # network_tier is admin-gated: a tier change widens what the project's
    # workspaces can reach at the pod-network layer (e.g. home-allowed
    # exposes the homelab LAN). Letting any project owner choose their own
    # tier defeats the operator-side control plane. See
    # docs/features/workspace_network_isolation.md §3.
    if body.network_tier is not None:
        await _require_admin(request)
    success = await postgres_db.update_project(project_id, **kwargs)
    if not success:
        raise HTTPException(status_code=404, detail=f"Project '{project_id}' not found")

    # Sync cloud_storage_read_only to the project-scoped WebDAV datasource
    if body.cloud_storage_read_only is not None:
        try:
            project_ds = await postgres_db.list_project_datasources(project_id)
            for ds in project_ds:
                if ds["type"] == "webdav":
                    await postgres_db.link_datasource_to_project(
                        project_id=project_id,
                        datasource_id=str(ds["id"]),
                        read_only=body.cloud_storage_read_only,
                    )
                    break
        except Exception as e:
            logger.warning(
                f"Failed to sync cloud_storage_read_only to datasource "
                f"for project {project_id}: {e}"
            )

    return {"status": "updated"}


@app.delete("/api/projects/{project_id}")
async def delete_project(project_id: str, request: Request) -> dict[str, str]:
    """Delete a project. Caller must be a project owner or admin. Cannot delete default projects."""
    # H5: pre-fix, anyone could cascade-delete any project (repos,
    # Keycloak groups, cloud folders, knowledge index, ...).
    _, project = await require_project_owner(request, postgres_db, project_id)
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
            await conn.execute(
                "DELETE FROM knowledge_index WHERE project_id = $1", UUID(project_id)
            )
    except Exception as e:
        logger.warning(
            f"Failed to clean up knowledge_index for project {project_id}: {e}"
        )

    # Detach referencing rows that lack ON DELETE CASCADE/SET NULL
    uuid_val = UUID(project_id)
    async with postgres_db.acquire() as conn:
        await conn.execute(
            "UPDATE jobs SET project_id = NULL WHERE project_id = $1", uuid_val
        )
        await conn.execute(
            "UPDATE datasources SET project_id = NULL WHERE project_id = $1", uuid_val
        )

    success = await postgres_db.delete_project(project_id)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to delete project")

    # Clean up Keycloak group
    if keycloak_groups.is_initialized:
        await keycloak_groups.delete_project_group(project_id)

    # Clean up main-cloud project folder via the row's backend dispatch
    backend = main_cloud_router.for_project(project)
    handle_str = project.get("main_cloud_folder_handle") or (
        str(project["nextcloud_folder_id"])
        if project.get("nextcloud_folder_id")
        else None
    )
    if handle_str and backend.is_initialized:
        try:
            handle = ProjectFolderHandle.from_db(
                handle_str,
                backend=project.get("main_cloud_backend") or backend.backend_id,
            )
            await backend.delete_project_folder(handle)
        except Exception as e:
            logger.warning(
                f"Failed to delete main-cloud folder for project {project_id}: {e}"
            )

    return {"status": "deleted"}


@app.get("/api/projects/{project_id}/members")
async def list_project_members(
    request: Request, project_id: str
) -> list[dict[str, Any]]:
    """List members of a project with user info."""
    await require_project_member(request, postgres_db, project_id)
    return await postgres_db.get_project_members(project_id)


@app.post("/api/projects/{project_id}/members")
async def add_project_member(
    project_id: str, body: ProjectMemberAdd, request: Request
) -> dict[str, Any]:
    """Add a member to a project. Caller must be a project owner or admin."""
    # H3: pre-fix, anyone could invite themselves as owner of any project
    # and then access everything in it. This is the foundational
    # privilege-escalation path that opens every other gate.
    _, project = await require_project_owner(request, postgres_db, project_id)
    try:
        result = await postgres_db.add_project_member(
            project_id=project_id,
            user_id=body.user_id,
            role=body.role,
        )

        # Sync to Keycloak project group AND the main-cloud LibreGraph group.
        # Both writes are load-bearing — see _sync_project_member_to_groups.
        user = await postgres_db.get_user(body.user_id)
        if user:
            backend = main_cloud_router.for_project(project)
            await _sync_project_member_to_groups(
                project_id, f"project-{project_id}", user, backend
            )

        # Grant Gitea access to all managed project repos
        if gitea_client.is_initialized:
            try:
                if user and user.get("email"):
                    repos = await postgres_db.get_project_repositories(project_id)
                    for repo in repos:
                        if repo.get("is_managed"):
                            await gitea_client.grant_user_repo_access(
                                user["email"], repo["name"]
                            )
            except Exception as e:
                logger.warning(
                    f"Failed to grant Gitea access for member {body.user_id}: {e}"
                )

        return result
    except Exception as e:
        if "duplicate key" in str(e).lower() or "unique" in str(e).lower():
            raise HTTPException(status_code=409, detail="User is already a member")
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.patch("/api/projects/{project_id}/members/{user_id}")
async def update_project_member(
    project_id: str, user_id: str, body: ProjectMemberUpdate, request: Request
) -> dict[str, str]:
    """Update a member's role in a project. Caller must be a project owner or admin."""
    # H3: role changes are sensitive — restrict to owners/admins.
    await require_project_owner(request, postgres_db, project_id)
    success = await postgres_db.update_project_member_role(
        project_id=project_id, user_id=user_id, role=body.role
    )
    if not success:
        raise HTTPException(status_code=404, detail="Member not found")
    return {"status": "updated"}


@app.delete("/api/projects/{project_id}/members/{user_id}")
async def remove_project_member(
    project_id: str, user_id: str, request: Request
) -> dict[str, str]:
    """Remove a member from a project. Owner/admin can remove anyone; any member can remove themselves. Cannot remove the last owner."""
    # H3: pre-fix, anyone could remove anyone (only the last-owner check
    # was enforced). Allow self-removal so members can leave projects.
    caller = await require_approved_user(request, postgres_db)
    if str(caller["id"]) != str(user_id) and not caller.get("is_admin"):
        caller_role = await postgres_db.get_user_role_in_project(
            project_id, str(caller["id"])
        )
        if caller_role != "owner":
            raise HTTPException(status_code=403, detail="Project owner role required")

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

    # Revoke Gitea access from managed project repos
    if gitea_client.is_initialized:
        try:
            removed_user = await postgres_db.get_user(user_id)
            if removed_user and removed_user.get("email"):
                repos = await postgres_db.get_project_repositories(project_id)
                for repo in repos:
                    if repo.get("is_managed"):
                        await gitea_client.revoke_user_repo_access(
                            removed_user["email"], repo["name"]
                        )
        except Exception as e:
            logger.warning(f"Failed to revoke Gitea access for member {user_id}: {e}")

    # Main-cloud group removal flows through Keycloak — OpenCloud reconciles
    # LibreGraph memberships from the OIDC `groups` claim on login, so the
    # Keycloak-side remove above is sufficient. No direct backend.remove call
    # needed.

    return {"status": "removed"}


@app.get("/api/projects/{project_id}/repositories")
async def list_project_repositories(
    request: Request,
    project_id: str,
    role: str | None = Query(default=None),
) -> list[dict[str, Any]]:
    """List repositories attached to a project."""
    await require_project_member(request, postgres_db, project_id)
    return await postgres_db.get_project_repositories(project_id, role=role)


@app.post("/api/projects/{project_id}/repositories")
async def add_project_repository(
    request: Request, project_id: str, body: ProjectRepositoryCreate
) -> dict[str, Any]:
    """Attach a repository to a project. Owner or admin only (creates managed Gitea repo)."""
    await require_project_owner(request, postgres_db, project_id)

    repo_url = body.repo_url
    is_managed = False

    # Create a managed Gitea repo if requested
    if body.create_managed and gitea_client.is_initialized:
        repo_url = await gitea_client.create_repo(body.name)
        if not repo_url:
            raise HTTPException(
                status_code=502, detail="Failed to create Gitea repository"
            )
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
    request: Request, project_id: str, repo_id: str, body: ProjectRepositoryUpdate
) -> dict[str, str]:
    """Update a project repository. Owner or admin only."""
    await require_project_owner(request, postgres_db, project_id)
    kwargs = {k: v for k, v in body.model_dump().items() if v is not None}
    if not kwargs:
        raise HTTPException(status_code=400, detail="No fields to update")
    success = await postgres_db.update_project_repository(repo_id, **kwargs)
    if not success:
        raise HTTPException(status_code=404, detail="Repository not found")
    return {"status": "updated"}


@app.delete("/api/projects/{project_id}/repositories/{repo_id}")
async def remove_project_repository(
    request: Request, project_id: str, repo_id: str
) -> dict[str, str]:
    """Remove a repository from a project. Owner or admin only. Cannot remove the jobs repo."""
    await require_project_owner(request, postgres_db, project_id)
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


# -- Project Datasources (N:M) -----------------------------------------------


@app.get("/api/projects/{project_id}/datasources")
async def list_project_datasources(
    request: Request, project_id: str
) -> list[dict[str, Any]]:
    """List datasources linked to a project. F3: project membership required."""
    await require_project_member(request, postgres_db, project_id)
    try:
        rows = await postgres_db.list_project_datasources(project_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    return redact_datasources(rows)


@app.post("/api/projects/{project_id}/datasources/{datasource_id}")
async def link_datasource_to_project(
    request: Request,
    project_id: str,
    datasource_id: str,
    body: ProjectDatasourceSettings | None = None,
) -> dict[str, str]:
    """Link an existing datasource to a project.

    F3: caller must be project owner of the target project AND must be
    able to see the datasource (admin / creator / member of one of its
    projects). Prevents a project owner from probing for stranger
    datasources by guessing UUIDs.

    Optionally pass project-level overrides (read_only, description).
    Also creates a knowledge entry so agents discover the datasource.
    """
    user, _ = await require_project_owner(request, postgres_db, project_id)
    ds = await postgres_db.get_datasource(datasource_id)
    if not ds:
        raise HTTPException(
            status_code=404, detail=f"Datasource '{datasource_id}' not found"
        )
    if not await user_can_access_datasource(user, postgres_db, ds):
        raise HTTPException(
            status_code=403, detail="Not authorized to link this datasource"
        )

    try:
        await postgres_db.link_datasource_to_project(
            project_id,
            datasource_id,
            read_only=body.read_only if body else None,
            description=body.description if body else None,
        )
        # Build effective datasource dict for KB entry (apply overrides)
        effective_ds = dict(ds)
        if body:
            if body.read_only is not None:
                effective_ds["project_read_only"] = body.read_only
            if body.description is not None:
                effective_ds["description"] = body.description
        await _sync_datasource_knowledge(project_id, effective_ds)
        return {"status": "linked"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.patch("/api/projects/{project_id}/datasources/{datasource_id}")
async def update_project_datasource(
    request: Request,
    project_id: str,
    datasource_id: str,
    body: ProjectDatasourceSettings,
) -> dict[str, str]:
    """Update project-level settings for a linked datasource. F3: project owner only.

    Pass null to clear an override and fall back to datasource defaults.
    """
    await require_project_owner(request, postgres_db, project_id)
    success = await postgres_db.update_project_datasource(
        project_id,
        datasource_id,
        read_only=body.read_only,
        description=body.description,
    )
    if not success:
        raise HTTPException(
            status_code=404,
            detail=f"Link between project '{project_id}' and datasource '{datasource_id}' not found",
        )

    # Re-sync knowledge entry with updated overrides
    ds = await postgres_db.get_datasource(datasource_id)
    if ds:
        effective_ds = dict(ds)
        if body.read_only is not None:
            effective_ds["project_read_only"] = body.read_only
        if body.description is not None:
            effective_ds["description"] = body.description
        await _sync_datasource_knowledge(project_id, effective_ds)

    return {"status": "updated"}


@app.delete("/api/projects/{project_id}/datasources/{datasource_id}")
async def unlink_datasource_from_project(
    request: Request, project_id: str, datasource_id: str
) -> dict[str, str]:
    """Unlink a datasource from a project. F3: project owner only.

    Also removes the knowledge entry.
    """
    await require_project_owner(request, postgres_db, project_id)
    removed = await postgres_db.unlink_datasource_from_project(
        project_id, datasource_id
    )
    if not removed:
        raise HTTPException(
            status_code=404,
            detail=f"Link between project '{project_id}' and datasource '{datasource_id}' not found",
        )

    await _delete_datasource_knowledge(project_id, datasource_id)
    return {"status": "unlinked"}


@app.post("/api/projects/{project_id}/jobs")
async def create_project_job(
    request: Request, project_id: str, job: JobCreate
) -> dict[str, Any]:
    """Create a job within a project — delegates to create_job. Requires editor or higher."""
    await require_project_member(request, postgres_db, project_id, min_role="editor")
    job.project_id = project_id
    return await create_job(request, job)


@app.get("/api/projects/{project_id}/jobs")
async def list_project_jobs(
    request: Request,
    project_id: str,
    status: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[dict[str, Any]]:
    """List jobs belonging to a project."""
    await require_project_member(request, postgres_db, project_id)
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

        # cloud_review_mode: all rows share one project, so resolve its
        # cloud-folder state once (the job_summary view has no projects JOIN).
        project = await postgres_db.get_project(project_id)
        has_cloud_folder = bool(project and project.get("main_cloud_folder_handle"))

        # Enrich with audit counts (single batched query, not N+1)
        if audit_reader.is_available:
            counts = await audit_reader.get_audit_counts(
                [str(job["id"]) for job in jobs]
            )
            for job in jobs:
                job["audit_count"] = counts.get(str(job["id"]), 0)
        else:
            for job in jobs:
                job["audit_count"] = None

        result = []
        for job in jobs:
            job = _redact_job_config_override(job)
            job["project_has_cloud_folder"] = has_cloud_folder
            result.append(_with_cloud_review_mode(job))
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/api/jobs/{job_id}/promote")
async def promote_job(
    request: Request,
    job_id: str,
    body: PromoteRequest,
) -> dict[str, Any]:
    """Promote a default-project job into a dedicated project.

    Creates a new project, seeds its jobs repo from the job's branch content
    (preserving git history), and moves the job to the new project.

    P4c: ``body.user_id`` is forced to the caller (mirrors F2 — no cross-user
    promotion).
    """
    caller, _ = await require_job_access(request, postgres_db, job_id)
    body.user_id = caller["id"]
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
            name=body.name,
            description=body.description,
            goal=body.goal,
        )
        new_project_id = str(new_project["id"])

        # Add user as owner
        await postgres_db.add_project_member(
            project_id=new_project_id,
            user_id=body.user_id,
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
                        [
                            "git",
                            "clone",
                            "--branch",
                            branch_name,
                            old_repo_url,
                            clone_path,
                        ],
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
                            logger.error(
                                f"Promote: clone fallback also failed: {result.stderr[:200]}"
                            )

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


def _build_datasource_note_content(ds: dict[str, Any]) -> str:
    """Build markdown content for a datasource knowledge entry.

    Content varies by type and access mode:
    - generic: lists env var names + CLI hint
    - repository: cloned path + git usage
    - managed connectors (read-write): CLI tool + env vars
    - managed connectors (read-only): available tools list
    - webdav: always tools
    """
    ds_type = ds.get("type", "unknown")
    ds_name = ds.get("name", "Unnamed")
    desc = ds.get("description") or ""
    is_read_only = ds.get("project_read_only", False)

    if ds_type == "generic":
        return _build_generic_note(ds_name, desc, ds)
    elif ds_type == "repository":
        return _build_repository_note(ds_name, desc, ds)
    elif ds_type == "webdav":
        return _build_webdav_note(ds_name, desc, is_read_only)
    elif ds_type in ("postgresql", "neo4j", "mongodb"):
        if is_read_only:
            return _build_managed_readonly_note(ds_name, desc, ds_type)
        else:
            return _build_managed_readwrite_note(ds_name, desc, ds_type)
    else:
        return f"## Datasource: {ds_name}\n{desc}"


def _build_generic_note(name: str, desc: str, ds: dict) -> str:
    """KB entry for generic datasources."""
    lines = [f"## Datasource: {name}"]
    if desc:
        lines.append(desc)

    url = ds.get("connection_url")
    cli_hint = ds.get("cli_hint")
    if url or cli_hint:
        lines.append("\n### Connection")
        if url:
            lines.append(f"- **URL:** {url} (credentials via env vars)")
        if cli_hint:
            lines.append(f"- **CLI:** `{cli_hint}`")

    creds = ds.get("credentials") or {}
    if isinstance(creds, str):
        try:
            creds = json.loads(creds)
        except (json.JSONDecodeError, ValueError):
            creds = {}
    env_vars = creds.get("env_vars", {})
    if env_vars:
        lines.append("\n### Environment Variables")
        for key in env_vars:
            lines.append(f"- `{key}` — available in workspace")

    return "\n".join(lines)


def _build_repository_note(name: str, desc: str, ds: dict) -> str:
    """KB entry for repository datasources."""
    import re

    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    lines = [f"## Repository: {name}"]
    if desc:
        lines.append(desc)
    lines.append("\n### Location")
    lines.append(f"Cloned to `./repos/{slug}/` — git is pre-authenticated.")
    lines.append("\n### Usage")
    lines.append("Use standard git commands:")
    lines.append(f"- `cd repos/{slug} && git status`")
    lines.append("- `git pull`, `git commit`, `git push`")
    lines.append("- No login or credential setup required.")
    branch = ds.get("default_branch")
    if branch:
        lines.append(f"- Default branch: `{branch}`")
    return "\n".join(lines)


def _build_managed_readwrite_note(name: str, desc: str, ds_type: str) -> str:
    """KB entry for managed connectors in read-write (CLI) mode."""
    cli_info = {
        "postgresql": {
            "tool": "psql",
            "env_vars": "`PGHOST`, `PGUSER`, `PGPASSWORD`, `PGDATABASE`",
            "examples": [
                '`psql -c "SELECT * FROM users LIMIT 10"`',
                '`psql -c "CREATE TABLE ..."`',
            ],
        },
        "neo4j": {
            "tool": "cypher-shell",
            "env_vars": "`NEO4J_URI`, `NEO4J_USERNAME`, `NEO4J_PASSWORD`",
            "examples": [
                '`cypher-shell "MATCH (n) RETURN n LIMIT 10"`',
                "`cypher-shell \"CREATE (n:Label {name: 'test'})\"`",
            ],
        },
        "mongodb": {
            "tool": "mongosh",
            "env_vars": "`MONGOSH_URI`",
            "examples": [
                '`mongosh --eval "db.users.find().limit(10)"`',
                '`mongosh --eval "db.users.insertOne({...})"`',
            ],
        },
    }
    info = cli_info.get(ds_type, {})
    lines = [
        f"## Datasource: {name}",
        f"**Type:** {ds_type} | **Access:** full (CLI)",
    ]
    if desc:
        lines.append(f"\n{desc}")
    lines.append("\n### Connection")
    lines.append(
        f"Use `{info.get('tool', ds_type)}` to connect — credentials are pre-configured via environment variables."
    )
    lines.append("\n### Environment Variables")
    lines.append(
        f"- {info.get('env_vars', 'Check environment for connection details')} — pre-configured"
    )
    if info.get("examples"):
        lines.append("\n### Examples")
        for ex in info["examples"]:
            lines.append(f"- {ex}")
    return "\n".join(lines)


def _build_managed_readonly_note(name: str, desc: str, ds_type: str) -> str:
    """KB entry for managed connectors in read-only (tools) mode."""
    tool_info = {
        "postgresql": [
            "- `sql_query` — execute SELECT queries",
            "- `sql_schema` — inspect tables, columns, types, constraints",
        ],
        "neo4j": [
            "- `cypher_query` — execute read-only Cypher queries",
            "- `cypher_execute` — execute write Cypher statements (CREATE, MERGE, DELETE, SET)",
            "- `get_database_schema` — inspect labels, relationships, properties",
        ],
        "mongodb": [
            "- `mongo_query` — document queries with filters",
            "- `mongo_aggregate` — aggregation pipelines",
            "- `mongo_schema` — collections, fields, indexes",
        ],
    }
    tools = tool_info.get(ds_type, ["- Check available tools for this datasource type"])
    lines = [
        f"## Datasource: {name}",
        f"**Type:** {ds_type} | **Access:** read-only (tools)",
    ]
    if desc:
        lines.append(f"\n{desc}")
    lines.append("\n### Available Tools")
    lines.extend(tools)
    lines.append("\nNo CLI access or write operations available.")
    return "\n".join(lines)


def _build_webdav_note(name: str, desc: str, is_read_only: bool) -> str:
    """KB entry for WebDAV datasources (always tools)."""
    access = "read-only" if is_read_only else "read-write"
    lines = [
        f"## Datasource: {name}",
        f"**Type:** webdav | **Access:** {access}",
    ]
    if desc:
        lines.append(f"\n{desc}")
    lines.append("\n### Available Tools")
    lines.append("- `webdav_list` — list files and directories")
    lines.append("- `webdav_read` — read file contents")
    lines.append("- `webdav_info` — get file metadata")
    if not is_read_only:
        lines.append("- `webdav_write` — write/upload files")
        lines.append("- `webdav_delete` — delete files")
    return "\n".join(lines)


async def _sync_datasource_knowledge(
    project_id: str, datasource: dict[str, Any]
) -> None:
    """Create or update a knowledge entry for a datasource in a project."""
    ds_id = str(datasource["id"]).replace("-", "")[:8]
    note_id = f"ds-{ds_id}"
    ds_name = datasource.get("name", "Unnamed")
    ds_type = datasource.get("type", "unknown")
    content = _build_datasource_note_content(datasource)

    if ds_type == "repository":
        retrieval_messages = [
            f"{ds_name} repository",
            f"git repo {ds_name}",
            f"How to access {ds_name} code",
            "available repositories",
        ]
    elif ds_type == "generic":
        retrieval_messages = [
            f"{ds_name} connection",
            f"How to access {ds_name}",
            "available datasources",
        ]
    else:
        retrieval_messages = [
            f"{ds_name} database connection",
            f"{ds_type} access",
            f"How do I connect to {ds_name}?",
            "What databases are available?",
        ]

    # Write to Neo4j (upsert with deterministic note_id)
    kg = _get_knowledge_graph()
    if kg:
        try:
            title = f"Datasource: {ds_name} ({ds_type})"
            from datetime import datetime, timezone

            now = datetime.now(timezone.utc).isoformat()
            kg._db.execute_write(
                """
                MERGE (n:Note {project_id: $pid, id: $nid})
                ON CREATE SET
                    n.type = 'datasource',
                    n.title = $title,
                    n.content = $content,
                    n.status = 'active',
                    n.confidence = 'high',
                    n.retrieval_messages = $retrieval_messages,
                    n.created = datetime($now),
                    n.modified = datetime($now)
                ON MATCH SET
                    n.title = $title,
                    n.content = $content,
                    n.retrieval_messages = $retrieval_messages,
                    n.modified = datetime($now)
                """,
                {
                    "pid": project_id,
                    "nid": note_id,
                    "title": title,
                    "content": content,
                    "retrieval_messages": retrieval_messages,
                    "now": now,
                },
            )
            # Ensure tags exist
            for tag_name in ["datasource", ds_type]:
                kg._db.execute_write(
                    """
                    MATCH (n:Note {project_id: $pid, id: $nid})
                    MERGE (t:Tag {name: $tag, project_id: $pid})
                    MERGE (n)-[:TAGGED]->(t)
                    """,
                    {"pid": project_id, "nid": note_id, "tag": tag_name},
                )
        except Exception as e:
            logger.warning(f"Neo4j datasource knowledge sync failed: {e}")

    # Write to pgvector search index
    try:
        async with vector_db.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO knowledge_index (
                    note_id, project_id, title, note_type, status,
                    confidence, tags, content, retrieval_messages, modified_at
                ) VALUES ($1, $2::uuid, $3, 'datasource', 'active', 'high',
                          $4::text[], $5, $6::text[], NOW())
                ON CONFLICT (project_id, note_id) DO UPDATE SET
                    title = EXCLUDED.title,
                    content = EXCLUDED.content,
                    retrieval_messages = EXCLUDED.retrieval_messages,
                    tags = EXCLUDED.tags,
                    modified_at = NOW(),
                    content_hash = NULL
                """,
                note_id,
                project_id,
                f"Datasource: {ds_name} ({ds_type})",
                ["datasource", ds_type],
                content,
                retrieval_messages,
            )
    except Exception as e:
        logger.warning(f"pgvector datasource knowledge sync failed: {e}")


async def _delete_datasource_knowledge(project_id: str, datasource_id: str) -> None:
    """Remove the knowledge entry for a datasource from a project."""
    ds_id = datasource_id.replace("-", "")[:8]
    note_id = f"ds-{ds_id}"

    kg = _get_knowledge_graph()
    if kg:
        try:
            kg._db.execute_write(
                "MATCH (n:Note {project_id: $pid, id: $nid}) DETACH DELETE n",
                {"pid": project_id, "nid": note_id},
            )
        except Exception as e:
            logger.warning(f"Neo4j datasource knowledge delete failed: {e}")

    try:
        async with vector_db.acquire() as conn:
            await conn.execute(
                "DELETE FROM knowledge_index WHERE project_id = $1::uuid AND note_id = $2",
                project_id,
                note_id,
            )
    except Exception as e:
        logger.warning(f"pgvector datasource knowledge delete failed: {e}")


@app.get("/api/projects/{project_id}/knowledge/summary")
async def get_knowledge_summary(request: Request, project_id: str) -> dict[str, Any]:
    """Get knowledge base summary statistics for a project. F5: member-only."""
    await require_project_member(request, postgres_db, project_id)

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
    request: Request,
    project_id: str,
    note_type: str | None = Query(default=None, alias="type"),
    status: str | None = Query(default=None),
    tag: str | None = Query(default=None),
    job_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """List knowledge notes for a project with optional filters. F5: member-only."""
    await require_project_member(request, postgres_db, project_id)

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
async def get_knowledge_note(
    request: Request, project_id: str, note_id: str
) -> dict[str, Any]:
    """Get a single knowledge note with full content. F5: member-only."""
    await require_project_member(request, postgres_db, project_id)
    try:
        async with vector_db.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM knowledge_index WHERE project_id = $1 AND note_id = $2",
                project_id,
                note_id,
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
    request: Request,
    project_id: str,
    body: KnowledgeSearchRequest,
) -> dict[str, Any]:
    """Hybrid search over project knowledge base. F5: member-only."""
    await require_project_member(request, postgres_db, project_id)

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
                    body.query,
                    str(embedding),
                    project_id,
                    body.limit,
                )
            else:
                # Sparse-only fallback: tsvector keyword search
                rows = await conn.fetch(
                    "SELECT * FROM knowledge_index "
                    "WHERE project_id = $1 AND search_doc @@ websearch_to_tsquery($2) "
                    "ORDER BY ts_rank_cd(search_doc, websearch_to_tsquery($2)) DESC "
                    "LIMIT $3",
                    project_id,
                    body.query,
                    body.limit,
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
    request: Request,
    project_id: str,
    note_id: str,
    body: KnowledgeNoteUpdate,
) -> dict[str, str]:
    """Update a knowledge note's status or tags. F5: member-only."""
    await require_project_member(request, postgres_db, project_id)
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
                project_id,
                note_id,
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
async def delete_knowledge_note(
    request: Request, project_id: str, note_id: str
) -> dict[str, str]:
    """Hard delete a knowledge note from both stores. F5: member-only."""
    await require_project_member(request, postgres_db, project_id)
    try:
        # Delete from vector DB
        async with vector_db.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM knowledge_index WHERE project_id = $1 AND note_id = $2",
                project_id,
                note_id,
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
async def export_knowledge(request: Request, project_id: str) -> dict[str, Any]:
    """Export project knowledge base as Obsidian-compatible markdown files.

    F5: member-only. Same access requirement as the per-note read endpoint
    — the bulk export is equivalent to scraping the list and getting each
    note individually, so a tighter gate wouldn't close a real gap.
    """
    _, project = await require_project_member(request, postgres_db, project_id)

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
