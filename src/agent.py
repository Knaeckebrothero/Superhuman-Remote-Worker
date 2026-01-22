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
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional

import aiosqlite
from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from .core.workspace import WorkspaceManager, WorkspaceManagerConfig, get_checkpoints_path


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
from .tools.description_generator import generate_workspace_tool_docs
from .tools.description_override import apply_description_overrides
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

        # Create LLM
        self._llm = create_llm(self.config.llm)

        self._initialized = True
        logger.info(f"{self.config.display_name} initialized successfully")

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
    ) -> Dict[str, Any]:
        """Process a single job.

        Creates a workspace for the job, loads tools, builds the graph,
        and executes until completion.

        Args:
            job_id: Unique job identifier
            metadata: Job-specific data (document_path, requirement_id, etc.)
            stream: If True, return an async iterator of state updates

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

            # Load tools for this job
            await self._setup_job_tools()

            # Create checkpointer for this job (enables resume after crash)
            checkpoint_path = self._get_checkpoint_path(job_id)
            self._checkpoint_conn = await aiosqlite.connect(checkpoint_path)
            # Wrap connection to add is_alive() for langgraph-checkpoint-sqlite 3.x compatibility
            wrapped_conn = _AiosqliteConnectionWrapper(self._checkpoint_conn)
            self._checkpointer = AsyncSqliteSaver(wrapped_conn)
            logger.info(f"Checkpointer initialized at {checkpoint_path}")

            # Build graph for this job
            workspace_template = self._load_workspace_template()

            self._graph = build_phase_alternation_graph(
                llm_with_tools=self._llm_with_tools,
                tools=self._tools,
                config=self.config,
                workspace=self._workspace_manager,
                todo_manager=self._todo_manager,
                workspace_template=workspace_template,
                checkpointer=self._checkpointer,
                summarization_llm=self._llm,  # Use base LLM for context summarization
            )

            # Execute graph
            thread_config = {
                "configurable": {
                    "thread_id": f"{self.config.agent_id}_{job_id}",
                },
                "recursion_limit": self.config.limits.max_iterations * 2,
            }

            # Check if we should resume from checkpoint or start fresh
            # When resuming, pass None to continue from checkpoint state
            # When starting fresh, pass initial_state to initialize
            graph_input = None
            if resume:
                # Check if checkpoint exists
                checkpoint_state = await self._graph.aget_state(thread_config)
                if checkpoint_state and checkpoint_state.values:
                    logger.info(
                        f"Resuming from checkpoint: iteration={checkpoint_state.values.get('iteration', 0)}, "
                        f"phase={checkpoint_state.values.get('phase_number', 0)}, "
                        f"initialized={checkpoint_state.values.get('initialized', False)}"
                    )
                    graph_input = None  # Continue from checkpoint
                else:
                    logger.warning(f"Resume requested but no checkpoint found for job {job_id}, starting fresh")
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
        # Template is at src/config/workspace_template.md
        config_dir = Path(__file__).parent / "config"
        template_path = config_dir / "workspace_template.md"

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

        # Create workspace manager
        # base_path is None - let WorkspaceManager use get_workspace_base_path()
        self._workspace_manager = WorkspaceManager(
            job_id=job_id,
            config=WorkspaceManagerConfig(
                structure=self.config.workspace.structure,
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

        # Copy instructions to workspace
        instructions = load_instructions(self.config)
        self._workspace_manager.write_file(
            "instructions.md",
            instructions,
        )

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

        # Handle multiple documents (document_paths list)
        if metadata.get("document_paths"):
            copied_paths = []
            original_paths = []
            for doc_path in metadata["document_paths"]:
                source_path = Path(doc_path)
                if source_path.exists():
                    dest_filename = source_path.name
                    dest_relative = f"documents/{dest_filename}"
                    dest_path = self._workspace_manager.get_path(dest_relative)
                    dest_path.parent.mkdir(parents=True, exist_ok=True)

                    shutil.copy2(source_path, dest_path)
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
                # Copy to documents/ folder in workspace
                dest_filename = source_path.name
                dest_relative = f"documents/{dest_filename}"
                dest_path = self._workspace_manager.get_path(dest_relative)
                dest_path.parent.mkdir(parents=True, exist_ok=True)

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
        # Merge agent_id into config for citation audit trails
        tool_config = {**self.config.extra, "agent_id": self.config.agent_id}
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

        # Bind tools to LLM
        self._llm_with_tools = self._llm.bind_tools(self._tools)

        logger.debug(f"Loaded {len(self._tools)} tools")

    def _get_checkpoint_path(self, job_id: str) -> Path:
        """Get SQLite checkpoint file path for a job.

        Args:
            job_id: Unique job identifier

        Returns:
            Path to SQLite checkpoint file (e.g., workspace/checkpoints/job_<id>.db)
        """
        return get_checkpoints_path() / f"job_{job_id}.db"

    async def start_polling(self) -> None:
        """Start the polling loop for jobs.

        Continuously polls the configured table for pending jobs
        and processes them. Runs until shutdown is requested.

        Example:
            ```python
            agent = UniversalAgent.from_config("creator")
            await agent.initialize()

            # Start polling (blocks until shutdown)
            await agent.start_polling()
            ```
        """
        if not self._initialized:
            await self.initialize()

        if not self.config.polling.enabled:
            logger.warning("Polling is disabled in config")
            return

        logger.info(
            f"Starting polling loop: table={self.config.polling.table}, "
            f"field={self.config.polling.status_field}, "
            f"interval={self.config.polling.interval_seconds}s"
        )

        while not self._shutdown_requested:
            try:
                # Poll for pending job
                job = await self._poll_for_job()

                if job:
                    job_id = str(job.get("id", job.get("job_id", uuid.uuid4())))
                    metadata = self._extract_job_metadata(job)

                    # Update status to processing
                    await self._update_job_status(
                        job_id,
                        self.config.polling.status_value_processing,
                    )

                    # Process the job
                    result = await self.process_job(job_id, metadata)

                    # Update status based on result
                    if result.get("error"):
                        await self._update_job_status(
                            job_id,
                            self.config.polling.status_value_failed,
                            error=result["error"],
                        )
                    else:
                        await self._update_job_status(
                            job_id,
                            self.config.polling.status_value_complete,
                        )

                else:
                    # No job found, wait before polling again
                    await asyncio.sleep(self.config.polling.interval_seconds)

            except asyncio.CancelledError:
                logger.info("Polling cancelled")
                break

            except Exception as e:
                logger.error(f"Polling error: {e}", exc_info=True)
                await asyncio.sleep(self.config.polling.interval_seconds)

        logger.info("Polling loop ended")

    async def _poll_for_job(self) -> Optional[Dict[str, Any]]:
        """Poll the database for a pending job.

        Uses configuration to determine which table and field to check.
        Supports SKIP LOCKED for concurrent processing.
        """
        if not self.postgres_conn:
            logger.warning("No database connection for polling")
            return None

        table = self.config.polling.table
        status_field = self.config.polling.status_field
        pending_value = self.config.polling.status_value_pending
        use_skip_locked = self.config.polling.use_skip_locked

        skip_locked_clause = "FOR UPDATE SKIP LOCKED" if use_skip_locked else ""

        query = f"""
            SELECT * FROM {table}
            WHERE {status_field} = $1
            ORDER BY created_at ASC
            LIMIT 1
            {skip_locked_clause}
        """

        try:
            row = await self.postgres_conn.fetchrow(query, pending_value)
            return dict(row) if row else None
        except Exception as e:
            logger.error(f"Polling query failed: {e}")
            return None

    async def _update_job_status(
        self,
        job_id: str,
        status: str,
        error: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Update job status in the database."""
        if not self.postgres_conn:
            return

        table = self.config.polling.table
        status_field = self.config.polling.status_field

        if error:
            query = f"""
                UPDATE {table}
                SET {status_field} = $1, error = $2, updated_at = NOW()
                WHERE id = $3::uuid
            """
            await self.postgres_conn.execute(query, status, str(error), job_id)
        else:
            query = f"""
                UPDATE {table}
                SET {status_field} = $1, updated_at = NOW()
                WHERE id = $2::uuid
            """
            await self.postgres_conn.execute(query, status, job_id)

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

        # Handle mentioned_objects (may be JSON string or list)
        mentioned_objects = req.get('mentioned_objects')
        if mentioned_objects:
            if isinstance(mentioned_objects, str):
                try:
                    mentioned_objects = json.loads(mentioned_objects)
                except (json.JSONDecodeError, TypeError):
                    mentioned_objects = []
            if mentioned_objects:
                lines.extend([
                    "## Mentioned Business Objects",
                    "",
                    *[f"- {obj}" for obj in mentioned_objects],
                    "",
                ])

        # Handle mentioned_messages (may be JSON string or list)
        mentioned_messages = req.get('mentioned_messages')
        if mentioned_messages:
            if isinstance(mentioned_messages, str):
                try:
                    mentioned_messages = json.loads(mentioned_messages)
                except (json.JSONDecodeError, TypeError):
                    mentioned_messages = []
            if mentioned_messages:
                lines.extend([
                    "## Mentioned Messages",
                    "",
                    *[f"- {msg}" for msg in mentioned_messages],
                    "",
                ])

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
                "polling_enabled": self.config.polling.enabled,
                "max_iterations": self.config.limits.max_iterations,
                "model": self.config.llm.model,
            },
        }

