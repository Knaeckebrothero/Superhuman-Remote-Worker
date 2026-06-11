"""Tool context for dependency injection.

Provides a container for dependencies that tools need access to,
such as workspace managers, database connections, and configuration.
"""

import hashlib
import logging
import re
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Deque, Dict, List, Optional
from urllib.parse import urlparse

from ..core.workspace import WorkspaceManager

logger = logging.getLogger(__name__)

# Avoid circular imports with TYPE_CHECKING
if TYPE_CHECKING:
    from ..database.postgres_db import PostgresDB


@dataclass
class ToolContext:
    """Context container for tool dependencies.

    This class holds all dependencies that tools may need during execution.
    It's passed to tool creation functions to enable dependency injection
    without global state.

    Attributes:
        workspace_manager: WorkspaceManager for file operations
        todo_manager: TodoManager for task tracking (optional)
        postgres_db: PostgresDB instance for orchestrator database operations
        datasources: Dictionary of external datasource connections keyed by type
            (e.g. {"neo4j": Neo4jDB(...), "postgresql": asyncpg_pool, ...})
        config: Additional configuration dictionary
        job_id: Override job ID (if not using workspace_manager)
        citation_engine: CitationEngine instance for citation management
        _source_registry: Cache of registered source identifiers to source IDs

    Example:
        ```python
        workspace = WorkspaceManager(job_id="job-123")
        workspace.initialize()

        context = ToolContext(
            workspace_manager=workspace,
            config={"max_file_size": 1024 * 1024}
        )

        tools = create_workspace_tools(context)
        ```
    """

    workspace_manager: Optional[WorkspaceManager] = None
    todo_manager: Optional[Any] = (
        None  # TodoManager, imported later to avoid circular deps
    )
    postgres_db: Optional["PostgresDB"] = None
    datasources: Dict[str, Any] = field(default_factory=dict)
    config: Dict[str, Any] = field(default_factory=dict)
    _job_id: Optional[str] = None  # Direct job_id override
    citation_engine: Optional[Any] = None  # CitationEngine, imported lazily
    _source_registry: Dict[str, int] = field(
        default_factory=dict
    )  # path/url -> source_id
    _inaccessible_sources: Dict[str, str] = field(
        default_factory=dict
    )  # url -> error message
    _recent_reads: Deque[str] = field(
        default_factory=lambda: deque(maxlen=10)
    )  # Recently read file paths
    _current_phase: Optional[str] = None
    _llm_config: Optional[Any] = None  # LLMConfig for phase-aware multimodal
    _instruction_files: List[Any] = field(
        default_factory=list
    )  # List[InstructionFileEntry]
    recall_store: Optional[Any] = None  # RecallStore instance (Memory Light)
    shell_manager: Optional[Any] = None  # ShellManager (persistent terminal sessions)
    session_task_manager: Optional[Any] = (
        None  # SessionTaskManager (persistent session todos)
    )
    knowledge_graph: Optional[Any] = (
        None  # KnowledgeGraphDB (system Neo4j for knowledge base)
    )
    knowledge_store: Optional[Any] = None  # KnowledgeStore (pgvector search index)
    _project_id: Optional[str] = None  # Project UUID for knowledge scoping
    _project_ids: List[str] = field(
        default_factory=list
    )  # Multi-project UUIDs for persistent sessions
    _pending_memories: List[Dict[str, Any]] = field(
        default_factory=list
    )  # Sync-safe memory queue
    _freeze_request: Optional[Dict[str, Any]] = (
        None  # Tool-requested job freeze (blocking send_message)
    )
    _snapshot_callback: Optional[Any] = (
        None  # Callable[[str], None] — pre-write file snapshot for undo
    )
    orchestrator_client: Optional[Any] = None  # OrchestratorClient for delegation
    _thread_id: Optional[str] = (
        None  # Persistent-session thread UUID. Set by persistent_session so
        # session-spawned worker jobs (create_worker_job) can carry the
        # session's thread back to the orchestrator, which derives the
        # owning user_id + project_id and applies their model preferences
        # during dispatch. Unset in worker-job mode.
    )
    user_id: Optional[str] = (
        None  # Originating user UUID. Set by persistent_session from the
        # thread row's owner so agent-initiated calls to the orchestrator
        # (jobs.py, messaging.py, orchestrator_client.py) can forward
        # `X-MCP-User-Id` alongside `X-Internal-Key`. The orchestrator's
        # `_get_user_from_mcp_headers` then resolves the user and the
        # call is accepted by `require_approved_user` / `require_job_access`
        # instead of 401-ing. Unset in worker-job mode (no user identity
        # to forward; lifecycle calls still rely on the internal key alone).
    )
    _job_metadata: Dict[str, Any] = field(
        default_factory=dict
    )  # job_id, project_id, priority, config_name, repo_name

    def __post_init__(self):
        """Validate context after initialization."""
        # Workspace manager is required for workspace tools
        if (
            self.workspace_manager is not None
            and not self.workspace_manager.is_initialized
        ):
            raise ValueError(
                "WorkspaceManager must be initialized before creating ToolContext. "
                "Call workspace_manager.initialize() first."
            )

    @property
    def job_id(self) -> Optional[str]:
        """Get the current job ID.

        Returns job_id from _job_id override, or from workspace_manager if available.
        """
        if self._job_id:
            return self._job_id
        if self.workspace_manager:
            return self.workspace_manager.job_id
        return None

    @job_id.setter
    def job_id(self, value: Optional[str]) -> None:
        """Set the job ID directly."""
        self._job_id = value

    @property
    def thread_id(self) -> Optional[str]:
        """Persistent-session thread UUID (None outside session mode)."""
        return self._thread_id

    @thread_id.setter
    def thread_id(self, value: Optional[str]) -> None:
        self._thread_id = value

    def has_workspace(self) -> bool:
        """Check if workspace manager is available."""
        return self.workspace_manager is not None

    def has_todo(self) -> bool:
        """Check if todo manager is available."""
        return self.todo_manager is not None

    def has_postgres(self) -> bool:
        """Check if PostgreSQL connection is available."""
        return self.postgres_db is not None

    def has_datasource(self, ds_type: str) -> bool:
        """Check if a datasource of the given type is available.

        Args:
            ds_type: Datasource type (e.g. "neo4j", "postgresql", "mongodb")

        Returns:
            True if datasource is available
        """
        return ds_type in self.datasources and self.datasources[ds_type] is not None

    def get_datasource(self, ds_type: str) -> Optional[Any]:
        """Get a datasource connection by type.

        Args:
            ds_type: Datasource type (e.g. "neo4j", "postgresql", "mongodb")

        Returns:
            Datasource connection object, or None if not available
        """
        return self.datasources.get(ds_type)

    def has_git(self) -> bool:
        """Check if git manager is available and active.

        Returns True only if workspace_manager exists, has a git_manager,
        and the git_manager is active (git available and repo initialized).
        """
        if not self.has_workspace():
            return False
        gm = self.workspace_manager.git_manager
        return gm is not None and gm.is_active

    def has_shell(self) -> bool:
        """Check if ShellManager is available for persistent terminal sessions."""
        return self.shell_manager is not None

    def has_knowledge(self) -> bool:
        """Check if knowledge base (Neo4j + pgvector) is available."""
        return self.knowledge_graph is not None and self.knowledge_store is not None

    @property
    def project_id(self) -> Optional[str]:
        """Get the project ID for knowledge scoping."""
        return self._project_id

    @project_id.setter
    def project_id(self, value: Optional[str]) -> None:
        """Set the project ID."""
        self._project_id = value

    @property
    def project_ids(self) -> List[str]:
        """Get all project IDs for multi-project scoping."""
        if self._project_ids:
            return self._project_ids
        if self._project_id:
            return [self._project_id]
        return []

    @project_ids.setter
    def project_ids(self, value: List[str]) -> None:
        """Set project IDs (also updates primary project_id)."""
        self._project_ids = value or []
        self._project_id = value[0] if value else None

    @property
    def db(self) -> Optional["PostgresDB"]:
        """Get PostgresDB instance.

        Returns:
            PostgresDB instance if available, None otherwise
        """
        return self.postgres_db

    def get_config(self, key: str, default: Any = None) -> Any:
        """Get a configuration value.

        Args:
            key: Configuration key
            default: Default value if key not found

        Returns:
            Configuration value or default
        """
        return self.config.get(key, default)

    def get_citation_engine(self) -> Any:
        """Lazily initialize and return CitationEngine.

        Creates a CitationEngine instance on first call, reuses it afterwards.
        Uses multi-agent mode (PostgreSQL); the DSN is composed at runtime
        from CITATION_POSTGRES_* env vars (with fallback to legacy URL envs).

        Returns:
            CitationEngine instance

        Raises:
            ImportError: If citation_engine package is not installed
        """
        if self.citation_engine is None:
            from citation_engine import CitationEngine, CitationContext

            from src.utils.db_url import build_postgres_url

            # Create context for audit trails using job_id as session
            ctx = CitationContext(
                session_id=self.job_id or "unknown",
                agent_id=self.config.get("agent_id", "unknown"),
            )

            db_url = (
                build_postgres_url("CITATION_POSTGRES", fallback_env="CITATION_DB_URL")
                or build_postgres_url("VECTOR_POSTGRES", fallback_env="VECTOR_DB_URL")
                or build_postgres_url("POSTGRES", fallback_env="DATABASE_URL")
            )
            self.citation_engine = CitationEngine(
                mode="multi-agent", context=ctx, db_url=db_url
            )
            self.citation_engine._connect()

        return self.citation_engine

    def get_or_register_doc_source(
        self, file_path: str, name: Optional[str] = None
    ) -> int:
        """Get cached source_id or register new document source.

        Checks the source registry first to avoid re-registering the same document.

        Args:
            file_path: Path to the document file
            name: Optional human-readable name for the source

        Returns:
            source_id for use in citations

        Raises:
            FileNotFoundError: If document doesn't exist
        """
        if file_path in self._source_registry:
            return self._source_registry[file_path]

        engine = self.get_citation_engine()
        source = engine.add_doc_source(file_path, name=name)
        self._source_registry[file_path] = source.id
        return source.id

    def get_or_register_web_source(
        self, url: str, name: Optional[str] = None
    ) -> tuple[int, Optional[str]]:
        """Get cached source_id or register new web source.

        Checks the source registry first to avoid re-registering the same URL.
        If the URL cannot be fetched (e.g. 403 Forbidden), the source is still
        registered with metadata only and a fetch_error is returned.

        Args:
            url: URL of the web source
            name: Optional human-readable name for the source

        Returns:
            Tuple of (source_id, fetch_error). fetch_error is None if content
            was fetched successfully, or a string describing the error.
        """
        if url in self._source_registry:
            source_id = self._source_registry[url]
            fetch_error = self._inaccessible_sources.get(url)
            return source_id, fetch_error

        engine = self.get_citation_engine()
        source = engine.add_web_source(url, name=name)
        self._source_registry[url] = source.id

        # Check if content was actually fetched
        fetch_error = None
        if source.metadata and source.metadata.get("fetch_error"):
            fetch_error = source.metadata["fetch_error"]
            self._inaccessible_sources[url] = fetch_error

        return source.id, fetch_error

    def save_web_content_to_disk(
        self,
        url: str,
        content: str,
        title: Optional[str] = None,
        source_id: Optional[int] = None,
    ) -> Optional[str]:
        """Save web content as a markdown file with YAML front-matter.

        Generates deterministic filenames from URL (same URL = same file).
        Skips write if file already exists (first save wins).

        Args:
            url: Source URL
            content: Raw text content to save
            title: Optional page title
            source_id: Optional CitationEngine source ID

        Returns:
            Workspace-relative path (e.g. "documents/external/example_com_a1b2c3d4.md"),
            or None if no workspace is available.
        """
        if not self.has_workspace():
            return None

        # Generate deterministic filename from URL
        url_hash = hashlib.sha256(url.encode()).hexdigest()[:8]
        parsed = urlparse(url)
        domain = parsed.netloc or "unknown"
        # Sanitize domain for filesystem
        safe_domain = re.sub(r"[^a-zA-Z0-9_.-]", "_", domain)
        filename = f"{safe_domain}_{url_hash}.md"
        relative_path = f"documents/external/{filename}"

        # Skip if file already exists (first save wins)
        if self.workspace_manager.exists(relative_path):
            return relative_path

        # Ensure directory exists (use backend, not local mkdir)
        self.workspace_manager.backend.mkdir("documents/external")

        # Build YAML front-matter
        front_matter_lines = [
            "---",
            f"url: {url}",
        ]
        if title:
            # Escape quotes in title for YAML
            safe_title = title.replace('"', '\\"')
            front_matter_lines.append(f'title: "{safe_title}"')
        front_matter_lines.append(
            f"fetched_at: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}"
        )
        if source_id is not None:
            front_matter_lines.append(f"source_id: {source_id}")
        front_matter_lines.append("---")
        front_matter_lines.append("")

        file_content = "\n".join(front_matter_lines) + content

        try:
            self.workspace_manager.write_file(relative_path, file_content)
            logger.debug(f"Saved web content to {relative_path}")
        except Exception as e:
            logger.warning(f"Failed to save web content to disk: {e}")
            return None

        return relative_path

    def queue_memory(
        self,
        content: str,
        keywords: Optional[List[str]] = None,
        importance: float = 0.5,
        source: str = "observer",
        source_phase: Optional[int] = None,
        memory_type: str = "factual",
    ) -> None:
        """Queue a memory for async storage by the graph's audited_tools node.

        This is sync-safe — can be called from ``@tool`` functions which run
        synchronously. The actual async ``RecallStore.store()`` happens when
        ``drain_pending_memories()`` is called from the async graph node.

        Args:
            content: Memory content text
            keywords: Keywords for sparse search
            importance: Importance score 0-1
            source: Memory source (todo, compaction, phase_archive, tool_error)
            source_phase: Phase number when memory was created
            memory_type: factual, procedural, error_solution, vocabulary, relational
        """
        self._pending_memories.append(
            {
                "content": content,
                "keywords": keywords,
                "importance": importance,
                "source": source,
                "source_phase": source_phase,
                "memory_type": memory_type,
            }
        )

    def drain_pending_memories(self) -> List[Dict[str, Any]]:
        """Return and clear all pending memories.

        Called from async graph nodes to flush queued memories into RecallStore.

        Returns:
            List of memory dicts ready for RecallStore.store(**mem)
        """
        pending = self._pending_memories[:]
        self._pending_memories.clear()
        return pending

    def request_freeze(self, freeze_data: Dict[str, Any]) -> None:
        """Request a job freeze from a tool (e.g., blocking send_message).

        The freeze is consumed by the graph's audited_tools node after
        tool execution completes. This is sync-safe — can be called from
        ``@tool`` functions.

        Args:
            freeze_data: Freeze metadata dict (freeze_type, thread_id, etc.)
        """
        self._freeze_request = freeze_data

    def consume_freeze_request(self) -> Optional[Dict[str, Any]]:
        """Return and clear any pending freeze request.

        Called from the async audited_tools graph node after tool execution.

        Returns:
            Freeze data dict if a freeze was requested, None otherwise.
        """
        req = self._freeze_request
        self._freeze_request = None
        return req

    def close_citation_engine(self) -> None:
        """Close CitationEngine connection if open.

        Should be called when the tool context is being disposed of
        to properly clean up database connections.
        """
        if self.citation_engine is not None:
            self.citation_engine.close()
            self.citation_engine = None
            self._source_registry.clear()

    def record_file_read(self, path: str) -> None:
        """Record that a file was read. Uses normalized path.

        This is called by read_file to track which files have been recently
        accessed. The tracking window is limited to the last N reads (default 10).

        Args:
            path: Path to the file that was read
        """
        normalized = path.lstrip("/").strip()
        # Remove if already present (we'll re-add at the end)
        if normalized in self._recent_reads:
            self._recent_reads.remove(normalized)
        self._recent_reads.append(normalized)

    def was_recently_read(self, path: str) -> bool:
        """Check if file was read within the tracking window.

        Used by edit_file and write_file to enforce read-before-write discipline.

        Args:
            path: Path to check

        Returns:
            True if the file was recently read, False otherwise
        """
        normalized = path.lstrip("/").strip()
        return normalized in self._recent_reads

    def get_read_tracking_limit(self) -> int:
        """Get the tracking window size from config or default.

        Returns:
            Number of recent reads to track (default 10)
        """
        return self.get_config("read_tracking_limit", 10)

    def get_enforcement_files(self, tool_name: str) -> List[str]:
        """Get instruction files that must be read before using a tool.

        Checks instruction_files config for entries with trigger type
        'before_tool' matching the given tool name and enforce=True.

        Args:
            tool_name: Name of the tool being called

        Returns:
            List of workspace-relative file paths that must be read first
        """
        required = []
        for entry in self._instruction_files:
            if (
                entry.enforce
                and entry.trigger_type == "before_tool"
                and entry.trigger_target == tool_name
            ):
                required.append(entry.file)
        return required

    def check_tool_enforcement(self, tool_name: str) -> Optional[str]:
        """Check if a tool's instruction file enforcement requirements are met.

        Returns an error message if any required instruction files have not
        been recently read. Returns None if all requirements are met.

        Args:
            tool_name: Name of the tool being called

        Returns:
            Error message string if enforcement fails, None if OK
        """
        required_files = self.get_enforcement_files(tool_name)
        for file_path in required_files:
            if not self.was_recently_read(file_path):
                from src.services.guardrails import format_nudge

                model = self._llm_config.model if self._llm_config is not None else None
                return format_nudge(
                    "read_file_required_error",
                    model=model,
                    file_path=file_path,
                    tool_name=tool_name,
                )
        return None

    def get_phase_instruction_files(self, phase: str) -> List[Any]:
        """Get instruction files triggered by a phase transition.

        Returns entries with trigger type 'phase' matching the given phase.

        Args:
            phase: Phase name ('strategic' or 'tactical')

        Returns:
            List of InstructionFileEntry objects for this phase
        """
        return [
            entry
            for entry in self._instruction_files
            if entry.trigger_type == "phase" and entry.trigger_target == phase
        ]

    def set_current_phase(self, phase: str) -> None:
        """Set the current execution phase for phase-aware behavior.

        Args:
            phase: Phase name ("strategic" or "tactical")
        """
        self._current_phase = phase

    def get_phase_multimodal(self) -> bool:
        """Get the effective multimodal setting for the current phase.

        If an LLMConfig is available and phase is set, uses the phase-specific
        config (which may override the base multimodal setting). Otherwise
        falls back to the static config value.

        Returns:
            True if the current phase's model supports multimodal input
        """
        if self._llm_config is not None and self._current_phase is not None:
            phase_config = self._llm_config.get_phase_config(self._current_phase)
            return phase_config.multimodal
        return self.get_config("multimodal", False)

    # ── Browser session lifecycle ────────────────────────────────────

    def should_include_screenshots(self) -> bool:
        """Whether browser tools should include screenshots in results.

        Resolves the 'auto' setting: true if the current model is multimodal.
        """
        browser_cfg = self.config.get("browser", {})
        snapshot_cfg = browser_cfg.get("snapshot", {})
        setting = snapshot_cfg.get("include_screenshot", "auto")
        if setting == "auto":
            return self.get_phase_multimodal()
        return bool(setting)

    def get_max_dom_chars(self) -> int:
        """Max DOM text characters to return from browser tools."""
        browser_cfg = self.config.get("browser", {})
        snapshot_cfg = browser_cfg.get("snapshot", {})
        return snapshot_cfg.get("max_dom_chars", 40000)

    async def browser_exec(self, action: str, **args: Any) -> Dict[str, Any]:
        """Run one browser action on the workspace via the browser-exec helper.

        Drives Chromium on the workspace over SSH (``exec_command``) rather
        than speaking CDP across the pod boundary. The workspace daemon holds
        the persistent session so element refs survive between calls. Returns
        the parsed JSON result, or an ``{"error": ...}`` dict on any failure.

        See docs/features/browser_workspace_executor.md.
        """
        import asyncio
        import json as _json
        import shlex

        if not self.has_workspace():
            return {
                "error": (
                    "browser tools require a workspace running the "
                    "browser-exec daemon; no workspace backend is attached"
                )
            }

        backend = self.workspace_manager.backend
        payload = _json.dumps(args)
        cmd = f"browser-exec {shlex.quote(action)} --json {shlex.quote(payload)}"
        try:
            # exec_command is blocking SSH; keep the event loop responsive.
            out = await asyncio.to_thread(backend.exec_command, cmd, 200)
        except Exception as e:
            return {"error": f"browser-exec call failed: {e}"}

        out = (out or "").strip()
        if not out:
            return {"error": "browser-exec returned no output"}
        try:
            # The client prints exactly one JSON line to stdout.
            return _json.loads(out.splitlines()[-1])
        except Exception:
            return {"error": f"browser-exec returned non-JSON: {out[:500]}"}

    async def close_browser(self) -> None:
        """Shut down the workspace browser-exec daemon. Called on job/session end."""
        if not self.has_workspace():
            return
        try:
            await self.browser_exec("shutdown")
        except Exception as e:
            logger.debug(f"browser-exec shutdown failed: {e}")
        logger.info("Browser cleaned up")
