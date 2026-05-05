"""Agent Implementation.

This Agent is a configurable, workspace-centric autonomous agent
that can be deployed as Creator, Validator, or any future agent type by
changing its configuration file.

Key Features:
- Config-driven behavior from JSON files
- Workspace-centric architecture with filesystem for strategic planning
- TodoManager for tactical execution with archiving
- Dynamic tool loading based on configuration
- Simplified 4-node LangGraph workflow
"""

import asyncio
import logging
import os
import shutil
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

import aiosqlite
import yaml
from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from .core.loader import (
    AgentConfig,
    LLMConfig,
    load_agent_config,
    create_llm,
    load_instructions,
    get_all_tool_names,
    resolve_config_path,
    resolve_model_settings,
)
from .core.loader import get_project_root
from .core.phase_snapshot import PhaseSnapshotManager
from .core.state import UniversalAgentState, create_initial_state
from .core.workspace import (
    WorkspaceManager,
    WorkspaceManagerConfig,
    get_checkpoints_path,
)
from .graph import build_phase_alternation_graph, run_graph_with_streaming
from .managers import TodoManager
from .tools import ToolContext, load_tools, apply_instruction_enforcement
from .tools.description_manager import (
    generate_workspace_tool_docs,
    apply_description_overrides,
)


class _AiosqliteConnectionWrapper:
    """Wrapper for aiosqlite.Connection that adds is_alive() method.

    langgraph-checkpoint-sqlite 3.x expects connections to have is_alive(),
    but aiosqlite.Connection doesn't provide it. This wrapper adds compatibility.
    """

    def __init__(self, conn: aiosqlite.Connection):
        self._conn = conn

    def is_alive(self) -> bool:
        """Check if connection is alive (always True for established connections)."""
        return True

    def __getattr__(self, name):
        """Delegate all other attributes to the wrapped connection."""
        return getattr(self._conn, name)


logger = logging.getLogger(__name__)


def _format_delegation_results(delegation_results: list) -> str:
    """Format delegation child results as a human-readable message.

    Injected into the parent's graph state as a HumanMessage on delegation
    resume so the agent can review each child's outcome.
    """
    lines = [
        "## Delegation Results",
        "",
        f"All {len(delegation_results)} subagent(s) have completed. "
        "Review each child's changes below, then merge or resume with feedback.",
        "",
    ]
    for child in delegation_results:
        status = child.get("status", "unknown")
        emoji = "completed" if status == "completed" else status
        lines.append(f"### Child {child.get('creation_order', '?')}: {emoji}")
        lines.append(f"- **Job ID**: {child.get('job_id', 'unknown')}")
        lines.append(f"- **Config**: {child.get('config', 'unknown')}")
        lines.append(f"- **Status**: {status}")
        if child.get("confidence") is not None:
            lines.append(f"- **Confidence**: {child['confidence']}")
        if child.get("branch_name"):
            lines.append(f"- **Branch**: {child['branch_name']}")
        if child.get("summary"):
            lines.append(f"- **Summary**: {child['summary']}")
        lines.append("")
        lines.append(
            f"To review: `git diff main..{child.get('branch_name', 'subagent/?')}`"
        )
        lines.append("")

    lines.append(
        "Use `git_diff` to review each branch. Approve by squash-merging "
        "in creation order, or resume a child with feedback if changes need revision."
    )
    return "\n".join(lines)


class UniversalAgent:
    """
    Configurable autonomous agent using workspace-centric architecture.

    The Universal Agent reads its behavior from a JSON configuration file,
    enabling a single implementation to serve as Creator, Validator, or
    any other agent type.

    Architecture:
    - Strategic Planning: Filesystem-based plans in workspace/plans/
    - Tactical Execution: TodoManager with next_phase_todos()
    - Context Management: Automatic compaction and summarization
    - Tool Loading: Dynamic based on config.tools
    """

    def __init__(
        self,
        config: AgentConfig,
        postgres_conn: Optional[Any] = None,
    ):
        """
        Initialize the Universal Agent.

        Args:
            config: Agent configuration (from JSON file)
            postgres_conn: PostgreSQL connection (optional, created if needed)
        """
        self.config = config
        self._base_config = config  # Immutable snapshot for reset between jobs
        self.postgres_conn = postgres_conn

        # Components (initialized lazily or via initialize())
        self._llm: Optional[BaseChatModel] = None
        self._llm_with_tools: Optional[BaseChatModel] = None
        self._tools: Optional[List] = None
        self._graph = None
        self._checkpointer: Optional[AsyncSqliteSaver] = None
        self._checkpoint_conn: Optional[aiosqlite.Connection] = None

        # Phase-specific LLMs (created if phase overrides configured)
        self._strategic_llm: Optional[BaseChatModel] = None
        self._tactical_llm: Optional[BaseChatModel] = None
        self._summarization_llm: Optional[BaseChatModel] = None
        self._strategic_llm_with_tools: Optional[BaseChatModel] = None
        self._tactical_llm_with_tools: Optional[BaseChatModel] = None

        # Auxiliary LLM for support tasks (summarization, memory extraction, curation)
        self._auxiliary_llm = None

        # Tool context (for phase-aware behavior)
        self._tool_context: Optional[ToolContext] = None

        # Current job state
        self._workspace_manager: Optional[WorkspaceManager] = None
        self._todo_manager: Optional[TodoManager] = None
        self._current_job_id: Optional[str] = None
        self._job_metadata: Optional[Dict[str, Any]] = None
        self._datasource_connections: Dict[str, Any] = {}
        self._datasource_clients: Dict[
            str, Any
        ] = {}  # Parent clients for cleanup (e.g. MongoClient)

        # Orchestrator client (injected by app layer for delegation/reporting)
        self._orchestrator_client = None

        # Persistent shell sessions (tmux-backed)
        self._shell_manager = None

        # Background document registration task
        self._doc_registration_task: Optional[asyncio.Task] = None

        # Knowledge base connection (for inline curation)
        self._knowledge_graph = None

        # Control flags
        self._initialized = False
        self._shutdown_requested = False

        # Metrics
        self._jobs_processed = 0
        self._start_time = datetime.utcnow()

        logger.info(f"Created {config.display_name} (agent_id={config.agent_id})")

    @property
    def agent_id(self) -> str:
        """Get the agent ID."""
        return self.config.agent_id

    @property
    def display_name(self) -> str:
        """Get the display name."""
        return self.config.display_name

    @classmethod
    def from_config(
        cls,
        config_path: str,
        postgres_conn: Optional[Any] = None,
    ) -> "UniversalAgent":
        """
        Create an agent from a configuration file.

        Args:
            config_path: Path to config file or config name (e.g., "creator")
            postgres_conn: Optional PostgreSQL connection

        Returns:
            UniversalAgent instance
        """
        resolved_path, deployment_dir = resolve_config_path(config_path)
        config = load_agent_config(resolved_path, deployment_dir)
        return cls(config, postgres_conn)

    async def initialize(self) -> None:
        """
        Initialize the agent and its components.

        This must be called before processing jobs. Sets up:
        - Database connections (if not provided)
        - LLM instance
        - Context manager
        - Base tools (workspace, to-do)

        Raises:
            RuntimeError: If required connections cannot be established
        """
        if self._initialized:
            logger.warning("Agent already initialized")
            return

        logger.info(f"Initializing {self.config.display_name}...")

        # Set up database connections if needed
        await self._setup_connections()

        # Create LLM(s) with context limit validation (Layer 0 safety)
        self._create_phase_llms()

        self._initialized = True
        logger.info(f"{self.config.display_name} initialized successfully")

    def _create_phase_llms(self) -> None:
        """Create phase-specific LLMs based on configuration.

        If phase overrides are configured, creates separate LLMs for:
        - Strategic phase (planning, high-level decisions)
        - Tactical phase (execution)
        - Summarization (context compaction)

        If no overrides configured, reuses the same LLM for all phases.
        """
        llm_config = self.config.llm
        limits = self.config.limits

        if llm_config.has_phase_overrides():
            # Create phase-specific LLMs
            strategic_config = llm_config.get_phase_config("strategic")
            tactical_config = llm_config.get_phase_config("tactical")
            summarization_config = llm_config.get_phase_config("summarization")

            self._strategic_llm = create_llm(strategic_config, limits=limits)
            logger.info(f"Created strategic LLM: {strategic_config.model}")

            # Optimization: reuse LLM if fully identical config (not just model name)
            if tactical_config == strategic_config:
                self._tactical_llm = self._strategic_llm
                logger.info(
                    f"Tactical LLM: reusing strategic ({tactical_config.model})"
                )
            else:
                self._tactical_llm = create_llm(tactical_config, limits=limits)
                logger.info(f"Created tactical LLM: {tactical_config.model}")

            if not llm_config.summarization:
                # No explicit summarization override — reuse strategic LLM
                # (avoids creating a separate LLM with potentially unreachable base config)
                self._summarization_llm = self._strategic_llm
                logger.info(
                    f"Summarization LLM: reusing strategic ({strategic_config.model}) (no override)"
                )
            elif summarization_config == strategic_config:
                self._summarization_llm = self._strategic_llm
                logger.info(
                    f"Summarization LLM: reusing strategic ({summarization_config.model})"
                )
            elif summarization_config == tactical_config:
                self._summarization_llm = self._tactical_llm
                logger.info(
                    f"Summarization LLM: reusing tactical ({summarization_config.model})"
                )
            else:
                self._summarization_llm = create_llm(
                    summarization_config, limits=limits
                )
                logger.info(f"Created summarization LLM: {summarization_config.model}")

            # Base LLM defaults to strategic for backwards compatibility
            self._llm = self._strategic_llm
        else:
            # No phase overrides - single LLM for all phases
            self._llm = create_llm(llm_config, limits=limits)
            self._strategic_llm = self._llm
            self._tactical_llm = self._llm
            self._summarization_llm = self._llm
            logger.info(f"Created single LLM for all phases: {llm_config.model}")

        # Create AuxiliaryLLM for support tasks (summarization, memory, curation)
        self._initialize_auxiliary_llm(llm_config, limits)

    def _initialize_auxiliary_llm(self, llm_config, limits) -> None:
        """Create the AuxiliaryLLM instance for support tasks.

        Uses auxiliary.model/base_url if configured, otherwise falls back
        to the summarization LLM (which itself falls back to strategic LLM).
        """
        from src.services.auxiliary import AuxiliaryLLM

        aux_config = self.config.auxiliary
        if not aux_config.enabled:
            # Wrap summarization LLM as fallback even when auxiliary is disabled
            self._auxiliary_llm = AuxiliaryLLM(llm=self._summarization_llm)
            logger.info("AuxiliaryLLM disabled, using summarization LLM as fallback")
            return

        if aux_config.model:
            # Dedicated auxiliary model — resolve settings matrix for its family
            model_settings = resolve_model_settings(
                aux_config.model, self.config._deployment_dir
            )
            # AuxiliaryConfig fields (temperature) take precedence;
            # settings matrix provides top_p, top_k, model_max_context_tokens
            aux_llm_config = LLMConfig(
                model=aux_config.model,
                base_url=aux_config.base_url,
                api_key=aux_config.api_key,
                temperature=aux_config.temperature,
                top_p=model_settings.get("top_p"),
                top_k=model_settings.get("top_k"),
                model_max_context_tokens=model_settings.get("model_max_context_tokens"),
                max_retries=1,
            )
            aux_llm = create_llm(aux_llm_config, limits=limits)
            logger.info(
                f"Created auxiliary LLM: {aux_config.model}"
                f" (settings matrix: top_p={aux_llm_config.top_p},"
                f" top_k={aux_llm_config.top_k},"
                f" max_ctx={aux_llm_config.model_max_context_tokens})"
            )
        else:
            # Reuse summarization LLM (which is already the best fallback chain)
            aux_llm = self._summarization_llm
            logger.info("AuxiliaryLLM: reusing summarization LLM")

        self._auxiliary_llm = AuxiliaryLLM(
            llm=aux_llm,
            max_iterations=aux_config.max_iterations,
            timeout=aux_config.timeout,
        )

    async def _setup_connections(self) -> None:
        """Set up required database connections.

        Falls back to environment variables for configuration.
        External datasources (Neo4j, MongoDB, etc.) are resolved per-job
        via the datasource connector system — see docs/datasources.md.
        """
        # PostgreSQL connection (always required for job management)
        if self.postgres_conn is None and self.config.connections.postgres:
            from src.database.postgres_db import PostgresDB

            db_url = os.getenv("DATABASE_URL")
            if db_url:
                self.postgres_conn = PostgresDB(connection_string=db_url)
                await self.postgres_conn.connect()
                logger.info("PostgreSQL connection established (PostgresDB)")
            else:
                logger.warning("DATABASE_URL not set, PostgreSQL unavailable")

        # Vector DB connection (for citations, memories + knowledge index)
        vector_url = os.getenv("VECTOR_DB_URL")
        if vector_url:
            from src.database.postgres_db import PostgresDB as _VectorDB

            self.vector_conn = _VectorDB(connection_string=vector_url)
            await self.vector_conn.connect()
            logger.info("Vector DB connection established (separate instance)")
        else:
            logger.warning("VECTOR_DB_URL not set, vector features unavailable")
            self.vector_conn = None

    async def process_job(
        self,
        job_id: str,
        metadata: Optional[Dict[str, Any]] = None,
        stream: bool = False,
        resume: bool = False,
        feedback: Optional[str] = None,
        original_config_name: Optional[str] = None,
        previous_status: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Process a single job.

        Creates a workspace for the job, loads tools, builds the graph,
        and executes until completion.

        Args:
            job_id: Unique job identifier
            metadata: Job-specific data (document_path, requirement_id, etc.)
            stream: If True, return an async iterator of state updates
            resume: If True, resume from last completed phase snapshot
            feedback: Optional feedback message to inject when resuming a frozen job
            original_config_name: Original config name used when job was created
                (for legacy checkpoint lookup when resuming old jobs)
            previous_status: Job status before resume. Graceful stops (cancelled,
                paused, pending_review) skip snapshot recovery; crash states
                (processing, failed, None) use snapshot recovery.

        Returns:
            Final state dictionary with results

        Example:
            ```python
            result = await agent.process_job(
                job_id="job_123",
                metadata={"document_path": "/data/doc.pdf"}
            )
            print(result["should_stop"])  # True if completed
            ```
        """
        if not self._initialized:
            await self.initialize()

        # Reset config to base snapshot before applying per-job overrides
        self.config = self._base_config

        self._current_job_id = job_id
        self._job_metadata = metadata or {}
        self._datasource_connections = {}
        self._datasource_clients = {}
        logger.info(f"Processing job {job_id}")

        # Wire archiver + job context into AuxiliaryLLM for auxiliary call logging
        if self._auxiliary_llm:
            from src.core.archiver import get_archiver as _get_archiver_for_aux

            self._auxiliary_llm.set_job_context(
                archiver=_get_archiver_for_aux(),
                job_id=job_id,
                agent_type=self.config.agent_id,
            )

        try:
            # Create workspace for this job
            # Base path comes from WORKSPACE_PATH env var or defaults
            # This also copies documents to workspace and returns updated metadata
            updated_metadata = await self._setup_job_workspace(
                job_id, metadata, resume=resume
            )

            # Handle frozen job resume
            if resume:
                frozen_path = self._workspace_manager.get_path("output/job_frozen.json")
                if frozen_path.exists():
                    logger.info(f"Resuming frozen job {job_id}")
                    # Remove the frozen marker so the graph can continue
                    frozen_path.unlink()
                    logger.info("Removed job_frozen.json to allow continuation")
                    # NOTE: Status is set to 'processing' by the orchestrator
                    # when it dispatches/resumes the job — no DB write needed here.

            # Load tools for this job
            await self._setup_job_tools()

            # Create checkpointer for this job (enables resume after crash)
            checkpoint_path = self._get_checkpoint_path(job_id)
            self._checkpoint_conn = await aiosqlite.connect(checkpoint_path)
            # Wrap connection to add is_alive() for langgraph-checkpoint-sqlite 3.x compatibility
            wrapped_conn = _AiosqliteConnectionWrapper(self._checkpoint_conn)
            self._checkpointer = AsyncSqliteSaver(wrapped_conn)
            logger.info(f"Checkpointer initialized at {checkpoint_path}")

            # Create snapshot manager for phase recovery
            # Pass workspace backend so snapshots are extracted from VM to pod-local storage
            ws_backend = (
                self._workspace_manager.backend if self._workspace_manager else None
            )
            snapshot_manager = PhaseSnapshotManager(
                job_id,
                workspace_backend=ws_backend,
            )

            # Build graph for this job
            self._graph = build_phase_alternation_graph(
                strategic_llm_with_tools=self._strategic_llm_with_tools,
                tactical_llm_with_tools=self._tactical_llm_with_tools,
                tools=self._tools,
                config=self.config,
                workspace=self._workspace_manager,
                todo_manager=self._todo_manager,
                workspace_template="",
                checkpointer=self._checkpointer,
                auxiliary_llm=self._auxiliary_llm,
                snapshot_manager=snapshot_manager,
                tool_context=self._tool_context,
                postgres_db=self.postgres_conn,
            )

            # Execute graph
            # Use job_id as thread_id (new format), with fallback to legacy format for old jobs
            thread_id = job_id
            thread_config = {
                "configurable": {
                    "thread_id": thread_id,
                },
                "recursion_limit": 1000000,  # Effectively unlimited
            }

            # Check if we should resume from phase snapshot or use checkpoint directly
            # Graceful stops (cancel/pause/review) have a valid checkpoint.db with current todos,
            # so we skip snapshot recovery. Crash states need snapshot recovery because the
            # checkpoint may be corrupted or incomplete.
            GRACEFUL_STOP_STATUSES = {
                "cancelled",
                "paused",
                "pending_review",
                "waiting",
            }
            graph_input = None
            if resume:
                is_graceful = previous_status in GRACEFUL_STOP_STATUSES
                if is_graceful:
                    logger.info(
                        f"Graceful resume from '{previous_status}' — using checkpoint directly "
                        f"(skipping snapshot recovery to preserve in-progress todos)"
                    )
                    # Use checkpoint.db as-is — discover thread_id and verify checkpoint exists
                    (
                        graph_input,
                        thread_id,
                        thread_config,
                    ) = await self._resume_from_checkpoint(
                        job_id,
                        thread_id,
                        thread_config,
                        original_config_name,
                        updated_metadata,
                    )
                    if graph_input is not None:
                        # Checkpoint lookup failed — fall back to snapshot recovery
                        logger.warning(
                            f"No valid checkpoint found for graceful resume from '{previous_status}', "
                            f"falling back to snapshot recovery"
                        )
                        (
                            graph_input,
                            thread_id,
                            thread_config,
                        ) = await self._resume_from_snapshot(
                            job_id,
                            snapshot_manager,
                            thread_id,
                            thread_config,
                            original_config_name,
                            updated_metadata,
                        )
                else:
                    # Crash/failure recovery — use snapshot (more reliable than potentially corrupted checkpoint)
                    if previous_status:
                        logger.info(
                            f"Crash recovery from '{previous_status}' — using snapshot recovery"
                        )
                    (
                        graph_input,
                        thread_id,
                        thread_config,
                    ) = await self._resume_from_snapshot(
                        job_id,
                        snapshot_manager,
                        thread_id,
                        thread_config,
                        original_config_name,
                        updated_metadata,
                    )
            else:
                # Fresh start - create initial state
                graph_input = create_initial_state(
                    job_id=job_id,
                    workspace_path=str(self._workspace_manager.path),
                    metadata=updated_metadata,
                )

            # Inject feedback into graph state via aupdate_state
            # This sets resume_feedback so route_entry routes to restore_from_feedback
            if resume and feedback and graph_input is None:
                await self._graph.aupdate_state(
                    thread_config,
                    {
                        "resume_feedback": feedback,
                        "should_stop": False,
                        "goal_achieved": False,
                        "is_final_phase": False,
                    },
                    as_node="__start__",
                )
                logger.info("Injected feedback into graph state via aupdate_state")

            # Inject delegation results into graph state when resuming from waiting
            delegation_results = (updated_metadata or {}).get("delegation_results")
            if resume and delegation_results and graph_input is None:
                from langchain_core.messages import HumanMessage

                results_msg = _format_delegation_results(delegation_results)
                await self._graph.aupdate_state(
                    thread_config,
                    {
                        "messages": [HumanMessage(content=results_msg)],
                        "should_stop": False,
                        "goal_achieved": False,
                    },
                    as_node="restore_todo_state",
                )
                logger.info(
                    f"Injected delegation results into graph state "
                    f"({len(delegation_results)} children)"
                )

            if stream:
                # For streaming, cleanup happens inside the generator
                return self._process_job_streaming(graph_input, thread_config)
            else:
                try:
                    final_state = await self._graph.ainvoke(
                        graph_input,
                        config=thread_config,
                    )
                    self._jobs_processed += 1
                    return dict(final_state)
                finally:
                    self._current_job_id = None
                    self._cleanup_shell_manager()
                    self._close_datasource_connections()
                    await self._cleanup_checkpointer()

        except Exception as e:
            # Detect workspace unavailable errors (VM connection lost)
            from .core.workspace_backend import WorkspaceUnavailableError

            is_vm_error = isinstance(e, WorkspaceUnavailableError)

            if is_vm_error:
                logger.error(
                    f"Job {job_id}: VM workspace unavailable — will request recovery: {e}"
                )
            else:
                logger.error(f"Job {job_id} failed: {e}", exc_info=True)

            self._cleanup_shell_manager()
            self._close_datasource_connections()
            await self._cleanup_checkpointer()
            self._current_job_id = None
            error_state = {
                "job_id": job_id,
                "error": {
                    "message": str(e),
                    "type": "workspace_unavailable" if is_vm_error else "job_error",
                    "recoverable": is_vm_error,
                },
                "should_stop": True,
            }
            if stream:
                # Return async generator that yields the error state
                return self._yield_error_state(error_state)
            return error_state

    async def _cleanup_checkpointer(self) -> None:
        """Clean up checkpointer connection."""
        if self._checkpoint_conn:
            try:
                await self._checkpoint_conn.close()
            except Exception as e:
                logger.warning(f"Error closing checkpointer connection: {e}")
            self._checkpoint_conn = None
            self._checkpointer = None

    def _cleanup_shell_manager(self) -> None:
        """Clean up ShellManager (kill tmux session)."""
        if self._shell_manager:
            try:
                self._shell_manager.cleanup()
            except Exception:
                pass
            self._shell_manager = None

    async def _yield_error_state(
        self, error_state: Dict[str, Any]
    ) -> AsyncIterator[Dict[str, Any]]:
        """Yield a single error state for streaming mode."""
        yield error_state

    async def _process_job_streaming(
        self,
        graph_input: Optional[UniversalAgentState],
        config: Dict[str, Any],
    ) -> AsyncIterator[Dict[str, Any]]:
        """Process job with streaming state updates.

        Args:
            graph_input: Initial state for new jobs, or None to resume from checkpoint
            config: LangGraph config with thread_id
        """
        try:
            async for state in run_graph_with_streaming(
                self._graph, graph_input, config
            ):
                yield state

            self._jobs_processed += 1
        finally:
            # Clean up after streaming completes (or errors)
            self._current_job_id = None
            self._cleanup_shell_manager()
            self._close_datasource_connections()
            await self._cleanup_checkpointer()

    def _load_workspace_template(self) -> str:
        """Load the workspace.md template for the nested loop graph.

        Uses InstructionMatrixResolver for model-aware resolution.

        Returns:
            Template content for workspace.md
        """
        from .core.loader import InstructionMatrixResolver
        from .core.model_registry import family_of

        # Check for pre-resolved content
        resolved = self.config.extra.get("_resolved_instructions", {})
        if resolved.get("workspace_template"):
            return resolved["workspace_template"]

        model_family = family_of(self.config.llm.model)
        resolver = InstructionMatrixResolver(self.config._deployment_dir, model_family)
        return resolver.load("workspace_template")

    def _inject_repo_context_to_workspace(self, git_url: str, git_branch: str) -> None:
        """Append repository context to workspace.md after clone.

        This gives the agent persistent knowledge of the git remote URL,
        branch, and Gitea API endpoint so it can push and create PRs.
        The info survives context compaction since workspace.md is re-injected
        on every LLM call.
        """
        from urllib.parse import urlparse

        parsed = urlparse(git_url)
        # Gitea API base: scheme://host/api/v1
        gitea_api_base = f"{parsed.scheme}://{parsed.hostname}"
        if parsed.port:
            gitea_api_base += f":{parsed.port}"
        gitea_api_base += "/api/v1"

        # Repo path: strip .git suffix and leading slash
        repo_path = parsed.path.rstrip("/")
        if repo_path.endswith(".git"):
            repo_path = repo_path[:-4]
        repo_path = repo_path.lstrip("/")
        # owner/repo
        owner_repo = repo_path  # e.g. "user/my-repo"

        section = f"""

## Repository Context

- **Remote URL**: `{git_url}` (credentials embedded — use for push)
- **Branch**: `{git_branch}`
- **Gitea API**: `{gitea_api_base}`
- **Repo path**: `{owner_repo}`

### Push & PR Workflow

```bash
# Push (credentials are in the remote URL)
git push origin {git_branch}

# Create PR via Gitea API
curl -s -X POST "{gitea_api_base}/repos/{owner_repo}/pulls" \\
  -H "Content-Type: application/json" \\
  -d '{{"title": "PR_TITLE", "head": "{git_branch}", "base": "main", "body": "PR_DESCRIPTION"}}'
```
"""
        try:
            existing = self._workspace_manager.read_file("workspace.md")
            self._workspace_manager.write_file("workspace.md", existing + section)
            logger.info("Injected repository context into workspace.md")
        except Exception as e:
            logger.warning(f"Failed to inject repo context into workspace.md: {e}")

    async def _setup_job_workspace(
        self,
        job_id: str,
        metadata: Optional[Dict[str, Any]] = None,
        resume: bool = False,
    ) -> Dict[str, Any]:
        """Set up the workspace for a job.

        Creates the workspace directory structure, copies initial files
        (instructions, documents, etc.), and returns updated metadata
        with workspace-relative paths.

        Base path is determined by:
        1. WORKSPACE_PATH environment variable
        2. /workspace (container mode)
        3. ./workspace (development mode)

        Args:
            job_id: Unique job identifier
            metadata: Job metadata (may contain document_path, etc.)

        Returns:
            Updated metadata with workspace-relative paths
        """
        metadata = metadata or {}

        # On resume: try to load frozen config from JSONB (prevents config drift).
        # NOTE: serialize_resolved_config strips api_key from agent.llm before
        # storage, so the loaded config has llm.api_key=None. The orchestrator's
        # resume dispatch re-injects credentials into metadata.config_override
        # (see _inject_dispatch_credentials), which the override block below
        # layers on top — so we deliberately defer _create_phase_llms() until
        # after that merge happens at line ~1014 instead of recreating LLMs
        # twice with a half-built config.
        _config_from_db = False
        if resume and self.postgres_conn:
            try:
                from .core.loader import load_config_from_resolved
                import uuid as _uuid

                resolved = await self.postgres_conn.jobs.get_resolved_config(
                    _uuid.UUID(job_id)
                )
                if resolved:
                    self.config = load_config_from_resolved(resolved)
                    _config_from_db = True
                    logger.info(f"Loaded frozen config for resumed job {job_id}")
            except Exception as e:
                logger.warning(
                    f"Failed to load frozen config, falling back to disk: {e}"
                )

        # Handle expert config name - load the named config (tools, prompts, workspace settings)
        # This must happen before config_upload_id and config_override so those can further override
        if not _config_from_db and metadata.get("config_name"):
            from .core.loader import (
                _apply_settings_matrix,
                load_and_merge_config,
                load_agent_config_from_dict,
                resolve_config_path,
            )

            expert_name = metadata["config_name"]
            try:
                config_path, deployment_dir = resolve_config_path(expert_name)
                logger.info(f"Loading expert config '{expert_name}' from {config_path}")
                merged_config_data = load_and_merge_config(config_path)

                # Apply settings_matrix (model-family defaults) — mirrors load_agent_config()
                raw_expert_llm_keys: set[str] = set()
                try:
                    with open(config_path, "r", encoding="utf-8") as f:
                        raw_expert = yaml.safe_load(f) or {}
                    raw_expert_llm_keys = set((raw_expert.get("llm") or {}).keys())
                except Exception:
                    pass
                _apply_settings_matrix(
                    merged_config_data, raw_expert_llm_keys, deployment_dir
                )

                self.config = load_agent_config_from_dict(
                    merged_config_data, deployment_dir=deployment_dir
                )
                logger.info(
                    f"Applied expert config '{expert_name}' (tools: {list(self.config.tools.__dict__.keys())})"
                )
            except Exception as e:
                logger.warning(
                    f"Failed to load expert config '{expert_name}': {e}. Continuing with current config."
                )

        # Handle config upload - load and merge with defaults BEFORE workspace setup
        # This must happen first since config affects workspace settings
        if not _config_from_db and metadata.get("config_upload_id"):
            config_upload_id = metadata["config_upload_id"]
            from .core.workspace import get_workspace_base_path
            from .core.loader import load_uploaded_config, load_agent_config_from_dict
            import tempfile

            config_loaded = False

            # Try HTTP download first
            with tempfile.TemporaryDirectory() as temp_dir:
                temp_path = Path(temp_dir)
                downloaded_files = await self._download_upload_files(
                    config_upload_id, temp_path, logger
                )

                if downloaded_files:
                    # Find the YAML file from downloaded files
                    yaml_files = list(temp_path.glob("*.yaml")) + list(
                        temp_path.glob("*.yml")
                    )
                    if yaml_files:
                        uploaded_config_path = yaml_files[0]
                        logger.info(
                            f"Loading uploaded config (HTTP): {uploaded_config_path.name}"
                        )

                        # Load and merge with defaults
                        merged_config_data = load_uploaded_config(uploaded_config_path)

                        # Create new config object (replaces self.config for this job)
                        self.config = load_agent_config_from_dict(merged_config_data)
                        logger.info("Applied uploaded config overrides")
                        config_loaded = True

            # Fall back to local filesystem
            if not config_loaded:
                config_uploads_dir = (
                    get_workspace_base_path() / "uploads" / config_upload_id
                )

                if config_uploads_dir.exists():
                    # Find the YAML file
                    yaml_files = list(config_uploads_dir.glob("*.yaml")) + list(
                        config_uploads_dir.glob("*.yml")
                    )
                    if yaml_files:
                        uploaded_config_path = yaml_files[0]
                        logger.info(
                            f"Loading uploaded config (local): {uploaded_config_path.name}"
                        )

                        # Load and merge with defaults
                        merged_config_data = load_uploaded_config(uploaded_config_path)

                        # Create new config object (replaces self.config for this job)
                        self.config = load_agent_config_from_dict(merged_config_data)
                        logger.info("Applied uploaded config overrides")
                    else:
                        logger.warning(
                            f"No YAML files found in config upload: {config_upload_id}"
                        )
                else:
                    logger.warning(
                        f"Config upload directory not found: {config_uploads_dir}"
                    )

        # Handle inline config override - merge on top of current config.
        # Runs even when _config_from_db (resume path) so the orchestrator's
        # re-injected credentials (api_key/base_url stripped from frozen
        # config) can layer back onto the merged config before LLMs are
        # created. Without this, resumed jobs hit the user's router with
        # api_key=None and 401.
        if metadata.get("config_override"):
            from .core.loader import (
                _apply_settings_matrix,
                deep_merge,
                load_agent_config_from_dict,
            )
            import dataclasses

            config_override = metadata["config_override"]
            logger.info(
                f"Applying inline config override: {list(config_override.keys())}"
            )

            # Convert current config to dict, merge, and reload
            # Preserve _deployment_dir so instruction file resolution still works
            prev_deployment_dir = self.config._deployment_dir
            current_config_dict = dataclasses.asdict(self.config)
            merged_config_data = deep_merge(current_config_dict, config_override)

            # If the override changes the model, re-apply settings_matrix for the
            # new model family. Override LLM keys are treated as "explicitly set"
            # so the matrix won't overwrite them.
            if config_override.get("llm"):
                override_llm_keys = set((config_override.get("llm") or {}).keys())
                _apply_settings_matrix(
                    merged_config_data, override_llm_keys, prev_deployment_dir
                )

            self.config = load_agent_config_from_dict(
                merged_config_data, deployment_dir=prev_deployment_dir
            )
            logger.info("Applied inline config overrides")

        # Apply env_keys overrides (user/project API keys for non-LLM providers)
        env_keys = (metadata.get("config_override") or {}).get("env_keys")
        if env_keys:
            import os as _os

            for k, v in env_keys.items():
                _os.environ[k] = v
            logger.info(f"Applied {len(env_keys)} env key override(s)")

        # Recreate LLMs if config was modified for this job. On resume we
        # always recreate when frozen config was loaded — frozen config has
        # api_key stripped and the override applied above carries the
        # re-injected credentials. Without recreation the strategic/tactical
        # LLMs would still hold whatever was built at agent boot (if any) or
        # would never be created at all.
        config_dirty = bool(
            metadata.get("config_name")
            or metadata.get("config_upload_id")
            or metadata.get("config_override")
        )
        if (not _config_from_db and config_dirty) or _config_from_db:
            logger.info("Config changed for this job — recreating LLMs")
            self._create_phase_llms()

        # Freeze resolved config on first run (not resume)
        if self.postgres_conn and not resume and not _config_from_db:
            try:
                from .core.loader import serialize_resolved_config
                import uuid as _uuid

                resolved = serialize_resolved_config(
                    self.config, model=self.config.llm.model
                )
                await self.postgres_conn.jobs.store_resolved_config(
                    _uuid.UUID(job_id), resolved
                )
                logger.info(f"Froze resolved config for job {job_id}")
            except Exception as e:
                logger.warning(f"Failed to freeze resolved config: {e}")

        # Create workspace backend. The agent never operates on its own
        # filesystem — backend must be "sandbox" or "vm" with SSH credentials
        # provided by the orchestrator at dispatch time.
        if self.config.workspace.backend not in ("sandbox", "vm"):
            raise RuntimeError(
                f"Unsupported workspace.backend={self.config.workspace.backend!r}. "
                f"The agent requires backend='sandbox' or 'vm' with SSH "
                f"credentials injected by the orchestrator."
            )
        if not self.config.workspace.remote:
            raise RuntimeError(
                f"workspace.backend={self.config.workspace.backend!r} but no "
                f"workspace.remote config was provided. The orchestrator must "
                f"inject SSH credentials pointing at a provisioned workspace "
                f"container or VM."
            )

        try:
            from .core.backends.remote import RemoteBackend

            remote_cfg = self.config.workspace.remote
            shell_config = self.config.extra.get("shell", {})
            workspace_backend = RemoteBackend(
                host=remote_cfg["host"],
                port=remote_cfg.get("port", 22),
                username=remote_cfg.get("username", "agent-host"),
                key_path=remote_cfg.get("key_path"),
                workspace_path=remote_cfg.get(
                    "workspace_path", "/home/agent-host/workspace"
                ),
                job_id=job_id,
                scrollback_limit=shell_config.get("scrollback_limit", 5000),
                default_timeout=shell_config.get("default_timeout", 120),
                max_tabs=shell_config.get("max_tabs", 15),
                blocked_commands=shell_config.get("blocked_commands"),
                sudo_action=shell_config.get("sudo_action", "freeze"),
            )
            workspace_backend.connect()
            logger.info(f"Remote workspace backend connected to {remote_cfg['host']}")
        except Exception as e:
            logger.error(f"Failed to create remote backend: {e}")
            raise

        # Worktree creation: subjobs on shared VM/container get a git worktree
        # instead of a full clone. The worktree is created on the remote machine.
        worktree_path = metadata.get("worktree_path")
        if worktree_path and workspace_backend and workspace_backend.supports_shell:
            parent_workspace = "/home/agent-host/workspace"
            branch_name = metadata.get("branch_name", "main")
            try:
                # Fetch the subjob branch (created by orchestrator on Gitea)
                workspace_backend._exec(
                    f"git -C {parent_workspace} fetch origin {branch_name}",
                    timeout=60,
                )
                # Create worktree directory parent
                workspace_backend._exec(
                    f"mkdir -p $(dirname {worktree_path})",
                    timeout=10,
                )
                # Create worktree for the subjob's branch
                workspace_backend._exec(
                    f"git -C {parent_workspace} worktree add {worktree_path} {branch_name}",
                    timeout=30,
                )
                logger.info(
                    f"Created git worktree at {worktree_path} (branch: {branch_name})"
                )
            except Exception as e:
                logger.warning(
                    f"Failed to create git worktree at {worktree_path}: {e}. "
                    "Falling back to standard workspace init."
                )
                # Clear worktree_path so we fall through to normal init
                worktree_path = None
                metadata.pop("worktree_path", None)

        # Create workspace manager
        self._workspace_manager = WorkspaceManager(
            job_id=job_id,
            config=WorkspaceManagerConfig(
                structure=self.config.workspace.structure,
                git_versioning=self.config.workspace.git_versioning,
                git_remote_url=metadata.get("git_remote_url"),
                branch_name=metadata.get("branch_name"),
                repositories=metadata.get("repositories"),
            ),
            backend=workspace_backend,
        )

        # VM recovery: seed fresh VM workspace from last snapshot if needed
        if resume and workspace_backend and workspace_backend.supports_shell:
            try:
                if not workspace_backend.exists("workspace.md"):
                    logger.info(
                        f"VM workspace is fresh — seeding from last snapshot for job {job_id}"
                    )
                    from .core.phase_snapshot import PhaseSnapshotManager

                    recovery_mgr = PhaseSnapshotManager(
                        job_id, workspace_backend=workspace_backend
                    )
                    latest = recovery_mgr.get_latest_snapshot()
                    if latest:
                        # Ensure base directories exist on VM
                        for subdir in self.config.workspace.structure:
                            try:
                                workspace_backend.mkdir(subdir.rstrip("/"))
                            except Exception:
                                pass
                        # Push snapshot files to VM
                        recovery_mgr.recover_to_phase(
                            latest.phase_number,
                            workspace_manager=self._workspace_manager,
                        )
                        logger.info(
                            f"Seeded VM workspace from phase {latest.phase_number} snapshot"
                        )
                    else:
                        logger.warning("No snapshots available to seed VM workspace")
            except Exception as e:
                logger.warning(f"VM workspace seeding failed: {e}")

        # Pod handoff: clone workspace from Gitea if resuming on a new pod
        if (
            resume
            and not self._workspace_manager.path.exists()
            and metadata.get("git_remote_url")
        ):
            from .managers.git_manager import GitManager

            logger.info(f"Pod handoff: cloning workspace for job {job_id}")
            git_mgr = GitManager.clone(
                metadata["git_remote_url"],
                self._workspace_manager.path,
                backend=self._workspace_manager.backend,
            )
            if git_mgr:
                # Checkout the correct branch for project jobs
                branch = metadata.get("branch_name")
                if branch:
                    git_mgr.checkout_branch(branch)

                self._workspace_manager._git_manager = git_mgr
                self._workspace_manager._initialized = True

                # Clone source/reference repos if project workspace
                if metadata.get("repositories"):
                    self._workspace_manager._clone_auxiliary_repos()

                self._todo_manager = TodoManager(
                    workspace=self._workspace_manager,
                    model_name=self.config.llm.model,
                )
                logger.info(f"Pod handoff complete for job {job_id}")
                return metadata or {}
            logger.warning(
                f"Pod handoff clone failed for job {job_id}, falling through to normal init"
            )

        # Check if resuming an existing workspace
        if resume and self._workspace_manager.path.exists():
            logger.info(f"Resuming job {job_id} with existing workspace")
            # Verify workspace has required files
            instructions_path = self._workspace_manager.path / "instructions.md"
            if not instructions_path.exists():
                # Only write instructions if missing
                instructions = load_instructions(
                    self.config, model=self.config.llm.model
                )
                self._workspace_manager.write_file("instructions.md", instructions)
                logger.debug("Wrote missing instructions.md to workspace")

            # Initialize git manager if git versioning is enabled (safe on existing repos)
            if (
                self._workspace_manager.config.git_versioning
                and self._workspace_manager.git_manager is None
            ):
                self._workspace_manager._initialize_git()
                self._workspace_manager._initialized = True

            # Ensure git remote is configured for workspace delivery
            if metadata.get("git_remote_url") and self._workspace_manager.git_manager:
                self._workspace_manager.git_manager.add_remote(
                    "origin", metadata["git_remote_url"]
                )

            # Ensure correct branch for project jobs
            if metadata.get("branch_name") and self._workspace_manager.git_manager:
                current = self._workspace_manager.git_manager.current_branch()
                expected = metadata["branch_name"]
                if current != expected:
                    self._workspace_manager.git_manager.checkout_branch(expected)
                    logger.info(f"Switched to expected branch: {expected}")

            # Create todo manager for this workspace
            self._todo_manager = TodoManager(
                workspace=self._workspace_manager,
                model_name=self.config.llm.model,
            )

            logger.debug(f"Resumed workspace at {self._workspace_manager.path}")
            return metadata or {}

        # Initialize workspace (creates directories)
        if metadata.get("repositories"):
            self._workspace_manager.initialize_project_workspace()
        else:
            self._workspace_manager.initialize()

        # Copy instructions to workspace (priority: inline > upload > template)
        if metadata.get("instructions"):
            # Use inline instructions (from job creation form or builder)
            self._workspace_manager.write_file(
                "instructions.md", metadata["instructions"]
            )
            logger.info("Using inline instructions from job metadata")
        elif metadata.get("instructions_upload_id"):
            # Use uploaded instructions
            instr_upload_id = metadata["instructions_upload_id"]
            from .core.workspace import get_workspace_base_path
            import tempfile

            instructions_written = False

            # Try HTTP download first
            with tempfile.TemporaryDirectory() as temp_dir:
                temp_path = Path(temp_dir)
                downloaded_files = await self._download_upload_files(
                    instr_upload_id, temp_path, logger
                )

                if downloaded_files:
                    # Find the instructions file from downloaded files
                    instr_files = list(temp_path.glob("*.md")) + list(
                        temp_path.glob("*.txt")
                    )
                    if instr_files:
                        uploaded_instr_path = instr_files[0]
                        content = uploaded_instr_path.read_text(encoding="utf-8")
                        self._workspace_manager.write_file("instructions.md", content)
                        logger.info(
                            f"Copied uploaded instructions (HTTP): {uploaded_instr_path.name}"
                        )
                        instructions_written = True

            # Fall back to local filesystem
            if not instructions_written:
                instr_uploads_dir = (
                    get_workspace_base_path() / "uploads" / instr_upload_id
                )

                if instr_uploads_dir.exists():
                    # Find the instructions file (.md or .txt)
                    instr_files = list(instr_uploads_dir.glob("*.md")) + list(
                        instr_uploads_dir.glob("*.txt")
                    )
                    if instr_files:
                        uploaded_instr_path = instr_files[0]
                        content = uploaded_instr_path.read_text(encoding="utf-8")
                        self._workspace_manager.write_file("instructions.md", content)
                        logger.info(
                            f"Copied uploaded instructions (local): {uploaded_instr_path.name}"
                        )
                        instructions_written = True
                    else:
                        logger.warning(
                            f"No .md/.txt files found in instructions upload: {instr_upload_id}"
                        )
                else:
                    logger.warning(
                        f"Instructions upload directory not found: {instr_uploads_dir}"
                    )

            # Fall back to template if upload failed
            if not instructions_written:
                pass  # Template-based fallback handled by _deploy_instruction_files()
        else:
            pass  # Template-based instructions handled by _deploy_instruction_files()

        # Write task brief to workspace (description + optional kickoff message)
        description = metadata.get("description", "")
        kickoff_message = metadata.get("kickoff_message", "")
        brief_parts = [f"# Task Brief\n\n## Description\n\n{description}"]
        if kickoff_message:
            brief_parts.append(f"\n\n## Kickoff Message\n\n{kickoff_message}")
        self._workspace_manager.write_file("task_brief.md", "".join(brief_parts))
        logger.debug("Wrote task_brief.md to workspace")

        # Process initial_files from config (e.g., workspace.md template)
        if self.config.workspace.initial_files:
            config_dir = Path(__file__).parent.parent / "config" / "agents"
            for dest_path, source_path in self.config.workspace.initial_files.items():
                # Skip instructions.md - already handled above
                if dest_path == "instructions.md":
                    continue
                try:
                    source_full = config_dir / source_path
                    if source_full.exists():
                        content = source_full.read_text(encoding="utf-8")
                        self._workspace_manager.write_file(dest_path, content)
                        logger.debug(
                            f"Initialized file: {dest_path} from {source_path}"
                        )
                    else:
                        logger.warning(
                            f"Initial file template not found: {source_path}"
                        )
                except Exception as e:
                    logger.warning(f"Failed to initialize {dest_path}: {e}")

        # Clone git repository if git_url is provided (for coding agents)
        if metadata.get("git_url") and not resume:
            git_url = metadata["git_url"]
            git_branch = metadata.get("git_branch", "main")
            repo_dir = self._workspace_manager.get_path("repo")
            logger.info(
                f"Cloning {git_url} (branch: {git_branch}) into workspace/repo/"
            )
            try:
                import subprocess

                # Clone the repository
                clone_result = subprocess.run(
                    ["git", "clone", "--branch", git_branch, git_url, str(repo_dir)],
                    capture_output=True,
                    text=True,
                    timeout=300,
                )
                if clone_result.returncode == 0:
                    logger.info(f"Repository cloned successfully into {repo_dir}")
                else:
                    # Try cloning without --branch (branch might not exist yet)
                    clone_result2 = subprocess.run(
                        ["git", "clone", git_url, str(repo_dir)],
                        capture_output=True,
                        text=True,
                        timeout=300,
                    )
                    if clone_result2.returncode == 0:
                        # Create and checkout the branch
                        subprocess.run(
                            ["git", "checkout", "-b", git_branch],
                            cwd=str(repo_dir),
                            capture_output=True,
                            text=True,
                        )
                        logger.info(f"Repository cloned, created branch: {git_branch}")
                    else:
                        logger.error(f"Git clone failed: {clone_result2.stderr}")
            except subprocess.TimeoutExpired:
                logger.error("Git clone timed out after 300s")
            except Exception as e:
                logger.error(f"Git clone failed: {e}")

            # Inject repo context into workspace.md if clone succeeded
            if repo_dir.exists() and any(repo_dir.iterdir()):
                self._inject_repo_context_to_workspace(git_url, git_branch)

        # Copy documents to workspace if provided
        updated_metadata = dict(metadata)

        # Handle upload_id (files uploaded via orchestrator UI)
        if metadata.get("upload_id"):
            upload_id = metadata["upload_id"]
            from .core.workspace import get_workspace_base_path
            import tempfile

            copied_paths = []
            original_paths = []

            # Ensure documents directory exists (use backend for remote compat)
            backend = self._workspace_manager.backend
            backend.mkdir("documents")

            upload_source_dir = None

            # Try HTTP download first
            temp_dir_obj = tempfile.TemporaryDirectory()
            temp_path = Path(temp_dir_obj.name)
            downloaded_files = await self._download_upload_files(
                upload_id, temp_path, logger
            )

            if downloaded_files:
                upload_source_dir = temp_path
                logger.info(f"Processing documents from HTTP download: {upload_id}")
            else:
                # Fall back to local filesystem
                local_uploads_dir = get_workspace_base_path() / "uploads" / upload_id
                if local_uploads_dir.exists():
                    upload_source_dir = local_uploads_dir
                    logger.info(
                        f"Processing documents from local path: {local_uploads_dir}"
                    )
                else:
                    logger.warning(
                        f"Upload directory not found locally or via HTTP: {upload_id}"
                    )

            if upload_source_dir:
                for file_path in sorted(upload_source_dir.iterdir()):
                    # Skip metadata.json
                    if file_path.name == "metadata.json":
                        continue
                    if file_path.is_file():
                        # Check if zip - extract instead of copy
                        if file_path.suffix.lower() == ".zip":
                            extracted = self._extract_zip(
                                file_path, "documents", logger
                            )
                            copied_paths.extend(extracted)
                            original_paths.extend([str(file_path)] * len(extracted))
                            logger.info(
                                f"Processed zip file: {file_path.name} ({len(extracted)} files extracted)"
                            )
                        else:
                            # Regular file - copy via backend with conflict handling
                            dest_name = file_path.name
                            counter = 1
                            stem = Path(file_path.name).stem
                            suffix = Path(file_path.name).suffix
                            while backend.exists(f"documents/{dest_name}"):
                                dest_name = f"{stem}_{counter}{suffix}"
                                counter += 1

                            dest_relative = f"documents/{dest_name}"
                            backend.write_file(dest_relative, file_path.read_bytes())
                            logger.info(
                                f"Copied uploaded file to workspace: {dest_relative}"
                            )

                            copied_paths.append(dest_relative)
                            original_paths.append(str(file_path))

            # Clean up temp directory
            temp_dir_obj.cleanup()

            if copied_paths:
                updated_metadata["document_paths"] = copied_paths
                updated_metadata["original_document_paths"] = original_paths
                # For backwards compatibility, set document_path to first document
                updated_metadata["document_path"] = copied_paths[0]
                updated_metadata["original_document_path"] = original_paths[0]

        # Handle multiple documents (document_paths list)
        elif metadata.get("document_paths"):
            copied_paths = []
            original_paths = []

            # Ensure documents directory exists (use backend for remote compat)
            backend = self._workspace_manager.backend
            backend.mkdir("documents")

            for doc_path in metadata["document_paths"]:
                source_path = Path(doc_path)
                if source_path.exists():
                    # Check if zip - extract instead of copy
                    if source_path.suffix.lower() == ".zip":
                        extracted = self._extract_zip(source_path, "documents", logger)
                        copied_paths.extend(extracted)
                        original_paths.extend([str(source_path)] * len(extracted))
                        logger.info(
                            f"Processed zip file: {source_path.name} ({len(extracted)} files extracted)"
                        )
                    else:
                        # Regular file - copy via backend with conflict handling
                        dest_name = source_path.name
                        counter = 1
                        stem = Path(source_path.name).stem
                        suffix = Path(source_path.name).suffix
                        while backend.exists(f"documents/{dest_name}"):
                            dest_name = f"{stem}_{counter}{suffix}"
                            counter += 1

                        dest_relative = f"documents/{dest_name}"
                        backend.write_file(dest_relative, source_path.read_bytes())
                        logger.info(f"Copied document to workspace: {dest_relative}")

                        copied_paths.append(dest_relative)
                        original_paths.append(str(source_path))
                else:
                    logger.warning(f"Document not found: {source_path}")

            if copied_paths:
                updated_metadata["document_paths"] = copied_paths
                updated_metadata["original_document_paths"] = original_paths
                # For backwards compatibility, set document_path to first document
                updated_metadata["document_path"] = copied_paths[0]
                updated_metadata["original_document_path"] = original_paths[0]

        # Handle single document (document_path) - backwards compatibility
        elif metadata.get("document_path"):
            source_path = Path(metadata["document_path"])
            if source_path.exists():
                # Ensure documents directory exists (use backend for remote compat)
                backend = self._workspace_manager.backend
                backend.mkdir("documents")

                # Check if zip - extract instead of copy
                if source_path.suffix.lower() == ".zip":
                    extracted = self._extract_zip(source_path, "documents", logger)
                    if extracted:
                        updated_metadata["document_paths"] = extracted
                        updated_metadata["original_document_paths"] = [
                            str(source_path)
                        ] * len(extracted)
                        updated_metadata["document_path"] = extracted[0]
                        updated_metadata["original_document_path"] = str(source_path)
                    logger.info(
                        f"Processed zip file: {source_path.name} ({len(extracted)} files extracted)"
                    )
                else:
                    # Regular file - copy via backend to documents/ folder
                    dest_relative = f"documents/{source_path.name}"
                    backend.write_file(dest_relative, source_path.read_bytes())
                    logger.info(f"Copied document to workspace: {dest_relative}")

                    # Update metadata to use workspace-relative path
                    updated_metadata["document_path"] = dest_relative
                    updated_metadata["original_document_path"] = str(source_path)
            else:
                logger.warning(f"Document not found: {source_path}")

        # Write requirement data to workspace if provided (for validator agent)
        if metadata.get("requirement_data"):
            req = metadata["requirement_data"]
            requirement_md = self._format_requirement_as_markdown(req)
            self._workspace_manager.write_file(
                "analysis/requirement_input.md", requirement_md
            )
            logger.info("Wrote requirement to analysis/requirement_input.md")

        # Create todo manager for this workspace
        self._todo_manager = TodoManager(
            workspace=self._workspace_manager,
            model_name=self.config.llm.model,
        )

        # Instruction files (todo_guide.md, instruction_files, template-based instructions.md)
        # are deployed in _deploy_instruction_files() after tools are loaded, so that
        # Jinja2 conditionals can reference which tools are actually available.

        logger.debug(f"Workspace created at {self._workspace_manager.path}")

        return updated_metadata

    async def _setup_job_tools(self) -> None:
        """Set up tools for the current job.

        Loads tools based on configuration and injects dependencies
        (workspace manager, todo manager, connections).

        Also creates datasource connections from job metadata (sent by orchestrator)
        and injects them into the ToolContext.
        """
        # Process datasources from job metadata (sent by orchestrator)
        from src.core.datasource_setup import (
            inject_datasource_index,
            process_datasources,
        )

        ds_configs = (
            self._job_metadata.get("datasources", []) if self._job_metadata else []
        )
        ws = self._workspace_manager
        workspace_dir = getattr(ws, "workspace_dir", None) or os.getcwd()

        datasources_dict, client_registry, cli_ds_types = process_datasources(
            ds_configs, workspace_dir=workspace_dir
        )
        # Track connections for cleanup
        self._datasource_connections.update(datasources_dict)
        self._datasource_clients.update(client_registry)

        if ds_configs:
            inject_datasource_index(ds_configs, ws)

        if cli_ds_types:
            self.config.extra["_cli_datasources"] = cli_ds_types

        # Create tool context with dependencies
        # Merge agent_id and LLM settings into config for tools
        tool_config = {
            **self.config.extra,
            "agent_id": self.config.agent_id,
            "multimodal": self.config.llm.multimodal,  # For vision-aware file reading
        }
        # Build job metadata for delegation tool access
        job_metadata = {
            "job_id": self._current_job_id,
            "project_id": (self._job_metadata or {}).get("project_id"),
            "priority": (self._job_metadata or {}).get("priority", 5),
            "config_name": (self._job_metadata or {}).get(
                "config_name", self.config.agent_id
            ),
            "repo_name": (self._job_metadata or {}).get("repo_name"),
        }

        context = ToolContext(
            workspace_manager=self._workspace_manager,
            todo_manager=self._todo_manager,
            postgres_db=self.postgres_conn,
            datasources=datasources_dict,
            config=tool_config,
            _job_id=self._current_job_id,
            _llm_config=self.config.llm,
            _instruction_files=self.config.instruction_files,
            orchestrator_client=self._orchestrator_client,
            _job_metadata=job_metadata,
        )
        self._tool_context = context

        # Initialize ShellManager for persistent terminal sessions
        ws_backend = self._workspace_manager.backend
        use_remote_shell = getattr(ws_backend, "supports_shell", False)

        if use_remote_shell or shutil.which("tmux"):
            try:
                from src.tools.shell.shell_manager import ShellManager

                shell_config = self.config.extra.get("shell", {})
                # sudo_action comes from config (default "freeze").
                # The orchestrator injects "allow" for VM workspaces
                # (where the sudo gate handles approval via NATS).
                sudo_action = shell_config.get("sudo_action", "freeze")

                shell_manager = ShellManager(
                    job_id=self._current_job_id,
                    max_tabs=shell_config.get("max_tabs", 15),
                    scrollback_limit=shell_config.get("scrollback_limit", 5000),
                    default_timeout=shell_config.get("default_timeout", 120),
                    blocked_commands=shell_config.get("blocked_commands"),
                    sandbox_cwd=str(self._workspace_manager.path)
                    if shell_config.get("sandbox", True)
                    else None,
                    backend=ws_backend if use_remote_shell else None,
                    sudo_action=sudo_action,
                )
                context.shell_manager = shell_manager
                self._shell_manager = shell_manager
                logger.info(f"ShellManager initialized for job {self._current_job_id}")
            except Exception as e:
                logger.warning(f"Failed to initialize ShellManager (non-fatal): {e}")
        else:
            logger.debug("tmux not found — shell tools disabled")

        # Initialize RecallStore for Memory Light (if enabled)
        if self.config.memory.enabled:
            try:
                from src.services.embedding_service import get_embedding_service
                from src.services.recall_store import RecallStore
                import uuid as _uuid

                # Resolve project_id for project-scoped memory sharing
                project_id_for_memory = None
                if self.config.memory.project_scoped:
                    raw_pid = (
                        self._job_metadata.get("project_id")
                        if self._job_metadata
                        else None
                    )
                    if raw_pid:
                        project_id_for_memory = _uuid.UUID(str(raw_pid))

                from src.core.archiver import get_archiver as _get_archiver

                embedding_service = get_embedding_service()
                recall_store = RecallStore(
                    db=self.vector_conn,
                    embedding_service=embedding_service,
                    job_id=_uuid.UUID(self._current_job_id),
                    config=self.config.memory,
                    agent_id=self.config.agent_id,
                    project_id=project_id_for_memory,
                    archiver=_get_archiver(),
                )
                context.recall_store = recall_store
                scope_msg = (
                    f"project {project_id_for_memory}"
                    if project_id_for_memory
                    else f"job {self._current_job_id}"
                )
                logger.info(f"RecallStore initialized (scope: {scope_msg})")
                # Memory extraction is now handled by AuxiliaryLLM in the graph
                # (see extract_and_store_memories in src/services/auxiliary.py)

            except Exception as e:
                logger.warning(f"Failed to initialize RecallStore (non-fatal): {e}")

        # Initialize KnowledgeGraphDB + KnowledgeStore for project knowledge base
        project_id = (
            self._job_metadata.get("project_id") if self._job_metadata else None
        )
        if project_id:
            try:
                from src.services.knowledge_graph import KnowledgeGraphDB
                from src.services.knowledge_store import KnowledgeStore
                from src.services.embedding_service import get_embedding_service

                kg = KnowledgeGraphDB()
                if kg.connect():
                    embedding_service = get_embedding_service()
                    ks = KnowledgeStore(
                        db=self.vector_conn,
                        embedding_service=embedding_service,
                    )
                    context.knowledge_graph = kg
                    context.knowledge_store = ks
                    context.project_id = str(project_id)
                    self._knowledge_graph = kg  # Track for cleanup
                    logger.info(f"Knowledge base initialized for project {project_id}")
                else:
                    logger.warning(
                        "Failed to connect to Neo4j — inline curation disabled"
                    )
            except Exception as e:
                logger.warning(f"Failed to initialize knowledge base (non-fatal): {e}")

        # Load tools from registry
        tool_names = get_all_tool_names(self.config)

        try:
            self._tools = load_tools(tool_names, context)
        except ValueError as e:
            # Some tools might not be implemented yet
            logger.warning(f"Tool loading warning: {e}")
            # Load only implemented tools
            implemented_tools = []
            for name in tool_names:
                try:
                    implemented_tools.extend(load_tools([name], context))
                except ValueError:
                    logger.debug(f"Tool not implemented: {name}")

            self._tools = implemented_tools

        # Generate tool documentation in workspace (before overrides so full docstrings are captured)
        tools_dir = self._workspace_manager.get_path("tools")

        def _write_tool_doc(rel_path: str, content: str) -> None:
            self._workspace_manager.write_file(f"tools/{rel_path}", content)

        loaded_tool_names = [t.name for t in self._tools]
        generate_workspace_tool_docs(
            loaded_tool_names, tools_dir, tools=self._tools, write_fn=_write_tool_doc
        )

        # Deploy instruction files with Jinja2 rendering (after tools loaded)
        self._deploy_instruction_files(loaded_tool_names)

        # Apply description overrides for deferred tools
        # Domain tools get short descriptions; agent reads full docs from workspace
        self._tools = apply_description_overrides(self._tools)

        # Apply instruction file enforcement wrappers (before_tool triggers)
        self._tools = apply_instruction_enforcement(self._tools, context)

        # Configure parallel tool calls from config (defaults to False to prevent
        # overwhelming the agent loop with 20+ simultaneous tool calls).
        # OpenAI o-series reasoning models don't support this parameter.
        bind_kwargs = {}
        model_name = (self.config.llm.model or "").lower()
        if not model_name.startswith(("o1", "o3", "o4")):
            bind_kwargs["parallel_tool_calls"] = self.config.llm.parallel_tool_calls

        # Phase-filter tools: each LLM only sees tools declared for its phase.
        # The ToolNode keeps the full list (LLM schema binding is primary enforcement).
        from .tools.registry import filter_tools_by_phase

        strategic_names = set(
            filter_tools_by_phase([t.name for t in self._tools], "strategic")
        )
        tactical_names = set(
            filter_tools_by_phase([t.name for t in self._tools], "tactical")
        )
        strategic_tools = [t for t in self._tools if t.name in strategic_names]
        tactical_tools = [t for t in self._tools if t.name in tactical_names]

        # Inject family-specific Examples blocks into tool descriptions before
        # binding. This is where the model first sees the tool catalog and
        # decides on a wire format — see docs/design/guardrails_matrix.md.
        from src.services.guardrails import apply_guardrails_to_tools

        strategic_tools = apply_guardrails_to_tools(
            strategic_tools, model=self.config.llm.model
        )
        tactical_tools = apply_guardrails_to_tools(
            tactical_tools, model=self.config.llm.model
        )

        self._strategic_llm_with_tools = self._strategic_llm.bind_tools(
            strategic_tools, **bind_kwargs
        )
        self._tactical_llm_with_tools = self._tactical_llm.bind_tools(
            tactical_tools, **bind_kwargs
        )

        # Keep _llm_with_tools for backwards compatibility
        self._llm_with_tools = self._strategic_llm_with_tools

        logger.info(
            f"Loaded {len(self._tools)} tools "
            f"(strategic: {len(strategic_tools)}, tactical: {len(tactical_tools)})"
        )

        # Auto-register input documents as CitationEngine sources (background)
        self._doc_registration_task = asyncio.create_task(
            self._register_initial_documents_background(context)
        )

    def _deploy_instruction_files(self, loaded_tool_names: List[str]) -> None:
        """Deploy instruction files to workspace with Jinja2 rendering.

        Called after tools are loaded so that template conditionals like
        ``{% if has_tool("kb_write") %}`` resolve correctly.

        Deploys:
        - instructions.md (from template, only if not already written from upload/inline)
        - todo_guide.md (via instruction matrix)
        - Additional instruction_files from config
        """
        from .core.loader import (
            InstructionMatrixResolver,
            FileResolver,
            render_instruction_content,
            load_instructions,
        )
        from .core.model_registry import family_of

        # instructions.md — only deploy template if not already present (upload/inline)
        instructions_path = self._workspace_manager.get_path("instructions.md")
        if not instructions_path.exists():
            instructions = load_instructions(self.config, model=self.config.llm.model)
            instructions = render_instruction_content(instructions, loaded_tool_names)
            self._workspace_manager.write_file("instructions.md", instructions)
            logger.debug("Deployed template-based instructions.md to workspace")

        # todo_guide.md — via instruction matrix
        model_family = family_of(self.config.llm.model)
        instr_resolver = InstructionMatrixResolver(
            self.config._deployment_dir, model_family
        )
        try:
            resolved = self.config.extra.get("_resolved_instructions", {})
            todo_guide = resolved.get("todo_guide") or instr_resolver.load("todo_guide")
            todo_guide = render_instruction_content(todo_guide, loaded_tool_names)
            self._workspace_manager.write_file("todo_guide.md", todo_guide)
            logger.debug("Deployed todo_guide.md to workspace")
        except FileNotFoundError:
            logger.warning("todo_guide.md not found via instruction matrix")

        # Additional instruction files (config-driven)
        if self.config.instruction_files:
            templates_dir = get_project_root() / "config" / "templates"
            file_resolver = FileResolver(
                deployment_dir=self.config._deployment_dir,
                framework_dir=templates_dir,
            )
            resolved_instructions = self.config.extra.get("_resolved_instructions", {})
            for entry in self.config.instruction_files:
                try:
                    # Skip todo_guide.md — already handled above via matrix
                    if entry.file == "todo_guide.md":
                        continue
                    # Check resolved config first (resumed jobs)
                    basename = Path(entry.file).stem
                    content = resolved_instructions.get(basename)
                    if not content:
                        content = file_resolver.load(Path(entry.file).name)
                    content = render_instruction_content(content, loaded_tool_names)
                    # Ensure parent directory exists (use backend, not local mkdir)
                    parent_dir = str(Path(entry.file).parent)
                    if parent_dir and parent_dir != ".":
                        self._workspace_manager.backend.mkdir(parent_dir)
                    self._workspace_manager.write_file(entry.file, content)
                    logger.debug(
                        f"Deployed instruction file to workspace: {entry.file}"
                    )
                except FileNotFoundError:
                    logger.warning(f"Instruction file not found: {entry.file}")

    async def _register_initial_documents_background(
        self, context: "ToolContext"
    ) -> None:
        """Background async wrapper for parallel document registration.

        Runs the synchronous _register_initial_documents in a thread executor
        so the agent's ReAct loop can start immediately.

        Args:
            context: ToolContext with workspace and citation engine
        """
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._register_initial_documents, context)

    def _register_initial_documents(self, context: "ToolContext") -> None:
        """Register input documents in documents/ as CitationEngine sources.

        Scans the documents/ directory for supported file types and registers
        each as a source in parallel using ThreadPoolExecutor, enabling hybrid
        vector search via search_library.
        Skips documents/external/ (web content registered separately).

        Each worker thread creates its own CitationEngine instance for thread
        safety (the shared context.citation_engine uses a single DB connection).

        Non-fatal: failures are logged but do not block job execution.

        Args:
            context: ToolContext with workspace and citation engine
        """
        if not context.has_workspace():
            return

        SUPPORTED_EXTENSIONS = {
            ".pdf",
            ".txt",
            ".md",
            ".docx",
            ".doc",
            ".pptx",
            ".html",
            ".htm",
            ".csv",
            ".json",
            ".xml",
            ".rtf",
        }

        try:
            docs_path = context.workspace_manager.get_path("documents")
            if not docs_path.exists():
                return

            # Collect eligible files
            files: List[Tuple[Path, str]] = []
            for file_path in sorted(docs_path.rglob("*")):
                if not file_path.is_file():
                    continue

                # Skip documents/external/ (web content, registered by research tools)
                try:
                    file_path.relative_to(docs_path / "external")
                    continue
                except ValueError:
                    pass  # Not under external/, proceed

                # Filter to supported extensions
                if file_path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                    continue

                files.append((file_path, file_path.name))

            if not files:
                return

            start_time = time.monotonic()
            logger.info(
                f"Starting background registration of {len(files)} document(s)..."
            )

            # Process in parallel — each thread gets its own CitationEngine
            max_workers = min(len(files), 4)
            results: List[Optional[Tuple[str, int]]] = []

            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = [
                    executor.submit(
                        self._process_single_document,
                        file_path,
                        name,
                        context,
                    )
                    for file_path, name in files
                ]
                for future in futures:
                    results.append(future.result())

            # Update source registry from results (single-threaded, no race)
            registered_count = 0
            for result in results:
                if result is not None:
                    file_path_str, source_id = result
                    context._source_registry[file_path_str] = source_id
                    registered_count += 1

            elapsed = time.monotonic() - start_time
            if registered_count > 0:
                logger.info(
                    f"Registered {registered_count} document(s) in {elapsed:.1f}s (parallel)"
                )

        except Exception as e:
            logger.warning(
                f"Auto-registration of input documents failed (non-fatal): {e}"
            )

    def _process_single_document(
        self,
        file_path: Path,
        name: str,
        context: "ToolContext",
    ) -> Optional[Tuple[str, int]]:
        """Process a single document in a worker thread.

        Creates an independent CitationEngine instance with its own DB
        connection for thread safety.

        Args:
            file_path: Absolute path to the document file
            name: Human-readable name for the source
            context: ToolContext (used only for job_id and agent_id)

        Returns:
            Tuple of (file_path_str, source_id) on success, None on failure
        """
        engine = None
        try:
            from citation_engine import CitationEngine, CitationContext

            ctx = CitationContext(
                session_id=context.job_id or "unknown",
                agent_id=context.config.get("agent_id", "unknown"),
            )
            engine = CitationEngine(mode="multi-agent", context=ctx)
            engine._connect()

            source = engine.add_doc_source(str(file_path), name=name)
            return (str(file_path), source.id)

        except Exception as e:
            logger.debug(f"Could not register document {name}: {e}")
            return None
        finally:
            if engine is not None:
                try:
                    engine.close()
                except Exception:
                    pass

    def _inject_datasource_index(self, ds_configs: list) -> None:
        """Inject a compact datasource index into workspace.md.

        This ensures the agent always knows what datasources are available,
        even before KB retrieval fires. Full details are in the knowledge base.
        """
        lines = ["\n\n## Available Datasources\n"]
        for ds in ds_configs:
            ds_type = ds.get("type", "unknown")
            name = ds.get("name", "Unnamed")
            is_ro = ds.get("project_read_only", False)

            if ds_type == "generic":
                cli = ds.get("cli_hint", "CLI via env vars")
                lines.append(f"- **{name}** (generic) — {cli}")
            elif ds_type == "repository":
                import re

                slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
                lines.append(f"- **{name}** (repository) — cloned at `./repos/{slug}/`")
            elif ds_type == "webdav":
                access = "read-only tools" if is_ro else "read-write tools"
                lines.append(f"- **{name}** (webdav, {access})")
            elif ds_type in ("postgresql", "neo4j", "mongodb"):
                if is_ro:
                    lines.append(f"- **{name}** ({ds_type}, read-only) — query tools")
                else:
                    lines.append(self._format_rw_cli_block(name, ds_type))
            else:
                lines.append(f"- **{name}** ({ds_type})")

        try:
            existing = self._workspace_manager.read_file("workspace.md")
            self._workspace_manager.write_file(
                "workspace.md", existing + "\n".join(lines)
            )
            logger.info(
                f"Injected datasource index ({len(ds_configs)} entries) into workspace.md"
            )
        except Exception as e:
            logger.warning(f"Failed to inject datasource index: {e}")

    @staticmethod
    def _format_rw_cli_block(name: str, ds_type: str) -> str:
        """Format an expanded CLI usage block for a read-write managed datasource."""
        blocks = {
            "postgresql": (
                f"- **{name}** (postgresql, read-write):\n"
                f"  Use `run_command` with `psql`. Credentials are pre-configured — do NOT pass connection flags.\n"
                f"  ```\n"
                f"  psql -c \"SELECT table_name FROM information_schema.tables WHERE table_schema='public'\"\n"
                f'  psql -c "\\dt"\n'
                f"  ```"
            ),
            "neo4j": (
                f"- **{name}** (neo4j, read-write):\n"
                f"  Use `run_command` with `cypher-shell`. Credentials are pre-configured — do NOT pass connection flags.\n"
                f"  ```\n"
                f'  cypher-shell --format plain "MATCH (n) RETURN labels(n), count(*)"\n'
                f"  cypher-shell --format plain \"CREATE (n:Note {{text: 'hello'}}) RETURN n\"\n"
                f"  ```"
            ),
            "mongodb": (
                f"- **{name}** (mongodb, read-write):\n"
                f"  Use `run_command` with `mongosh`. Credentials are pre-configured — do NOT pass connection flags.\n"
                f"  ```\n"
                f'  mongosh --quiet --eval "db.getCollectionNames()"\n'
                f'  mongosh --quiet --eval "db.users.find().limit(5)"\n'
                f"  ```"
            ),
        }
        return blocks.get(
            ds_type,
            f"- **{name}** ({ds_type}, read-write) — CLI via env vars",
        )

    def _setup_repository_datasource(self, ds: Dict[str, Any]) -> None:
        """Clone a repository into the workspace and configure git credentials.

        The agent never sees raw tokens/SSH keys — credentials are
        configured transparently via git credential helpers or SSH config.
        """
        import re
        import subprocess

        repo_url = ds.get("connection_url", "")
        creds = ds.get("credentials") or {}
        name = re.sub(r"[^a-z0-9]+", "-", ds.get("name", "repo").lower()).strip("-")
        branch = ds.get("default_branch")

        # Determine workspace path
        ws = self._workspace_manager
        workspace_dir = getattr(ws, "workspace_dir", None) or os.getcwd()
        repos_dir = os.path.join(workspace_dir, "repos")
        os.makedirs(repos_dir, exist_ok=True)
        clone_path = os.path.join(repos_dir, name)

        if os.path.exists(clone_path):
            logger.info(f"Repository already exists at {clone_path}, skipping clone")
            return

        auth_method = creds.get("auth_method", "token")

        if auth_method == "ssh":
            # Write SSH key and configure
            ssh_dir = os.path.expanduser("~/.ssh")
            os.makedirs(ssh_dir, mode=0o700, exist_ok=True)
            key_file = os.path.join(ssh_dir, f"repo_{name}")
            with open(key_file, "w") as f:
                f.write(creds.get("ssh_key", ""))
            os.chmod(key_file, 0o600)

            # Parse host from SSH URL
            from urllib.parse import urlparse

            parsed = urlparse(repo_url)
            host = parsed.hostname or "github.com"

            config_path = os.path.join(ssh_dir, "config")
            with open(config_path, "a") as f:
                f.write(
                    f"\nHost {host}\n  IdentityFile {key_file}\n  StrictHostKeyChecking accept-new\n"
                )

        elif auth_method == "token" and creds.get("token"):
            # Configure git credential helper
            cred_file = os.path.expanduser("~/.git-credentials")
            from urllib.parse import urlparse

            parsed = urlparse(repo_url)
            host = parsed.hostname or "github.com"
            scheme = parsed.scheme or "https"
            cred_line = f"{scheme}://oauth2:{creds['token']}@{host}"
            with open(cred_file, "a") as f:
                f.write(cred_line + "\n")
            os.chmod(cred_file, 0o600)
            subprocess.run(
                ["git", "config", "--global", "credential.helper", "store"],
                check=False,
                capture_output=True,
            )

        # Clone
        cmd = ["git", "clone", repo_url, clone_path]
        if branch:
            cmd.extend(["--branch", branch])
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            logger.warning(f"Git clone failed: {result.stderr}")
            raise RuntimeError(f"Failed to clone repository: {result.stderr}")
        logger.info(f"Cloned repository to {clone_path}")

    def _inject_typed_env_vars(self, ds_type: str, ds: Dict[str, Any]) -> None:
        """Inject well-known environment variables for managed connector CLI access."""
        url = ds.get("connection_url", "")
        creds = ds.get("credentials") or {}

        if ds_type == "postgresql":
            # Parse connection URL into PG* env vars
            from urllib.parse import urlparse

            parsed = urlparse(url)
            if parsed.hostname:
                os.environ["PGHOST"] = parsed.hostname
            if parsed.port:
                os.environ["PGPORT"] = str(parsed.port)
            if parsed.username:
                os.environ["PGUSER"] = parsed.username
            password = parsed.password or creds.get("password", "")
            if password:
                os.environ["PGPASSWORD"] = password
            db_name = parsed.path.lstrip("/").split("?")[0]
            if db_name:
                os.environ["PGDATABASE"] = db_name

        elif ds_type == "neo4j":
            os.environ["NEO4J_URI"] = url
            os.environ["NEO4J_USERNAME"] = creds.get("username", "neo4j")
            os.environ["NEO4J_PASSWORD"] = creds.get("password", "")

        elif ds_type == "mongodb":
            os.environ["MONGOSH_URI"] = url

    def _create_datasource_connection(self, ds: Dict[str, Any]) -> Any:
        """Create a connection to an external datasource.

        Args:
            ds: Datasource config dict with type, connection_url, credentials, etc.

        Returns:
            Connection object (e.g. Neo4jDB instance)

        Raises:
            NotImplementedError: If datasource type is not yet supported
            ValueError: If datasource type is unknown
        """
        ds_type = ds["type"]
        url = ds.get("connection_url") or ""
        creds = ds.get("credentials") or {}

        if ds_type == "neo4j":
            from src.database.neo4j_db import Neo4jDB

            db = Neo4jDB(
                uri=url,
                username=creds.get("username", "neo4j"),
                password=creds.get("password", ""),
            )
            db.connect()
            return db

        elif ds_type == "postgresql":
            import psycopg

            conn = psycopg.connect(url, autocommit=False)
            # Test connection
            conn.execute("SELECT 1")
            conn.rollback()  # Clean transaction state after test
            return conn

        elif ds_type == "mongodb":
            from pymongo import MongoClient
            from urllib.parse import urlparse

            client = MongoClient(url, serverSelectionTimeoutMS=5000)
            client.admin.command("ping")
            # Extract database name from URL path
            parsed = urlparse(url)
            db_name = parsed.path.lstrip("/").split("?")[0] or "default"
            db = client[db_name]
            # Store client for cleanup (db object doesn't have close())
            self._datasource_clients[ds_type] = client
            return db

        elif ds_type == "webdav":
            from webdav3.client import Client

            client = Client(
                {
                    "webdav_hostname": url,
                    "webdav_login": creds.get("username"),
                    "webdav_password": creds.get("password"),
                }
            )
            client.list("/")  # Connection test
            return client

        else:
            raise ValueError(f"Unknown datasource type: {ds_type}")

    def _close_datasource_connections(self) -> None:
        """Close all datasource connections opened for the current job."""
        # Close knowledge graph connection (inline curation)
        if self._knowledge_graph:
            try:
                self._knowledge_graph.close()
                logger.debug("Closed knowledge graph connection")
            except Exception as e:
                logger.warning(f"Error closing knowledge graph: {e}")
            self._knowledge_graph = None

        for ds_type, conn in self._datasource_connections.items():
            try:
                if hasattr(conn, "close"):
                    conn.close()
                    logger.debug(f"Closed {ds_type} datasource connection")
            except Exception as e:
                logger.warning(f"Error closing {ds_type} datasource: {e}")
        self._datasource_connections = {}
        # Close parent clients (e.g. MongoClient) that aren't in _datasource_connections
        for ds_type, client in self._datasource_clients.items():
            try:
                if hasattr(client, "close"):
                    client.close()
                    logger.debug(f"Closed {ds_type} datasource client")
            except Exception as e:
                logger.warning(f"Error closing {ds_type} datasource client: {e}")
        self._datasource_clients = {}

    async def _resume_from_checkpoint(
        self,
        job_id: str,
        thread_id: str,
        thread_config: Dict[str, Any],
        original_config_name: Optional[str],
        updated_metadata: Dict[str, Any],
    ) -> tuple:
        """Resume using existing checkpoint.db directly (no snapshot recovery).

        Used for graceful stops where the checkpoint has valid in-progress state.

        Returns:
            (graph_input, thread_id, thread_config) — graph_input is None on success
            (meaning resume from checkpoint), or an initial state dict if no checkpoint found.
        """
        logger = logging.getLogger(__name__)
        from .core.phase_snapshot import discover_thread_id_from_checkpoint

        # Try to discover the correct thread_id from checkpoint DB
        checkpoint_path = self._get_checkpoint_path(job_id)
        discovered_thread_id = discover_thread_id_from_checkpoint(
            checkpoint_path, job_id
        )
        if discovered_thread_id:
            logger.info(f"Discovered thread_id from checkpoint: {discovered_thread_id}")
            thread_id = discovered_thread_id
            thread_config = {
                "configurable": {"thread_id": thread_id},
                "recursion_limit": 1000000,
            }
            checkpoint_state = await self._graph.aget_state(thread_config)
            if checkpoint_state and checkpoint_state.values:
                logger.info(f"Found checkpoint with thread_id: {thread_id}")
                return None, thread_id, thread_config

        # Fallback: try job_id as thread_id (new format)
        checkpoint_state = await self._graph.aget_state(thread_config)
        if checkpoint_state and checkpoint_state.values:
            logger.debug(f"Found checkpoint with thread_id: {thread_id}")
            return None, thread_id, thread_config

        # Fallback: try legacy format
        legacy_config_name = original_config_name or self.config.agent_id
        legacy_thread_id = f"{legacy_config_name}_{job_id}"
        legacy_config = {
            "configurable": {"thread_id": legacy_thread_id},
            "recursion_limit": 1000000,
        }
        legacy_state = await self._graph.aget_state(legacy_config)
        if legacy_state and legacy_state.values:
            logger.info(f"Using legacy thread_id format: {legacy_thread_id}")
            return None, legacy_thread_id, legacy_config

        # No checkpoint found — return initial state so caller can fall back
        logger.warning("No checkpoint found with any thread_id format")
        graph_input = create_initial_state(
            job_id=job_id,
            workspace_path=str(self._workspace_manager.path),
            metadata=updated_metadata,
        )
        return graph_input, thread_id, thread_config

    async def _resume_from_snapshot(
        self,
        job_id: str,
        snapshot_manager,
        thread_id: str,
        thread_config: Dict[str, Any],
        original_config_name: Optional[str],
        updated_metadata: Dict[str, Any],
    ) -> tuple:
        """Resume using phase snapshot recovery (overwrites checkpoint.db).

        Used for crash/failure recovery where the checkpoint may be corrupted.

        Returns:
            (graph_input, thread_id, thread_config) — graph_input is None on success
            (meaning resume from checkpoint), or an initial state dict if recovery fails.
        """
        logger = logging.getLogger(__name__)

        latest_snapshot = snapshot_manager.get_latest_snapshot()
        if not latest_snapshot:
            logger.warning(f"No phase snapshots found for job {job_id}, starting fresh")
            graph_input = create_initial_state(
                job_id=job_id,
                workspace_path=str(self._workspace_manager.path),
                metadata=updated_metadata,
            )
            return graph_input, thread_id, thread_config

        logger.info(
            f"Resuming from phase {latest_snapshot.phase_number} snapshot "
            f"(iteration={latest_snapshot.iteration})"
        )

        if not snapshot_manager.recover_to_phase(latest_snapshot.phase_number):
            logger.warning(
                f"Failed to recover from phase {latest_snapshot.phase_number} snapshot, starting fresh"
            )
            graph_input = create_initial_state(
                job_id=job_id,
                workspace_path=str(self._workspace_manager.path),
                metadata=updated_metadata,
            )
            return graph_input, thread_id, thread_config

        # Delete any stale snapshots from failed runs after this phase
        deleted = snapshot_manager.delete_snapshots_after(latest_snapshot.phase_number)
        if deleted:
            logger.info(
                f"Deleted {deleted} stale snapshot(s) after phase {latest_snapshot.phase_number}"
            )

        # Determine the correct thread_id for checkpoint lookup
        # Priority: 1) snapshot.thread_id, 2) discover from checkpoint DB, 3) try known formats
        discovered_thread_id = None

        if latest_snapshot.thread_id:
            discovered_thread_id = latest_snapshot.thread_id
            logger.info(f"Using thread_id from snapshot: {discovered_thread_id}")
        else:
            from .core.phase_snapshot import discover_thread_id_from_checkpoint

            checkpoint_path = self._get_checkpoint_path(job_id)
            discovered_thread_id = discover_thread_id_from_checkpoint(
                checkpoint_path, job_id
            )
            if discovered_thread_id:
                logger.info(
                    f"Discovered thread_id from checkpoint: {discovered_thread_id}"
                )

        if discovered_thread_id:
            thread_id = discovered_thread_id
            thread_config = {
                "configurable": {"thread_id": thread_id},
                "recursion_limit": 1000000,
            }
            checkpoint_state = await self._graph.aget_state(thread_config)
            if checkpoint_state and checkpoint_state.values:
                logger.info(f"Found checkpoint with thread_id: {thread_id}")
                return None, thread_id, thread_config
            else:
                logger.warning(
                    f"Discovered thread_id {thread_id} has no checkpoint data, starting fresh"
                )
                graph_input = create_initial_state(
                    job_id=job_id,
                    workspace_path=str(self._workspace_manager.path),
                    metadata=updated_metadata,
                )
                return graph_input, thread_id, thread_config
        else:
            # Fallback: try new format then legacy format
            checkpoint_state = await self._graph.aget_state(thread_config)
            if checkpoint_state and checkpoint_state.values:
                logger.debug(f"Found checkpoint with new thread_id format: {job_id}")
                return None, thread_id, thread_config
            else:
                legacy_config_name = original_config_name or self.config.agent_id
                legacy_thread_id = f"{legacy_config_name}_{job_id}"
                legacy_config = {
                    "configurable": {"thread_id": legacy_thread_id},
                    "recursion_limit": 1000000,
                }
                legacy_state = await self._graph.aget_state(legacy_config)
                if legacy_state and legacy_state.values:
                    logger.info(f"Using legacy thread_id format: {legacy_thread_id}")
                    return None, legacy_thread_id, legacy_config
                else:
                    logger.warning(
                        "No checkpoint found with any thread_id format, starting fresh"
                    )
                    graph_input = create_initial_state(
                        job_id=job_id,
                        workspace_path=str(self._workspace_manager.path),
                        metadata=updated_metadata,
                    )
                    return graph_input, thread_id, thread_config

    def _get_checkpoint_path(self, job_id: str) -> Path:
        """Get SQLite checkpoint file path for a job.

        Args:
            job_id: Unique job identifier

        Returns:
            Path to SQLite checkpoint file (e.g., workspace/checkpoints/job_<id>.db)
        """
        return get_checkpoints_path() / f"job_{job_id}.db"

    def _extract_job_metadata(self, job: Dict[str, Any]) -> Dict[str, Any]:
        """Extract metadata from a job record for processing.

        Handles both:
        - Jobs table rows (for Creator): extracts document_path, prompt, etc.
        - Requirements table rows (for Validator): wraps as requirement_data
        """
        metadata = {}

        # Check if this is a requirements table row (has 'text' field but no 'prompt')
        # Requirements have: id, text, name, type, priority, gobd_relevant, etc.
        if "text" in job and "prompt" not in job:
            # This is a requirement row from polling - wrap it as requirement_data
            metadata["requirement_data"] = job
            logger.debug(
                f"Extracted requirement data: {job.get('name', job.get('id', 'unknown'))}"
            )
            return metadata

        # Otherwise, handle as jobs table row
        metadata_fields = [
            "document_path",
            "prompt",
            "requirement_id",
            "requirement_data",
            "source_document",
            "config",
            "options",
        ]

        for field in metadata_fields:
            if field in job:
                metadata[field] = job[field]

        # Include job-specific data if present
        if "data" in job and isinstance(job["data"], dict):
            metadata.update(job["data"])

        return metadata

    def _format_requirement_as_markdown(self, req: Dict[str, Any]) -> str:
        """Format requirement data as markdown for the workspace.

        Creates a structured markdown document from requirement data
        for the validator agent to read from analysis/requirement_input.md.

        Args:
            req: Requirement dictionary from PostgreSQL

        Returns:
            Formatted markdown string
        """
        import json

        lines = [
            "# Requirement Input",
            "",
            f"**ID:** `{req.get('id', 'N/A')}`",
            f"**Name:** {req.get('name', 'Unnamed')}",
            "",
            "## Text",
            "",
            req.get("text", "(No text provided)"),
            "",
            "## Metadata",
            "",
            f"- **Type:** {req.get('type', 'N/A')}",
            f"- **Priority:** {req.get('priority', 'N/A')}",
            f"- **GoBD Relevant:** {req.get('gobd_relevant', False)}",
            f"- **GDPR Relevant:** {req.get('gdpr_relevant', False)}",
            f"- **Confidence:** {req.get('confidence', 'N/A')}",
            "",
            "## Source",
            "",
            f"- **Document:** {req.get('source_document', 'N/A')}",
        ]

        # Handle source_location which may be JSON string or dict
        source_location = req.get("source_location")
        if source_location:
            if isinstance(source_location, str):
                try:
                    source_location = json.loads(source_location)
                except (json.JSONDecodeError, TypeError):
                    pass
            lines.append(f"- **Location:** {source_location}")
        else:
            lines.append("- **Location:** N/A")

        lines.append("")

        if req.get("reasoning"):
            lines.extend(
                [
                    "## Extraction Reasoning",
                    "",
                    req["reasoning"],
                    "",
                ]
            )

        if req.get("research_notes"):
            lines.extend(
                [
                    "## Research Notes",
                    "",
                    req["research_notes"],
                    "",
                ]
            )

        return "\n".join(lines)

    def _extract_zip(
        self,
        zip_path: Path,
        dest_dir_relative: str,
        job_logger: logging.Logger,
    ) -> List[str]:
        """Extract zip file contents preserving directory structure.

        Uses the workspace backend for writes so this works with both
        local and remote (SSH/SFTP) backends.  Skips hidden files and
        macOS __MACOSX folders.

        Args:
            zip_path: Path to the zip file (local to the agent container)
            dest_dir_relative: Destination directory relative to workspace
                root (e.g. "documents")
            job_logger: Logger instance

        Returns:
            List of relative paths to extracted files (e.g., ["documents/subdir/file.pdf"])
        """
        extracted_paths = []
        backend = self._workspace_manager.backend

        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                for zip_info in zf.infolist():
                    # Skip directories (created implicitly)
                    if zip_info.is_dir():
                        continue

                    # Get relative path within zip
                    relative_path = Path(zip_info.filename)

                    # Skip hidden files and macOS metadata
                    if any(part.startswith(".") for part in relative_path.parts):
                        continue
                    if "__MACOSX" in zip_info.filename:
                        continue

                    # Skip empty filenames
                    if not relative_path.name:
                        continue

                    # Build workspace-relative path (e.g. "documents/subdir/file.pdf")
                    ws_relative = f"{dest_dir_relative}/{relative_path}"

                    # Extract file content and write via backend
                    with zf.open(zip_info) as source:
                        backend.write_file(ws_relative, source.read())

                    extracted_paths.append(ws_relative)
                    job_logger.debug(f"Extracted: {zip_info.filename} -> {ws_relative}")

            job_logger.info(
                f"Extracted {len(extracted_paths)} files from {zip_path.name}"
            )

        except zipfile.BadZipFile as e:
            job_logger.error(f"Invalid zip file {zip_path.name}: {e}")
        except Exception as e:
            job_logger.error(f"Failed to extract zip {zip_path.name}: {e}")

        return extracted_paths

    async def _download_upload_files(
        self,
        upload_id: str,
        dest_dir: Path,
        job_logger: logging.Logger,
    ) -> Optional[List[str]]:
        """Download files from orchestrator upload via HTTP.

        Attempts to download files from the orchestrator API. If the orchestrator
        is not configured or the download fails, returns None to signal that the
        caller should fall back to local filesystem access.

        Args:
            upload_id: Upload identifier
            dest_dir: Destination directory for downloaded files
            job_logger: Logger instance

        Returns:
            List of downloaded filenames, or None if HTTP download failed/unavailable
        """
        # Use same default as orchestrator_client.py
        orchestrator_url = os.getenv("ORCHESTRATOR_URL", "http://localhost:8085")

        # Import here to avoid circular imports
        from .api.orchestrator_client import OrchestratorClient

        # Create a temporary client for downloads (no registration needed)
        client = OrchestratorClient(
            orchestrator_url=orchestrator_url,
            pod_ip="",  # Not needed for downloads
            pod_port=0,
            hostname="",
            config_name="",
        )

        try:
            await client.connect()

            # Get upload info
            upload_info = await client.get_upload_info(upload_id)
            if not upload_info:
                job_logger.info(
                    f"Upload {upload_id} not found on orchestrator, will try local"
                )
                return None

            # Ensure destination directory exists
            dest_dir.mkdir(parents=True, exist_ok=True)

            downloaded_files = []
            for file_info in upload_info.files:
                # Skip metadata.json
                if file_info.name == "metadata.json":
                    continue

                content = await client.download_file(upload_id, file_info.name)
                if content is None:
                    job_logger.warning(
                        f"Failed to download {file_info.name} from {upload_id}, will try local"
                    )
                    return None

                # Save file
                dest_path = dest_dir / file_info.name
                dest_path.write_bytes(content)
                downloaded_files.append(file_info.name)
                job_logger.debug(
                    f"Downloaded via HTTP: {upload_id}/{file_info.name} ({len(content)} bytes)"
                )

            job_logger.info(
                f"Downloaded {len(downloaded_files)} files from orchestrator for upload {upload_id}"
            )
            return downloaded_files

        except Exception as e:
            job_logger.warning(
                f"HTTP download failed for {upload_id}: {e}, will try local"
            )
            return None
        finally:
            await client.close()

    async def approve_frozen_job(self, job_id: str) -> Dict[str, Any]:
        """Approve a frozen job, marking it as truly completed.

        This method is called when a human operator reviews a frozen job
        and decides it is ready to be marked as completed.

        Delegates to the orchestrator's approve endpoint, which handles
        all DB writes (status, completed_at, freeze_data) and workspace
        file management (job_frozen.json → job_completion.json).

        Args:
            job_id: The job ID to approve

        Returns:
            Dict with approval result

        Raises:
            ValueError: If approval fails
        """
        import os
        from .api.orchestrator_client import OrchestratorClient

        orchestrator_url = os.getenv("ORCHESTRATOR_URL", "http://localhost:8085")
        client = OrchestratorClient(
            orchestrator_url=orchestrator_url,
            pod_ip="",
            pod_port=0,
            hostname="",
            config_name="",
        )

        try:
            await client.connect()
            success = await client.approve_job(job_id)
            if not success:
                raise ValueError(f"Orchestrator failed to approve job {job_id}")

            return {
                "job_id": job_id,
                "status": "approved",
            }
        finally:
            await client.close()

    async def shutdown(self) -> None:
        """Shutdown the agent and cleanup resources."""
        logger.info(f"Shutting down {self.config.display_name}...")
        self._shutdown_requested = True

        # Close database connections
        if (
            hasattr(self, "vector_conn")
            and self.vector_conn
            and self.vector_conn is not self.postgres_conn
        ):
            try:
                await self.vector_conn.close()
            except Exception as e:
                logger.warning(f"Error closing Vector DB: {e}")

        if self.postgres_conn:
            try:
                # PostgresDB uses close() not disconnect()
                await self.postgres_conn.close()
            except Exception as e:
                logger.warning(f"Error closing PostgreSQL: {e}")

        self._initialized = False
        logger.info(f"{self.config.display_name} shutdown complete")

    def get_status(self) -> Dict[str, Any]:
        """Get current agent status and metrics."""
        uptime = (datetime.utcnow() - self._start_time).total_seconds()

        return {
            "agent_id": self.config.agent_id,
            "display_name": self.config.display_name,
            "initialized": self._initialized,
            "shutdown_requested": self._shutdown_requested,
            "current_job": self._current_job_id,
            "jobs_processed": self._jobs_processed,
            "uptime_seconds": uptime,
            "connections": {
                "postgres": self.postgres_conn is not None,
            },
            "config": {
                "model": self.config.llm.model,
            },
        }
