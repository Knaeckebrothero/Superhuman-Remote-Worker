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

import logging
import os
import shutil
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional

import aiosqlite
from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from .core.workspace import WorkspaceManager, WorkspaceManagerConfig, get_checkpoints_path
from .core.phase_snapshot import PhaseSnapshotManager
from .core.loader import get_project_root


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


from .managers import TodoManager
from .tools import ToolContext, load_tools

from .core.state import UniversalAgentState, create_initial_state
from .core.loader import (
    AgentConfig,
    load_agent_config,
    create_llm,
    load_instructions,
    get_all_tool_names,
    resolve_config_path,
)
from .tools.description_manager import generate_workspace_tool_docs, apply_description_overrides
from .graph import build_phase_alternation_graph, run_graph_with_streaming


logger = logging.getLogger(__name__)


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
        neo4j_conn: Optional[Any] = None,
    ):
        """
        Initialize the Universal Agent.

        Args:
            config: Agent configuration (from JSON file)
            postgres_conn: PostgreSQL connection (optional, created if needed)
            neo4j_conn: Neo4j connection (optional, created if needed)
        """
        self.config = config
        self.postgres_conn = postgres_conn
        self.neo4j_conn = neo4j_conn

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

        # Current job state
        self._workspace_manager: Optional[WorkspaceManager] = None
        self._todo_manager: Optional[TodoManager] = None
        self._current_job_id: Optional[str] = None

        # Control flags
        self._initialized = False
        self._shutdown_requested = False

        # Metrics
        self._jobs_processed = 0
        self._start_time = datetime.utcnow()

        logger.info(
            f"Created {config.display_name} (agent_id={config.agent_id})"
        )

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
        neo4j_conn: Optional[Any] = None,
    ) -> "UniversalAgent":
        """
        Create an agent from a configuration file.

        Args:
            config_path: Path to config file or config name (e.g., "creator")
            postgres_conn: Optional PostgreSQL connection
            neo4j_conn: Optional Neo4j connection

        Returns:
            UniversalAgent instance
        """
        resolved_path, deployment_dir = resolve_config_path(config_path)
        config = load_agent_config(resolved_path, deployment_dir)
        return cls(config, postgres_conn, neo4j_conn)

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

            # Optimization: reuse LLM if same config
            if tactical_config.model == strategic_config.model:
                self._tactical_llm = self._strategic_llm
                logger.info(f"Tactical LLM: reusing strategic ({tactical_config.model})")
            else:
                self._tactical_llm = create_llm(tactical_config, limits=limits)
                logger.info(f"Created tactical LLM: {tactical_config.model}")

            if summarization_config.model == strategic_config.model:
                self._summarization_llm = self._strategic_llm
                logger.info(f"Summarization LLM: reusing strategic ({summarization_config.model})")
            elif summarization_config.model == tactical_config.model:
                self._summarization_llm = self._tactical_llm
                logger.info(f"Summarization LLM: reusing tactical ({summarization_config.model})")
            else:
                self._summarization_llm = create_llm(summarization_config, limits=limits)
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

    async def _setup_connections(self) -> None:
        """Set up required database connections using new DB classes.

        Uses PostgresDB and Neo4jDB from Phase 1 refactoring.
        Falls back to environment variables for configuration.
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

        # Neo4j connection (optional, based on config)
        if self.neo4j_conn is None and self.config.connections.neo4j:
            from src.database.neo4j_db import Neo4jDB

            neo4j_uri = os.getenv("NEO4J_URI")
            neo4j_username = os.getenv("NEO4J_USERNAME", "neo4j")
            neo4j_password = os.getenv("NEO4J_PASSWORD")

            if neo4j_uri and neo4j_password:
                self.neo4j_conn = Neo4jDB(
                    uri=neo4j_uri,
                    username=neo4j_username,
                    password=neo4j_password,
                )
                # Neo4jDB.connect() is sync and returns bool
                if self.neo4j_conn.connect():
                    logger.info("Neo4j connection established (Neo4jDB)")
                else:
                    logger.error("Neo4j connection failed")
                    self.neo4j_conn = None
            else:
                logger.warning("Neo4j credentials not set")

    async def process_job(
        self,
        job_id: str,
        metadata: Optional[Dict[str, Any]] = None,
        stream: bool = False,
        resume: bool = False,
        feedback: Optional[str] = None,
        original_config_name: Optional[str] = None,
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

        self._current_job_id = job_id
        logger.info(f"Processing job {job_id}")

        try:
            # Create workspace for this job
            # Base path comes from WORKSPACE_PATH env var or defaults
            # This also copies documents to workspace and returns updated metadata
            updated_metadata = await self._setup_job_workspace(job_id, metadata, resume=resume)

            # Handle frozen job resume
            if resume:
                frozen_path = self._workspace_manager.get_path("output/job_frozen.json")
                if frozen_path.exists():
                    logger.info(f"Resuming frozen job {job_id}")
                    # Remove the frozen marker so the graph can continue
                    frozen_path.unlink()
                    logger.info(f"Removed job_frozen.json to allow continuation")

                    # Inject feedback if provided
                    if feedback:
                        feedback_section = (
                            f"\n\n## Human Feedback on Resume\n\n"
                            f"The job was previously frozen for review. "
                            f"A human operator has provided the following feedback:\n\n"
                            f"{feedback}\n"
                        )
                        self._workspace_manager.append_file("instructions.md", feedback_section)
                        logger.info(f"Injected human feedback into instructions.md")

                    # Update database status back to processing
                    if self.postgres_conn:
                        try:
                            await self.postgres_conn.execute(
                                "UPDATE jobs SET status = 'processing' WHERE id = $1::uuid",
                                job_id
                            )
                            logger.info(f"Updated job status to 'processing' for resumed frozen job")
                        except Exception as e:
                            logger.warning(f"Failed to update job status: {e}")

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
            snapshot_manager = PhaseSnapshotManager(job_id)

            # Build graph for this job
            workspace_template = self._load_workspace_template()

            self._graph = build_phase_alternation_graph(
                strategic_llm_with_tools=self._strategic_llm_with_tools,
                tactical_llm_with_tools=self._tactical_llm_with_tools,
                tools=self._tools,
                config=self.config,
                workspace=self._workspace_manager,
                todo_manager=self._todo_manager,
                workspace_template=workspace_template,
                checkpointer=self._checkpointer,
                summarization_llm=self._summarization_llm,
                snapshot_manager=snapshot_manager,
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

            # Check if we should resume from phase snapshot or start fresh
            # When resuming, use the last completed phase snapshot (more reliable than raw checkpoint)
            graph_input = None
            if resume:
                # Try to recover from the last completed phase snapshot
                latest_snapshot = snapshot_manager.get_latest_snapshot()
                if latest_snapshot:
                    logger.info(
                        f"Resuming from phase {latest_snapshot.phase_number} snapshot "
                        f"(iteration={latest_snapshot.iteration})"
                    )
                    # Recover workspace files and checkpoint from snapshot
                    if snapshot_manager.recover_to_phase(latest_snapshot.phase_number):
                        # Delete any stale snapshots from failed runs after this phase
                        deleted = snapshot_manager.delete_snapshots_after(latest_snapshot.phase_number)
                        if deleted:
                            logger.info(f"Deleted {deleted} stale snapshot(s) after phase {latest_snapshot.phase_number}")

                        # Determine the correct thread_id for checkpoint lookup
                        # Priority: 1) snapshot.thread_id, 2) discover from checkpoint DB, 3) try known formats
                        discovered_thread_id = None

                        if latest_snapshot.thread_id:
                            # Snapshot has thread_id stored (new snapshots)
                            discovered_thread_id = latest_snapshot.thread_id
                            logger.info(f"Using thread_id from snapshot: {discovered_thread_id}")
                        else:
                            # Old snapshot without thread_id - discover from checkpoint DB
                            from .core.phase_snapshot import discover_thread_id_from_checkpoint
                            checkpoint_path = self._get_checkpoint_path(job_id)
                            discovered_thread_id = discover_thread_id_from_checkpoint(checkpoint_path, job_id)
                            if discovered_thread_id:
                                logger.info(f"Discovered thread_id from checkpoint: {discovered_thread_id}")

                        if discovered_thread_id:
                            thread_id = discovered_thread_id
                            thread_config = {
                                "configurable": {"thread_id": thread_id},
                                "recursion_limit": 1000000,
                            }
                            # Verify checkpoint exists with this thread_id
                            checkpoint_state = await self._graph.aget_state(thread_config)
                            if checkpoint_state and checkpoint_state.values:
                                logger.info(f"Found checkpoint with thread_id: {thread_id}")
                                # graph_input stays None to continue from checkpoint
                            else:
                                logger.warning(f"Discovered thread_id {thread_id} has no checkpoint data, starting fresh")
                                graph_input = create_initial_state(
                                    job_id=job_id,
                                    workspace_path=str(self._workspace_manager.path),
                                    metadata=updated_metadata,
                                )
                        else:
                            # Fallback: try new format then legacy format
                            checkpoint_state = await self._graph.aget_state(thread_config)
                            if checkpoint_state and checkpoint_state.values:
                                logger.debug(f"Found checkpoint with new thread_id format: {job_id}")
                            else:
                                # Try legacy format
                                legacy_config_name = original_config_name or self.config.agent_id
                                legacy_thread_id = f"{legacy_config_name}_{job_id}"
                                legacy_config = {
                                    "configurable": {"thread_id": legacy_thread_id},
                                    "recursion_limit": 1000000,
                                }
                                legacy_state = await self._graph.aget_state(legacy_config)
                                if legacy_state and legacy_state.values:
                                    logger.info(f"Using legacy thread_id format: {legacy_thread_id}")
                                    thread_config = legacy_config
                                    thread_id = legacy_thread_id
                                else:
                                    logger.warning(
                                        f"No checkpoint found with any thread_id format, starting fresh"
                                    )
                                    graph_input = create_initial_state(
                                        job_id=job_id,
                                        workspace_path=str(self._workspace_manager.path),
                                        metadata=updated_metadata,
                                    )
                    else:
                        logger.warning(
                            f"Failed to recover from phase {latest_snapshot.phase_number} snapshot, starting fresh"
                        )
                        graph_input = create_initial_state(
                            job_id=job_id,
                            workspace_path=str(self._workspace_manager.path),
                            metadata=updated_metadata,
                        )
                else:
                    logger.warning(f"No phase snapshots found for job {job_id}, starting fresh")
                    graph_input = create_initial_state(
                        job_id=job_id,
                        workspace_path=str(self._workspace_manager.path),
                        metadata=updated_metadata,
                    )
            else:
                # Fresh start - create initial state
                graph_input = create_initial_state(
                    job_id=job_id,
                    workspace_path=str(self._workspace_manager.path),
                    metadata=updated_metadata,
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
                    await self._cleanup_checkpointer()

        except Exception as e:
            logger.error(f"Job {job_id} failed: {e}", exc_info=True)
            await self._cleanup_checkpointer()
            self._current_job_id = None
            error_state = {
                "job_id": job_id,
                "error": {
                    "message": str(e),
                    "type": "job_error",
                    "recoverable": False,
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

    async def _yield_error_state(self, error_state: Dict[str, Any]) -> AsyncIterator[Dict[str, Any]]:
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
            await self._cleanup_checkpointer()

    def _load_workspace_template(self) -> str:
        """Load the workspace.md template for the nested loop graph.

        Returns:
            Template content for workspace.md
        """
        # Template is at config/templates/workspace_template.md
        templates_dir = get_project_root() / "config" / "templates"
        template_path = templates_dir / "workspace_template.md"

        if not template_path.exists():
            raise FileNotFoundError(f"Workspace template not found: {template_path}")
        return template_path.read_text(encoding="utf-8")

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

        # Handle config upload - load and merge with defaults BEFORE workspace setup
        # This must happen first since config affects workspace settings
        if metadata.get("config_upload_id"):
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
                        logger.info(f"Loading uploaded config (HTTP): {uploaded_config_path.name}")

                        # Load and merge with defaults
                        merged_config_data = load_uploaded_config(uploaded_config_path)

                        # Create new config object (replaces self.config for this job)
                        self.config = load_agent_config_from_dict(merged_config_data)
                        logger.info("Applied uploaded config overrides")
                        config_loaded = True

            # Fall back to local filesystem
            if not config_loaded:
                config_uploads_dir = get_workspace_base_path() / "uploads" / config_upload_id

                if config_uploads_dir.exists():
                    # Find the YAML file
                    yaml_files = list(config_uploads_dir.glob("*.yaml")) + list(
                        config_uploads_dir.glob("*.yml")
                    )
                    if yaml_files:
                        uploaded_config_path = yaml_files[0]
                        logger.info(f"Loading uploaded config (local): {uploaded_config_path.name}")

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
                    logger.warning(f"Config upload directory not found: {config_uploads_dir}")

        # Handle inline config override - merge on top of current config
        if metadata.get("config_override"):
            from .core.loader import deep_merge, load_agent_config_from_dict
            import dataclasses

            config_override = metadata["config_override"]
            logger.info(f"Applying inline config override: {list(config_override.keys())}")

            # Convert current config to dict, merge, and reload
            current_config_dict = dataclasses.asdict(self.config)
            merged_config_data = deep_merge(current_config_dict, config_override)
            self.config = load_agent_config_from_dict(merged_config_data)
            logger.info("Applied inline config overrides")

        # Create workspace manager
        # base_path is None - let WorkspaceManager use get_workspace_base_path()
        self._workspace_manager = WorkspaceManager(
            job_id=job_id,
            config=WorkspaceManagerConfig(
                structure=self.config.workspace.structure,
                git_versioning=self.config.workspace.git_versioning,
                git_ignore_patterns=self.config.workspace.git_ignore_patterns,
            )
        )

        # Check if resuming an existing workspace
        if resume and self._workspace_manager.path.exists():
            logger.info(f"Resuming job {job_id} with existing workspace")
            # Verify workspace has required files
            instructions_path = self._workspace_manager.path / "instructions.md"
            if not instructions_path.exists():
                # Only write instructions if missing
                instructions = load_instructions(self.config)
                self._workspace_manager.write_file("instructions.md", instructions)
                logger.debug("Wrote missing instructions.md to workspace")

            # Create todo manager for this workspace
            self._todo_manager = TodoManager(workspace=self._workspace_manager)

            logger.debug(f"Resumed workspace at {self._workspace_manager.path}")
            return metadata or {}

        # Initialize workspace (creates directories)
        self._workspace_manager.initialize()

        # Copy instructions to workspace (from upload or template)
        if metadata.get("instructions_upload_id"):
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
                        logger.info(f"Copied uploaded instructions (HTTP): {uploaded_instr_path.name}")
                        instructions_written = True

            # Fall back to local filesystem
            if not instructions_written:
                instr_uploads_dir = get_workspace_base_path() / "uploads" / instr_upload_id

                if instr_uploads_dir.exists():
                    # Find the instructions file (.md or .txt)
                    instr_files = list(instr_uploads_dir.glob("*.md")) + list(
                        instr_uploads_dir.glob("*.txt")
                    )
                    if instr_files:
                        uploaded_instr_path = instr_files[0]
                        content = uploaded_instr_path.read_text(encoding="utf-8")
                        self._workspace_manager.write_file("instructions.md", content)
                        logger.info(f"Copied uploaded instructions (local): {uploaded_instr_path.name}")
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
                instructions = load_instructions(self.config)
                self._workspace_manager.write_file("instructions.md", instructions)
        else:
            # Use template-based instructions
            instructions = load_instructions(self.config)
            self._workspace_manager.write_file("instructions.md", instructions)

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
                        logger.debug(f"Initialized file: {dest_path} from {source_path}")
                    else:
                        logger.warning(f"Initial file template not found: {source_path}")
                except Exception as e:
                    logger.warning(f"Failed to initialize {dest_path}: {e}")

        # Copy documents to workspace if provided
        updated_metadata = dict(metadata)

        # Handle upload_id (files uploaded via orchestrator UI)
        if metadata.get("upload_id"):
            upload_id = metadata["upload_id"]
            from .core.workspace import get_workspace_base_path
            import tempfile

            copied_paths = []
            original_paths = []

            # Ensure documents directory exists
            documents_dir = self._workspace_manager.get_path("documents")
            documents_dir.mkdir(parents=True, exist_ok=True)

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
                    logger.info(f"Processing documents from local path: {local_uploads_dir}")
                else:
                    logger.warning(f"Upload directory not found locally or via HTTP: {upload_id}")

            if upload_source_dir:
                for file_path in sorted(upload_source_dir.iterdir()):
                    # Skip metadata.json
                    if file_path.name == "metadata.json":
                        continue
                    if file_path.is_file():
                        # Check if zip - extract instead of copy
                        if file_path.suffix.lower() == '.zip':
                            extracted = self._extract_zip(
                                file_path, documents_dir, logger
                            )
                            copied_paths.extend(extracted)
                            original_paths.extend([str(file_path)] * len(extracted))
                            logger.info(f"Processed zip file: {file_path.name} ({len(extracted)} files extracted)")
                        else:
                            # Regular file - copy with conflict handling
                            dest_path = documents_dir / file_path.name
                            counter = 1
                            stem = Path(file_path.name).stem
                            suffix = Path(file_path.name).suffix
                            while dest_path.exists():
                                dest_path = documents_dir / f"{stem}_{counter}{suffix}"
                                counter += 1

                            shutil.copy2(file_path, dest_path)
                            dest_relative = f"documents/{dest_path.name}"
                            logger.info(f"Copied uploaded file to workspace: {dest_relative}")

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

            # Ensure documents directory exists
            documents_dir = self._workspace_manager.get_path("documents")
            documents_dir.mkdir(parents=True, exist_ok=True)

            for doc_path in metadata["document_paths"]:
                source_path = Path(doc_path)
                if source_path.exists():
                    # Check if zip - extract instead of copy
                    if source_path.suffix.lower() == '.zip':
                        extracted = self._extract_zip(
                            source_path, documents_dir, logger
                        )
                        copied_paths.extend(extracted)
                        original_paths.extend([str(source_path)] * len(extracted))
                        logger.info(f"Processed zip file: {source_path.name} ({len(extracted)} files extracted)")
                    else:
                        # Regular file - copy with conflict handling
                        dest_path = documents_dir / source_path.name
                        counter = 1
                        stem = Path(source_path.name).stem
                        suffix = Path(source_path.name).suffix
                        while dest_path.exists():
                            dest_path = documents_dir / f"{stem}_{counter}{suffix}"
                            counter += 1

                        shutil.copy2(source_path, dest_path)
                        dest_relative = f"documents/{dest_path.name}"
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
                # Ensure documents directory exists
                documents_dir = self._workspace_manager.get_path("documents")
                documents_dir.mkdir(parents=True, exist_ok=True)

                # Check if zip - extract instead of copy
                if source_path.suffix.lower() == '.zip':
                    extracted = self._extract_zip(
                        source_path, documents_dir, logger
                    )
                    if extracted:
                        updated_metadata["document_paths"] = extracted
                        updated_metadata["original_document_paths"] = [str(source_path)] * len(extracted)
                        updated_metadata["document_path"] = extracted[0]
                        updated_metadata["original_document_path"] = str(source_path)
                    logger.info(f"Processed zip file: {source_path.name} ({len(extracted)} files extracted)")
                else:
                    # Regular file - copy to documents/ folder
                    dest_filename = source_path.name
                    dest_relative = f"documents/{dest_filename}"
                    dest_path = self._workspace_manager.get_path(dest_relative)

                    shutil.copy2(source_path, dest_path)
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
            analysis_dir = self._workspace_manager.get_path("analysis")
            analysis_dir.mkdir(parents=True, exist_ok=True)
            self._workspace_manager.write_file("analysis/requirement_input.md", requirement_md)
            logger.info(f"Wrote requirement to analysis/requirement_input.md")

        # Create todo manager for this workspace
        self._todo_manager = TodoManager(workspace=self._workspace_manager)

        # Generate tool documentation in workspace
        tool_names = get_all_tool_names(self.config)
        tools_dir = self._workspace_manager.get_path("tools")
        generate_workspace_tool_docs(tool_names, tools_dir)

        logger.debug(f"Workspace created at {self._workspace_manager.path}")

        return updated_metadata

    async def _setup_job_tools(self) -> None:
        """Set up tools for the current job.

        Loads tools based on configuration and injects dependencies
        (workspace manager, todo manager, connections).
        """
        # Create tool context with dependencies
        # Merge agent_id and LLM settings into config for tools
        tool_config = {
            **self.config.extra,
            "agent_id": self.config.agent_id,
            "multimodal": self.config.llm.multimodal,  # For vision-aware file reading
        }
        context = ToolContext(
            workspace_manager=self._workspace_manager,
            todo_manager=self._todo_manager,
            postgres_db=self.postgres_conn,
            neo4j_db=self.neo4j_conn,
            config=tool_config,
            _job_id=self._current_job_id,
        )

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

        # Apply description overrides for deferred tools
        # Domain tools get short descriptions; agent reads full docs from workspace
        self._tools = apply_description_overrides(self._tools)

        # Bind tools to phase-specific LLMs
        self._strategic_llm_with_tools = self._strategic_llm.bind_tools(self._tools)
        self._tactical_llm_with_tools = self._tactical_llm.bind_tools(self._tools)

        # Keep _llm_with_tools for backwards compatibility
        self._llm_with_tools = self._strategic_llm_with_tools

        logger.debug(f"Loaded {len(self._tools)} tools")

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
            logger.debug(f"Extracted requirement data: {job.get('name', job.get('id', 'unknown'))}")
            return metadata

        # Otherwise, handle as jobs table row
        metadata_fields = [
            "document_path", "prompt", "requirement_id", "requirement_data",
            "source_document", "config", "options",
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
            req.get('text', '(No text provided)'),
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
        source_location = req.get('source_location')
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

        if req.get('reasoning'):
            lines.extend([
                "## Extraction Reasoning",
                "",
                req['reasoning'],
                "",
            ])

        if req.get('research_notes'):
            lines.extend([
                "## Research Notes",
                "",
                req['research_notes'],
                "",
            ])

        return "\n".join(lines)

    def _extract_zip(
        self,
        zip_path: Path,
        dest_dir: Path,
        job_logger: logging.Logger,
    ) -> List[str]:
        """Extract zip file contents preserving directory structure.

        Skips hidden files and macOS __MACOSX folders.

        Args:
            zip_path: Path to the zip file
            dest_dir: Destination directory for extracted files
            job_logger: Logger instance

        Returns:
            List of relative paths to extracted files (e.g., ["documents/subdir/file.pdf"])
        """
        extracted_paths = []

        try:
            with zipfile.ZipFile(zip_path, 'r') as zf:
                for zip_info in zf.infolist():
                    # Skip directories (created implicitly)
                    if zip_info.is_dir():
                        continue

                    # Get relative path within zip
                    relative_path = Path(zip_info.filename)

                    # Skip hidden files and macOS metadata
                    if any(part.startswith('.') for part in relative_path.parts):
                        continue
                    if '__MACOSX' in zip_info.filename:
                        continue

                    # Skip empty filenames
                    if not relative_path.name:
                        continue

                    # Preserve directory structure
                    dest_path = dest_dir / relative_path
                    dest_path.parent.mkdir(parents=True, exist_ok=True)

                    # Extract file
                    with zf.open(zip_info) as source:
                        dest_path.write_bytes(source.read())

                    # Return path relative to workspace
                    rel_to_workspace = dest_path.relative_to(dest_dir.parent)
                    extracted_paths.append(str(rel_to_workspace))
                    job_logger.debug(f"Extracted: {zip_info.filename} -> {rel_to_workspace}")

            job_logger.info(f"Extracted {len(extracted_paths)} files from {zip_path.name}")

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
            job_logger.warning(f"HTTP download failed for {upload_id}: {e}, will try local")
            return None
        finally:
            await client.close()

    async def approve_frozen_job(self, job_id: str) -> Dict[str, Any]:
        """Approve a frozen job, marking it as truly completed.

        This method is called when a human operator reviews a frozen job
        and decides it is ready to be marked as completed.

        It performs the following:
        1. Reads the frozen job data from job_frozen.json
        2. Converts it to job_completion.json (marks as truly completed)
        3. Removes the job_frozen.json file
        4. Updates the database status to 'completed'

        Args:
            job_id: The job ID to approve

        Returns:
            Dict with approval result

        Raises:
            ValueError: If job is not frozen or workspace doesn't exist
        """
        import json
        from datetime import datetime

        # Set up workspace for this job (existing workspace)
        workspace_manager = WorkspaceManager(
            job_id=job_id,
            config=WorkspaceManagerConfig(
                structure=self.config.workspace.structure,
                git_versioning=self.config.workspace.git_versioning,
                git_ignore_patterns=self.config.workspace.git_ignore_patterns,
            )
        )

        if not workspace_manager.path.exists():
            raise ValueError(f"Workspace for job {job_id} does not exist")

        frozen_path = workspace_manager.get_path("output/job_frozen.json")
        completion_path = workspace_manager.get_path("output/job_completion.json")

        if not frozen_path.exists():
            raise ValueError(f"Job {job_id} is not frozen (no job_frozen.json found)")

        # Read frozen data
        frozen_data = json.loads(frozen_path.read_text())

        # Convert to completion data
        completion_data = {
            **frozen_data,
            "status": "job_completed",
            "approved_at": datetime.now().isoformat(),
            "approved_by": "human_operator",
        }

        # Write completion file
        completion_path.parent.mkdir(parents=True, exist_ok=True)
        completion_path.write_text(json.dumps(completion_data, indent=2, ensure_ascii=False))
        logger.info(f"Wrote job_completion.json for job {job_id}")

        # Remove frozen file
        frozen_path.unlink()
        logger.info(f"Removed job_frozen.json for job {job_id}")

        # Update database status to completed
        if self.postgres_conn:
            try:
                await self.postgres_conn.execute(
                    "UPDATE jobs SET status = 'completed', completed_at = NOW() WHERE id = $1::uuid",
                    job_id
                )
                logger.info(f"Updated job {job_id} status to 'completed' in database")
            except Exception as e:
                logger.warning(f"Failed to update job status in database: {e}")

        return {
            "job_id": job_id,
            "status": "approved",
            "summary": completion_data.get("summary", ""),
            "deliverables": completion_data.get("deliverables", []),
            "approved_at": completion_data["approved_at"],
        }

    async def shutdown(self) -> None:
        """Shutdown the agent and cleanup resources."""
        logger.info(f"Shutting down {self.config.display_name}...")
        self._shutdown_requested = True

        # Close database connections
        if self.postgres_conn:
            try:
                # PostgresDB uses close() not disconnect()
                await self.postgres_conn.close()
            except Exception as e:
                logger.warning(f"Error closing PostgreSQL: {e}")

        if self.neo4j_conn:
            try:
                # Neo4jDB uses close() (sync)
                self.neo4j_conn.close()
            except Exception as e:
                logger.warning(f"Error closing Neo4j: {e}")

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
                "neo4j": self.neo4j_conn is not None,
            },
            "config": {
                "model": self.config.llm.model,
            },
        }

