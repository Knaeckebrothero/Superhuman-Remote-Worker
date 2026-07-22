"""Persistent Agent Session State.

Encapsulates all state for an interactive persistent agent session.
Created once during lifespan startup, lives until the session ends.

Composes around UniversalAgent — reuses its initialized LLMs, DB connections,
and config without subclassing or modifying it.
"""

import asyncio
import hashlib
import json
import logging
import os
import time
from dataclasses import asdict as _dc_asdict
from dataclasses import dataclass, field
from dataclasses import is_dataclass as _dc_is_dataclass
from dataclasses import replace as _dc_replace
from pathlib import Path
from typing import Any, Dict, List, Optional

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage

from ..core.context import ContextConfig, ContextManager
from ..core.loader import (
    AgentConfig,
    FileResolver,
    get_all_tool_names,
    get_phase_system_prompt,
    get_project_root,
    load_auxiliary_prompt,
    render_instruction_content,
    supports_parallel_tool_calls,
)
from ..core.workspace import WorkspaceManager, WorkspaceManagerConfig
from ..core.workspace_backend import WorkspaceUnavailableError
from ..tools import ToolContext, load_tools, apply_instruction_enforcement
from ..tools.description_manager import apply_description_overrides

logger = logging.getLogger(__name__)


class MemoryUnavailableError(RuntimeError):
    """A configured (required) memory component could not be set up.

    Raised during session setup when the memory pipeline is configured but its
    embedding-backed stores failed to initialize, or a plugin factory could not
    resolve its transport (e.g. reranker with no reachable endpoint). Treated
    like the worker path's ``memory.required`` freeze: the session must NOT run
    half-working (silently without memory/reranking) — it fails loud. The
    lifespan handler exits the pod cleanly (status 0, no crash-loop) and the
    cockpit re-surfaces the reason via the orchestrator's create/prepare
    pre-flight. See
    docs/issues/openrouter_auxiliary_crashes_session_via_memory_reranker.md.
    """


class CloudOverlayUnavailable(Exception):
    """Precondition signal for ``POST /cloud-overlay/reset``: no session, no
    overlay manager, or an inactive overlay — the reset target simply isn't
    there (route maps it to 404 "give up", never retry).

    Deliberately NOT a ``RuntimeError`` subclass: the mount managers' real
    failure types (``OverlayMountError``, ``RcloneMountError``) both subclass
    ``RuntimeError``, so a RuntimeError-based precondition would let a genuine
    remount/vfs-refresh failure be swallowed into the 404 branch instead of
    surfacing as a 500 (retry/alert) to the orchestrator caller.
    """


def resolve_memory_extraction_prompt(config: AgentConfig) -> str:
    """Load the memory-extraction prompt through the prompt matrix.

    Mirrors the worker graph's resolution (graph.py): the auxiliary model
    drives model-family resolution, falling back to the summarization phase
    model, then the main model. ``MemoryConfig`` has no prompt attribute —
    the prompt must be resolved here and threaded to every extraction call
    site (docs/issues/memory_bugs.md B1).
    """
    aux_model = (
        config.auxiliary.model
        or config.llm.get_phase_config("summarization").model
        or config.llm.model
    )
    try:
        return load_auxiliary_prompt(config, "memory_extraction", model=aux_model)
    except Exception as e:
        logger.warning(
            "Memory extraction prompt could not be loaded — extraction "
            "will run without instructions: %s",
            e,
        )
        return ""


def resolve_citation_verification_prompt(config: AgentConfig) -> str:
    """Load the citation-verification prompt through the prompt matrix.

    Mirrors ``resolve_memory_extraction_prompt``. Persistent sessions verify
    citations on the auxiliary model (the dedicated CITATION_LLM model slot is
    a worker-dispatch concept), so resolve against the aux model family.
    """
    aux_model = (
        config.auxiliary.model
        or config.llm.get_phase_config("summarization").model
        or config.llm.model
    )
    try:
        return load_auxiliary_prompt(config, "citation_verification", model=aux_model)
    except Exception as e:
        logger.warning(
            "Citation verification prompt could not be loaded — verification "
            "will run without instructions: %s",
            e,
        )
        return ""


# Phase-specific tools that don't apply to interactive mode
_EXCLUDED_TOOLS = frozenset(
    {
        "next_phase_todos",
        "todo_complete",
        "todo_list",
        "todo_rewind",
        "mark_complete",
        "job_complete",
    }
)

_FLEET_MANAGEMENT_DISABLED_KEY = "_fleet_management_disabled"
_AGENT_CATALOG_DISABLED_KEY = "_agent_catalog_disabled"
_WORKFLOWS_DISABLED_KEY = "_workflows_disabled"
_CANVAS_DISABLED_KEY = "_canvas_disabled"
_FLEET_MANAGEMENT_CONTROL_TOOLS = {"request_workspace_upgrade"}
_CANVAS_SKILL_NAME = "present-with-canvas"
_CANVAS_SKILL_MANIFEST = f"skills/{_CANVAS_SKILL_NAME}/.srw-managed.json"
_CANVAS_SKILL_MANIFEST_OWNER = "srw-present-with-canvas-v1"

# ENOTCONN watchdog probe interval for the protected-mode capture overlay
# (design §11.6 #3). Module-level so tests can monkeypatch it down to a tiny
# value instead of sleeping through the real 60s.
_CLOUD_OVERLAY_MONITOR_INTERVAL_SECONDS = 60.0


def _fleet_management_enabled(config: Any) -> bool:
    """Return whether SRW control-plane tools should be exposed.

    Existing session configs predate a UI toggle for these app-control tools,
    so absence of the marker means enabled. The marker is written only when the
    user explicitly disables ``tools.orchestrator`` in the session config
    override. Expert/skill catalog visibility is controlled separately by
    ``tools.agent_catalog``.
    """
    extra = getattr(config, "extra", {}) or {}
    return extra.get(_FLEET_MANAGEMENT_DISABLED_KEY) is not True


def _agent_catalog_enabled(config: Any) -> bool:
    """Return whether expert/skill catalog tools should be exposed."""
    extra = getattr(config, "extra", {}) or {}
    return extra.get(_AGENT_CATALOG_DISABLED_KEY) is not True


def _workflows_enabled(config: Any) -> bool:
    """Return whether automation/project-loop workflow tools should be exposed."""
    extra = getattr(config, "extra", {}) or {}
    return extra.get(_WORKFLOWS_DISABLED_KEY) is not True


def _canvas_enabled(config: Any) -> bool:
    """Return whether the session's independent Canvas tool group is enabled."""
    extra = getattr(config, "extra", {}) or {}
    return extra.get(_CANVAS_DISABLED_KEY) is not True


@dataclass
class PersistentSession:
    """State for an interactive persistent agent session.

    Holds all components needed for the persistent loop:
    workspace, tools, LLM, context manager, and conversation history.
    """

    thread_id: str
    config: AgentConfig

    # Permission mode (switchable at runtime)
    permission_mode: str = "supervised"
    # Narration mode (switchable at runtime)
    narration_mode: str = "auto"

    # Conversation state
    messages: List[BaseMessage] = field(default_factory=list)
    turn_count: int = 0

    # Initialized during setup()
    workspace_manager: Optional[WorkspaceManager] = None
    tools: Optional[List[Any]] = None
    llm_with_tools: Optional[BaseChatModel] = None
    context_manager: Optional[ContextManager] = None
    tool_context: Optional[ToolContext] = None
    system_prompt: str = ""
    auxiliary_llm: Optional[Any] = None
    # Matrix-resolved prompt for memory extraction; threaded into the loop
    # and the teardown extraction sites (MemoryConfig carries no prompt).
    memory_extraction_prompt: str = ""
    # Citation verification (Phase 2): the AuxiliaryLLM used to verify citations
    # (the aux model in sessions) + its matrix-resolved prompt. Threaded onto
    # ToolContext so the citation engine schedules async verdict write-back.
    citation_verify_aux: Optional[Any] = None
    citation_verification_prompt: str = ""
    shell_manager: Optional[Any] = None

    # DB connections (for message persistence + memory)
    postgres_conn: Optional[Any] = None
    vector_conn: Optional[Any] = None

    # Memory/knowledge stores (initialized during setup)
    recall_store: Optional[Any] = None
    knowledge_store: Optional[Any] = None
    # MemoryManager seam (src.services.memory) — bound in _setup_memory()
    # behind memory.manager.enabled; None keeps the legacy direct-store
    # paths in persistent_graph.py and persistent_app.py.
    memory_service: Optional[Any] = None
    # B11 double-extraction guard: set after a manager-path session_end/
    # idle_archive capture so _terminate_session doesn't re-extract.
    final_memory_extracted: bool = False
    _knowledge_graph: Optional[Any] = None
    project_ids: List[str] = field(default_factory=list)
    # Owning user UUID (read from the threads row during setup). Plumbed
    # into ToolContext so agent-initiated orchestrator calls (jobs.py,
    # messaging.py) can forward X-MCP-User-Id and reach user-scoped
    # endpoints like GET /api/jobs/{id} without 401-ing.
    user_id: Optional[str] = None

    # Raw LLM (without tools bound, for summarization fallback)
    _llm: Optional[BaseChatModel] = None

    # Session task manager (lightweight in-session todos)
    session_task_manager: Optional[Any] = None

    # File checkpoints for undo (turn_id -> list of snapshots)
    file_checkpoints: Dict[int, List[Dict[str, Any]]] = field(default_factory=dict)
    # Exact hashes of companion-skill files this runtime created. The persisted
    # manifest carries the same ownership proof across session-agent restarts.
    _managed_canvas_skill_files: Dict[str, str] = field(default_factory=dict)
    _canvas_skill_manifest_owned: bool = False

    # Nextcloud workspace sync (initialized if session has nc_session_folder)
    workspace_sync: Optional[Any] = None
    # Lazy rclone cloud mounts (initialized from cloud_mount payload)
    cloud_mount_manager: Optional[Any] = None
    cloud_mount_error: Optional[str] = None
    # Capture overlay stacked on the RO lower for protected sessions (B9)
    overlay_mount_manager: Optional[Any] = None
    # mount_id of the protected_lower rclone mount, read from the mount
    # payload at overlay-creation time (never re-derived) so the ENOTCONN
    # monitor can target restart_mount() at the right mount (Task 11).
    _protected_mount_id: Optional[str] = None
    # ENOTCONN watchdog task for the capture overlay; started alongside the
    # overlay mount, cancelled in cleanup() (Task 11).
    _cloud_overlay_monitor_task: Optional[asyncio.Task] = None

    # Datasource connections keyed by type (for ToolContext)
    datasources: Dict[str, Any] = field(default_factory=dict)
    # Authorized native + selected external OKF KB scopes. External entries
    # carry ids/display metadata only; repository credentials stay orchestrator-side.
    knowledge_bindings: List[Any] = field(default_factory=list)
    # Parent clients for cleanup (e.g. MongoClient)
    _datasource_clients: Dict[str, Any] = field(default_factory=dict)
    # Raw datasource payloads (orchestrator-shaped dicts) currently attached —
    # the diff baseline + datasources.md input for live datasource changes
    # (live_session_settings.md Slice B). Set at attach; replaced by
    # resetup_datasources().
    datasource_configs: List[Dict[str, Any]] = field(default_factory=list)

    # Per-tool-call approval decisions (tool_call_id -> 'approved'|'denied').
    # Populated by the WS permission_check; consumed at turn save so the
    # decision is persisted alongside the tool call in thread_messages.
    tool_decisions: Dict[str, str] = field(default_factory=dict)

    @property
    def project_id(self) -> Optional[str]:
        """Primary project (first in list) for backward compat."""
        return self.project_ids[0] if self.project_ids else None

    async def setup(
        self,
        llm: BaseChatModel,
        auxiliary_llm: Optional[Any] = None,
        postgres_conn: Optional[Any] = None,
        vector_conn: Optional[Any] = None,
        workspace_override: Optional[Dict[str, Any]] = None,
        git_remote_url: Optional[str] = None,
        cloud_mount_cfg: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Initialize session resources.

        Creates workspace, loads tools, binds LLM, and builds system prompt.
        Reuses the same infrastructure as UniversalAgent but without phase
        alternation components.

        Args:
            llm: Base LLM instance (will be bound with tools)
            auxiliary_llm: For summarization during compaction
            postgres_conn: PostgreSQL connection (for DB tools)
            vector_conn: Vector DB connection (for citations/memory)
            workspace_override: Remote workspace config from orchestrator
                (e.g. {"backend": "remote", "remote": {"host": ..., "port": 22, ...}})
            git_remote_url: Gitea repo URL for workspace versioning
        """
        self._llm = llm
        self.auxiliary_llm = auxiliary_llm
        self.postgres_conn = postgres_conn
        self.vector_conn = vector_conn
        self.permission_mode = self.config.interactive.permission_mode
        self.narration_mode = self.config.interactive.narration_mode
        self.memory_extraction_prompt = resolve_memory_extraction_prompt(self.config)
        # Citation verification (Phase 2): reuse the auxiliary model in sessions,
        # gated by auxiliary.tasks.verify_citations.
        _verify_task = self.config.auxiliary.tasks.get("verify_citations")
        if (
            self.config.auxiliary.enabled
            and _verify_task is not None
            and _verify_task.enabled
        ):
            self.citation_verify_aux = self.auxiliary_llm
            self.citation_verification_prompt = resolve_citation_verification_prompt(
                self.config
            )
        else:
            self.citation_verify_aux = None
            self.citation_verification_prompt = ""

        # 1. Create workspace (with optional remote backend + git)
        await self._setup_workspace(
            workspace_override=workspace_override, git_remote_url=git_remote_url
        )

        # 2. Set up lazy cloud mounts before shell/tools so `/workspace/cloud`
        #    exists when the agent starts using the workspace.
        await self._setup_cloud_mount(cloud_mount_cfg)

        # 3. Set up shell manager BEFORE tools so shell tools can detect it
        self._setup_shell_manager()

        # 4. Initialize knowledge base connections BEFORE tools so knowledge
        #    tools can detect them via ToolContext.has_knowledge()
        self._setup_knowledge(vector_conn)

        # 4b. Resolve the owning user from the thread row so tools can forward
        #     X-MCP-User-Id on orchestrator calls (fixes the agent's read-job
        #     401 against require_approved_user / require_job_access endpoints).
        if postgres_conn is not None and self.user_id is None:
            try:
                thread = await postgres_conn.get_thread(self.thread_id)
                if thread and thread.get("user_id"):
                    self.user_id = str(thread["user_id"])
            except Exception as e:
                # Non-fatal — tools that need user_id will fall back to the
                # internal-only auth path. Lifecycle calls keep working.
                logger.warning(
                    "Could not resolve user_id for thread %s: %s",
                    self.thread_id,
                    e,
                )

        # 5. Create tool context and load tools
        self._setup_tools(postgres_conn)

        # 6. Bind tools to LLM
        self._bind_tools()

        # 7. Create context manager
        self._setup_context_manager()

        # 8. Build system prompt (interactive mode has its own prompt files)
        self.system_prompt = get_phase_system_prompt(
            self.config,
            is_strategic=False,
            model=self.config.llm.model or "",
            tool_names=[t.name for t in self.tools] if self.tools else None,
            prompt_type="interactive",
        )

        # 9. Set up memory (RecallStore) if enabled
        self._setup_memory(postgres_conn, vector_conn)

        logger.info(
            f"PersistentSession initialized: thread={self.thread_id}, "
            f"tools={len(self.tools or [])}, "
            f"mode={self.permission_mode}"
        )

    async def _setup_workspace(
        self,
        workspace_override: Optional[Dict[str, Any]] = None,
        git_remote_url: Optional[str] = None,
    ) -> None:
        """Create workspace using a remote backend (required).

        Persistent sessions always require an isolated workspace container or
        VM. If the remote backend is not immediately reachable (e.g. sshd
        still starting), retries with exponential backoff for up to 5 minutes
        before raising ``WorkspaceUnavailableError``.

        Args:
            workspace_override: If provided, overrides config workspace settings.
                Expected shape: {"backend": "remote", "remote": {"host": ..., "port": 22, ...}}
            git_remote_url: Gitea repo URL for workspace versioning (clones on init)
        """
        ws_data = self.config.workspace
        base_path = os.getenv("WORKSPACE_PATH", "./workspace")

        effective_backend = (workspace_override or {}).get("backend") or ws_data.backend
        remote_cfg = (workspace_override or {}).get("remote") or ws_data.remote

        # No-workspace tiers (virtual/none): no SSH workspace pod. Build the
        # lite backend directly, with git off (§8 — lite tiers have no git).
        from ..core.backends.factory import LITE_BACKENDS, create_lite_backend

        if effective_backend in LITE_BACKENDS:
            from types import SimpleNamespace

            lite_cfg = SimpleNamespace(
                backend=effective_backend,
                mounts=(workspace_override or {}).get("mounts") or ws_data.mounts,
            )
            workspace_backend = create_lite_backend(lite_cfg, job_id=self.thread_id)
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, workspace_backend.connect)
            self.workspace_manager = WorkspaceManager(
                job_id=self.thread_id,
                base_path=base_path,
                config=WorkspaceManagerConfig(
                    base_path=base_path,
                    structure=ws_data.structure,
                    git_versioning=False,
                ),
                backend=workspace_backend,
            )
            self.workspace_manager.initialize()
            self._deploy_instruction_files()
            logger.info(
                "Lite workspace ready (backend=%s, no workspace pod)",
                effective_backend,
            )
            return

        if not (effective_backend in ("sandbox", "vm", "remote") and remote_cfg):
            raise RuntimeError(
                "No workspace configured. Persistent sessions require "
                "an isolated workspace (sandbox or vm) with SSH credentials."
            )

        from ..core.backends.remote import RemoteBackend

        shell_config = self.config.extra.get("shell", {})
        max_duration = 300  # 5 minutes
        start = time.monotonic()
        backoff = 5.0
        attempt = 0
        workspace_backend = None

        while True:
            attempt += 1
            try:
                workspace_backend = RemoteBackend(
                    host=remote_cfg["host"],
                    port=remote_cfg.get("port", 22),
                    username=remote_cfg.get("username", "agent-host"),
                    key_path=remote_cfg.get("key_path", "/run/secrets/vm-ssh-key"),
                    workspace_path=remote_cfg.get(
                        "workspace_path", "/home/agent-host/workspace"
                    ),
                    job_id=self.thread_id,
                    default_timeout=shell_config.get("default_timeout", 120),
                    max_tabs=shell_config.get("max_tabs", 15),
                    connect_timeout=remote_cfg.get("connect_timeout", 30),
                    max_retries=remote_cfg.get("max_retries", 5),
                    retry_timeouts_as_booting=remote_cfg.get(
                        "retry_timeouts_as_booting", False
                    ),
                    sudo_action=shell_config.get("sudo_action", "freeze"),
                )
                # Only the internal attach API can attest that this exact SSH
                # endpoint is paired to a Canvas generation and pinned host
                # identity. Direct remote config, VMs, and legacy overrides all
                # remain fail-closed even when the connection itself is usable.
                workspace_backend.supports_canvas_presentation = (
                    workspace_override or {}
                ).get("canvas_presentation_available") is True
                workspace_backend.supports_canvas_live_apps = (
                    workspace_override or {}
                ).get("canvas_live_apps_available") is True
                workspace_backend.supports_canvas_shared_browser = (
                    workspace_override or {}
                ).get("canvas_shared_browser_available") is True
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, workspace_backend.connect)
                logger.info(
                    f"Remote workspace backend connected to {remote_cfg['host']}"
                )
                break
            except Exception as e:
                elapsed = time.monotonic() - start
                if elapsed >= max_duration:
                    raise WorkspaceUnavailableError(
                        f"Failed to connect to workspace {remote_cfg['host']} "
                        f"after {attempt} attempts ({elapsed:.0f}s): {e}"
                    ) from e
                logger.warning(
                    "Workspace connect attempt %d failed (%.0fs elapsed, "
                    "retrying in %.0fs): %s",
                    attempt,
                    elapsed,
                    backoff,
                    e,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

        ws_config = WorkspaceManagerConfig(
            base_path=base_path,
            structure=ws_data.structure,
            git_versioning=ws_data.git_versioning,
            git_remote_url=git_remote_url,
        )
        self.workspace_manager = WorkspaceManager(
            job_id=self.thread_id,
            base_path=base_path,
            config=ws_config,
            backend=workspace_backend,
        )
        self.workspace_manager.initialize()
        self._deploy_instruction_files()
        logger.info(
            f"Workspace created at {self.workspace_manager.path} (backend=remote)"
        )

    async def _setup_cloud_mount(
        self, cloud_mount_cfg: Optional[Dict[str, Any]]
    ) -> None:
        """Start the RO rclone lower, then (protected mode) the capture overlay."""
        if not cloud_mount_cfg:
            return
        if not self.workspace_manager:
            return
        try:
            from src.services.cloud_mount import RcloneMountManager

            self.cloud_mount_manager = RcloneMountManager(
                thread_id=self.thread_id,
                cloud_cfg=cloud_mount_cfg,
                workspace_backend=self.workspace_manager.backend,
                workspace_root=self.workspace_manager.path,
            )
            await self.cloud_mount_manager.start_all()
            logger.info(
                "Cloud mount manager started with %d mount(s)",
                len(self.cloud_mount_manager.mounts),
            )
        except Exception as e:
            self.cloud_mount_error = str(e)
            self.cloud_mount_manager = None
            logger.warning("Failed to start cloud mount manager: %s", e)
            return

        if cloud_mount_cfg.get("protected") and cloud_mount_cfg.get("overlay"):
            try:
                from src.services.cloud_overlay import OverlayMountManager

                # Read the protected_lower mount_id from the payload the
                # session actually received — never re-derive the
                # f"protected-{thread_id}" format (Task 11).
                self._protected_mount_id = next(
                    (
                        str(m.get("mount_id"))
                        for m in cloud_mount_cfg.get("mounts") or []
                        if m.get("mount_kind") == "protected_lower"
                    ),
                    None,
                )
                self.overlay_mount_manager = OverlayMountManager(
                    thread_id=self.thread_id,
                    overlay_cfg=cloud_mount_cfg["overlay"],
                    workspace_backend=self.workspace_manager.backend,
                    workspace_root=self.workspace_manager.path,
                )
                # runs the mount script on the workspace pod over SSH
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, self.overlay_mount_manager.mount)
                logger.info(
                    "Capture overlay mounted for protected session %s", self.thread_id
                )
                # ENOTCONN watchdog (design §11.6 #3) — started here alongside
                # the overlay, cancelled in cleanup() alongside the unmount.
                self._cloud_overlay_monitor_task = asyncio.create_task(
                    self._cloud_overlay_monitor_loop(),
                    name=f"cloud-overlay-monitor-{self.thread_id[:8]}",
                )
            except Exception as e:
                self.cloud_mount_error = f"overlay: {e}"
                self.overlay_mount_manager = None
                self._protected_mount_id = None
                # Fail-safe: a protected session whose overlay failed to mount
                # must NOT keep writing to the raw RO lower thinking it's
                # protected. Tear down the rclone mount too so the session
                # ends up with NO cloud access rather than a half-protected
                # (unprotected) one (deviation from the brief's snippet,
                # which left the lower mounted on overlay failure).
                logger.warning(
                    "Failed to mount capture overlay: %s — tearing down RO lower "
                    "to avoid a half-protected session",
                    e,
                )
                try:
                    await self.cloud_mount_manager.aclose()
                except Exception as close_err:
                    logger.warning(
                        "Error tearing down RO lower after overlay failure: %s",
                        close_err,
                    )
                self.cloud_mount_manager = None

    async def _cloud_overlay_monitor_loop(self) -> None:
        """ENOTCONN watchdog for the protected overlay (design §11.6 #3).

        Every probe interval, when the overlay is active: probe with
        ``health_check()``; on a dead lower, log and heal via
        ``overlay.heal(remount_lower=...)``, where the callback restarts the
        one rclone mount backing the lower (never the whole manager). Started
        alongside the overlay mount in ``_setup_cloud_mount`` and cancelled in
        ``cleanup()``. Must never die from an exception — a health_check or
        heal failure is logged and simply retried on the next tick.
        """
        while True:
            # Everything after the sleep is guarded (mirrors
            # RcloneMountManager._token_refresh_loop): an unexpected failure
            # anywhere here — including reading overlay_mount_manager/.active,
            # not just the health_check/heal calls — must not kill the task.
            try:
                await asyncio.sleep(_CLOUD_OVERLAY_MONITOR_INTERVAL_SECONDS)
                overlay = self.overlay_mount_manager
                if overlay is None or not overlay.active:
                    continue
                healthy = await asyncio.to_thread(overlay.health_check)
                if healthy:
                    continue
                logger.warning(
                    "cloud overlay unhealthy (ENOTCONN) — healing thread=%s",
                    self.thread_id,
                )
                await asyncio.to_thread(
                    overlay.heal,
                    lambda: self.cloud_mount_manager.restart_mount(
                        self._protected_mount_id
                    ),
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("overlay heal failed (will retry next probe): %s", e)

    def reset_cloud_overlay(self) -> None:
        """Post-apply/reject reset: discard the staged upperdir and remount
        with a fresh workdir, then refresh the RO lower.

        Blocking (runs remote scripts over SSH) — the route/caller must run
        this via ``asyncio.to_thread``. Called by the orchestrator via
        ``POST /cloud-overlay/reset`` after a user applies or rejects a
        staged cloud diff (Task 10).
        """
        overlay = self.overlay_mount_manager
        if overlay is None or not overlay.active:
            raise CloudOverlayUnavailable("no active cloud overlay")
        overlay.reset_upper(
            refresh_lower=lambda: self.cloud_mount_manager.refresh_vfs()
        )

    def _scope_skills_for_tool_names(self, tool_names: List[str]) -> None:
        """Apply capability-aware optional-skill scope without mutating a base config."""
        from src.core.skill_resolution import (
            add_default_canvas_skill,
            scope_skills_for_tools,
        )

        skill_catalog = self.config.extra.get(
            "_unscoped_resolved_skills",
            self.config.extra.get("_resolved_skills", {}),
        )
        # Canvas is available independently of the optional DB skill catalog.
        # Add exactly its bundled companion as a floor; the scope call below
        # still withholds it until both tools actually instantiated.
        skill_catalog = add_default_canvas_skill(skill_catalog)
        scoped = scope_skills_for_tools(skill_catalog, tool_names)
        next_extra = {
            **self.config.extra,
            "_unscoped_resolved_skills": skill_catalog,
            "_resolved_skills": scoped,
        }
        if _dc_is_dataclass(self.config):
            self.config = _dc_replace(self.config, extra=next_extra)
        else:  # Lightweight test/config adapters may not be real dataclasses.
            self.config.extra = next_extra
        if self.tool_context is not None:
            # use_skill authorizes by the CURRENT scoped menu, not by stale
            # workspace bytes. Keep its long-lived ToolContext synchronized on
            # every backend/config rebind.
            self.tool_context.config["_resolved_skills"] = scoped

    def _deploy_catalog_skill_files(
        self, only_names: Optional[set[str]] = None
    ) -> None:
        """Materialize currently scoped model-invoked skill files.

        Ordinary catalog skills retain their historical add-only behavior.
        The capability-scoped Canvas companion is reconciled in both
        directions: files created by SRW are withdrawn when either required
        tool disappears, while modified files and unrelated files in the same
        directory are treated as user content and preserved.
        """
        from src.core.skill_resolution import skill_files_to_workspace

        skills_files = self.config.extra.get("_resolved_skills", {}).get("files", {})
        if only_names is not None:
            skills_files = {
                name: files
                for name, files in skills_files.items()
                if name in only_names
            }
        ordinary_files = {
            name: files
            for name, files in skills_files.items()
            if name != _CANVAS_SKILL_NAME
        }
        for ws_path, content in skill_files_to_workspace(ordinary_files).items():
            if self.workspace_manager.exists(ws_path):
                continue  # don't overwrite on session resume
            parent_dir = str(Path(ws_path).parent)
            if parent_dir and parent_dir != ".":
                self.workspace_manager.backend.mkdir(parent_dir)
            self.workspace_manager.write_file(ws_path, content)
            logger.debug(f"Deployed skill file to workspace: {ws_path}")

        if only_names is None or _CANVAS_SKILL_NAME in only_names:
            desired = skills_files.get(_CANVAS_SKILL_NAME)
            self._reconcile_canvas_skill_files(
                desired if isinstance(desired, dict) else {}
            )

    @staticmethod
    def _skill_file_digest(content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def _load_canvas_skill_manifest(self, allowed_paths: set[str]) -> Dict[str, str]:
        """Load only a manifest carrying SRW's exact ownership marker."""

        self._canvas_skill_manifest_owned = False
        try:
            if not self.workspace_manager.exists(_CANVAS_SKILL_MANIFEST):
                return {}
            payload = json.loads(
                self.workspace_manager.read_file(_CANVAS_SKILL_MANIFEST)
            )
            if payload.get("managed_by") != _CANVAS_SKILL_MANIFEST_OWNER:
                return {}
            raw_files = payload.get("files")
            if not isinstance(raw_files, dict):
                return {}
            from src.core.skill_format import validate_skill_path

            loaded: Dict[str, str] = {}
            for rel_path, digest in raw_files.items():
                validate_skill_path(rel_path)
                if not isinstance(digest, str) or len(digest) != 64:
                    return {}
                int(digest, 16)
                workspace_path = f"skills/{_CANVAS_SKILL_NAME}/{rel_path}"
                # The manifest is an ownership record, not authority to delete
                # arbitrary sibling files. Only currently resolved bundle paths
                # may ever be managed.
                if workspace_path in allowed_paths:
                    loaded[workspace_path] = digest
            self._canvas_skill_manifest_owned = True
            return loaded
        except Exception:
            # An unrecognized file at the reserved path may be user content.
            # Never overwrite or delete it without our ownership marker.
            logger.warning("Could not load the managed Canvas skill manifest")
            return {}

    def _store_canvas_skill_manifest(self, managed: Dict[str, str]) -> None:
        prefix = f"skills/{_CANVAS_SKILL_NAME}/"
        if not managed:
            if self._canvas_skill_manifest_owned and self.workspace_manager.exists(
                _CANVAS_SKILL_MANIFEST
            ):
                self.workspace_manager.delete_file(_CANVAS_SKILL_MANIFEST)
            self._canvas_skill_manifest_owned = False
            return

        if (
            self.workspace_manager.exists(_CANVAS_SKILL_MANIFEST)
            and not self._canvas_skill_manifest_owned
        ):
            # Preserve a pre-existing unowned marker-shaped path. In-memory
            # ownership still makes withdrawal safe for this runtime.
            return
        payload = {
            "managed_by": _CANVAS_SKILL_MANIFEST_OWNER,
            "files": {
                path.removeprefix(prefix): digest
                for path, digest in sorted(managed.items())
                if path.startswith(prefix)
            },
        }
        self.workspace_manager.backend.mkdir(prefix.rstrip("/"))
        self.workspace_manager.write_file(
            _CANVAS_SKILL_MANIFEST,
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
        )
        self._canvas_skill_manifest_owned = True

    def _reconcile_canvas_skill_files(self, desired_files: Dict[str, str]) -> None:
        """Converge the one capability-scoped skill without deleting user work."""

        from src.core.skill_format import validate_skill_path

        prefix = f"skills/{_CANVAS_SKILL_NAME}/"
        unscoped_files = (
            self.config.extra.get("_unscoped_resolved_skills", {})
            .get("files", {})
            .get(_CANVAS_SKILL_NAME, {})
        )
        allowed_paths: set[str] = set()
        if isinstance(unscoped_files, dict):
            for rel_path in unscoped_files:
                if not isinstance(rel_path, str):
                    continue
                try:
                    allowed_paths.add(f"{prefix}{validate_skill_path(rel_path)}")
                except ValueError:
                    logger.warning(
                        "Ignoring unsafe Canvas skill path in resolved catalog: %r",
                        rel_path,
                    )

        desired: Dict[str, str] = {}
        for rel_path, content in desired_files.items():
            if not isinstance(rel_path, str) or not isinstance(content, str):
                logger.warning("Ignoring malformed Canvas skill file entry")
                continue
            try:
                path = f"{prefix}{validate_skill_path(rel_path)}"
            except ValueError:
                logger.warning(
                    "Ignoring unsafe Canvas skill path in scoped catalog: %r",
                    rel_path,
                )
                continue
            desired[path] = content
            allowed_paths.add(path)

        managed = self._load_canvas_skill_manifest(allowed_paths)
        managed.update(
            {
                path: digest
                for path, digest in self._managed_canvas_skill_files.items()
                if path in allowed_paths
            }
        )

        # Withdraw obsolete/disabled files only when their current bytes still
        # match the hash SRW recorded when it wrote them. A modified file has
        # become user content and is deliberately released from management.
        for path, digest in list(managed.items()):
            if path in desired:
                continue
            try:
                if not self.workspace_manager.exists(path):
                    managed.pop(path, None)
                    continue
                unchanged = (
                    self._skill_file_digest(self.workspace_manager.read_file(path))
                    == digest
                )
                if not unchanged:
                    # Modified managed bytes have become user content.
                    managed.pop(path, None)
                    continue
                if self.workspace_manager.delete_file(path) is False:
                    logger.warning(
                        "Could not withdraw managed Canvas skill file: %s", path
                    )
                    continue
            except Exception:
                logger.warning("Could not withdraw managed Canvas skill file: %s", path)
                # Retain ownership after a transient workspace failure so a
                # later reconciliation can retry the safe digest-checked delete.
                continue
            managed.pop(path, None)

        # Never overwrite a pre-existing or user-modified file. An unchanged
        # SRW-owned file may be upgraded when bundled desired bytes change.
        for path, content in desired.items():
            expected = self._skill_file_digest(content)
            if self.workspace_manager.exists(path):
                try:
                    current = self._skill_file_digest(
                        self.workspace_manager.read_file(path)
                    )
                except Exception:
                    continue
                recorded = managed.get(path)
                if recorded != current:
                    managed.pop(path, None)
                    continue
                if current != expected:
                    self.workspace_manager.write_file(path, content)
                    managed[path] = expected
                    logger.debug("Upgraded managed Canvas skill file: %s", path)
                continue
            parent_dir = str(Path(path).parent)
            self.workspace_manager.backend.mkdir(parent_dir)
            self.workspace_manager.write_file(path, content)
            managed[path] = expected
            logger.debug("Deployed managed Canvas skill file: %s", path)

        self._managed_canvas_skill_files = managed
        self._store_canvas_skill_manifest(managed)

    def _deploy_instruction_files(self) -> None:
        """Deploy instruction files from config to workspace.

        Mirrors the worker-mode pattern in agent.py._deploy_instruction_files().
        Copies files like design_guide.md from the expert config directory into
        the workspace so the agent can read them via workspace tools.
        """
        # Workspace creation precedes ToolContext identity resolution and tool
        # instantiation. Fail closed here: deploy ordinary catalog skills now,
        # but admit present-with-canvas only after the actual loaded tool list is
        # known in _load_tools_for_backend().
        self._scope_skills_for_tool_names([])

        # Skill directories (Slice 2) — mirror of the agent.py worker path. Runs
        # before the instruction-files guard since skills come from the frozen
        # blob (_resolved_skills), not the expert config dir.
        self._deploy_catalog_skill_files()

        if not self.config.instruction_files:
            return

        # file_resolver is only needed for literal ``file:`` instruction entries
        # (loaded from the deployment/templates dir). Bound ``skill:`` entries come
        # from the frozen ``_resolved_instructions`` blob and need no deployment
        # dir — so DON'T gate the whole loop on ``_deployment_dir`` (which is None
        # for sessions), or bound skills silently fail to deploy and their
        # ``before_tool`` enforce gate bricks the tool (the cite-as-you-write case).
        file_resolver = None
        if self.config._deployment_dir:
            templates_dir = get_project_root() / "config" / "templates"
            file_resolver = FileResolver(
                deployment_dir=self.config._deployment_dir,
                framework_dir=templates_dir,
            )
        for entry in self.config.instruction_files:
            try:
                if entry.skill:
                    # Bound skill: flag-independent instructions channel → skills/<skill>/SKILL.md.
                    # Backend-aware check: get_path().exists() tests the agent
                    # pod's local filesystem and is always False on remote
                    # workspaces, which would redeploy (clobber) on resume.
                    if self.workspace_manager.exists(entry.path):
                        continue  # don't overwrite on session resume
                    content = self.config.extra.get("_resolved_instructions", {}).get(
                        entry.skill
                    )
                    if not content:
                        logger.warning(
                            f"Bound skill content missing from blob: {entry.skill}"
                        )
                        continue
                    content = render_instruction_content(content, [])
                    parent_dir = str(Path(entry.path).parent)
                    if parent_dir and parent_dir != ".":
                        self.workspace_manager.backend.mkdir(parent_dir)
                    self.workspace_manager.write_file(entry.path, content)
                    logger.debug(f"Deployed bound skill to workspace: {entry.path}")
                    continue
                # Skip if already present (don't overwrite on session resume).
                # Backend-aware for the same reason as the skill branch above.
                if self.workspace_manager.exists(entry.file):
                    continue
                if file_resolver is None:
                    logger.warning(
                        f"Cannot deploy instruction file {entry.file}: "
                        "no deployment dir"
                    )
                    continue
                content = file_resolver.load(Path(entry.file).name)
                content = render_instruction_content(content, [])
                # Ensure parent directory exists
                parent_dir = str(Path(entry.file).parent)
                if parent_dir and parent_dir != ".":
                    self.workspace_manager.backend.mkdir(parent_dir)
                self.workspace_manager.write_file(entry.file, content)
                logger.debug(f"Deployed instruction file to workspace: {entry.file}")
            except FileNotFoundError:
                logger.warning(f"Instruction file not found: {entry.file}")
            except Exception as e:
                logger.warning(f"Failed to deploy instruction file {entry.file}: {e}")

    def _setup_knowledge(self, vector_conn: Optional[Any]) -> None:
        """Initialize the pgvector store and optional Neo4j Graph tier.

        Must be called BEFORE _setup_tools() so that the ToolContext
        has knowledge_graph and knowledge_store set, allowing knowledge
        tools to pass the has_knowledge() guard in load_tools().

        Mirrors the worker agent pattern in agent.py._setup_job_tools().
        """
        if not self.knowledge_bindings and not self.project_ids:
            return  # No native or selected external knowledge scope

        self._kb_degraded = False
        try:
            if vector_conn is None:
                raise RuntimeError("Vector database connection is unavailable")

            from src.services.embedding_service import get_kb_embedding_service
            from src.services.knowledge_store import KnowledgeStore

            embedding_service = get_kb_embedding_service()
            self.knowledge_store = KnowledgeStore(
                db=vector_conn,
                embedding_service=embedding_service,
            )
            logger.info(
                "Knowledge store initialized for %d KB binding(s)",
                len(self.knowledge_bindings) or len(self.project_ids),
            )
        except Exception as e:
            self._kb_degraded = True
            from src.core.archiver import audit_unavailable as _audit_unavailable

            _audit_unavailable(
                job_id=self.thread_id,
                agent_type=self.config.agent_id,
                step_type="kb_unavailable",
                component="KnowledgeStore",
                error=e,
                node_name="session_setup",
                extra={
                    "embedding_provider": os.environ.get(
                        "KB_EMBEDDING_PROVIDER",
                        os.environ.get("EMBEDDING_PROVIDER", "local"),
                    ),
                },
            )
            logger.warning(
                f"Failed to initialize knowledge store (non-fatal): {e} "
                f"[embedding_provider={os.environ.get('KB_EMBEDDING_PROVIDER', os.environ.get('EMBEDDING_PROVIDER', 'local'))}]"
            )

        if not self.project_ids:
            return

        try:
            from src.services.knowledge_graph import KnowledgeGraphDB

            kg = KnowledgeGraphDB()
            if kg.connect():
                self._knowledge_graph = kg
                logger.info(
                    f"Knowledge Graph tier initialized for project(s) "
                    f"{self.project_ids}"
                )
            else:
                logger.warning("Failed to connect to Neo4j — Graph tier disabled")
        except Exception as e:
            # Neo4j is optional: do not mark vector search/read as degraded.
            logger.warning(f"Failed to initialize Neo4j Graph tier (non-fatal): {e}")

    def _setup_tools(self, postgres_conn: Optional[Any]) -> None:
        """Load tools from config, excluding phase-specific ones."""
        tool_config = {
            **self.config.extra,
            "agent_id": self.config.agent_id,
            "multimodal": self.config.llm.multimodal,
            # Lets bulk readers cap a single tool result relative to the main
            # model's window (session_silent_failure_audit.md #5).
            "model_max_context_tokens": self.config.limits.model_max_context_tokens,
            # Per-family page-render DPI (None -> renderer default 150).
            "pdf_render_dpi": getattr(self.config.limits, "pdf_render_dpi", None),
            # Delegation tools read their settings from this plain dict —
            # "delegation" is a parsed/known config field, so it is NOT part
            # of config.extra (mirrors agent.py's worker tool_config).
            "delegation": _dc_asdict(self.config.delegation),
            "cloud_mount": {
                "active": bool(
                    self.cloud_mount_manager and self.cloud_mount_manager.active
                ),
                "root": "/cloud",
                "workspace_entry": "/workspace/cloud",
                "scan_guard": self.config.extra.get("cloud_scan_guard", "block"),
                "_manager": self.cloud_mount_manager,
                "protected": bool(
                    self.overlay_mount_manager and self.overlay_mount_manager.active
                ),
                "_overlay_manager": self.overlay_mount_manager,
            },
        }
        # Initialize session task manager
        from ..managers.session_tasks import SessionTaskManager

        self.session_task_manager = SessionTaskManager()

        self.tool_context = ToolContext(
            workspace_manager=self.workspace_manager,
            todo_manager=None,  # No TodoManager in persistent mode
            postgres_db=postgres_conn,
            vector_db=self.vector_conn,  # Citations live in srw_vector
            verify_aux=self.citation_verify_aux,
            verify_citation_prompt=self.citation_verification_prompt,
            datasources=self.datasources,
            config=tool_config,
            _job_id=self.thread_id,
            _thread_id=self.thread_id,
            user_id=self.user_id,
            _llm_config=self.config.llm,
            _instruction_files=self.config.instruction_files,
            shell_manager=self.shell_manager,  # Set before tool loading
            session_task_manager=self.session_task_manager,
            knowledge_graph=self._knowledge_graph,
            knowledge_store=self.knowledge_store,
            knowledge_bindings=list(self.knowledge_bindings),
        )
        if self.project_ids:
            self.tool_context.project_ids = self.project_ids

        # Wire file checkpoint callback for undo support
        self.tool_context._snapshot_callback = lambda path: self.snapshot_file(
            path, self.turn_count
        )

        self._load_tools_for_backend()

    def _load_tools_for_backend(self) -> None:
        """Resolve, filter, load, document, and post-process the toolset for
        the CURRENT workspace backend, setting ``self.tools``.

        Factored out of ``_setup_tools`` so ``resetup_tools_for_backend`` can
        re-derive the toolset after a live backend swap (e.g. ``virtual`` →
        ``sandbox``) WITHOUT rebuilding ``tool_context`` or resetting
        ``session_task_manager``. Reads only instance state already set by
        ``_setup_tools`` (``tool_context``, ``config``, ``workspace_manager``,
        ``cloud_mount_manager``), so it is safe to call again post-swap.
        """
        # Get all tool names and filter out phase-specific ones
        all_names = get_all_tool_names(self.config)
        tool_names = [n for n in all_names if n not in _EXCLUDED_TOOLS]

        # Always include session task tools in persistent mode
        for name in ["task_add", "task_complete", "task_list"]:
            if name not in tool_names:
                tool_names.append(name)

        # Fleet Management is the UI-facing group for SRW control-plane tools.
        # Experts & Skills and Automations & Loops are separate groups keyed by
        # ``tools.agent_catalog`` and ``tools.workflows``. New resolved configs
        # carry explicit off-markers from their complete merged tool policy;
        # marker absence stays enabled only for legacy-session compatibility.
        from ..tools.registry import get_tools_by_category

        fleet_management_enabled = _fleet_management_enabled(self.config)
        agent_catalog_enabled = _agent_catalog_enabled(self.config)
        workflows_enabled = _workflows_enabled(self.config)
        fleet_management_tools = set(get_tools_by_category("orchestrator"))
        fleet_management_tools.update(_FLEET_MANAGEMENT_CONTROL_TOOLS)
        agent_catalog_tools = set(get_tools_by_category("agent_catalog"))
        workflow_tools = set(get_tools_by_category("workflows"))
        canvas_tools = set(get_tools_by_category("canvas"))

        if not fleet_management_enabled:
            tool_names = [
                name for name in tool_names if name not in fleet_management_tools
            ]
        else:
            _ORCHESTRATOR_TOOLS = [
                "get_session_context",
                "create_worker_job",
                "list_worker_jobs",
                "get_worker_job",
                "get_job_workspace_file",
                "approve_worker_job",
                "resume_worker_job",
                "cancel_worker_job",
                "pause_worker_job",
                "get_current_project",
                "list_project_jobs",
                "list_project_repositories",
                "get_default_project_repository",
            ]
            for name in _ORCHESTRATOR_TOOLS:
                if name not in tool_names:
                    tool_names.append(name)

        if not agent_catalog_enabled:
            tool_names = [
                name for name in tool_names if name not in agent_catalog_tools
            ]
        else:
            _AGENT_CATALOG_TOOLS = [
                "list_experts",
                "get_expert",
                "list_skills",
                "search_skills",
                "get_skill",
            ]
            for name in _AGENT_CATALOG_TOOLS:
                if name not in tool_names:
                    tool_names.append(name)

        if not workflows_enabled:
            tool_names = [name for name in tool_names if name not in workflow_tools]
        else:
            _WORKFLOW_DEFAULT_TOOLS = [
                "list_automations",
                "get_automation",
                "list_automation_runs",
                "propose_automation",
                "get_project_loop",
                "list_project_loop_jobs",
                "explain_project_loop",
            ]
            for name in _WORKFLOW_DEFAULT_TOOLS:
                if name not in tool_names:
                    tool_names.append(name)

        if not _canvas_enabled(self.config):
            tool_names = [name for name in tool_names if name not in canvas_tools]

        if self.cloud_mount_manager and self.cloud_mount_manager.active:
            if "srw_cloud_status" not in tool_names:
                tool_names.append("srw_cloud_status")

        # Lite-tier only: expose the agent-initiated upgrade request
        # (workspace_tier_upgrade.md §4.2 S5) so a no-shell session can ASK for a
        # real sandbox. Gated on the backend lacking a shell — after a virtual →
        # sandbox swap this re-derives against the now-shell-capable backend and
        # the tool drops out (nothing left to upgrade to).
        _backend = getattr(self.workspace_manager, "backend", None)
        if (
            fleet_management_enabled
            and _backend is not None
            and getattr(_backend, "supports_shell", False)
        ):
            if "checkout_project_repository" not in tool_names:
                tool_names.append("checkout_project_repository")
        if (
            fleet_management_enabled
            and _backend is not None
            and not getattr(_backend, "supports_shell", False)
        ):
            if "request_workspace_upgrade" not in tool_names:
                tool_names.append("request_workspace_upgrade")

        # Capability gate: drop tools the workspace backend can't support (lite
        # tiers — no_workspace_agent_mode.md §3.2/§7). Mirrors the worker path.
        # On a backend swap this RE-FILTERS against the new backend, so an
        # upgrade (virtual → sandbox) re-admits shell/git/file tools the lite
        # tier had stripped.
        from ..tools.registry import filter_tools_by_backend

        tool_names = filter_tools_by_backend(
            tool_names, getattr(self.workspace_manager, "backend", None)
        )

        try:
            self.tools = load_tools(tool_names, self.tool_context)
        except ValueError as e:
            logger.warning(f"Tool loading warning: {e}")
            # Load only implemented tools individually
            self.tools = []
            for name in tool_names:
                try:
                    self.tools.extend(load_tools([name], self.tool_context))
                except ValueError:
                    logger.debug(f"Tool not implemented: {name}")

        if not self.tools:
            # Floor rule (live_session_settings.md Slice B, provider research):
            # never rebind to an EMPTY toolset when history may contain tool
            # calls — proxies 400, Anthropic degrades to empty responses,
            # strict chat templates crash. Structurally the session task tools
            # are always appended above, so this only fires if every candidate
            # failed to instantiate — keep a minimal built-in belt anyway.
            logger.warning(
                "Toolset resolved empty — binding minimal session task tools "
                "(never-bind-zero floor)"
            )
            self.tools = load_tools(
                ["task_add", "task_complete", "task_list"], self.tool_context
            )

        # The model-facing skill menu follows tools that actually instantiated,
        # not merely configured candidates. This fails closed if a Canvas
        # adapter or persistent identity was unavailable during registration.
        loaded_tool_names = [tool.name for tool in self.tools]
        self._scope_skills_for_tool_names(loaded_tool_names)
        # This is the first point at which present-with-canvas may touch the
        # workspace. Re-running after a none→workspace upgrade is idempotent and
        # admits it only when both use_skill and set_canvas actually loaded.
        self._deploy_catalog_skill_files({"present-with-canvas"})

        # Generate tool documentation in workspace (before overrides so full
        # docstrings are captured — mirrors agent.py._setup_job_tools)
        try:
            from ..tools import generate_workspace_tool_docs

            tools_dir = self.workspace_manager.get_path("tools")

            def _write_tool_doc(rel_path: str, content: str) -> None:
                self.workspace_manager.write_file(f"tools/{rel_path}", content)

            loaded_names = [t.name for t in self.tools]
            generate_workspace_tool_docs(
                loaded_names, tools_dir, tools=self.tools, write_fn=_write_tool_doc
            )
        except Exception as e:
            logger.warning(f"Failed to generate tool docs: {e}")

        # Apply description overrides and enforcement
        self.tools = apply_description_overrides(self.tools)
        self.tools = apply_instruction_enforcement(self.tools, self.tool_context)

        logger.info(f"Loaded {len(self.tools)} tools for persistent session")

    def resetup_tools_for_backend(self) -> None:
        """Re-derive + rebind the toolset after a live backend swap.

        ``swap_backend`` rebuilds the ShellManager (and, when the new backend
        supports a shell, repoints ``tool_context.shell_manager``) but leaves
        ``self.tools`` / ``self.llm_with_tools`` bound to the OLD backend's
        (lite-filtered) toolset. After a ``virtual`` → ``sandbox`` upgrade the
        new backend supports a shell + file tools, so shell, git, and file
        tools must be re-derived; this recomputes the tool list against the new
        backend and rebinds the LLM — without touching ``session_task_manager``
        or rebuilding ``tool_context`` the way a full ``_setup_tools`` would
        (that would drop in-flight session state).

        The per-turn ``get_current_tools()`` re-read in ``persistent_graph``
        then exposes the new tools on the next turn with no further plumbing.
        Safe to call only after ``_setup_tools`` has run once.
        """
        if not self.tool_context:
            logger.warning(
                "resetup_tools_for_backend called before tool setup — skipping"
            )
            return
        # swap_backend rebuilds self.shell_manager but only repoints
        # tool_context.shell_manager when the NEW backend supports a shell.
        # Repoint unconditionally so a swap to a no-shell backend (downgrade /
        # kill-switch) clears the stale manager too.
        self.tool_context.shell_manager = self.shell_manager
        # The WorkspaceManager object is unchanged by swap_backend (only its
        # ._backend flips), so tool_context.workspace_manager stays valid.
        self._load_tools_for_backend()
        self._bind_tools()
        if self.system_prompt:
            # Capability-scoped skill menus are embedded in the system prompt.
            # Rebuild it so a none→workspace upgrade that admits Canvas also
            # makes present-with-canvas discoverable on the next turn.
            self.system_prompt = get_phase_system_prompt(
                self.config,
                is_strategic=False,
                model=self.config.llm.model or "",
                tool_names=[tool.name for tool in self.tools],
                prompt_type="interactive",
            )
        backend_name = type(getattr(self.workspace_manager, "backend", None)).__name__
        logger.info(
            f"Re-derived {len(self.tools)} tools after backend swap ({backend_name})"
        )

    def resetup_datasources(
        self, new_datasources: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Apply a live datasource selection change (live_session_settings.md
        Slice B).

        Rebuilds the type-keyed connection registry from the NEW full payload
        through the same path attach takes (``process_datasources`` sorts
        read-only first, so a read-write entry wins a mixed same-type slot and
        multi-same-type stays correct by construction), applies the derived
        tool categories directly to ``config.tools`` (the validated session
        tools override's closed vocabulary silently drops sql/graph/mongodb/
        webdav, so they must never ride ``config.update``), clones added
        repositories, rewrites the datasources.md index, and re-derives +
        rebinds the toolset — which also rebuilds the system prompt for the
        per-turn ``messages[0]`` refresh (P0.1).

        The REPLACED connections are NOT closed here: bound tools captured
        them in closures at load time, so an in-flight turn's call would error
        mid-turn. They are returned under ``stale_connections`` /
        ``stale_clients``; the caller must close them once no turn is
        executing (deferred-close rule — "changes apply at the next turn" in
        both directions).

        kb-type datasources are out of scope for live changes (v1): their
        knowledge bindings wire into memory/KB machinery that ToolContext
        holds a copy of. Existing kb entries pass through untouched; a changed
        kb selection takes effect on the next attach.

        Repository removals keep their clone + SSH key on the workspace
        (cheap honesty — scrubbing is not a security boundary) but drop the
        ``source_repos`` registration. A live repository ADD whose clone name
        collides with an existing clone fails that one clone with a warning
        (the existing clone is never touched); a resume re-resolves suffixed
        names over the full list.

        Args:
            new_datasources: Full datasource payload for the thread, as
                returned by ``GET /api/agents/threads/{id}/workspace``.

        Returns:
            Summary dict: ``added``/``removed`` display names (transcript
            stamp), ``stale_connections``/``stale_clients`` (caller's
            deferred close), ``kb_deferred`` when a kb change was skipped.
        """
        from ..core.datasource_setup import (
            clone_repository_datasources,
            datasource_tool_categories,
            inject_datasource_index,
            process_datasources,
            resolve_repo_clone_names,
        )

        if not self.tool_context:
            logger.warning("resetup_datasources called before tool setup — skipping")
            return {
                "added": [],
                "removed": [],
                "stale_connections": {},
                "stale_clients": {},
            }

        new_configs = list(new_datasources or [])
        old_configs = list(self.datasource_configs or [])

        # The internal payload strips datasource ids, so identity for the
        # add/remove summary is (type, name) — unique enough for display and
        # for repository clone bookkeeping (clone names derive from names).
        def _key(ds: Dict[str, Any]) -> str:
            return f"{ds.get('type')}:{ds.get('name')}"

        old_keys = {_key(ds) for ds in old_configs}
        new_keys = {_key(ds) for ds in new_configs}
        added = [ds for ds in new_configs if _key(ds) not in old_keys]
        removed = [ds for ds in old_configs if _key(ds) not in new_keys]

        kb_changed = any(ds.get("type") == "kb" for ds in added + removed)
        if kb_changed:
            logger.warning(
                "kb-type datasource selection changed live — knowledge "
                "bindings apply on the next attach, not mid-session"
            )

        non_repo = [
            ds for ds in new_configs if ds.get("type") not in ("repository", "kb")
        ]
        new_conns, new_clients, cli_ds_types = process_datasources(non_repo)

        stale_connections = dict(self.datasources)
        stale_clients = dict(self._datasource_clients)
        # ToolContext shares this dict by REFERENCE — mutate in place, never
        # rebind, or live tools keep reading the orphaned old registry.
        self.datasources.clear()
        self.datasources.update(new_conns)
        self._datasource_clients = new_clients

        for category, names in datasource_tool_categories(new_configs).items():
            setattr(self.config.tools, category, list(names))
        self.config.extra["_cli_datasources"] = cli_ds_types

        added_repos = [ds for ds in added if ds.get("type") == "repository"]
        removed_repos = {_key(ds) for ds in removed if ds.get("type") == "repository"}
        if added_repos and self.workspace_manager:
            try:
                clone_repository_datasources(added_repos, self.workspace_manager)
            except Exception as e:
                logger.warning("Live repository clone failed: %s", e)
        if removed_repos and self.workspace_manager:
            # Resolve clone names over the OLD full repo list (payload order)
            # so collision suffixes match what attach actually registered.
            old_repos = [ds for ds in old_configs if ds.get("type") == "repository"]
            for ds, clone_name in zip(old_repos, resolve_repo_clone_names(old_repos)):
                if _key(ds) in removed_repos:
                    self.workspace_manager.source_repos.pop(clone_name, None)

        self.datasource_configs = new_configs

        if self.workspace_manager:
            # inject_datasource_index rewrites (cuts the previous section), so
            # connection names stay truthful for the next turn — including the
            # explicit "no datasources" state after a remove-all.
            try:
                inject_datasource_index(new_configs, self.workspace_manager)
            except Exception as e:
                logger.warning("Failed to rewrite datasource index: %s", e)

        self.resetup_tools_for_backend()

        summary: Dict[str, Any] = {
            "added": [ds.get("name", "unnamed") for ds in added],
            "removed": [ds.get("name", "unnamed") for ds in removed],
            "stale_connections": stale_connections,
            "stale_clients": stale_clients,
        }
        if kb_changed:
            summary["kb_deferred"] = True
        logger.info(
            "Datasources re-set up live: %d attached (%d added, %d removed), "
            "%d connections",
            len(new_configs),
            len(added),
            len(removed),
            len(new_conns),
        )
        return summary

    def _bind_tools(self) -> None:
        """Bind tools to LLM."""
        if not self._llm or not self.tools:
            return

        bind_kwargs = {}
        if supports_parallel_tool_calls(
            self.config.llm.provider, self.config.llm.model
        ):
            bind_kwargs["parallel_tool_calls"] = self.config.llm.parallel_tool_calls

        from src.services.guardrails import apply_guardrails_to_tools

        bound_tools = apply_guardrails_to_tools(self.tools, model=self.config.llm.model)
        self.llm_with_tools = self._llm.bind_tools(bound_tools, **bind_kwargs)

    # --- File checkpoints / undo ---

    def snapshot_file(self, path: str, turn_id: int) -> None:
        """Record original file content before a write/edit for undo."""
        import time

        if turn_id not in self.file_checkpoints:
            self.file_checkpoints[turn_id] = []
        # Don't snapshot same file twice in one turn
        if any(cp["path"] == path for cp in self.file_checkpoints[turn_id]):
            return
        try:
            content = self.workspace_manager.read_file(path)
        except (FileNotFoundError, OSError):
            content = None  # File doesn't exist yet
        self.file_checkpoints[turn_id].append(
            {
                "path": path,
                "original_content": content,
                "timestamp": time.time(),
            }
        )

    def undo_turn(self, turn_id: Optional[int] = None) -> List[str]:
        """Restore files from the given turn's checkpoints. Defaults to latest."""
        if turn_id is None:
            if not self.file_checkpoints:
                return []
            turn_id = max(self.file_checkpoints.keys())
        checkpoints = self.file_checkpoints.pop(turn_id, [])
        restored = []
        for cp in checkpoints:
            try:
                if cp["original_content"] is None:
                    self.workspace_manager.delete_file(cp["path"])
                else:
                    self.workspace_manager.write_file(
                        cp["path"], cp["original_content"]
                    )
                restored.append(cp["path"])
            except Exception as e:
                logger.warning(f"Failed to restore {cp['path']}: {e}")
        return restored

    def _build_context_config(self, config: Optional[Any] = None) -> ContextConfig:
        """Derive the ContextConfig from a config's limits (default: current).

        Shared by initial construction, hot-swap refresh, and the model-swap
        fit ladder (which derives the *candidate* config's thresholds before
        the swap is applied) so all agree on how thresholds derive from
        ``config.limits``.
        """
        cfg = config if config is not None else self.config
        ctx = cfg.context_management
        lim = cfg.limits
        return ContextConfig(
            compaction_threshold_tokens=lim.context_threshold_tokens,
            summarization_threshold_tokens=lim.context_threshold_tokens,
            message_count_threshold=lim.message_count_threshold,
            message_count_min_tokens=lim.message_count_min_tokens,
            keep_recent_tool_results=ctx.keep_recent_tool_results,
            keep_recent_messages=ctx.keep_recent_messages,
            keep_window_max_tool_result_chars=ctx.keep_window_max_tool_result_chars,
            # Safety-layer constant (model-aware; see loader fractions).
            # Summarization budgets are computed at call time from the
            # aux model's window (src/core/summarizer.py).
            model_max_context_tokens=lim.model_max_context_tokens,
            # Per-family image-token estimator (matrix settings.image_tokens).
            image_tokens=lim.image_tokens,
        )

    def _setup_context_manager(self) -> None:
        """Create context manager for token counting and compaction.

        Mirrors the worker path (``graph.py::build_phase_alternation_graph``):
        the token thresholds come from the model-aware values the loader derives
        as fractions of ``model_max_context_tokens`` (``config.limits.*``), NOT
        the ``ContextConfig`` defaults. Without this a 1M-context session would
        compact at the 80k fallback regardless of its real window. The
        ``LimitsConfig`` defaults equal the ``ContextConfig`` defaults, so this
        is a no-op when the derivation didn't fire (no regression).
        """
        self.context_manager = ContextManager(
            config=self._build_context_config(),
            model=self.config.llm.model or "gpt-4",
            summarization_call_timeout=(
                self.config.auxiliary.summarization_call_timeout
            ),
        )

    def refresh_context_limits(self) -> None:
        """Re-derive context thresholds after a config/model hot-swap.

        Updates the EXISTING ContextManager in place (``update_limits``) so the
        running loop's captured reference stays valid and the provider-usage
        anchor survives — a downswitch to a smaller-window model then compacts
        on the next turn instead of dead-ending in empty responses. See
        docs/done/session_model_switch_stale_context_manager_empty_response.md.
        """
        if getattr(self, "context_manager", None) is None:
            return
        self.context_manager.update_limits(
            self._build_context_config(),
            self.config.llm.model or "gpt-4",
        )

    def _setup_shell_manager(self) -> None:
        """Initialize the shell manager over a shell-capable workspace backend.

        Shells run only on the workspace — there is no local (in-pod) tmux
        fallback. Without a shell-capable backend, shell tools stay disabled.
        """
        ws_backend = self.workspace_manager.backend if self.workspace_manager else None

        if not getattr(ws_backend, "supports_shell", False):
            logger.info(
                "Workspace backend does not support shell — shell tools "
                "disabled (no local fallback)"
            )
            return

        try:
            from src.tools.shell.shell_manager import ShellManager

            shell_config = self.config.extra.get("shell", {})
            self.shell_manager = ShellManager(
                job_id=self.thread_id,
                max_tabs=shell_config.get("max_tabs", 15),
                scrollback_limit=shell_config.get("scrollback_limit", 5000),
                default_timeout=shell_config.get("default_timeout", 120),
                blocked_commands=shell_config.get("blocked_commands"),
                sandbox_cwd=str(self.workspace_manager.path)
                if shell_config.get("sandbox", True)
                else None,
                backend=ws_backend,
                sudo_action=shell_config.get("sudo_action", "freeze"),
            )
            if self.tool_context:
                self.tool_context.shell_manager = self.shell_manager
            logger.info("ShellManager initialized (backend delegation)")
        except Exception as e:
            logger.warning(f"Failed to initialize ShellManager (non-fatal): {e}")

    def _setup_memory(
        self,
        postgres_conn: Optional[Any],
        vector_conn: Optional[Any],
    ) -> None:
        """Initialize RecallStore and KnowledgeStore if enabled.

        Raises MemoryUnavailableError when a *configured* (required) memory
        component can't be set up — a store that won't init or a plugin whose
        transport won't resolve. "Configured ⇒ required": if the manager
        pipeline is on (or memory.required is set) the session must fail loud
        rather than run half-working. See
        docs/issues/openrouter_auxiliary_crashes_session_via_memory_reranker.md.
        """
        if not vector_conn:
            return

        # Degradation flags — mirror the worker path (src/agent.py). A store
        # that fails to init flips its flag; the required-gate below turns that
        # into a loud MemoryUnavailableError instead of a silent half-session.
        self._memory_degraded = False
        self._kb_degraded = False

        # RecallStore (memory injection/extraction)
        if self.config.memory.enabled:
            try:
                from src.services.embedding_service import get_embedding_service
                from src.services.recall_store import RecallStore
                import uuid as _uuid

                embedding_service = get_embedding_service()
                self.recall_store = RecallStore(
                    db=vector_conn,
                    embedding_service=embedding_service,
                    job_id=_uuid.UUID(self.thread_id),
                    config=self.config.memory,
                    agent_id=self.config.agent_id,
                    project_id=_uuid.UUID(self.project_ids[0])
                    if self.project_ids
                    else None,
                    project_ids=[_uuid.UUID(p) for p in self.project_ids]
                    if self.project_ids
                    else None,
                )
                if self.tool_context:
                    self.tool_context.recall_store = self.recall_store
                logger.info("RecallStore initialized for persistent session")

                # B4 guard: background-probe the endpoint's dimensionality so
                # a misconfigured provider surfaces as one ERROR at init
                # instead of a swallowed WARNING per write.
                asyncio.create_task(embedding_service.verify_dimensions())
            except Exception as e:
                from src.core.archiver import audit_unavailable as _audit_unavailable

                _provider = os.environ.get("EMBEDDING_PROVIDER", "local")
                _audit_unavailable(
                    job_id=self.thread_id,
                    agent_type=self.config.agent_id,
                    step_type="memory_unavailable",
                    component="RecallStore",
                    error=e,
                    node_name="session_setup",
                    extra={
                        "embedding_provider": _provider,
                        "embedding_model": os.environ.get("EMBEDDING_MODEL", "unknown"),
                    },
                )
                logger.warning(
                    f"Failed to initialize RecallStore (non-fatal): {e} "
                    f"[embedding_provider={_provider}]"
                )
                self._memory_degraded = True

        # KnowledgeStore (knowledge injection, project-scoped)
        # Skip if already initialized by _setup_knowledge() (for tool loading)
        if self.knowledge_store is None:
            try:
                from src.services.embedding_service import get_kb_embedding_service
                from src.services.knowledge_store import KnowledgeStore

                embedding_service = get_kb_embedding_service()
                self.knowledge_store = KnowledgeStore(
                    db=vector_conn,
                    embedding_service=embedding_service,
                )
                logger.info("KnowledgeStore initialized for persistent session")
            except Exception as e:
                from src.core.archiver import audit_unavailable as _audit_unavailable

                _audit_unavailable(
                    job_id=self.thread_id,
                    agent_type=self.config.agent_id,
                    step_type="kb_unavailable",
                    component="KnowledgeStore",
                    error=e,
                    node_name="session_setup",
                    extra={
                        "embedding_provider": os.environ.get(
                            "KB_EMBEDDING_PROVIDER",
                            os.environ.get("EMBEDDING_PROVIDER", "local"),
                        ),
                    },
                )
                logger.warning(
                    f"Failed to initialize KnowledgeStore (non-fatal): {e} "
                    f"[embedding_provider={os.environ.get('KB_EMBEDDING_PROVIDER', os.environ.get('EMBEDDING_PROVIDER', 'local'))}]"
                )
                self._kb_degraded = True

        # Configured ⇒ required. If the manager pipeline is on (or
        # memory.required is set), a store that failed to init must not run the
        # session blind — fail loud, exactly like the worker path's
        # memory.required freeze (src/agent.py). The lifespan handler turns this
        # into a clean pod exit + a cockpit-surfaced reason via the orchestrator
        # pre-flight (no crash-loop).
        _pipeline = getattr(self.config.memory, "pipeline", None)
        _memory_required = self.config.memory.required or (
            self.config.memory.manager_enabled
            and bool(
                getattr(_pipeline, "scorers", None)
                or getattr(_pipeline, "retrievers", None)
            )
        )
        if _memory_required and self._memory_degraded:
            raise MemoryUnavailableError(
                "memory is required for this session but the embedding-backed "
                "RecallStore failed to initialize "
                f"(EMBEDDING_BASE_URL={os.environ.get('EMBEDDING_BASE_URL', 'unset')}, "
                f"EMBEDDING_MODEL={os.environ.get('EMBEDDING_MODEL', 'unset')}). "
                "Check the embedding model/endpoint (Admin → Models)."
            )
        if _memory_required and self.knowledge_bindings and self._kb_degraded:
            raise MemoryUnavailableError(
                "memory is required for this session but the KnowledgeStore "
                "failed to initialize — the embedding endpoint is unavailable "
                f"(EMBEDDING_BASE_URL={os.environ.get('EMBEDDING_BASE_URL', 'unset')})."
            )

        # MemoryManager seam (memory overhaul Phase 1, behind
        # memory.manager.enabled). Constructed after both stores so the
        # retriever factories bind real handles; the writers read
        # auxiliary_llm/extraction_prompt from the runtime at event time,
        # which is what lets the config.update handler hot-swap them
        # (persistent_app.py keeps runtime in lockstep). A configured pipeline
        # is required — a bind failure (unknown plugin name, or a plugin whose
        # transport won't resolve, e.g. the reranker endpoint) fails the session
        # loud rather than silently mid-turn. Sessions without a vector_conn
        # return above and keep the legacy (no-op) paths.
        if self.config.memory.manager_enabled:
            from src.services.memory import MemoryManager as MemorySeamManager
            from src.services.memory import MemoryRuntime

            try:
                self.memory_service = MemorySeamManager.from_config(
                    self.config.memory,
                    MemoryRuntime(
                        recall_store=self.recall_store,
                        knowledge_store=self.knowledge_store,
                        auxiliary_llm=self.auxiliary_llm,
                        memory_config=self.config.memory,
                        auxiliary_config=self.config.auxiliary,
                        extraction_prompt=self.memory_extraction_prompt,
                        assembler_prompt=None,  # persistent mode has no assembler
                        job_id=self.thread_id,
                        project_id=self.project_id,
                        project_ids=list(self.project_ids),
                        # The legacy persistent path bounds each store call at
                        # 5 s (_RETRIEVAL_TIMEOUT in persistent_graph.py).
                        retrieval_timeout=5.0,
                    ),
                )
            except Exception as e:
                raise MemoryUnavailableError(
                    "memory pipeline failed to bind: "
                    f"{type(e).__name__}: {e}. A configured memory plugin could "
                    "not resolve its transport (e.g. the reranker endpoint). Fix "
                    "the config or drop the plugin from memory.pipeline."
                ) from e

        # Ingestion verdicts + bi-temporal supersede (overhaul Phase 4). Wired
        # onto the store independently of the manager cutover — a write-path
        # change behind memory.ingestion.enabled, used by legacy + seam writers.
        from src.services.memory.ingestion import maybe_attach_ingestion_verdict

        maybe_attach_ingestion_verdict(
            self.recall_store,
            getattr(self, "auxiliary_llm", None),
            self.config.memory,
        )

    def swap_backend(self, new_backend: Any) -> None:
        """Hot-swap workspace backend at runtime (e.g. container → VM).

        Connects the new backend, disconnects the old one, replaces
        the WorkspaceManager's backend, and rebuilds the ShellManager.

        Args:
            new_backend: A connected (or connectable) WorkspaceBackend instance.
        """
        if not self.workspace_manager:
            raise RuntimeError("No workspace manager to swap backend on")

        old_backend = self.workspace_manager.backend

        # Connect new backend first (fail fast)
        if (
            hasattr(new_backend, "connect")
            and not getattr(new_backend, "is_connected", lambda: False)()
        ):
            new_backend.connect()

        # Disconnect old backend
        if hasattr(old_backend, "disconnect") and hasattr(old_backend, "is_connected"):
            try:
                if old_backend.is_connected():
                    old_backend.disconnect()
            except Exception as e:
                logger.warning(f"Old backend disconnect error: {e}")

        # Swap on WorkspaceManager
        self.workspace_manager._backend = new_backend

        # Rebuild ShellManager with new backend
        self._setup_shell_manager()

        logger.info(
            f"Backend swapped to {type(new_backend).__name__} "
            f"({getattr(new_backend, '_host', 'local')})"
        )

    async def cleanup(self) -> None:
        """Clean up session resources."""
        if self.tool_context is not None:
            self.tool_context.citation_verdict_callback = None
            self.tool_context.canvas_event_callback = None

        if self._cloud_overlay_monitor_task is not None:
            self._cloud_overlay_monitor_task.cancel()
            try:
                await self._cloud_overlay_monitor_task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.debug("cloud overlay monitor task exit", exc_info=True)
            self._cloud_overlay_monitor_task = None

        if self.overlay_mount_manager is not None:
            try:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, self.overlay_mount_manager.unmount)
            except Exception:
                logger.debug("overlay unmount failed", exc_info=True)
            self.overlay_mount_manager = None

        if self.cloud_mount_manager:
            try:
                await self.cloud_mount_manager.aclose()
            except Exception as e:
                logger.warning(f"Cloud mount cleanup error: {e}")
            self.cloud_mount_manager = None

        if self.shell_manager:
            try:
                self.shell_manager.cleanup()
            except Exception as e:
                logger.warning(f"Shell cleanup error: {e}")

        # Close datasource connections
        if self.datasources or self._datasource_clients:
            from ..core.datasource_setup import close_datasource_connections

            close_datasource_connections(self.datasources, self._datasource_clients)
            self.datasources = {}
            self._datasource_clients = {}

        # Close knowledge graph connection
        if self._knowledge_graph:
            try:
                self._knowledge_graph.close()
                logger.debug("Closed knowledge graph connection")
            except Exception as e:
                logger.warning(f"Error closing knowledge graph: {e}")
            self._knowledge_graph = None

        # Disconnect remote backend if connected
        if self.workspace_manager:
            backend = self.workspace_manager.backend
            if hasattr(backend, "disconnect") and hasattr(backend, "is_connected"):
                try:
                    if backend.is_connected():
                        backend.disconnect()
                        logger.info("Remote workspace backend disconnected")
                except Exception as e:
                    logger.warning(f"Backend disconnect error: {e}")

        logger.info(f"PersistentSession cleaned up: thread={self.thread_id}")
