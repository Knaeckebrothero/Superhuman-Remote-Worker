"""Persistent Agent Session State.

Encapsulates all state for an interactive persistent agent session.
Created once during lifespan startup, lives until the session ends.

Composes around UniversalAgent — reuses its initialized LLMs, DB connections,
and config without subclassing or modifying it.
"""

import logging
import os
import shutil
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage

from ..core.context import ContextConfig, ContextManager
from ..core.loader import (
    AgentConfig,
    get_all_tool_names,
    get_phase_system_prompt,
)
from ..core.workspace import WorkspaceManager, WorkspaceManagerConfig
from ..tools import ToolContext, load_tools, apply_instruction_enforcement
from ..tools.description_manager import apply_description_overrides

logger = logging.getLogger(__name__)

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
    shell_manager: Optional[Any] = None

    # DB connections (for message persistence + memory)
    postgres_conn: Optional[Any] = None
    vector_conn: Optional[Any] = None

    # Memory/knowledge stores (initialized during setup)
    recall_store: Optional[Any] = None
    knowledge_store: Optional[Any] = None
    project_ids: List[str] = field(default_factory=list)

    # Raw LLM (without tools bound, for summarization fallback)
    _llm: Optional[BaseChatModel] = None

    # Session task manager (lightweight in-session todos)
    session_task_manager: Optional[Any] = None

    # File checkpoints for undo (turn_id -> list of snapshots)
    file_checkpoints: Dict[int, List[Dict[str, Any]]] = field(default_factory=dict)

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

        # 1. Create workspace (with optional remote backend + git)
        self._setup_workspace(
            workspace_override=workspace_override, git_remote_url=git_remote_url
        )

        # 2. Set up shell manager BEFORE tools so coding tools can detect it
        self._setup_shell_manager()

        # 3. Create tool context and load tools
        self._setup_tools(postgres_conn)

        # 4. Bind tools to LLM
        self._bind_tools()

        # 5. Create context manager
        self._setup_context_manager()

        # 6. Build system prompt (interactive mode has its own prompt files)
        self.system_prompt = get_phase_system_prompt(
            self.config,
            is_strategic=False,
            model=self.config.llm.model or "",
            tool_names=[t.name for t in self.tools] if self.tools else None,
            prompt_type="interactive",
        )

        # 7. Set up memory (RecallStore) if enabled
        self._setup_memory(postgres_conn, vector_conn)

        logger.info(
            f"PersistentSession initialized: thread={self.thread_id}, "
            f"tools={len(self.tools or [])}, "
            f"mode={self.permission_mode}"
        )

    def _setup_workspace(
        self,
        workspace_override: Optional[Dict[str, Any]] = None,
        git_remote_url: Optional[str] = None,
    ) -> None:
        """Create workspace, optionally using a remote backend.

        Args:
            workspace_override: If provided, overrides config workspace settings.
                Expected shape: {"backend": "remote", "remote": {"host": ..., "port": 22, ...}}
            git_remote_url: Gitea repo URL for workspace versioning (clones on init)
        """
        ws_data = self.config.workspace
        base_path = os.getenv("WORKSPACE_PATH", "./workspace")

        # Determine backend: override > config > default (local)
        workspace_backend = None
        effective_backend = (workspace_override or {}).get("backend") or ws_data.backend
        remote_cfg = (workspace_override or {}).get("remote") or ws_data.remote

        if effective_backend == "remote" and remote_cfg:
            try:
                from ..core.backends.remote import RemoteBackend

                shell_config = self.config.extra.get("shell", {})
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
                )
                workspace_backend.connect()
                logger.info(
                    f"Remote workspace backend connected to {remote_cfg['host']}"
                )
            except Exception as e:
                logger.warning(
                    f"Failed to create remote backend (falling back to local): {e}"
                )
                workspace_backend = None

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
        backend_label = "remote" if workspace_backend else "local"
        logger.info(
            f"Workspace created at {self.workspace_manager.path} "
            f"(backend={backend_label})"
        )

    def _setup_tools(self, postgres_conn: Optional[Any]) -> None:
        """Load tools from config, excluding phase-specific ones."""
        tool_config = {
            **self.config.extra,
            "agent_id": self.config.agent_id,
            "multimodal": self.config.llm.multimodal,
        }
        # Initialize session task manager
        from ..managers.session_tasks import SessionTaskManager

        self.session_task_manager = SessionTaskManager()

        self.tool_context = ToolContext(
            workspace_manager=self.workspace_manager,
            todo_manager=None,  # No TodoManager in persistent mode
            postgres_db=postgres_conn,
            config=tool_config,
            _job_id=self.thread_id,
            _llm_config=self.config.llm,
            _instruction_files=self.config.instruction_files,
            shell_manager=self.shell_manager,  # Set before tool loading
            session_task_manager=self.session_task_manager,
        )

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

        # Apply description overrides and enforcement
        self.tools = apply_description_overrides(self.tools)
        self.tools = apply_instruction_enforcement(self.tools, self.tool_context)

        logger.info(f"Loaded {len(self.tools)} tools for persistent session")

    def _bind_tools(self) -> None:
        """Bind tools to LLM."""
        if not self._llm or not self.tools:
            return

        bind_kwargs = {}
        model_name = (self.config.llm.model or "").lower()
        if not model_name.startswith(("o1", "o3", "o4")):
            bind_kwargs["parallel_tool_calls"] = self.config.llm.parallel_tool_calls

        self.llm_with_tools = self._llm.bind_tools(self.tools, **bind_kwargs)

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
        self.file_checkpoints[turn_id].append({
            "path": path, "original_content": content, "timestamp": time.time(),
        })

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
                    self.workspace_manager.write_file(cp["path"], cp["original_content"])
                restored.append(cp["path"])
            except Exception as e:
                logger.warning(f"Failed to restore {cp['path']}: {e}")
        return restored

    def _setup_context_manager(self) -> None:
        """Create context manager for token counting and compaction."""
        ctx = self.config.context_management
        self.context_manager = ContextManager(
            config=ContextConfig(
                keep_recent_tool_results=ctx.keep_recent_tool_results,
                keep_recent_messages=ctx.keep_recent_messages,
            ),
            model=self.config.llm.model or "gpt-4",
        )

    def _setup_shell_manager(self) -> None:
        """Initialize shell manager, delegating to remote backend if available."""
        ws_backend = self.workspace_manager.backend if self.workspace_manager else None
        use_remote_shell = getattr(ws_backend, "supports_shell", False)

        if not use_remote_shell and not shutil.which("tmux"):
            logger.debug("tmux not found and no remote backend — shell tools disabled")
            return

        try:
            from src.tools.coding.shell_manager import ShellManager

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
                backend=ws_backend if use_remote_shell else None,
                sudo_action=shell_config.get("sudo_action", "freeze"),
            )
            if self.tool_context:
                self.tool_context.shell_manager = self.shell_manager
            logger.info(f"ShellManager initialized (remote={use_remote_shell})")
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
            except Exception as e:
                logger.warning(f"Failed to initialize RecallStore (non-fatal): {e}")

        # KnowledgeStore (knowledge injection, project-scoped)
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

        # Set project_ids on tool_context for knowledge tools
        if self.tool_context and self.project_ids:
            self.tool_context.project_ids = self.project_ids

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

    def get_workspace_content(self) -> str:
        """Read current workspace.md content for transient injection."""
        if not self.workspace_manager:
            return ""
        try:
            return self.workspace_manager.read_file("workspace.md")
        except (FileNotFoundError, OSError):
            return ""

    async def cleanup(self) -> None:
        """Clean up session resources."""
        if self.shell_manager:
            try:
                self.shell_manager.cleanup()
            except Exception as e:
                logger.warning(f"Shell cleanup error: {e}")

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
