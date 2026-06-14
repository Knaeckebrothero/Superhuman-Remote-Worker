"""Persistent Agent Session State.

Encapsulates all state for an interactive persistent agent session.
Created once during lifespan startup, lives until the session ends.

Composes around UniversalAgent — reuses its initialized LLMs, DB connections,
and config without subclassing or modifying it.
"""

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
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

    # Nextcloud workspace sync (initialized if session has nc_session_folder)
    workspace_sync: Optional[Any] = None
    # Lazy rclone cloud mounts (initialized from cloud_mount payload)
    cloud_mount_manager: Optional[Any] = None
    cloud_mount_error: Optional[str] = None

    # Datasource connections keyed by type (for ToolContext)
    datasources: Dict[str, Any] = field(default_factory=dict)
    # Parent clients for cleanup (e.g. MongoClient)
    _datasource_clients: Dict[str, Any] = field(default_factory=dict)

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
                    sudo_action=shell_config.get("sudo_action", "freeze"),
                )
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
        """Start rclone-backed cloud mounts for this session, if configured."""
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

    def _deploy_instruction_files(self) -> None:
        """Deploy instruction files from config to workspace.

        Mirrors the worker-mode pattern in agent.py._deploy_instruction_files().
        Copies files like design_guide.md from the expert config directory into
        the workspace so the agent can read them via workspace tools.
        """
        if not self.config.instruction_files or not self.config._deployment_dir:
            return

        templates_dir = get_project_root() / "config" / "templates"
        file_resolver = FileResolver(
            deployment_dir=self.config._deployment_dir,
            framework_dir=templates_dir,
        )
        for entry in self.config.instruction_files:
            try:
                # Skip if already present (don't overwrite on session resume)
                target_path = self.workspace_manager.get_path(entry.file)
                if target_path.exists():
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
        """Initialize knowledge base connections (Neo4j + pgvector).

        Must be called BEFORE _setup_tools() so that the ToolContext
        has knowledge_graph and knowledge_store set, allowing knowledge
        tools to pass the has_knowledge() guard in load_tools().

        Mirrors the worker agent pattern in agent.py._setup_job_tools().
        """
        if not self.project_ids:
            return  # No project context — knowledge base not applicable

        try:
            from src.services.knowledge_graph import KnowledgeGraphDB
            from src.services.knowledge_store import KnowledgeStore
            from src.services.embedding_service import get_embedding_service

            kg = KnowledgeGraphDB()
            if kg.connect():
                embedding_service = get_embedding_service()
                ks = KnowledgeStore(
                    db=vector_conn,
                    embedding_service=embedding_service,
                )
                self._knowledge_graph = kg
                self.knowledge_store = ks
                logger.info(
                    f"Knowledge base initialized for project(s) {self.project_ids}"
                )
            else:
                logger.warning("Failed to connect to Neo4j — knowledge tools disabled")
        except Exception as e:
            logger.warning(f"Failed to initialize knowledge base (non-fatal): {e}")

    def _setup_tools(self, postgres_conn: Optional[Any]) -> None:
        """Load tools from config, excluding phase-specific ones."""
        tool_config = {
            **self.config.extra,
            "agent_id": self.config.agent_id,
            "multimodal": self.config.llm.multimodal,
            # Lets bulk readers cap a single tool result relative to the main
            # model's window (session_silent_failure_audit.md #5).
            "model_max_context_tokens": self.config.limits.model_max_context_tokens,
            "cloud_mount": {
                "active": bool(
                    self.cloud_mount_manager and self.cloud_mount_manager.active
                ),
                "root": "/cloud",
                "workspace_entry": "/workspace/cloud",
                "scan_guard": self.config.extra.get("cloud_scan_guard", "block"),
                "_manager": self.cloud_mount_manager,
            },
        }
        # Initialize session task manager
        from ..managers.session_tasks import SessionTaskManager

        self.session_task_manager = SessionTaskManager()

        self.tool_context = ToolContext(
            workspace_manager=self.workspace_manager,
            todo_manager=None,  # No TodoManager in persistent mode
            postgres_db=postgres_conn,
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
        )
        if self.project_ids:
            self.tool_context.project_ids = self.project_ids

        # Wire file checkpoint callback for undo support
        self.tool_context._snapshot_callback = lambda path: self.snapshot_file(
            path, self.turn_count
        )

        # Get all tool names and filter out phase-specific ones
        all_names = get_all_tool_names(self.config)
        tool_names = [n for n in all_names if n not in _EXCLUDED_TOOLS]

        # Always include session task tools in persistent mode
        for name in ["task_add", "task_complete", "task_list"]:
            if name not in tool_names:
                tool_names.append(name)

        # Always include orchestrator tools in persistent mode (job delegation)
        _ORCHESTRATOR_TOOLS = [
            "create_worker_job",
            "list_worker_jobs",
            "get_worker_job",
            "get_job_workspace_file",
            "approve_worker_job",
            "resume_worker_job",
            "cancel_worker_job",
            "pause_worker_job",
        ]
        for name in _ORCHESTRATOR_TOOLS:
            if name not in tool_names:
                tool_names.append(name)

        if self.cloud_mount_manager and self.cloud_mount_manager.active:
            if "srw_cloud_status" not in tool_names:
                tool_names.append("srw_cloud_status")

        # Capability gate: drop tools the workspace backend can't support (lite
        # tiers — no_workspace_agent_mode.md §3.2/§7). Mirrors the worker path.
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
        ctx = self.config.context_management
        lim = self.config.limits
        self.context_manager = ContextManager(
            config=ContextConfig(
                compaction_threshold_tokens=lim.context_threshold_tokens,
                summarization_threshold_tokens=lim.context_threshold_tokens,
                message_count_threshold=lim.message_count_threshold,
                message_count_min_tokens=lim.message_count_min_tokens,
                keep_recent_tool_results=ctx.keep_recent_tool_results,
                keep_recent_messages=ctx.keep_recent_messages,
                # Safety-layer constant (model-aware; see loader fractions).
                # Summarization budgets are computed at call time from the
                # aux model's window (src/core/summarizer.py).
                model_max_context_tokens=lim.model_max_context_tokens,
                # Per-family image-token estimator (matrix settings.image_tokens).
                image_tokens=lim.image_tokens,
            ),
            model=self.config.llm.model or "gpt-4",
            summarization_call_timeout=(
                self.config.auxiliary.summarization_call_timeout
            ),
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
        """Initialize RecallStore and KnowledgeStore if enabled."""
        if not vector_conn:
            return

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
                logger.warning(f"Failed to initialize RecallStore (non-fatal): {e}")

        # KnowledgeStore (knowledge injection, project-scoped)
        # Skip if already initialized by _setup_knowledge() (for tool loading)
        if self.knowledge_store is None:
            try:
                from src.services.embedding_service import get_embedding_service
                from src.services.knowledge_store import KnowledgeStore

                embedding_service = get_embedding_service()
                self.knowledge_store = KnowledgeStore(
                    db=vector_conn,
                    embedding_service=embedding_service,
                )
                logger.info("KnowledgeStore initialized for persistent session")
            except Exception as e:
                logger.warning(f"Failed to initialize KnowledgeStore (non-fatal): {e}")

        # MemoryManager seam (memory overhaul Phase 1, behind
        # memory.manager.enabled). Constructed after both stores so the
        # retriever factories bind real handles; the writers read
        # auxiliary_llm/extraction_prompt from the runtime at event time,
        # which is what lets the config.update handler hot-swap them
        # (persistent_app.py keeps runtime in lockstep). Bind failures
        # (unknown plugin name) raise — a misconfigured cutover fails at
        # session setup, not silently mid-turn. Sessions without a
        # vector_conn return above and keep the legacy (no-op) paths.
        if self.config.memory.manager_enabled:
            from src.services.memory import MemoryManager as MemorySeamManager
            from src.services.memory import MemoryRuntime

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
