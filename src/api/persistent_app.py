"""FastAPI application for Persistent Agent mode.

Provides WebSocket endpoint for interactive sessions. Completely separate
from app.py (worker mode) — own globals, own lifespan, no shared state.

Start with: python agent.py --mode persistent --thread-id <uuid> --port 8001
Connect with: websocat ws://localhost:8001/ws/chat
"""

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from .orchestrator_client import OrchestratorClient, create_orchestrator_client_from_env
from .persistent_session import PersistentSession
from ..tools.registry import TOOL_REGISTRY
from ..agent import UniversalAgent
from ..persistent_graph import (
    IdleTimeoutError,
    PersistentLoopCallbacks,
    run_persistent_loop,
)

logger = logging.getLogger(__name__)

# --- Module globals ---
# Singleton layer (lives for the entire process lifetime):
_agent: Optional[UniversalAgent] = None
_config_path: Optional[str] = None
_orchestrator_client: Optional[OrchestratorClient] = None
_heartbeat_task: Optional[asyncio.Task] = None
_started_at: Optional[datetime] = None

# Session layer (created/destroyed per thread assignment):
_session: Optional[PersistentSession] = None
_thread_id: Optional[str] = None

# Pool mode: agent can be reused across sessions (Docker Compose mode)
_sessions_served: int = 0
_max_sessions_per_process: int = int(
    os.environ.get("MAX_SESSIONS_PER_PROCESS", "0")
)  # 0 = unlimited

# Pod exit scheduling
_pending_exit_task: Optional[asyncio.Task] = None

# Drain intent — set the first time the orchestrator's heartbeat response
# carries ``intents.should_drain=true``. Drives a one-shot detach + exit so
# the agent doesn't keep reacting on every subsequent heartbeat.
_drain_intent_handled: bool = False

# Self-cleanup watchdogs (PR 2 — protect against the abandoned-pod failure modes
# that the orchestrator reconciler can only catch with a 60s+ delay):
#   _ws_connected_event  → set when /ws/chat first accepts a connection.
#   _watchdog_tasks      → background tasks cancelled on detach/shutdown.
_ws_connected_event: Optional[asyncio.Event] = None
_watchdog_tasks: list[asyncio.Task] = []

# Reference to the currently running persistent-loop task. Set by ws_chat when
# it spawns the loop, cleared when _terminate_session runs. _terminate_session()
# cancels and awaits it before nulling _session, so out-of-band callers
# (heartbeat intents, thread-status watchdog, drain) can't race the in-flight
# turn into a NoneType.permission_mode crash. See
# docs/issues/persistent_session_permission_check_race.md.
#
# Headless sessions (chunk 1): the loop now outlives any single WebSocket. It is
# only cancelled by _terminate_session, never by WS close.
_loop_task: Optional[asyncio.Task] = None
_session_boot_ws_timeout_s: int = int(
    os.environ.get("SESSION_BOOT_WS_TIMEOUT_S", "600")
)
_thread_status_poll_s: int = int(os.environ.get("THREAD_STATUS_POLL_S", "60"))

# Subscriber registry for headless persistent sessions.
#
# Loop-driven output (token chunks, tool events, turn lifecycle, etc.) used to
# be sent directly to a single WebSocket scoped to ws_chat. Under headless
# semantics the loop must outlive any single WS attach, so the loop instead
# broadcasts via _broadcast() and each WebSocket connection registers its own
# queue via _subscribe(). A _run_subscriber_pump task drains each queue into
# its WS. Closing a WS just calls _unsubscribe() — the loop keeps running.
#
# Keyed by client_id (generated server-side per WS connection). Bounded queue
# protects the loop from a slow consumer: on overflow the oldest frame is
# dropped (token-stream pacing semantics).
_SUBSCRIBER_QUEUE_MAXSIZE: int = 1000
_subscribers: Dict[str, asyncio.Queue] = {}

# Loop-facing input primitives. Used to be closure-scoped inside ws_chat;
# hoisted to module level so they survive WS reconnect. All three are reset
# on session attach / cleared on _terminate_session.
_loop_user_queue: Optional[asyncio.Queue] = None
# Tri-state interrupt flag (phase 2): None = no interrupt pending,
# "graceful" = stop after current tool call completes, "hard" = cancel the
# in-flight LLM stream immediately and drop the partial AIMessage. Set by
# the agent's POST /api/interrupt handler based on current _tool_inflight
# state. Consumed by persistent_graph's check_interrupt callback at three
# sites (pre-LLM, mid-astream, between tool calls). Legacy WS interrupt
# path uses the same flag — sets "hard" when no tool is inflight.
_loop_interrupt_flag: Optional[str] = None
_loop_last_user_content: List[str] = [""]

# True while a tool call is mid-`ainvoke`. Read by POST /api/interrupt to
# pick hard vs graceful mode. Set in _loop_on_tool_start, cleared in
# _loop_on_tool_result.
_tool_inflight: bool = False

# Phase 2 event-log cursor. Allocated synchronously by _broadcast; the DB
# write is scheduled via asyncio.create_task (fire-and-forget). Initialized
# in _attach_session with epoch bump on cold restart; cleared on terminate.
_events_epoch: int = 0
_next_seq: int = 0


async def _handle_heartbeat_intents(response: dict[str, Any]) -> None:
    """Heartbeat-response callback: react to orchestrator-set intents.

    Currently only ``should_drain`` triggers anything — when set, the
    persistent agent detaches its session (which marks the thread
    ``ended`` so any active WS gets a normal close) and exits the pod.
    Idempotent: only fires once per process; later heartbeats observing
    the same intent are no-ops.
    """
    global _drain_intent_handled
    if _drain_intent_handled:
        return
    intents = response.get("intents") or {}
    if not isinstance(intents, dict):
        return
    if not intents.get("should_drain"):
        return
    _drain_intent_handled = True
    logger.info(
        "Drain intent received from orchestrator (reason=%s) — detaching and exiting",
        intents.get("drain_reason", "unspecified"),
    )
    try:
        await _terminate_session("drain")
    except Exception as e:
        logger.warning(f"Detach during drain-intent handling failed: {e}")
    _schedule_exit(delay=1.0)


def _schedule_exit(delay: float = 1.0) -> None:
    """Schedule process exit after a short delay (allows final I/O to flush)."""
    global _pending_exit_task

    if _pending_exit_task and not _pending_exit_task.done():
        _pending_exit_task.cancel()

    async def _exit():
        await asyncio.sleep(delay)
        logger.info("Session complete — exiting process")
        os._exit(0)

    _pending_exit_task = asyncio.create_task(_exit())


# ---------------------------------------------------------------------------
# Self-cleanup watchdogs (PR 2)
# ---------------------------------------------------------------------------


async def _boot_ws_watchdog(timeout_s: int) -> None:
    """Exit if no /ws/chat connection arrives within ``timeout_s`` of attach.

    A persistent agent that boots, attaches to a thread, then never receives
    a WebSocket has no other way to know it's been abandoned (e.g. user
    navigated away during creation). Without this watchdog the pod sits
    forever heartbeating and holding a slot. The orchestrator reconciler
    catches this too, but only after a 60s+ delay; this watchdog kills
    locally on the configured cadence.
    """
    if _ws_connected_event is None:
        return
    try:
        await asyncio.wait_for(_ws_connected_event.wait(), timeout=timeout_s)
        return  # WS arrived — normal lifecycle takes over
    except asyncio.TimeoutError:
        logger.warning(
            "No WebSocket connection within %ds for thread %s — "
            "exiting (likely abandoned during creation).",
            timeout_s,
            _thread_id,
        )
    try:
        await _terminate_session("boot_ws_timeout")
    except Exception as e:
        logger.warning(f"Detach during boot-WS timeout failed: {e}")
    _schedule_exit(delay=1.0)


async def _thread_status_watchdog(poll_s: int) -> None:
    """Exit if the bound thread transitions to a terminal state out-of-band.

    The orchestrator's stale_agent_detector can flip a thread to 'ended'
    via ``mark_orphaned_threads_ended`` or release the binding via
    ``mark_stuck_session_agents_ready`` (PR 1). When that happens this pod
    is orphaned — no work to do, holding a slot.

    'awaiting_user' is the eager-mode transient idle state set by this same
    agent's loop on natural pause with no subscribers (Phase 5,
    ``_loop_get_user_input``). It is NOT a terminal state — the orchestrator's
    attention-sleep watchdog owns the eventual ``awaiting_user → suspended``
    transition and we mustn't pre-empt it from here, or we kill the very
    untethered-survival behaviour Phase 1 + Phase 5 were built to enable.

    'suspended' means the orchestrator has already snapshotted + deleted the
    workspace pod — at that point we're a stranded agent with no workspace,
    so we exit.
    """
    while True:
        try:
            await asyncio.sleep(poll_s)
        except asyncio.CancelledError:
            raise
        if not _orchestrator_client or not _thread_id:
            continue
        try:
            lifecycle = await _orchestrator_client.get_thread_lifecycle(_thread_id)
        except Exception as e:
            logger.debug(f"Thread lifecycle poll failed (non-fatal): {e}")
            continue
        if not lifecycle:
            continue
        status = lifecycle.get("status")
        if status not in ("created", "active", "awaiting_user"):
            logger.info(
                "Thread %s status is '%s' — exiting (orphaned by orchestrator).",
                _thread_id,
                status,
            )
            try:
                await _terminate_session("thread_ended_oob")
            except Exception as e:
                logger.warning(f"Detach during status-watchdog exit failed: {e}")
            _schedule_exit(delay=1.0)
            return


def _start_watchdogs() -> None:
    """Start watchdog tasks for the active session. Safe to call repeatedly."""
    global _ws_connected_event, _watchdog_tasks

    # Stop any prior watchdogs (defensive — should already be cleared).
    for task in _watchdog_tasks:
        if not task.done():
            task.cancel()
    _watchdog_tasks = []

    _ws_connected_event = asyncio.Event()
    _watchdog_tasks = [
        asyncio.create_task(
            _boot_ws_watchdog(_session_boot_ws_timeout_s),
            name="boot-ws-watchdog",
        ),
        asyncio.create_task(
            _thread_status_watchdog(_thread_status_poll_s),
            name="thread-status-watchdog",
        ),
    ]


def _stop_watchdogs() -> None:
    """Cancel all active watchdogs. Skips the current task to avoid self-cancel."""
    global _watchdog_tasks
    current = asyncio.current_task()
    for task in _watchdog_tasks:
        if task is current or task.done():
            continue
        task.cancel()
    _watchdog_tasks = []


def _signal_ws_connected() -> None:
    """Signal that a WebSocket has connected. Cancels the boot-WS watchdog."""
    if _ws_connected_event is not None:
        _ws_connected_event.set()


def _get_agent_metrics() -> Optional[Dict[str, Any]]:
    """Collect metrics for heartbeat."""
    try:
        import psutil

        proc = psutil.Process()
        return {
            "memory_mb": round(proc.memory_info().rss / 1_048_576, 1),
            "cpu_percent": proc.cpu_percent(interval=0),
        }
    except Exception:
        return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize persistent agent, register with orchestrator, start heartbeat."""
    global \
        _agent, \
        _session, \
        _orchestrator_client, \
        _heartbeat_task, \
        _started_at, \
        _thread_id

    _started_at = datetime.now()
    pool_mode = _thread_id is None
    logger.info(
        f"Starting persistent agent: config={_config_path}, "
        f"thread={_thread_id or '(pool mode — waiting for assignment)'}"
    )

    # 1. Create and initialize UniversalAgent (singleton layer)
    _agent = UniversalAgent.from_config(_config_path)
    await _agent.initialize()

    # 2. Connect to orchestrator
    orchestrator_url = os.getenv("ORCHESTRATOR_URL", "http://localhost:8085")
    if orchestrator_url:
        try:
            _orchestrator_client = create_orchestrator_client_from_env(
                _agent.config.agent_id
            )
            await _orchestrator_client.connect()

            if pool_mode:
                # Pool mode: register as available, no thread yet.
                # The orchestrator will assign threads via POST /session/attach.
                await _orchestrator_client.register(
                    agent_mode="persistent",
                    thread_id=None,
                )
            else:
                # Dedicated mode: auto-create thread if needed (backwards compatible)
                if _thread_id is None:
                    created_id = await _orchestrator_client.create_thread(
                        config_name=_config_path or "persistent_defaults",
                        permission_mode=_agent.config.interactive.permission_mode,
                        title=f"Local Session ({_config_path or 'persistent_defaults'})",
                    )
                    if created_id:
                        _thread_id = created_id
                        logger.info(f"Auto-created thread: {_thread_id}")
                    else:
                        logger.warning(
                            "Failed to create thread — generating local UUID"
                        )

                if _thread_id is None:
                    import uuid

                    _thread_id = str(uuid.uuid4())

                await _orchestrator_client.register(
                    agent_mode="persistent",
                    thread_id=_thread_id,
                )

            # Start heartbeat
            def _heartbeat_status():
                return "ready" if _session is None else "session"

            _heartbeat_task = asyncio.create_task(
                _orchestrator_client.run_heartbeat_loop(
                    get_status=_heartbeat_status,
                    get_job_id=lambda: None,
                    get_metrics=_get_agent_metrics,
                    on_response=_handle_heartbeat_intents,
                )
            )
            logger.info("Registered with orchestrator as persistent agent")
        except Exception as e:
            logger.warning(f"Failed to register with orchestrator (non-fatal): {e}")
            _orchestrator_client = None
    else:
        logger.info("No ORCHESTRATOR_URL — running standalone")

    # If we have a thread_id (dedicated mode), set up the session immediately
    if _thread_id:
        # Fallback: generate UUID if still None (standalone mode)
        if _thread_id is None:
            import uuid

            _thread_id = str(uuid.uuid4())

        await _attach_session(_thread_id)
    else:
        logger.info(
            "Pool mode: waiting for session assignment via POST /session/attach"
        )

    yield

    # --- Shutdown ---
    logger.info("Shutting down persistent agent")

    # Detach any active session
    if _session:
        await _terminate_session("shutdown")

    if _orchestrator_client:
        try:
            _orchestrator_client.stop_heartbeat()
            if _heartbeat_task:
                _heartbeat_task.cancel()
                try:
                    await _heartbeat_task
                except asyncio.CancelledError:
                    pass
            await _orchestrator_client.deregister()
            await _orchestrator_client.close()
        except Exception as e:
            logger.warning(f"Orchestrator cleanup error: {e}")

    if _agent:
        await _agent.shutdown()

    logger.info("Persistent agent shutdown complete")


def _legacy_nc_cloud_cfg(nc_folder: str) -> Dict[str, Any]:
    """Translate legacy ``nc_session_folder`` + env vars into the cloud_sync schema.

    Used when the orchestrator is on a version that still returns the
    pre-refactor flat field. Drops away once the orchestrator rolls.
    """
    nc_url = os.getenv("NEXTCLOUD_URL", "http://localhost:8800")
    nc_user = os.getenv("NEXTCLOUD_AGENT_USER", "agent-service")
    nc_pass = os.getenv("NEXTCLOUD_AGENT_PASSWORD", "agent-service-dev")
    return {
        "backend": "nextcloud",
        "webdav_url": f"{nc_url.rstrip('/')}/remote.php/dav/files/{nc_user}/{nc_folder}/",
        "auth": {"type": "basic", "username": nc_user, "password": nc_pass},
    }


async def _attach_session(
    thread_id: str,
    config_override: Optional[Dict[str, Any]] = None,
    project_ids: Optional[List[str]] = None,
    datasources: Optional[List[Dict[str, Any]]] = None,
) -> None:
    """Create and attach a PersistentSession for the given thread.

    This is the core session setup logic, extracted from the lifespan so it
    can be reused by both dedicated mode (lifespan startup) and pool mode
    (POST /session/attach).
    """
    global _session, _thread_id

    if _session is not None:
        raise RuntimeError(
            f"Cannot attach thread {thread_id}: already attached to {_thread_id}"
        )

    _thread_id = thread_id

    # Wait for workspace container (if orchestrator is provisioning one)
    workspace_override = None
    if _orchestrator_client and _thread_id:
        workspace_override = await _poll_workspace_ready(
            _orchestrator_client, _thread_id, timeout=120
        )
        if workspace_override:
            logger.info(
                f"Workspace container ready: {workspace_override['remote']['host']}"
            )
        else:
            raise RuntimeError(
                "No workspace container provisioned for thread. "
                "Cannot attach session without an isolated workspace."
            )

    # Apply config overrides, project_ids, and datasources from thread metadata
    if not config_override:
        config_override = (workspace_override or {}).get("config_override")
    if not project_ids:
        project_ids = (workspace_override or {}).get("project_ids") or []
    if (
        (not config_override or not project_ids or not datasources)
        and _orchestrator_client
        and _thread_id
    ):
        try:
            ws_info = await _orchestrator_client.get_thread_workspace(_thread_id)
            if ws_info:
                if not config_override:
                    config_override = ws_info.get("config_override")
                if not project_ids:
                    project_ids = ws_info.get("project_ids") or []
                if not datasources:
                    datasources = ws_info.get("datasources")
        except Exception:
            pass

    # Process datasources: create connections, inject env vars, apply tool overrides
    # Note: repository cloning is deferred until AFTER the workspace is
    # initialized so that repos land inside the session workspace directory
    # (./workspace/job_{thread_id}/repos/) instead of the agent process CWD.
    datasources_dict: Dict[str, Any] = {}
    datasource_clients: Dict[str, Any] = {}
    repo_datasources: List[Dict[str, Any]] = []
    if datasources:
        from ..core.datasource_setup import (
            inject_datasource_index as _inject_ds_index,
            process_datasources,
        )

        # Separate repos (cloned later) from other datasources
        repo_datasources = [ds for ds in datasources if ds.get("type") == "repository"]
        non_repo_datasources = [
            ds for ds in datasources if ds.get("type") != "repository"
        ]
        datasources_dict, datasource_clients, cli_ds_types = process_datasources(
            non_repo_datasources, workspace_dir=os.getcwd()
        )

        # Inject datasource tool categories into config_override so the
        # correct tools are loaded when config is resolved below
        _ds_tool_map = {
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
                "category": "cloud",
                "read": ["cloud_list", "cloud_read", "cloud_info"],
                "write": [
                    "cloud_list",
                    "cloud_read",
                    "cloud_info",
                    "cloud_write",
                    "cloud_delete",
                ],
            },
        }
        config_override = dict(config_override or {})
        tools_override = dict(config_override.get("tools", {}))
        attached_types = {ds["type"] for ds in datasources}
        for ds_type, tool_info in _ds_tool_map.items():
            cat = tool_info["category"]
            if ds_type in attached_types:
                ds_entry = next(d for d in datasources if d["type"] == ds_type)
                is_ro = ds_entry.get("project_read_only", False)
                tools_override[cat] = tool_info["read"] if is_ro else tool_info["write"]
            else:
                tools_override.setdefault(cat, [])
        if tools_override:
            config_override["tools"] = tools_override

        if cli_ds_types:
            config_override.setdefault("extra", {})["_cli_datasources"] = cli_ds_types

        logger.info(
            "Processed %d datasource(s) for session: %d connections, %d CLI",
            len(datasources),
            len(datasources_dict),
            len(cli_ds_types),
        )

    effective_config = _agent.config
    llm = _agent._tactical_llm or _agent._llm
    if config_override:
        import dataclasses

        from ..core.loader import (
            _apply_settings_matrix,
            create_llm,
            deep_merge,
            load_agent_config_from_dict,
        )

        base_dict = dataclasses.asdict(effective_config)
        merged = deep_merge(base_dict, config_override)

        # If the override changes the model, re-apply settings_matrix for the
        # new model family so temperature/top_p/limits get correct defaults.
        # Override LLM keys are treated as "explicitly set" so the matrix
        # won't overwrite them.
        if config_override.get("llm"):
            override_llm_keys = set(config_override["llm"].keys())
            _apply_settings_matrix(
                merged, override_llm_keys, effective_config._deployment_dir
            )

        effective_config = load_agent_config_from_dict(
            merged, deployment_dir=effective_config._deployment_dir
        )
        if config_override.get("llm"):
            llm = create_llm(effective_config.llm, effective_config.limits)
            logger.info(
                f"Config override applied: model={effective_config.llm.model}, "
                f"temperature={effective_config.llm.temperature}"
            )

    # Auxiliary LLM rebuild. The boot-time _agent._auxiliary_llm is built from
    # config.auxiliary.model in the YAML default — for persistent sessions
    # without an override that's RedHatAI/... with no transport, which routes
    # title-generation/memory-extraction calls to api.openai.com with
    # not-needed and 401s. When the orchestrator's create_thread injection
    # (or a runtime config.update) supplies an auxiliary section, build a
    # session-scoped AuxiliaryLLM and pass it in instead of the singleton.
    auxiliary_llm = _agent._auxiliary_llm
    if config_override and config_override.get("auxiliary", {}).get("model"):
        from ..core.loader import LLMConfig, resolve_model_settings
        from ..services.auxiliary import AuxiliaryLLM

        aux_cfg = effective_config.auxiliary
        model_settings = resolve_model_settings(
            aux_cfg.model, effective_config._deployment_dir
        )
        aux_llm_config = LLMConfig(
            model=aux_cfg.model,
            base_url=aux_cfg.base_url,
            api_key=aux_cfg.api_key,
            temperature=aux_cfg.temperature,
            top_p=model_settings.get("top_p"),
            top_k=model_settings.get("top_k"),
            model_max_context_tokens=model_settings.get("model_max_context_tokens"),
            max_retries=1,
        )
        aux_inner = create_llm(aux_llm_config, effective_config.limits)
        auxiliary_llm = AuxiliaryLLM(
            llm=aux_inner,
            max_iterations=aux_cfg.max_iterations,
            timeout=aux_cfg.timeout,
        )
        logger.info(
            "Auxiliary override applied: model=%s, base_url=%s",
            aux_cfg.model,
            aux_cfg.base_url or "default",
        )

    # Embedding override. EmbeddingService is a process-wide singleton built
    # from EMBEDDING_* env vars at first call. When the orchestrator supplies
    # env_keys carrying embedding routing, push them onto os.environ and
    # clear the singleton so the next get_embedding_service() rebuilds with
    # the right base_url + api_key. Without this the singleton stays bound
    # to whatever was set at boot.
    if config_override and config_override.get("env_keys"):
        env_keys = config_override["env_keys"]
        embedding_keys = (
            "EMBEDDING_PROVIDER",
            "EMBEDDING_MODEL",
            "EMBEDDING_BASE_URL",
            "EMBEDDING_API_KEY",
        )
        if any(k in env_keys for k in embedding_keys):
            for k in embedding_keys:
                if k in env_keys and env_keys[k] is not None:
                    os.environ[k] = str(env_keys[k])
            from ..services import embedding_service as _embedding_module

            _embedding_module._embedding_service = None
            logger.info(
                "Embedding override applied: provider=%s, model=%s, base_url=%s",
                env_keys.get(
                    "EMBEDDING_PROVIDER", os.environ.get("EMBEDDING_PROVIDER")
                ),
                env_keys.get("EMBEDDING_MODEL", os.environ.get("EMBEDDING_MODEL")),
                env_keys.get(
                    "EMBEDDING_BASE_URL",
                    os.environ.get("EMBEDDING_BASE_URL", "default"),
                ),
            )

    # Create PersistentSession
    _session = PersistentSession(
        thread_id=_thread_id,
        config=effective_config,
        project_ids=project_ids or [],
        datasources=datasources_dict,
        _datasource_clients=datasource_clients,
    )
    if project_ids:
        logger.info(f"Session scoped to {len(project_ids)} project(s): {project_ids}")
    git_remote_url = (
        workspace_override.get("git_remote_url") if workspace_override else None
    )
    await _session.setup(
        llm=llm,
        auxiliary_llm=auxiliary_llm,
        postgres_conn=_agent.postgres_conn,
        vector_conn=getattr(_agent, "vector_conn", None),
        workspace_override=workspace_override,
        git_remote_url=git_remote_url,
    )

    # Clone repository datasources into the workspace (deferred from above).
    # Uses GitManager.clone() with the workspace backend so that repos are
    # cloned on the remote workspace container (not the agent pod).
    if repo_datasources and _session.workspace_manager:
        from ..managers.git_manager import GitManager
        from ..utils.git_url import repo_name_from_url
        from ..utils.ssh_key import normalize_private_key
        import re as _re

        ws_mgr = _session.workspace_manager
        backend = ws_mgr.backend if hasattr(ws_mgr, "backend") else None
        use_backend = backend is not None and getattr(backend, "supports_shell", False)
        # Track repo names already assigned this session so we can append a
        # numeric suffix when two datasources resolve to the same name (e.g.
        # forks of the same upstream).
        used_repo_names: set[str] = set()
        for ds in repo_datasources:
            # ds_name is the safe form of the user-supplied datasource label.
            # We keep using it as the SSH key filename and SSH config alias
            # so that two datasources with different keys for the same repo
            # don't clobber each other's auth material.
            ds_name = (
                _re.sub(r"[^a-z0-9]+", "-", ds.get("name", "repo").lower()).strip("-")
                or "repo"
            )
            try:
                repo_url = ds.get("connection_url", "")
                branch = ds.get("default_branch")
                creds = ds.get("credentials") or {}

                # The clone directory and source_repos registry key use the
                # upstream repo name (Superhuman-Remote-Worker, not
                # "read-only-version-of-..."). Fall back to the datasource
                # label only if URL parsing yields nothing usable.
                base_repo_name = repo_name_from_url(repo_url, fallback=ds_name)
                repo_name = base_repo_name
                suffix = 2
                while repo_name in used_repo_names:
                    repo_name = f"{base_repo_name}-{suffix}"
                    suffix += 1
                if repo_name != base_repo_name:
                    logger.info(
                        "Repo name collision for %s; cloning into %s instead",
                        base_repo_name,
                        repo_name,
                    )
                used_repo_names.add(repo_name)

                # Determine auth method: explicit field, or infer from
                # credentials keys (ssh_key present → ssh).
                auth_method = creds.get("auth_method")
                if not auth_method:
                    if creds.get("ssh_key"):
                        auth_method = "ssh"
                    elif creds.get("token"):
                        auth_method = "token"

                if auth_method == "ssh" and creds.get("ssh_key"):
                    import shlex
                    from urllib.parse import urlparse

                    # Normalize defensively: orchestrator validation already
                    # runs on save, but legacy rows in the datasources table
                    # may predate it. Cheap insurance.
                    ssh_key_text = normalize_private_key(creds["ssh_key"])

                    parsed = urlparse(repo_url)
                    host = parsed.hostname or "localhost"

                    if use_backend:
                        # Write SSH key and configure on the remote container.
                        # write_home_file lands the key under $HOME without
                        # tripping the workspace-boundary check on write_file;
                        # resolve_home_path gives us the absolute path for the
                        # subsequent chmod and SSH config IdentityFile entry.
                        rel_key = f".ssh/repo_{ds_name}"
                        key_path = backend.resolve_home_path(rel_key)
                        backend.shell_run(
                            "mkdir -p ~/.ssh && chmod 700 ~/.ssh",
                            timeout=10,
                            tab_name="git",
                        )
                        backend.write_home_file(rel_key, ssh_key_text)
                        backend.shell_run(
                            f"chmod 600 {shlex.quote(key_path)}",
                            timeout=10,
                            tab_name="git",
                        )
                        # Append SSH config for this host
                        ssh_config = (
                            f"\nHost {host}\n"
                            f"  IdentityFile {key_path}\n"
                            f"  StrictHostKeyChecking accept-new\n"
                        )
                        backend.shell_run(
                            f"printf %s {shlex.quote(ssh_config)} >> ~/.ssh/config",
                            timeout=10,
                            tab_name="git",
                        )
                    else:
                        # Local: write SSH key to agent filesystem
                        ssh_dir = os.path.expanduser("~/.ssh")
                        os.makedirs(ssh_dir, mode=0o700, exist_ok=True)
                        key_file = os.path.join(ssh_dir, f"repo_{ds_name}")
                        with open(key_file, "w") as f:
                            f.write(ssh_key_text)
                        os.chmod(key_file, 0o600)
                        config_path = os.path.join(ssh_dir, "config")
                        with open(config_path, "a") as f:
                            f.write(
                                f"\nHost {host}\n"
                                f"  IdentityFile {key_file}\n"
                                f"  StrictHostKeyChecking accept-new\n"
                            )

                    # Convert HTTPS URL to SSH URL so git uses the key.
                    # strip("/") handles trailing slashes too — datasource URLs
                    # entered as `.../repo/` would otherwise become `repo/.git`,
                    # which GitHub's SSH server rejects.
                    if parsed.scheme in ("http", "https"):
                        path = parsed.path.strip("/")
                        if not path.endswith(".git"):
                            path += ".git"
                        repo_url = f"git@{host}:{path}"
                        logger.info(
                            "Converted HTTPS URL to SSH for %s: %s",
                            ds_name,
                            repo_url,
                        )

                elif (auth_method == "token" or not auth_method) and creds.get("token"):
                    from urllib.parse import urlparse

                    parsed = urlparse(repo_url)
                    repo_url = parsed._replace(
                        netloc=f"oauth2:{creds['token']}@{parsed.hostname}"
                        + (f":{parsed.port}" if parsed.port else "")
                    ).geturl()

                target = ws_mgr.path / "repos" / repo_name
                remote_cwd = f"repos/{repo_name}"
                git_mgr = GitManager.clone(
                    repo_url,
                    target,
                    backend=backend,
                    remote_cwd=remote_cwd,
                )
                if git_mgr:
                    if branch:
                        git_mgr.checkout_branch(branch)
                    ws_mgr.source_repos[repo_name] = git_mgr
                    logger.info(
                        "Cloned repository datasource %r into repos/%s",
                        ds_name,
                        repo_name,
                    )
                else:
                    logger.warning(
                        "Failed to clone repository datasource %r (target repos/%s)",
                        ds_name,
                        repo_name,
                    )
            except Exception as e:
                logger.warning(
                    "Failed to clone repository datasource %s: %s",
                    ds.get("name", "unnamed"),
                    e,
                )

    # Inject datasource index into workspace.md (after workspace is initialized)
    if datasources and _session.workspace_manager:
        try:
            _inject_ds_index(datasources, _session.workspace_manager)
        except Exception as e:
            logger.warning(f"Failed to inject datasource index: {e}")

    # Initialize cloud workspace sync if the orchestrator gave us a config
    cloud_cfg = workspace_override.get("cloud_sync") if workspace_override else None
    nc_folder = (
        workspace_override.get("nc_session_folder") if workspace_override else None
    )
    if (not cloud_cfg or not nc_folder) and _orchestrator_client and _thread_id:
        try:
            ws_info = await _orchestrator_client.get_thread_workspace(_thread_id)
            if ws_info:
                cloud_cfg = cloud_cfg or ws_info.get("cloud_sync")
                nc_folder = nc_folder or ws_info.get("nc_session_folder")
        except Exception:
            pass
    # Back-compat: translate a bare nc_session_folder into the new schema
    if not cloud_cfg and nc_folder:
        cloud_cfg = _legacy_nc_cloud_cfg(nc_folder)
    if cloud_cfg:
        try:
            from src.services.cloud_sync import build_workspace_sync

            _session.workspace_sync = build_workspace_sync(
                workspace_path=_session.workspace_manager.path,
                cloud_cfg=cloud_cfg,
                workspace_backend=_session.workspace_manager.backend,
            )
            if _session.workspace_sync:
                # Initial push of existing workspace files, then background pull
                await _session.workspace_sync.push()
                await _session.workspace_sync.start_background_poll()
                logger.info(
                    "Cloud workspace sync started (backend=%s)",
                    cloud_cfg.get("backend"),
                )
        except Exception as e:
            logger.warning(f"Failed to start cloud workspace sync: {e}")

    # Restore message history from DB (for session resume)
    await _restore_session_messages()

    # Mark thread as active
    await _update_thread_status("active")

    # Initialize headless loop primitives. These survive WS reconnect so that
    # the loop can keep reading input / responding to interrupts across
    # transport churn. Cleared in _terminate_session.
    global _loop_user_queue, _loop_interrupt_flag, _loop_last_user_content
    _loop_user_queue = asyncio.Queue()
    _loop_interrupt_flag = None
    _loop_last_user_content = [""]

    # Phase 2 event-log cursor init. The current epoch lives on the threads
    # row; we bump it iff the previous epoch has events (i.e. this is a
    # cold-checkpoint restart that lost the in-memory seq counter). A fresh
    # epoch with no rows is reused as-is. _next_seq always starts at 0
    # locally — _broadcast pre-increments before writing the first event.
    global _events_epoch, _next_seq, _tool_inflight
    _tool_inflight = False
    _events_epoch = 0
    _next_seq = 0
    if _session is not None and _session.postgres_conn is not None:
        try:
            async with _session.postgres_conn.acquire() as conn:
                current_epoch = await conn.fetchval(
                    "SELECT events_epoch FROM threads WHERE id = $1",
                    _thread_id,
                )
                if current_epoch is None:
                    current_epoch = 0
                max_seq = await conn.fetchval(
                    "SELECT COALESCE(MAX(seq), 0) FROM thread_events "
                    "WHERE thread_id = $1 AND epoch = $2",
                    _thread_id,
                    current_epoch,
                )
                if max_seq and max_seq > 0:
                    # Cold-checkpoint restart: previous epoch has events
                    # but we lost the in-memory counter. Bump to a fresh
                    # epoch so cursors from the previous run trigger
                    # GONE_BEYOND_HORIZON on reconnect.
                    new_epoch = await conn.fetchval(
                        "UPDATE threads SET events_epoch = events_epoch + 1 "
                        "WHERE id = $1 RETURNING events_epoch",
                        _thread_id,
                    )
                    _events_epoch = int(new_epoch)
                    logger.info(
                        "Bumped events_epoch to %d for thread %s "
                        "(previous epoch had %d events)",
                        _events_epoch,
                        _thread_id,
                        max_seq,
                    )
                else:
                    _events_epoch = int(current_epoch)
        except Exception as e:
            logger.warning(
                "events_epoch init failed for thread %s (non-fatal): %s",
                _thread_id,
                e,
            )

    # Start self-cleanup watchdogs (PR 2): exit on boot-WS timeout or
    # out-of-band thread.status='ended'. Cancelled by _terminate_session.
    _start_watchdogs()

    logger.info(f"Session attached: thread={_thread_id} events_epoch={_events_epoch}")


async def _terminate_session(reason: str) -> None:
    """Tear down the current session and return to idle.

    Called by:
      - WS-handler finally block? NO — under headless semantics WS close only
        unsubscribes; the loop survives. WS close never calls this.
      - Out-of-band lifecycle: drain intent, boot-WS timeout, thread-status
        watchdog, REST /session/detach, process shutdown, MAX_SESSIONS sweep.
      - The persistent loop's own completion handler (idle timeout, crash,
        clean /done exit) routes here via _loop_completion_handler.

    Steps:
      1. Cancel in-flight persistent-loop task (prevents permission_check race
         that the commit 3a1d265 race-fix protects against).
      2. Mark thread as ended (still resumable — `ended` is the only inactive
         state).
      3. Git commit + push.
      4. Clean up session resources.
      5. Clear session globals AND headless input primitives + subscribers.
      6. Increment session counter, exit if max reached.

    `reason` is logged and stored for observability — e.g. "drain",
    "idle_timeout", "loop_crash", "loop_complete", "shutdown", "rest_detach",
    "thread_ended_oob", "boot_ws_timeout", "legacy".
    """
    global _session, _thread_id, _sessions_served, _loop_task
    global _loop_user_queue, _loop_interrupt_flag, _loop_last_user_content
    global _events_epoch, _next_seq, _tool_inflight

    if not _session:
        return

    thread_id = _thread_id
    logger.info(f"Terminating session: thread={thread_id} reason={reason}")

    # Cancel in-flight loop_task FIRST. Out-of-band callers (heartbeat-intent
    # drain, thread-status watchdog) reach this without going through the
    # loop's normal exit path, so without this the loop's next
    # _session.permission_mode access AttributeErrors when we null _session
    # below. Skipped when invoked from inside the loop itself (e.g. via
    # _loop_completion_handler's cleanup, which would deadlock awaiting self).
    loop_task = _loop_task
    if loop_task is not None and loop_task is not asyncio.current_task():
        if not loop_task.done():
            loop_task.cancel()
            try:
                await loop_task
            except (asyncio.CancelledError, Exception):
                pass
    _loop_task = None

    # Cancel self-cleanup watchdogs first — we're about to do the cleanup
    # they would have triggered, no point letting them race the detach.
    _stop_watchdogs()

    # Mark thread as ended (still resumable — `ended` is the only inactive state).
    await _update_thread_status("ended")

    # Final cloud sync + stop polling + drop secrets
    if _session.workspace_sync:
        try:
            await _session.workspace_sync.full_sync()
            await _session.workspace_sync.stop()
            await _session.workspace_sync.aclose()
        except Exception as e:
            logger.warning(f"Final cloud sync failed (non-fatal): {e}")

    # Final git commit + push
    if _session.workspace_manager:
        git_mgr = getattr(_session.workspace_manager, "git_manager", None)
        if git_mgr and git_mgr.is_active:
            try:
                if git_mgr.has_uncommitted_changes():
                    git_mgr.commit(f"Session detach: thread {thread_id}")
                git_mgr.push()
            except Exception as e:
                logger.warning(f"Final git push failed (non-fatal): {e}")

    # Clean up session resources
    await _session.cleanup()

    # Clear session state
    _session = None
    _thread_id = None

    # Clear headless input primitives + subscriber registry. The pump tasks
    # owned by each subscriber are cancelled by their ws_chat finally blocks
    # when those handlers notice the WS close; dropping the registry here
    # ensures stale entries don't accumulate across sessions.
    _loop_user_queue = None
    _loop_interrupt_flag = None
    _loop_last_user_content = [""]
    _subscribers.clear()

    # Phase 2 event-log cursor reset. The next session attach reads the
    # epoch fresh from the threads table.
    _events_epoch = 0
    _next_seq = 0
    _tool_inflight = False

    # Safety valve: restart after N sessions to guard against state leakage
    _sessions_served += 1
    if _max_sessions_per_process > 0 and _sessions_served >= _max_sessions_per_process:
        logger.info(
            f"Max sessions per process reached ({_sessions_served}/{_max_sessions_per_process}). "
            "Exiting — Docker will restart the container."
        )
        import sys

        sys.exit(0)

    logger.info(
        f"Session terminated: thread={thread_id} "
        f"reason={reason} (sessions served: {_sessions_served})"
    )


async def _detach_session() -> None:
    """Back-compat shim. Prefer _terminate_session(reason) at new call sites.

    Kept so existing tests patching `_detach_session` continue to work and so
    code paths not yet updated don't break. Logs at DEBUG so each invocation
    is traceable.
    """
    logger.debug("_detach_session() called via back-compat shim")
    await _terminate_session("legacy")


def create_persistent_app(config_path: str, thread_id: Optional[str] = None) -> FastAPI:
    """Create the persistent-mode FastAPI application.

    Args:
        config_path: Agent config name or path
        thread_id: Session thread UUID

    Returns:
        FastAPI app with WebSocket and health endpoints
    """
    global _config_path, _thread_id
    _config_path = config_path
    _thread_id = thread_id

    app = FastAPI(
        title="Persistent Agent API",
        description="Interactive persistent agent with WebSocket transport",
        version="1.0.0",
        lifespan=lifespan,
    )

    # --- Health endpoints (same pattern as worker) ---

    @app.get("/health")
    async def health():
        return JSONResponse(
            {
                "status": "healthy",
                "mode": "persistent",
                "thread_id": _thread_id,
                "uptime_seconds": (datetime.now() - _started_at).total_seconds()
                if _started_at
                else 0,
            }
        )

    @app.get("/ready")
    async def ready():
        is_ready = _session is not None and _session.llm_with_tools is not None
        return JSONResponse(
            {"ready": is_ready, "mode": "persistent", "thread_id": _thread_id},
            status_code=200 if is_ready else 503,
        )

    @app.get("/status")
    async def status():
        return JSONResponse(
            {
                "mode": "persistent",
                "thread_id": _thread_id,
                "config": _config_path,
                "permission_mode": _session.permission_mode if _session else None,
                "turn_count": _session.turn_count if _session else 0,
                "message_count": len(_session.messages) if _session else 0,
                "tools": [t.name for t in _session.tools]
                if _session and _session.tools
                else [],
            }
        )

    # --- Session attach/detach (pool mode) ---

    @app.post("/session/attach")
    async def session_attach(request: dict = {}):
        """Attach this agent to a thread (Docker Compose pool mode).

        Called by the orchestrator when a user creates a persistent thread
        and this agent is available.  Creates a new PersistentSession.

        Body:
            thread_id (str): Thread UUID to attach to
            config_override (dict, optional): Config overrides from thread metadata
            project_ids (list[str], optional): Project IDs for scoping
        """
        thread_id = request.get("thread_id")
        if not thread_id:
            return JSONResponse({"error": "thread_id is required"}, status_code=400)

        if _session is not None:
            return JSONResponse(
                {
                    "error": f"Already attached to thread {_thread_id}",
                    "current_thread_id": _thread_id,
                },
                status_code=409,
            )

        try:
            await _attach_session(
                thread_id=thread_id,
                config_override=request.get("config_override"),
                project_ids=request.get("project_ids"),
                datasources=request.get("datasources"),
            )
            return JSONResponse(
                {
                    "status": "attached",
                    "thread_id": thread_id,
                    "sessions_served": _sessions_served,
                }
            )
        except Exception as e:
            logger.exception(f"Failed to attach session for thread {thread_id}")
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.post("/session/detach")
    async def session_detach():
        """Detach from the current thread and return to idle pool.

        Called by the orchestrator when a thread ends, or by the agent
        itself on idle timeout.  Tears down the PersistentSession.
        """
        if _session is None:
            return JSONResponse({"status": "already_idle", "thread_id": None})

        thread_id = _thread_id
        try:
            await _terminate_session("rest_detach")
            return JSONResponse(
                {
                    "status": "detached",
                    "thread_id": thread_id,
                    "sessions_served": _sessions_served,
                }
            )
        except Exception as e:
            logger.exception(f"Failed to detach session for thread {thread_id}")
            return JSONResponse({"error": str(e)}, status_code=500)

    # --- Headless REST input endpoints (phase 2) ---
    #
    # Counterparts to the WS-receive-loop methods, exposed so the orchestrator's
    # SSE-based clients (cockpit chunk 3, MCP, curl) can drive the session
    # without a WebSocket. The orchestrator forwards from
    # POST /api/threads/{id}/{input,interrupt,approve/{approval_id}}.

    @app.post("/api/input")
    async def api_input(request: Request):
        return await handle_api_input(request)

    @app.post("/api/interrupt")
    async def api_interrupt():
        return await handle_api_interrupt()

    @app.post("/api/approve")
    async def api_approve(request: Request):
        return await handle_api_approve(request)

    # --- WebSocket endpoint ---

    @app.websocket("/ws/chat")
    async def ws_chat(ws: WebSocket):
        await handle_persistent_websocket(ws)

    return app


# --- REST handlers (module-level so dual_app can call them too) ---
#
# Reached from both:
#   - persistent_app.create_persistent_app()'s /api/{input,interrupt,approve}
#     routes (pure persistent mode, agent.py --mode persistent).
#   - dual_app.create_dual_app() routes (dual mode — adds pod-state pre-check
#     then delegates here).
#
# Mirror of the /ws/chat consolidation; same rationale, see
# docs/issues/persistent_session_dual_mode_phase1_gap.md.


async def handle_api_input(request: Request) -> JSONResponse:
    """Push user input onto the loop's queue. Body: {content, turn_id?}."""
    if _session is None or _loop_user_queue is None:
        return JSONResponse({"error": "Session not active"}, status_code=503)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    content = body.get("content", "")
    if not isinstance(content, str) or not content:
        return JSONResponse(
            {"error": "content must be a non-empty string"},
            status_code=400,
        )
    _loop_last_user_content[0] = content
    await _loop_user_queue.put(content)
    return JSONResponse(
        {
            "accepted": True,
            "turn_id": _session.turn_count,
            "queue_depth": _loop_user_queue.qsize(),
        }
    )


async def handle_api_interrupt() -> JSONResponse:
    """Signal the loop to stop. Mode is hard vs graceful based on
    whether a tool call is currently in flight."""
    global _loop_interrupt_flag
    if _session is None:
        return JSONResponse({"error": "Session not active"}, status_code=503)
    mode = "graceful" if _tool_inflight else "hard"
    _loop_interrupt_flag = mode
    logger.info(
        "Interrupt received via REST (mode=%s, tool_inflight=%s)",
        mode,
        _tool_inflight,
    )
    return JSONResponse({"ack": True, "mode": mode})


async def handle_api_approve(request: Request) -> JSONResponse:
    """Resolve a pending permission gate by UPDATEing the
    thread_permission_requests row. Body: {decision: approve|deny,
    approval_id?}. If approval_id is omitted, the most-recent-pending
    row for this thread is resolved (legacy single-pending-at-a-time
    contract). The DB trigger emits NOTIFY → agent's permission_check
    wakes up."""
    if _session is None:
        return JSONResponse({"error": "Session not active"}, status_code=503)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    decision_raw = body.get("decision")
    if decision_raw == "approve":
        decision = "approved"
    elif decision_raw == "deny":
        decision = "denied"
    else:
        return JSONResponse(
            {"error": "decision must be 'approve' or 'deny'"},
            status_code=400,
        )
    approval_id = body.get("approval_id")
    resolved = await _resolve_pending_permission(
        decision,
        approval_id=approval_id,
        decided_by="rest_client",
    )
    if resolved is None:
        return JSONResponse(
            {
                "error": "No matching pending request",
                "approval_id": approval_id,
            },
            status_code=404,
        )
    return JSONResponse(
        {
            "accepted": True,
            "decision": decision_raw,
            "approval_id": str(resolved["id"]),
            "tool_call_id": resolved["tool_call_id"],
        }
    )


# --- WebSocket handler (module-level so dual_app can call it too) ---


async def handle_persistent_websocket(ws: WebSocket) -> None:
    """WebSocket consumer for an already-running persistent session.

    Headless lifecycle (chunk 1):
      - First WS attach spawns the persistent loop with module-level
        callbacks. Subsequent attaches just register a subscriber and
        tap into the existing loop's broadcast stream.
      - WS close calls _unsubscribe() and cancels this connection's pump
        task. The loop keeps running. It only stops via
        _loop_completion_handler (idle timeout, /done, crash) or via
        out-of-band _terminate_session (drain, watchdog, REST detach).
      - The pod no longer exits when the WS closes — that was the
        WS-bound era. _schedule_exit is now driven only by drain intent
        and shutdown paths.

    Reached from both:
      - persistent_app.create_persistent_app()'s /ws/chat route (pure
        persistent mode, agent.py --mode persistent).
      - dual_app.ws_chat (dual mode — adds pod-state pre-checks then
        delegates here). Sharing this body is what closes the Phase-1
        gap described in
        docs/issues/persistent_session_dual_mode_phase1_gap.md.
    """
    import uuid

    await ws.accept()

    # Signal the boot-WS watchdog that a connection arrived. Done before
    # the readiness check so even a failed-to-be-ready connection counts:
    # the user clearly came back, and a different error path applies.
    _signal_ws_connected()

    # Readiness gates on the loop primitives, not just the session. In dual
    # mode /session/attach returns immediately and _attach_session runs
    # asynchronously: _session.llm_with_tools is set early (inside .setup()),
    # but _loop_user_queue isn't initialized until much later in the same
    # coroutine. The loop's _loop_get_user_input callback crashes hard if
    # the queue is None, so we must wait for it.
    if not _session or not _session.llm_with_tools or _loop_user_queue is None:
        await _ws_send(ws, "error", {"message": "Agent not ready"})
        await ws.close(code=4503, reason="Agent not ready")
        return

    # Register this WS as a subscriber on the broadcast hub.
    client_id = uuid.uuid4().hex
    queue = _subscribe(client_id)
    pump_task = asyncio.create_task(
        _run_subscriber_pump(ws, client_id, queue),
        name=f"subscriber-pump-{client_id[:8]}",
    )

    logger.info(f"WebSocket connected: thread={_thread_id} client={client_id[:8]}")

    # Send current session state so this client can sync. Direct send —
    # this is the welcome frame, only the connecting client cares.
    await _ws_send(
        ws,
        "session.state",
        {
            "thread_id": _thread_id,
            "permission_mode": _session.permission_mode,
            "narration_mode": _session.narration_mode,
            "turn_count": _session.turn_count,
            "message_count": len(_session.messages),
            "model": _session.config.llm.model,
            "temperature": _session.config.llm.temperature,
        },
    )

    # Spawn the persistent loop if it isn't already running. Reconnecting
    # to a session whose loop is mid-turn just joins the broadcast — no
    # restart, no replay (replay arrives in chunk 2 via the event log).
    global _loop_task
    if _loop_task is None or _loop_task.done():
        callbacks = PersistentLoopCallbacks(
            get_user_input=_loop_get_user_input,
            on_token=_loop_on_token,
            on_thinking=_loop_on_thinking,
            on_tool_start=_loop_on_tool_start,
            on_tool_result=_loop_on_tool_result,
            permission_check=_loop_permission_check,
            on_turn_start=_loop_on_turn_start,
            on_turn_complete=_loop_on_turn_complete,
            on_error=_loop_on_error,
            check_interrupt=_loop_check_interrupt,
            on_vm_upgrade_needed=_loop_on_vm_upgrade_needed,
        )
        _loop_task = asyncio.create_task(
            run_persistent_loop(
                llm_with_tools=_session.llm_with_tools,
                tools=_session.tools,
                context_manager=_session.context_manager,
                config=_session.config,
                system_prompt=_session.system_prompt,
                callbacks=callbacks,
                messages=_session.messages,
                auxiliary_llm=_session.auxiliary_llm,
                workspace_content=_session.get_workspace_content,
                recall_store=_session.recall_store,
                knowledge_store=_session.knowledge_store,
                project_id=_session.project_id,
                project_ids=_session.project_ids,
                tool_context=_session.tool_context,
                initial_turn_count=_session.turn_count,
                get_current_tools=lambda: (
                    _session.llm_with_tools,
                    _session.tools,
                ),
            ),
            name="persistent-loop",
        )
        asyncio.create_task(
            _loop_completion_handler(_loop_task),
            name="persistent-loop-completion",
        )
        logger.info(f"Persistent loop started: thread={_thread_id}")
    else:
        logger.info(
            f"Persistent loop already running, attached as subscriber "
            f"client={client_id[:8]}"
        )

    # --- WebSocket receive loop ---
    global _loop_interrupt_flag
    try:
        while True:
            raw = await ws.receive_text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                # Plain text → treat as message
                data = {"method": "message", "content": raw}

            method = data.get("method", "message")

            if method == "message":
                content = data.get("content", "")
                if content and _loop_user_queue is not None:
                    _loop_last_user_content[0] = content
                    await _loop_user_queue.put(content)

            elif method == "approve":
                # Phase 3: resolve the most-recent-pending permission
                # request in the DB. Cockpit can pass an explicit
                # approval_id to disambiguate when multiple are
                # pending (rare — agent's loop serializes most flows).
                approval_id = data.get("approval_id")
                asyncio.create_task(
                    _resolve_pending_permission(
                        "approved",
                        approval_id=approval_id,
                        decided_by="ws_client",
                    ),
                    name="resolve-approve",
                )

            elif method == "deny":
                approval_id = data.get("approval_id")
                asyncio.create_task(
                    _resolve_pending_permission(
                        "denied",
                        approval_id=approval_id,
                        decided_by="ws_client",
                    ),
                    name="resolve-deny",
                )

            elif method == "interrupt":
                # Mode picked from current _tool_inflight: graceful when
                # a tool is mid-invoke (let it finish, don't leak state);
                # hard otherwise (cancel the LLM stream now, drop the
                # partial AIMessage). See persistent_graph check sites.
                mode = "graceful" if _tool_inflight else "hard"
                _loop_interrupt_flag = mode
                await _ws_send(ws, "interrupt.ack", {"mode": mode})
                logger.info("Interrupt acknowledged (mode=%s)", mode)

            elif method == "mode.set":
                new_mode = data.get("mode", "supervised")
                if new_mode in ("supervised", "auto_accept", "autonomous"):
                    if _session is None:
                        await _ws_send(
                            ws,
                            "error",
                            {"message": "Session no longer active"},
                        )
                        continue
                    _session.permission_mode = new_mode
                    await _ws_send(ws, "mode.changed", {"mode": new_mode})
                    logger.info(f"Permission mode changed to: {new_mode}")
                else:
                    await _ws_send(
                        ws,
                        "error",
                        {"message": f"Invalid mode: {new_mode}"},
                    )

            elif method == "narration.set":
                new_mode = data.get("mode", "auto")
                if new_mode in ("silent", "verbose", "auto"):
                    if _session is None:
                        await _ws_send(
                            ws,
                            "error",
                            {"message": "Session no longer active"},
                        )
                        continue
                    _session.narration_mode = new_mode
                    await _ws_send(ws, "narration.changed", {"mode": new_mode})
                    logger.info(f"Narration mode changed to: {new_mode}")
                else:
                    await _ws_send(
                        ws,
                        "error",
                        {"message": f"Invalid narration mode: {new_mode}"},
                    )

            elif method == "config.update":
                config_override = data.get("config", {})
                if config_override:
                    asyncio.create_task(_handle_config_update(ws, config_override))

            elif method == "compact":
                # Manual compaction trigger (/compact command)
                focus = data.get("focus", "")
                asyncio.create_task(_handle_compact(ws, focus))

            elif method == "archive":
                # End session (/done command)
                asyncio.create_task(_handle_archive(ws))

            elif method == "upgrade-to-vm":
                # Upgrade workspace from container to VM
                asyncio.create_task(_handle_vm_upgrade(ws))

            elif method == "undo":
                if _session is None:
                    await _ws_send(
                        ws,
                        "error",
                        {"message": "Session no longer active"},
                    )
                    continue
                turn_id = data.get("turn_id")
                restored = _session.undo_turn(turn_id)
                if restored:
                    await _ws_send(
                        ws,
                        "files.restored",
                        {
                            "paths": restored,
                            "turn_id": turn_id,
                        },
                    )
                else:
                    await _ws_send(
                        ws,
                        "error",
                        {"message": "No checkpoints available to undo"},
                    )

            else:
                await _ws_send(ws, "error", {"message": f"Unknown method: {method}"})

    except WebSocketDisconnect:
        logger.info(
            f"WebSocket disconnected: thread={_thread_id} "
            f"client={client_id[:8]} (loop continues)"
        )
    except Exception as e:
        logger.exception(f"WebSocket error: {e}")
    finally:
        # Headless keystone: WS close only unsubscribes. The loop keeps
        # running until _loop_completion_handler routes its natural exit,
        # or out-of-band _terminate_session intervenes. We do NOT cancel
        # _loop_task here, and we do NOT schedule pod exit.
        _unsubscribe(client_id)
        if not pump_task.done():
            pump_task.cancel()
            try:
                await pump_task
            except asyncio.CancelledError:
                pass
        logger.info(
            f"WebSocket pump released: thread={_thread_id} client={client_id[:8]}"
        )


# --- Helpers ---


async def _ws_send(ws: WebSocket, method: str, params: Dict[str, Any]) -> None:
    """Send a JSON message over WebSocket. Silently drops if connection is closed.

    Used by WS-handler-direct sends (the receive-loop's acks, the welcome frame,
    fire-and-forget handler tasks that hold a ws reference). Loop-driven sends
    use _broadcast() instead, so a closed WS doesn't kill the loop's output.
    """
    try:
        await ws.send_json({"method": method, "params": params})
    except Exception:
        pass  # Connection already closed


def _subscribe(client_id: str) -> asyncio.Queue:
    """Register a new subscriber and return its outbound queue.

    Each WebSocket connection (and later, each SSE consumer) gets its own
    bounded queue. _broadcast() enqueues onto every registered queue;
    _run_subscriber_pump drains one queue into one WS.

    Phase 5: if this is the first subscriber after an untethered pause,
    schedule a status revert to 'active' so the attention-sleep watchdog
    disarms. Fire-and-forget — a failed status write doesn't block the
    attach.
    """
    was_empty = not _subscribers
    queue: asyncio.Queue = asyncio.Queue(maxsize=_SUBSCRIBER_QUEUE_MAXSIZE)
    _subscribers[client_id] = queue
    if was_empty and _orchestrator_client is not None and _thread_id is not None:
        asyncio.create_task(
            _safe_set_thread_status("active"), name="phase5-revert-active"
        )
    return queue


def _unsubscribe(client_id: str) -> None:
    """Remove a subscriber. Cheap — does not touch the loop or session state.

    This is what WS close calls. The loop keeps running with one fewer audience.
    """
    _subscribers.pop(client_id, None)


async def _safe_set_thread_status(status: str) -> None:
    """Best-effort wrapper around _update_thread_status for fire-and-forget
    Phase 5 transitions (awaiting_user / active revert). A transient failure
    here is acceptable — the next natural-pause or subscriber attach will
    retry.
    """
    try:
        await _update_thread_status(status)
    except Exception as e:
        logger.warning("Failed to set thread status to %s: %s", status, e)


def _broadcast(method: str, params: Dict[str, Any]) -> None:
    """Enqueue a frame onto every subscriber queue, persist to event log.

    Non-blocking. On a full subscriber queue, drops the oldest frame to make
    room for the new one — token-stream pacing semantics. We'd rather lose an
    old chunk than block the loop on a stuck consumer.

    Phase 2 (event log): allocates the next seq synchronously and stamps
    `(_events_epoch, seq)` into the frame's params under `_seq`. The actual
    DB write is scheduled via asyncio.create_task and is best-effort — a
    failed DB write doesn't block the loop or cancel the in-pod broadcast.
    Reconnecting SSE clients replay from this log; failed writes produce a
    cursor gap that GONE_BEYOND_HORIZON catches on the next mismatch.
    """
    global _next_seq
    _next_seq += 1
    seq = _next_seq
    epoch = _events_epoch
    # Stamp the cursor onto the frame so existing WS subscribers see the
    # same (epoch, seq) the event log records — keeps WS and SSE paths
    # consistent under reconnect.
    params_with_cursor = {**params, "_seq": [epoch, seq]}
    frame = {"method": method, "params": params_with_cursor}

    for client_id, queue in list(_subscribers.items()):
        try:
            queue.put_nowait(frame)
        except asyncio.QueueFull:
            # Drop oldest, retry. If the retry still fails (shouldn't — we just
            # made room), drop the new frame and move on.
            try:
                queue.get_nowait()
                queue.put_nowait(frame)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                logger.warning(
                    "Subscriber %s queue overflow — dropping frame %s",
                    client_id,
                    method,
                )

    # Fire-and-forget DB write. Doesn't block the broadcast — if persistence
    # fails the live subscribers still received the frame; only the SSE
    # replay path loses a row.
    if _session is not None and _session.postgres_conn is not None:
        asyncio.create_task(
            _persist_event(epoch, seq, method, params),
            name=f"persist-event-{seq}",
        )


async def _persist_event(
    epoch: int, seq: int, kind: str, payload: Dict[str, Any]
) -> None:
    """Insert one row into thread_events. Best-effort; failures are logged.

    Called fire-and-forget from _broadcast. Captures the postgres_conn at
    task start so a concurrent _terminate_session can null _session
    without blowing up this in-flight write.
    """
    if _session is None or _thread_id is None:
        return
    postgres_conn = _session.postgres_conn
    if postgres_conn is None:
        return
    try:
        # Serialize payload defensively — frames carry tool args, etc.
        safe_payload = _safe_serialize(payload)
        async with postgres_conn.acquire() as conn:
            await conn.execute(
                "INSERT INTO thread_events "
                "(thread_id, epoch, seq, kind, payload) "
                "VALUES ($1, $2, $3, $4, $5::jsonb)",
                _thread_id,
                epoch,
                seq,
                kind,
                json.dumps(safe_payload),
            )
    except Exception as e:
        # Best-effort: log and drop. The frame already reached live
        # subscribers; only SSE replay for this seq is lost.
        logger.warning(
            "thread_events write failed (thread=%s epoch=%d seq=%d kind=%s): %s",
            _thread_id,
            epoch,
            seq,
            kind,
            e,
        )


async def _run_subscriber_pump(
    ws: WebSocket, client_id: str, queue: asyncio.Queue
) -> None:
    """Drain a subscriber's queue into its WebSocket. Exits on send failure.

    One pump task per connected WebSocket. Cancelled by the ws_chat finally
    block when the WS closes; the queue is then garbage-collected after
    _unsubscribe removes the dict entry.
    """
    try:
        while True:
            frame = await queue.get()
            try:
                await ws.send_json(frame)
            except Exception:
                # WS is dead — let the receive loop's exception path clean up.
                return
    except asyncio.CancelledError:
        raise


# ---------------------------------------------------------------------------
# Persistent-loop callbacks (module-level under headless semantics).
#
# These used to be closures inside ws_chat. They've been hoisted so the loop
# can outlive any single WebSocket connection: callbacks reference module
# globals (_session, _loop_user_queue, _loop_interrupt_flag,
# _loop_last_user_content, _orchestrator_client, _thread_id) and emit via
# _broadcast() rather than writing to one ws.
# ---------------------------------------------------------------------------


async def _loop_get_user_input() -> str:
    """Wait for the next user input. Honors session idle timeout.

    On idle timeout, broadcasts session.idle_timeout to every subscriber and
    raises IdleTimeoutError — the loop unwinds, _loop_completion_handler
    routes it to _handle_idle_archive() + _terminate_session("idle_timeout").
    """
    queue = _loop_user_queue
    if queue is None:
        # _attach_session always initializes this. If we hit None here the
        # session is being torn down — fail loudly so the loop unwinds.
        raise RuntimeError("_loop_user_queue not initialized — session torn down?")

    _broadcast("ready", {})

    # Phase 5/6: natural-pause transition to 'awaiting_user'. Eager mode
    # (default) only flips when untethered — the agent is presumed to be
    # working in the background and we only need to flag-and-notify when
    # the user has nobody watching. Polite mode flips at every turn boundary
    # regardless of subscribers — the user has explicitly opted in to a
    # review-heavy "see every step" workflow and wants notification + an
    # explicit reply gate after each completed turn. Idempotent on the
    # orchestrator side: repeated writes preserve awaiting_user_since.
    headless_mode = "eager"
    if _session is not None:
        headless_cfg = getattr(_session.config, "headless", None)
        if headless_cfg is not None:
            headless_mode = getattr(headless_cfg, "mode", "eager") or "eager"
    should_flip = (
        _session is not None
        and _session.turn_count > 0
        and _orchestrator_client is not None
        and _thread_id is not None
        and (headless_mode == "polite" or not _subscribers)
    )
    if should_flip:
        asyncio.create_task(
            _safe_set_thread_status("awaiting_user"),
            name="phase5-flip-awaiting-user",
        )

    if _session is None:
        return await queue.get()

    idle_timeout_minutes = _session.config.interactive.idle_timeout_minutes
    if idle_timeout_minutes and idle_timeout_minutes > 0:
        idle_timeout_seconds = idle_timeout_minutes * 60
        try:
            return await asyncio.wait_for(queue.get(), timeout=idle_timeout_seconds)
        except asyncio.TimeoutError:
            logger.info(
                "Idle timeout (%dmin) for thread %s",
                idle_timeout_minutes,
                _thread_id,
            )
            _broadcast(
                "session.idle_timeout",
                {
                    "thread_id": _thread_id,
                    "message": (
                        "Session paused due to inactivity. Your work has been saved."
                    ),
                    "timeout_minutes": idle_timeout_minutes,
                },
            )
            raise IdleTimeoutError(f"Idle timeout after {idle_timeout_seconds}s")
    return await queue.get()


def _loop_check_interrupt() -> Optional[str]:
    """One-shot read of the interrupt flag. Returns the mode or None.

    Returns:
        None when no interrupt is pending.
        "hard" to cancel the in-flight LLM stream immediately and drop the
            partial AIMessage (set when interrupt fires with no tool active).
        "graceful" to stop after the current tool call completes (set when
            interrupt fires with a tool mid-`ainvoke`).

    Consumed by persistent_graph at three checkpoints. A `bool(result)`
    check preserves the legacy "any interrupt → stop" semantics for sites
    that don't yet branch on the mode.
    """
    global _loop_interrupt_flag
    mode = _loop_interrupt_flag
    if mode is not None:
        _loop_interrupt_flag = None
        return mode
    return None


async def _loop_on_token(token: str) -> None:
    _broadcast("token", {"content": token})


async def _loop_on_thinking(content: str) -> None:
    _broadcast("thinking", {"content": content})


async def _loop_on_tool_start(
    tool_name: str, tool_args: Dict[str, Any], tool_call_id: str
) -> None:
    global _tool_inflight
    _tool_inflight = True
    meta = TOOL_REGISTRY.get(tool_name, {})
    _broadcast(
        "tool.started",
        {
            "tool": tool_name,
            "args": _safe_serialize(tool_args),
            "id": tool_call_id,
            "category": meta.get("category", ""),
        },
    )


async def _loop_on_tool_result(
    tool_name: str,
    result: str,
    tool_call_id: str,
    is_error: bool = False,
) -> None:
    global _tool_inflight
    _tool_inflight = False
    # Truncate large results for transport (full result is in message history)
    display_result = result[:2000] + "..." if len(result) > 2000 else result
    _broadcast(
        "tool.completed",
        {
            "tool": tool_name,
            "result": display_result,
            "id": tool_call_id,
            "is_error": is_error,
        },
    )

    # Notify frontend of file checkpoint availability after writes
    if tool_name in ("write_file", "edit_file") and _session is not None:
        _broadcast(
            "file.checkpoint",
            {"turn_id": _session.turn_count},
        )

    # Broadcast task state after task tool calls
    if (
        tool_name in ("task_add", "task_complete", "task_list")
        and _session is not None
        and _session.session_task_manager
    ):
        _broadcast(
            "tasks.updated",
            {"tasks": _session.session_task_manager.to_dict_list()},
        )


# ---------------------------------------------------------------------------
# Phase 3: DB-backed permission gates (LISTEN/NOTIFY on thread_permission_requests)
# ---------------------------------------------------------------------------
#
# The agent INSERTs a pending row when permission_check fires, then waits for
# UPDATE → trigger → NOTIFY. Approval can arrive from any path (WS-attached
# cockpit, REST POST from MCP/cockpit, future email magic-link) — all converge
# on the same UPDATE statement. The agent never blocks on an in-memory queue
# anymore; the queue path is still in place for non-permission user input.
#
# Channel: thread_permission_updates (global). Payload carries the request id;
# the listener filters by it to match its own pending wait.

_PERMISSION_TIMEOUT_S: float = 300.0
_PERMISSION_NOTIFY_CHANNEL: str = "thread_permission_updates"


async def _insert_permission_request(
    tool_call_id: str, tool_name: str, tool_args: Dict[str, Any]
) -> Optional[str]:
    """INSERT a pending row and return its UUID. None on failure."""
    if _session is None or _session.postgres_conn is None or _thread_id is None:
        return None
    try:
        async with _session.postgres_conn.acquire() as conn:
            row_id = await conn.fetchval(
                "INSERT INTO thread_permission_requests "
                "(thread_id, tool_call_id, tool_name, tool_args) "
                "VALUES ($1, $2, $3, $4::jsonb) "
                "RETURNING id",
                _thread_id,
                tool_call_id,
                tool_name,
                json.dumps(_safe_serialize(tool_args)),
            )
        return str(row_id) if row_id is not None else None
    except Exception as e:
        logger.warning(
            "thread_permission_requests INSERT failed (tool=%s): %s",
            tool_name,
            e,
        )
        return None


async def _wait_for_permission_resolution(
    request_id: str, timeout: float = _PERMISSION_TIMEOUT_S
) -> str:
    """Block until the row's status flips from pending. Returns the final
    status string ('approved'/'denied'/'expired'). On any failure, returns
    'denied' as the conservative default.

    Uses asyncpg's connection-scoped add_listener on the global NOTIFY
    channel; filters by row id. After registering the listener, re-SELECTs
    the row's status to close the race window between INSERT and listen
    setup. On timeout, atomically marks the row 'expired' (only if it's
    still 'pending') before reading the canonical final status back.
    """
    if _session is None or _session.postgres_conn is None:
        return "denied"

    resolved = asyncio.Event()

    def _on_notify(_conn, _pid, _channel, payload):
        try:
            data = json.loads(payload)
        except Exception:
            return
        if str(data.get("id")) == request_id:
            resolved.set()

    try:
        async with _session.postgres_conn.acquire() as conn:
            await conn.add_listener(_PERMISSION_NOTIFY_CHANNEL, _on_notify)
            try:
                # Race-safe: an UPDATE between INSERT and add_listener
                # would have fired NOTIFY into the void; check the row's
                # current status before settling in to wait.
                current = await conn.fetchval(
                    "SELECT status FROM thread_permission_requests WHERE id = $1",
                    request_id,
                )
                if current in ("approved", "denied", "expired"):
                    return str(current)

                try:
                    await asyncio.wait_for(resolved.wait(), timeout=timeout)
                except asyncio.TimeoutError:
                    # CAS-style expire: only if nobody beat us to it.
                    await conn.execute(
                        "UPDATE thread_permission_requests "
                        "SET status = 'expired', decided_at = now(), "
                        "    decided_by = 'system' "
                        "WHERE id = $1 AND status = 'pending'",
                        request_id,
                    )

                final = await conn.fetchval(
                    "SELECT status FROM thread_permission_requests WHERE id = $1",
                    request_id,
                )
                return str(final) if final is not None else "denied"
            finally:
                try:
                    await conn.remove_listener(_PERMISSION_NOTIFY_CHANNEL, _on_notify)
                except Exception:
                    pass
    except Exception as e:
        logger.warning("Permission resolution wait failed (id=%s): %s", request_id, e)
        return "denied"


async def _resolve_pending_permission(
    decision: str,
    approval_id: Optional[str] = None,
    decided_by: str = "ws_client",
) -> Optional[Dict[str, Any]]:
    """UPDATE a pending permission row by id, or the most-recent-pending if
    no id given. Returns the resolved row dict or None if not found / no
    pending request matched."""
    if _session is None or _session.postgres_conn is None or _thread_id is None:
        return None
    if decision not in ("approved", "denied"):
        return None
    try:
        async with _session.postgres_conn.acquire() as conn:
            if approval_id is not None:
                row = await conn.fetchrow(
                    "UPDATE thread_permission_requests "
                    "SET status = $2, decided_at = now(), decided_by = $3 "
                    "WHERE id = $1 AND status = 'pending' "
                    "RETURNING id, status, tool_call_id, thread_id",
                    approval_id,
                    decision,
                    decided_by,
                )
            else:
                row = await conn.fetchrow(
                    "UPDATE thread_permission_requests "
                    "SET status = $2, decided_at = now(), decided_by = $3 "
                    "WHERE id = ("
                    "  SELECT id FROM thread_permission_requests "
                    "  WHERE thread_id = $1 AND status = 'pending' "
                    "  ORDER BY requested_at DESC LIMIT 1"
                    ") "
                    "RETURNING id, status, tool_call_id, thread_id",
                    _thread_id,
                    decision,
                    decided_by,
                )
        return dict(row) if row else None
    except Exception as e:
        logger.warning("Resolve pending permission failed: %s", e)
        return None


async def _loop_permission_check(
    tool_name: str, tool_args: Dict[str, Any], tool_call_id: str
) -> bool:
    """Check whether a tool call is approved. INSERTs a pending row, waits
    for the DB to flip via LISTEN/NOTIFY, returns True iff approved.

    The race-fix from commit 3a1d265: if _terminate_session nulled _session
    while permission_check was being scheduled, this returns False — the
    session is gone, the tool result has nowhere to land.
    """
    if _session is None:
        logger.warning(
            "permission_check fired with _session=None for tool %s — denying",
            tool_name,
        )
        return False

    mode = _session.permission_mode

    if mode == "autonomous":
        return True

    if mode == "auto_accept":
        # Auto-accept reads and writes; still ask for shell commands.
        shell_tools = {"run_command", "shell_execute", "shell_read"}
        if tool_name not in shell_tools:
            return True

    # Phase 5 wake path: if this tool_call_id was already resolved (typical
    # case: user clicked the magic-link approve/deny while the agent was
    # suspended; on wake LangGraph restores the same tool_call_id from
    # checkpoint), reuse that decision instead of inserting a fresh
    # request. We only honor terminal 'approved'/'denied' here — 'expired'
    # means the prior request timed out without a user response, so the
    # new attempt deserves a fresh prompt.
    if _session.postgres_conn is not None and _thread_id is not None:
        try:
            async with _session.postgres_conn.acquire() as conn:
                existing = await conn.fetchrow(
                    "SELECT status FROM thread_permission_requests "
                    "WHERE thread_id = $1 AND tool_call_id = $2 "
                    "  AND status IN ('approved', 'denied') "
                    "ORDER BY decided_at DESC NULLS LAST LIMIT 1",
                    _thread_id,
                    tool_call_id,
                )
            if existing is not None:
                decision = existing["status"]
                _session.tool_decisions[tool_call_id] = decision
                logger.info(
                    "Phase 5 wake: reusing prior %s decision for tool_call %s "
                    "(tool=%s)",
                    decision,
                    tool_call_id,
                    tool_name,
                )
                return decision == "approved"
        except Exception as e:
            # Soft-fail: fall through to the regular INSERT-and-wait path.
            logger.warning(
                "Wake-path SELECT for tool_call %s failed (%s); falling back",
                tool_call_id,
                e,
            )

    # Supervised mode (or shell under auto_accept): ask user via the
    # durable permission table, then wait on LISTEN/NOTIFY.
    request_id = await _insert_permission_request(tool_call_id, tool_name, tool_args)
    if request_id is None:
        # DB unavailable — conservative deny rather than risk silent
        # auto-approval. Logged at WARNING by the insert helper.
        if _session is not None:
            _session.tool_decisions[tool_call_id] = "denied"
        return False

    # Broadcast carries both ids so clients can refer back via either.
    _broadcast(
        "permission.request",
        {
            "id": tool_call_id,
            "approval_id": request_id,
            "tool": tool_name,
            "args": _safe_serialize(tool_args),
        },
    )

    # Phase 5: sudo gate hit untethered is the second natural-pause site.
    # Flip the thread so the attention-sleep watchdog can fire after the
    # configured TTL. Idempotent against the _loop_get_user_input write.
    if not _subscribers and _orchestrator_client is not None and _thread_id is not None:
        asyncio.create_task(
            _safe_set_thread_status("awaiting_user"),
            name="phase5-flip-awaiting-user-sudo",
        )

    final_status = await _wait_for_permission_resolution(request_id)
    approved = final_status == "approved"
    if _session is not None:
        _session.tool_decisions[tool_call_id] = final_status
    return approved


async def _loop_on_turn_start(turn_id: int) -> None:
    if _session is None:
        return
    _session.turn_count = turn_id
    _broadcast("turn.started", {"turn_id": turn_id})
    # Save user message to DB (bounded await — no messages lost on crash)
    if _orchestrator_client and _loop_last_user_content[0]:
        try:
            await asyncio.wait_for(
                _save_message(
                    _orchestrator_client,
                    _thread_id,
                    "user",
                    _loop_last_user_content[0],
                    None,
                    turn_id,
                ),
                timeout=5.0,
            )
        except asyncio.TimeoutError:
            logger.warning("User message save timed out (5s) — proceeding")


async def _loop_on_turn_complete(turn_id: int, metrics: Optional[dict] = None) -> None:
    if _session is None:
        return
    _broadcast("turn.completed", {"turn_id": turn_id})
    # Save AI messages from this turn to DB (bounded await)
    if _orchestrator_client:
        try:
            await asyncio.wait_for(
                _save_turn_ai_messages(
                    _orchestrator_client,
                    _thread_id,
                    _session.messages,
                    turn_id,
                    metrics=metrics,
                    tool_decisions=dict(_session.tool_decisions),
                ),
                timeout=5.0,
            )
        except asyncio.TimeoutError:
            logger.warning("AI message save timed out (5s) — proceeding")
    _session.tool_decisions.clear()

    # Auto-generate title after first few turns (fire-and-forget).
    # Retry on turns 1-3 in case the LLM is transiently unreachable.
    if turn_id <= 3 and _session.postgres_conn:
        asyncio.create_task(_auto_title_after_first_turn())

    # Push workspace changes to Nextcloud (fire-and-forget)
    if _session.workspace_sync:
        asyncio.create_task(_session.workspace_sync.push())


async def _loop_on_error(message: str) -> None:
    _broadcast("error", {"message": message})


async def _loop_on_vm_upgrade_needed(freeze_data: Dict[str, Any]) -> None:
    """Notify subscribers that sudo was detected and VM upgrade is available."""
    _broadcast(
        "vm_upgrade.needed",
        {
            "reason": freeze_data.get("reason", "sudo detected"),
            "command": freeze_data.get("command"),
        },
    )


async def _loop_completion_handler(loop_task: asyncio.Task) -> None:
    """Wait for the persistent loop to finish, then run reason-appropriate cleanup.

    Under headless semantics the WS handler no longer cleans up after the loop
    in its finally block — the loop outlives the WS. So we attach this
    completion handler when the loop is spawned, and it routes the exit path:

    - IdleTimeoutError → archive + terminate as "idle_timeout"
    - Other exceptions → terminate as "loop_crash"
    - Clean exit → terminate as "loop_complete"
    - CancelledError → already inside _terminate_session, do nothing
    """
    try:
        await loop_task
    except IdleTimeoutError:
        logger.info("Persistent loop exited via idle timeout")
        try:
            await _handle_idle_archive()
        except Exception as e:
            logger.warning(f"Idle archive failed: {e}")
        await _terminate_session("idle_timeout")
    except asyncio.CancelledError:
        # Cancellation came from _terminate_session itself — don't re-enter.
        # Re-raise so the wrapper task surfaces as cancelled.
        raise
    except Exception as e:
        logger.warning(f"Persistent loop crashed: {e}", exc_info=True)
        await _terminate_session("loop_crash")
    else:
        logger.info("Persistent loop completed cleanly")
        await _terminate_session("loop_complete")


def _safe_serialize(obj: Any) -> Any:
    """Make an object JSON-serializable (best effort)."""
    try:
        json.dumps(obj)
        return obj
    except (TypeError, ValueError):
        return str(obj)


async def _restore_session_messages() -> None:
    """Restore LangChain message history from DB into session.messages.

    Called during lifespan startup. On a fresh session this is a no-op.
    On pod restart or session resume, this restores the LLM's conversation
    context so it doesn't start with amnesia.
    """
    if not _session or not _agent or not _agent.postgres_conn or not _thread_id:
        return

    try:
        import uuid as _uuid
        from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

        db_messages = await _agent.postgres_conn.get_thread_messages_history(
            thread_id=_thread_id,
            limit=500,
        )

        if not db_messages:
            return

        restored: list = []
        # Track tool_call_ids from the last AIMessage for ToolMessage pairing
        pending_tool_call_ids: list[str] = []

        for db_msg in db_messages:
            role = db_msg["role"]
            content = db_msg["content"] or ""
            tool_calls = db_msg.get("tool_calls")

            # Generate a fresh UUID per restored message. Without an `id`,
            # `RemoveMessage(id=...)` in compaction is a no-op — meaning a
            # resumed session that needs compaction can never shrink. The
            # ID is a LangGraph state key, not user-facing or persisted.
            msg_id = str(_uuid.uuid4())

            if role in ("human", "user"):
                restored.append(HumanMessage(content=content, id=msg_id))

            elif role in ("ai", "assistant"):
                lc_tool_calls = []
                if tool_calls:
                    lc_tool_calls = [
                        {
                            "id": tc.get("id", ""),
                            "name": tc.get("name", ""),
                            "args": tc.get("args", {}),
                        }
                        for tc in tool_calls
                    ]
                    pending_tool_call_ids = [tc["id"] for tc in lc_tool_calls]
                else:
                    pending_tool_call_ids = []
                restored.append(
                    AIMessage(content=content, tool_calls=lc_tool_calls, id=msg_id)
                )

            elif role == "tool":
                # Pair with the next pending tool_call_id from the last AIMessage
                tool_call_id = (
                    pending_tool_call_ids.pop(0) if pending_tool_call_ids else ""
                )
                restored.append(
                    ToolMessage(
                        content=content,
                        tool_call_id=tool_call_id,
                        id=msg_id,
                    )
                )

            # Skip system messages — the loop adds a fresh one from current config

        if restored:
            _session.messages.extend(restored)
            # Set turn_count from the last message's turn_number
            last_turn = max((m.get("turn_number") or 0 for m in db_messages), default=0)
            _session.turn_count = last_turn
            logger.info(
                f"Restored {len(restored)} messages for thread {_thread_id} "
                f"(last turn: {last_turn})"
            )

    except Exception as e:
        logger.warning(f"Failed to restore session messages (non-fatal): {e}")


async def _save_message(
    client: Any,
    thread_id: str,
    role: str,
    content: Optional[str],
    tool_calls: Optional[Any],
    turn_number: int,
    tool_call_id: Optional[str] = None,
    thinking: Optional[str] = None,
) -> None:
    """Fire-and-forget: save a single message via orchestrator REST."""
    try:
        await client.save_thread_message(
            thread_id=thread_id,
            role=role,
            content=content,
            tool_calls=tool_calls,
            turn_number=turn_number,
            tool_call_id=tool_call_id,
            thinking=thinking,
        )
    except Exception as e:
        logger.warning(f"Failed to save message (non-fatal): {e}")


def _extract_thinking(msg: Any) -> Optional[str]:
    """Pull reasoning content out of an AIMessage for persistence.

    Two sources depending on the provider:
      - Anthropic: ``content`` is a list of blocks, thinking blocks carry
        ``{"type": "thinking", "thinking": "..."}``.
      - Other reasoning models (DeepSeek, GPT-5, etc.): ``additional_kwargs.
        reasoning_content`` carries a plain string.
    Returns None when the model didn't emit a visible reasoning channel.
    """
    content = getattr(msg, "content", None)
    if isinstance(content, list):
        parts = [
            b.get("thinking", "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "thinking"
        ]
        joined = "".join(parts).strip()
        if joined:
            return joined
    rc = getattr(msg, "additional_kwargs", {}).get("reasoning_content")
    return rc or None


async def _save_turn_ai_messages(
    client: Any,
    thread_id: str,
    messages: List[Any],
    turn_number: int,
    metrics: dict | None = None,
    tool_decisions: Optional[Dict[str, str]] = None,
) -> None:
    """Fire-and-forget: save AI + tool messages from the most recent turn via orchestrator REST.

    ``tool_decisions`` carries the per-call supervised approval outcome
    (``tool_call_id -> 'approved' | 'denied'``) so the decision survives
    history reload as a field on the persisted tool_calls.
    """
    try:
        # Walk backwards from the end to find messages from this turn
        # (after the last HumanMessage)
        to_save = []
        for msg in reversed(messages):
            if hasattr(msg, "type") and msg.type in ("human", "HumanMessageChunk"):
                break
            to_save.append(msg)
        to_save.reverse()

        for msg in to_save:
            raw_type = getattr(msg, "type", "unknown")
            # Normalize LangChain chunk types: AIMessageChunk → ai, etc.
            _role_map = {
                "ai": "ai",
                "AIMessageChunk": "ai",
                "human": "human",
                "HumanMessageChunk": "human",
                "tool": "tool",
                "ToolMessageChunk": "tool",
                "system": "system",
                "SystemMessageChunk": "system",
            }
            role = _role_map.get(raw_type, raw_type)
            content = msg.content if hasattr(msg, "content") else None
            tc = None
            if hasattr(msg, "tool_calls") and msg.tool_calls:
                tc = []
                for t in msg.tool_calls:
                    entry: Dict[str, Any] = {
                        "name": t.get("name"),
                        "args": t.get("args"),
                        "id": t.get("id"),
                    }
                    decision = (tool_decisions or {}).get(t.get("id") or "")
                    if decision:
                        entry["decision"] = decision
                    tc.append(entry)
            # Extract reasoning content + tool-call back-reference BEFORE we
            # flatten Anthropic's list-of-dicts content (which drops the
            # thinking blocks).
            thinking = _extract_thinking(msg) if role == "ai" else None
            tool_call_id = (
                getattr(msg, "tool_call_id", None) if role == "tool" else None
            )
            # Normalize content for Anthropic list-of-dicts format
            if isinstance(content, list):
                content = " ".join(
                    b.get("text", "") if isinstance(b, dict) else str(b)
                    for b in content
                )
            # Attach metrics only to AI messages (not tool results)
            msg_metrics = metrics if role == "ai" else None
            await client.save_thread_message(
                thread_id=thread_id,
                role=role,
                content=content,
                tool_calls=tc,
                turn_number=turn_number,
                metrics=msg_metrics,
                tool_call_id=tool_call_id,
                thinking=thinking,
            )
    except Exception as e:
        logger.warning(f"Failed to save turn messages (non-fatal): {e}")


async def _handle_compact(ws: WebSocket, focus: str = "") -> None:
    """Handle /compact command — trigger manual context compaction."""
    try:
        if not _session or not _session.context_manager:
            await _ws_send(ws, "error", {"message": "Session not ready"})
            return

        before_count = len(_session.messages)
        _session.messages[:] = await _session.context_manager.summarize_and_compact(
            messages=_session.messages,
            auxiliary=_session.auxiliary_llm,
            max_summary_length=getattr(
                _session.config.context_management, "max_summary_length", 10000
            ),
        )
        after_count = len(_session.messages)

        await _ws_send(
            ws,
            "context.compacted",
            {
                "before": before_count,
                "after": after_count,
                "focus": focus,
            },
        )
        logger.info(f"Manual compaction: {before_count} → {after_count} messages")

        # Commit + push workspace to Gitea on compaction (natural checkpoint boundary)
        if _session.workspace_manager:
            git_mgr = getattr(_session.workspace_manager, "git_manager", None)
            if git_mgr and git_mgr.is_active:
                try:
                    if git_mgr.has_uncommitted_changes():
                        git_mgr.commit(
                            f"Compaction checkpoint ({before_count} → {after_count} msgs)"
                        )
                    git_mgr.push()
                except Exception as e:
                    logger.debug(f"Git push on compaction failed (non-fatal): {e}")
    except Exception as e:
        logger.warning(f"Compaction failed: {e}")
        await _ws_send(ws, "error", {"message": f"Compaction failed: {e}"})


async def _handle_config_update(ws: WebSocket, config_override: Dict[str, Any]) -> None:
    """Apply runtime config changes (model, temperature, permission mode).

    Deep-merges *config_override* into the session config, rebuilds the
    LLM if the ``llm`` key changed, and persists the update to the
    orchestrator DB so it survives session resume.

    The cockpit only sends the model ID — never the matching ``base_url``
    or ``api_key``. We must let the orchestrator resolve credentials
    BEFORE rebuilding the LLM, otherwise endpoint-backed models silently
    route to api.openai.com with ``not-needed``.
    """
    global _session, _orchestrator_client, _thread_id

    if not _session:
        await _ws_send(ws, "error", {"message": "No active session"})
        return

    try:
        import dataclasses

        from ..core.loader import (
            _apply_settings_matrix,
            create_llm,
            deep_merge,
            load_agent_config_from_dict,
        )

        # Resolve credentials with the orchestrator first when any
        # credential-bearing slot is changing (chat model, auxiliary model,
        # or embedding env keys). The PATCH endpoint enriches the override
        # with the right base_url + api_key (custom/system endpoint or
        # built-in provider key) and returns the merged dict. Skip the
        # round trip for purely cosmetic changes (permission_mode,
        # temperature-only edits).
        embedding_env_keys = (
            "EMBEDDING_PROVIDER",
            "EMBEDDING_MODEL",
            "EMBEDDING_BASE_URL",
            "EMBEDDING_API_KEY",
        )
        env_block = config_override.get("env_keys") or {}
        needs_enrichment = bool(
            config_override.get("llm", {}).get("model")
            or config_override.get("auxiliary", {}).get("model")
            or any(k in env_block for k in embedding_env_keys)
        )
        effective_override = config_override
        if _orchestrator_client and _thread_id and needs_enrichment:
            try:
                enriched = await _orchestrator_client.update_thread_config(
                    _thread_id, config_override
                )
                if enriched is not None:
                    effective_override = enriched
                else:
                    logger.warning(
                        "Orchestrator config enrichment failed; falling back to "
                        "raw override (custom endpoints may misroute)"
                    )
            except Exception:
                logger.warning("Config persistence to orchestrator failed (non-fatal)")

        base_dict = dataclasses.asdict(_session.config)
        merged = deep_merge(base_dict, effective_override)

        # Re-apply settings_matrix when LLM config changes so model-family
        # defaults (temperature, top_p, limits) are resolved correctly.
        if effective_override.get("llm"):
            override_llm_keys = set(effective_override["llm"].keys())
            _apply_settings_matrix(
                merged, override_llm_keys, _session.config._deployment_dir
            )

        new_config = load_agent_config_from_dict(
            merged, deployment_dir=_session.config._deployment_dir
        )

        # Rebuild chat LLM if llm settings changed
        if effective_override.get("llm"):
            new_llm = create_llm(new_config.llm, new_config.limits)
            _session._llm = new_llm
            _session.config = new_config
            _session._bind_tools()
            logger.info(
                "LLM hot-swapped: model=%s, temperature=%s, base_url=%s",
                new_config.llm.model,
                new_config.llm.temperature,
                new_config.llm.base_url or "default",
            )
        else:
            _session.config = new_config

        # Rebuild auxiliary LLM if auxiliary settings changed. Symmetric to
        # the chat-side rebuild — the boot-time singleton on _agent doesn't
        # carry the new credentials, so we replace _session.auxiliary_llm
        # with a session-scoped instance.
        if effective_override.get("auxiliary"):
            from ..core.loader import LLMConfig, resolve_model_settings
            from ..services.auxiliary import AuxiliaryLLM

            aux_cfg = new_config.auxiliary
            model_settings = resolve_model_settings(
                aux_cfg.model, new_config._deployment_dir
            )
            aux_llm_config = LLMConfig(
                model=aux_cfg.model,
                base_url=aux_cfg.base_url,
                api_key=aux_cfg.api_key,
                temperature=aux_cfg.temperature,
                top_p=model_settings.get("top_p"),
                top_k=model_settings.get("top_k"),
                model_max_context_tokens=model_settings.get("model_max_context_tokens"),
                max_retries=1,
            )
            aux_inner = create_llm(aux_llm_config, new_config.limits)
            _session.auxiliary_llm = AuxiliaryLLM(
                llm=aux_inner,
                max_iterations=aux_cfg.max_iterations,
                timeout=aux_cfg.timeout,
            )
            logger.info(
                "Auxiliary hot-swapped: model=%s, base_url=%s",
                aux_cfg.model,
                aux_cfg.base_url or "default",
            )

        # Reset embedding singleton if embedding env keys changed.
        new_env_block = effective_override.get("env_keys") or {}
        if any(k in new_env_block for k in embedding_env_keys):
            for k in embedding_env_keys:
                if k in new_env_block and new_env_block[k] is not None:
                    os.environ[k] = str(new_env_block[k])
            from ..services import embedding_service as _embedding_module

            _embedding_module._embedding_service = None
            logger.info(
                "Embedding hot-swapped: provider=%s, model=%s, base_url=%s",
                new_env_block.get(
                    "EMBEDDING_PROVIDER", os.environ.get("EMBEDDING_PROVIDER")
                ),
                new_env_block.get("EMBEDDING_MODEL", os.environ.get("EMBEDDING_MODEL")),
                new_env_block.get(
                    "EMBEDDING_BASE_URL",
                    os.environ.get("EMBEDDING_BASE_URL", "default"),
                ),
            )

        # Update permission mode if included.
        # _session may have been detached concurrently — bail out cleanly
        # instead of AttributeError'ing on assignment.
        if _session is None:
            await _ws_send(ws, "error", {"message": "Session no longer active"})
            return
        pm = (config_override.get("interactive") or {}).get("permission_mode")
        if pm and pm in ("supervised", "auto_accept", "autonomous"):
            _session.permission_mode = pm
        nm = (config_override.get("interactive") or {}).get("narration_mode")
        if nm and nm in ("silent", "verbose", "auto"):
            _session.narration_mode = nm

        # Persist updates that didn't go through the enrichment PATCH above
        # (cosmetic-only changes like permission_mode, narration_mode,
        # temperature-without-model edits).
        if _orchestrator_client and _thread_id and not needs_enrichment:
            try:
                await _orchestrator_client.update_thread_config(
                    _thread_id, config_override
                )
            except Exception:
                logger.warning("Config persistence to orchestrator failed (non-fatal)")

        # Acknowledge with resolved values
        if _session is None:
            return
        await _ws_send(
            ws,
            "config.changed",
            {
                "model": new_config.llm.model,
                "temperature": new_config.llm.temperature,
                "permission_mode": _session.permission_mode,
            },
        )

    except Exception as e:
        logger.exception("Config update failed: %s", e)
        await _ws_send(ws, "error", {"message": f"Config update failed: {e}"})


async def _handle_archive(ws: WebSocket) -> None:
    """Handle /done command — end the session with memory extraction and title."""
    try:
        if not _session:
            await _ws_send(ws, "error", {"message": "Session not ready"})
            return

        # 0. Final cloud sync
        if _session.workspace_sync:
            try:
                await _session.workspace_sync.full_sync()
                await _session.workspace_sync.stop()
                await _session.workspace_sync.aclose()
            except Exception as e:
                logger.warning(f"Final cloud sync failed (non-fatal): {e}")

        # 1. Extract final memories
        recall_store = (
            getattr(_session.tool_context, "recall_store", None)
            if _session.tool_context
            else None
        )
        if recall_store and _session.auxiliary_llm and _session.messages:
            try:
                from ..services.auxiliary import extract_and_store_memories

                await extract_and_store_memories(
                    auxiliary_llm=_session.auxiliary_llm,
                    recall_store=recall_store,
                    messages=_session.messages,
                    memory_extraction_prompt=_session.config.memory.extraction_prompt
                    or "",
                )
                logger.info("Final memory extraction complete")
            except Exception as e:
                logger.warning(f"Final memory extraction failed (non-fatal): {e}")

        # 2. Generate title if untitled
        if _session.postgres_conn:
            try:
                thread = await _session.postgres_conn.get_thread(_thread_id)
                current = thread.get("title", "") if thread else ""
                if (
                    not current
                    or current.startswith("Local Session")
                    or current == "Untitled Session"
                ):
                    title = await _generate_title(
                        _session.messages, _session.auxiliary_llm
                    )
                    if title:
                        async with _session.postgres_conn.acquire() as conn:
                            await conn.execute(
                                "UPDATE threads SET title = $2 WHERE id = $1",
                                _thread_id,
                                title,
                            )
            except Exception as e:
                logger.warning(f"Title generation failed (non-fatal): {e}")

            # 3. Mark thread as ended
            try:
                await _session.postgres_conn.end_thread(_thread_id)
            except Exception as e:
                logger.warning(f"Thread end update failed: {e}")

        await _ws_send(ws, "session.ended", {"thread_id": _thread_id})
        logger.info(f"Session archived: thread={_thread_id}")
    except Exception as e:
        logger.warning(f"Archive failed: {e}")
        await _ws_send(ws, "error", {"message": f"Archive failed: {e}"})


async def _update_thread_status(status: str) -> None:
    """Update thread status via orchestrator REST (preferred) or direct DB."""
    if _orchestrator_client and _thread_id:
        try:
            await _orchestrator_client.update_thread_status(_thread_id, status)
            return
        except Exception:
            pass
    # Fallback to direct DB
    if _session and _session.postgres_conn and _thread_id:
        try:
            await _session.postgres_conn.update_thread_status(_thread_id, status)
        except Exception as e:
            logger.warning(f"Failed to update thread status to {status}: {e}")


async def _handle_idle_archive(ws: Optional[WebSocket] = None) -> None:
    """Handle idle timeout — archive session state, set thread to ended.

    `ws` is optional under headless semantics — when called from the loop's
    completion handler there's no single WS in scope; we broadcast to every
    subscriber instead. The argument is kept for back-compat with any callers
    still holding a ws reference; the broadcast reaches them too.
    """
    try:
        if not _session:
            return

        # 0. Tell every still-connected client that the session is ending so
        # the UI can flip to the resume card without waiting for a refresh.
        _broadcast("session.ended", {"thread_id": _thread_id, "reason": "idle_timeout"})

        # 1. Extract memories
        recall_store = (
            getattr(_session.tool_context, "recall_store", None)
            if _session.tool_context
            else None
        )
        if recall_store and _session.auxiliary_llm and _session.messages:
            try:
                from ..services.auxiliary import extract_and_store_memories

                await extract_and_store_memories(
                    auxiliary_llm=_session.auxiliary_llm,
                    recall_store=recall_store,
                    messages=_session.messages,
                    memory_extraction_prompt=_session.config.memory.extraction_prompt
                    or "",
                )
                logger.info("Idle archive: memory extraction complete")
            except Exception as e:
                logger.warning(f"Idle archive memory extraction failed: {e}")

        # 2. Generate title if untitled
        if _session.postgres_conn:
            try:
                thread = await _session.postgres_conn.get_thread(_thread_id)
                current = thread.get("title", "") if thread else ""
                if (
                    not current
                    or current.startswith("Local Session")
                    or current == "Untitled Session"
                ):
                    title = await _generate_title(
                        _session.messages, _session.auxiliary_llm
                    )
                    if title:
                        async with _session.postgres_conn.acquire() as conn:
                            await conn.execute(
                                "UPDATE threads SET title = $2 WHERE id = $1",
                                _thread_id,
                                title,
                            )
            except Exception as e:
                logger.warning(f"Idle title generation failed: {e}")

        # 3. Set thread to 'ended' (still resumable — `ended` is the only inactive state).
        await _update_thread_status("ended")

        # 4. Git commit + push
        if _session.workspace_manager:
            git_mgr = getattr(_session.workspace_manager, "git_manager", None)
            if git_mgr and git_mgr.is_active:
                try:
                    if git_mgr.has_uncommitted_changes():
                        git_mgr.commit(f"Idle timeout: thread {_thread_id}")
                    git_mgr.push()
                except Exception as e:
                    logger.warning(f"Idle git push failed: {e}")

        logger.info(f"Idle archive complete: thread={_thread_id}")
    except Exception as e:
        logger.warning(f"Idle archive failed: {e}")


async def _poll_workspace_ready(
    client: Any,
    thread_id: str,
    timeout: int = 120,
    poll_interval: float = 2.0,
) -> Optional[Dict[str, Any]]:
    """Poll orchestrator for workspace container readiness.

    Returns:
        Workspace config dict {"backend": "remote", "remote": {host, port, ...}}
        or None if timeout, unavailable, or no workspace provisioned.
    """
    import time

    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        ws = await client.get_thread_workspace(thread_id)
        if not ws:
            return None

        # SSH key: orchestrator sends the path it resolved (dev compose
        # key or K8s secret mount); fall back to the K8s default.
        ssh_key = ws.get("ssh_key_path") or "/run/secrets/vm-ssh-key"

        # Check VM workspace first (takes precedence over container)
        vm_status = ws.get("vm_status")
        if vm_status == "ready" and ws.get("vm_ssh_host"):
            return {
                "backend": "vm",
                "remote": {
                    "host": ws["vm_ssh_host"],
                    "port": ws.get("vm_ssh_port", 22),
                    "username": "agent-host",
                    "key_path": ssh_key,
                    "workspace_path": "/home/agent-host/workspace",
                },
                "git_remote_url": ws.get("git_remote_url"),
                "config_override": ws.get("config_override"),
                "nc_session_folder": ws.get("nc_session_folder"),
            }

        # Check container workspace
        status = ws.get("status", "none")

        if status == "ready" and ws.get("pod_ip"):
            return {
                "backend": "sandbox",
                "remote": {
                    "host": ws["pod_ip"],
                    "port": ws.get("pod_port") or 22,
                    "username": "agent-host",
                    "key_path": ssh_key,
                    "workspace_path": "/home/agent-host/workspace",
                },
                "git_remote_url": ws.get("git_remote_url"),
                "config_override": ws.get("config_override"),
                "nc_session_folder": ws.get("nc_session_folder"),
            }
        if status == "failed" and (not vm_status or vm_status == "failed"):
            logger.warning(f"Workspace provisioning failed: {ws}")
            return None
        if status == "none" and not vm_status:
            # No workspace provisioned for this thread (no K8s)
            return None

        # Still creating — wait and poll again
        await asyncio.sleep(poll_interval)

    logger.warning(f"Workspace polling timed out after {timeout}s")
    return None


async def _handle_vm_upgrade(ws: WebSocket) -> None:
    """Handle VM upgrade request from cockpit.

    Flow: request VM provisioning → poll until ready → hot-swap backend.
    """
    if not _session or not _orchestrator_client or not _thread_id:
        await _ws_send(ws, "vm_upgrade.failed", {"reason": "Session not ready"})
        return

    await _ws_send(ws, "vm_upgrade.started", {"thread_id": _thread_id})

    try:
        # 1. Request VM provisioning via orchestrator
        ok = await _orchestrator_client.request_thread_vm_upgrade(_thread_id)
        if not ok:
            await _ws_send(
                ws,
                "vm_upgrade.failed",
                {"reason": "Orchestrator rejected VM upgrade request"},
            )
            return

        # 2. Poll for VM readiness (up to 5 minutes)
        vm_config = await _poll_vm_ready(_orchestrator_client, _thread_id, timeout=300)
        if not vm_config:
            await _ws_send(
                ws,
                "vm_upgrade.failed",
                {"reason": "VM did not become ready in time"},
            )
            return

        # 3. Create new RemoteBackend pointing at VM
        from ..core.backends.remote import RemoteBackend

        shell_config = _session.config.extra.get("shell", {})
        ssh_key = os.environ.get("SSH_KEY_PATH", "/run/secrets/vm-ssh-key")
        new_backend = RemoteBackend(
            host=vm_config["ssh_host"],
            port=vm_config.get("ssh_port", 22),
            username="agent-host",
            key_path=ssh_key,
            workspace_path="/home/agent-host/workspace",
            job_id=_thread_id,
            default_timeout=shell_config.get("default_timeout", 120),
            max_tabs=shell_config.get("max_tabs", 15),
            sudo_action="allow",  # VM has its own sudo gate
        )

        # 4. Hot-swap backend on session
        _session.swap_backend(new_backend)

        # 5. VM has its own sudo gate — allow sudo through
        if _session.shell_manager and hasattr(_session.shell_manager, "sudo_action"):
            _session.shell_manager.sudo_action = "allow"

        await _ws_send(
            ws,
            "vm_upgrade.complete",
            {
                "thread_id": _thread_id,
                "ssh_host": vm_config["ssh_host"],
                "ssh_port": vm_config.get("ssh_port", 22),
            },
        )
        logger.info(f"VM upgrade complete for thread {_thread_id}")

    except Exception as e:
        logger.exception(f"VM upgrade failed for thread {_thread_id}")
        await _ws_send(ws, "vm_upgrade.failed", {"reason": str(e)})


async def _poll_vm_ready(
    client: Any,
    thread_id: str,
    timeout: int = 300,
    poll_interval: float = 3.0,
) -> Optional[Dict[str, Any]]:
    """Poll orchestrator for VM readiness.

    Returns:
        VM config dict {"ssh_host": ..., "ssh_port": ...} or None on timeout/failure.
    """
    import time

    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        ws = await client.get_thread_workspace(thread_id)
        if not ws:
            await asyncio.sleep(poll_interval)
            continue

        vm_status = ws.get("vm_status")
        if vm_status == "ready" and ws.get("vm_ssh_host"):
            return {
                "ssh_host": ws["vm_ssh_host"],
                "ssh_port": ws.get("vm_ssh_port", 22),
            }
        if vm_status == "failed":
            logger.warning(f"VM provisioning failed for thread {thread_id}")
            return None

        await asyncio.sleep(poll_interval)

    logger.warning(f"VM polling timed out after {timeout}s for thread {thread_id}")
    return None


async def _generate_title(messages: List[Any], auxiliary_llm: Any) -> Optional[str]:
    """Generate a short title from conversation using AuxiliaryLLM."""
    if not auxiliary_llm or not messages:
        logger.debug(
            "Title generation skipped: auxiliary_llm=%s, messages=%d",
            bool(auxiliary_llm),
            len(messages) if messages else 0,
        )
        return None
    try:
        from langchain_core.messages import HumanMessage as HM
        from langchain_core.messages import SystemMessage as SM

        # Grab first few exchanges for title generation
        sample = []
        for m in messages[:10]:
            content = getattr(m, "content", None)
            if isinstance(content, str) and content:
                sample.append(content[:200])
            elif isinstance(content, list):
                # Handle list-of-blocks content (e.g. responses API)
                text_parts = [
                    b.get("text", "") if isinstance(b, dict) else str(b)
                    for b in content
                ]
                joined = " ".join(t for t in text_parts if t)
                if joined:
                    sample.append(joined[:200])
        if not sample:
            logger.debug(
                "Title generation skipped: no text content in %d messages",
                len(messages),
            )
            return None

        response = await auxiliary_llm.llm.ainvoke(
            [
                SM(
                    content="Generate a short title (5-8 words) for this conversation. "
                    "Return ONLY the title, no quotes or punctuation."
                ),
                HM(content="\n".join(sample)),
            ]
        )
        text = getattr(response, "content", None) or ""
        title = text.strip()[:100] if text.strip() else None
        if not title:
            logger.debug("Title generation returned empty response")
        return title
    except Exception as e:
        logger.warning(f"Title generation error: {e}")
        return None


async def _auto_title_after_first_turn() -> None:
    """Generate and push a title after the first assistant turn (fire-and-forget).

    Loop-driven (fired from _loop_on_turn_complete), broadcasts to every
    subscriber so each attached client sees the new title.
    """
    try:
        if not _session or not _session.postgres_conn or not _thread_id:
            return
        # Check current title is still a default placeholder
        thread = await _session.postgres_conn.get_thread(_thread_id)
        current = thread.get("title", "") if thread else ""
        if (
            current
            and not current.startswith("Local Session")
            and current != "Untitled Session"
        ):
            return  # already has a real title
        title = await _generate_title(_session.messages, _session.auxiliary_llm)
        if not title:
            return
        async with _session.postgres_conn.acquire() as conn:
            await conn.execute(
                "UPDATE threads SET title = $2 WHERE id = $1",
                _thread_id,
                title,
            )
        _broadcast("title.updated", {"title": title})
        logger.info(f"Auto-titled thread {_thread_id}: {title}")
    except Exception as e:
        logger.warning(f"Auto-title generation failed (non-fatal): {e}")
