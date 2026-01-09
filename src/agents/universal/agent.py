"""Universal Agent Implementation.

The Universal Agent is a configurable, workspace-centric autonomous agent
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

from langchain_core.language_models import BaseChatModel

from src.agents.shared import (
    WorkspaceManager,
    WorkspaceConfig as WMConfig,
    TodoManager,
    ToolContext,
    load_tools,
    ContextManager,
    ContextConfig,
)

from .state import UniversalAgentState, create_initial_state
from .loader import (
    AgentConfig,
    load_agent_config,
    create_llm,
    load_system_prompt,
    load_instructions,
    get_all_tool_names,
    resolve_config_path,
)
from .graph import build_agent_graph, run_graph_with_streaming

logger = logging.getLogger(__name__)


class UniversalAgent:
    """Configurable autonomous agent using workspace-centric architecture.

    The Universal Agent reads its behavior from a JSON configuration file,
    enabling a single implementation to serve as Creator, Validator, or
    any other agent type.

    Architecture:
    - Strategic Planning: Filesystem-based plans in workspace/plans/
    - Tactical Execution: TodoManager with archive_and_reset()
    - Context Management: Automatic compaction and summarization
    - Tool Loading: Dynamic based on config.tools

    Example:
        ```python
        # Create agent from config
        agent = UniversalAgent.from_config("creator")
        await agent.initialize()

        # Process a job
        result = await agent.process_job(
            job_id="abc123",
            metadata={"document_path": "/data/doc.pdf"}
        )

        # Cleanup
        await agent.shutdown()
        ```
    """

    def __init__(
        self,
        config: AgentConfig,
        postgres_conn: Optional[Any] = None,
        neo4j_conn: Optional[Any] = None,
    ):
        """Initialize the Universal Agent.

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
        self._context_manager: Optional[ContextManager] = None

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

    @classmethod
    def from_config(
        cls,
        config_path: str,
        postgres_conn: Optional[Any] = None,
        neo4j_conn: Optional[Any] = None,
    ) -> "UniversalAgent":
        """Create an agent from a configuration file.

        Args:
            config_path: Path to config file or config name (e.g., "creator")
            postgres_conn: Optional PostgreSQL connection
            neo4j_conn: Optional Neo4j connection

        Returns:
            UniversalAgent instance

        Example:
            ```python
            # By name (looks in config/agents/)
            agent = UniversalAgent.from_config("creator")

            # By path
            agent = UniversalAgent.from_config("/path/to/my_agent.json")
            ```
        """
        resolved_path = resolve_config_path(config_path)
        config = load_agent_config(resolved_path)
        return cls(config, postgres_conn, neo4j_conn)

    async def initialize(self) -> None:
        """Initialize the agent and its components.

        This must be called before processing jobs. Sets up:
        - Database connections (if not provided)
        - LLM instance
        - Context manager
        - Base tools (workspace, todo)

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

        # Create context manager
        self._context_manager = ContextManager(
            config=ContextConfig(
                compaction_threshold_tokens=self.config.limits.context_threshold_tokens,
                keep_raw_turns=self.config.context_management.keep_recent_tool_results,
            )
        )

        self._initialized = True
        logger.info(f"{self.config.display_name} initialized successfully")

    async def _setup_connections(self) -> None:
        """Set up required database connections."""
        # PostgreSQL connection (always required for job management)
        if self.postgres_conn is None and self.config.connections.postgres:
            from src.core.postgres_utils import create_postgres_connection
            db_url = os.getenv("DATABASE_URL")
            if db_url:
                self.postgres_conn = create_postgres_connection(db_url)
                await self.postgres_conn.connect()
                logger.info("PostgreSQL connection established")
            else:
                logger.warning("DATABASE_URL not set, PostgreSQL unavailable")

        # Neo4j connection (optional, based on config)
        if self.neo4j_conn is None and self.config.connections.neo4j:
            from src.core.neo4j_utils import Neo4jConnection
            neo4j_uri = os.getenv("NEO4J_URI")
            neo4j_user = os.getenv("NEO4J_USER", "neo4j")
            neo4j_password = os.getenv("NEO4J_PASSWORD")

            if neo4j_uri and neo4j_password:
                self.neo4j_conn = Neo4jConnection(
                    uri=neo4j_uri,
                    user=neo4j_user,
                    password=neo4j_password,
                )
                logger.info("Neo4j connection established")
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

            # Build graph for this job
            system_prompt = load_system_prompt(self.config, job_id)
            self._graph = build_agent_graph(
                llm_with_tools=self._llm_with_tools,
                tools=self._tools,
                config=self.config,
                system_prompt=system_prompt,
            )

            # Create initial state with updated metadata (workspace-relative paths)
            initial_state = create_initial_state(
                job_id=job_id,
                workspace_path=str(self._workspace_manager.path),
                metadata=updated_metadata,
            )

            # Execute graph
            thread_config = {
                "configurable": {
                    "thread_id": f"{self.config.agent_id}_{job_id}",
                },
                "recursion_limit": self.config.limits.max_iterations * 2,
            }

            if stream:
                return self._process_job_streaming(initial_state, thread_config)
            else:
                final_state = await self._graph.ainvoke(
                    initial_state,
                    config=thread_config,
                )
                self._jobs_processed += 1
                return dict(final_state)

        except Exception as e:
            logger.error(f"Job {job_id} failed: {e}", exc_info=True)
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

        finally:
            self._current_job_id = None

    async def _yield_error_state(self, error_state: Dict[str, Any]) -> AsyncIterator[Dict[str, Any]]:
        """Yield a single error state for streaming mode."""
        yield error_state

    async def _process_job_streaming(
        self,
        initial_state: UniversalAgentState,
        config: Dict[str, Any],
    ) -> AsyncIterator[Dict[str, Any]]:
        """Process job with streaming state updates."""
        async for state in run_graph_with_streaming(
            self._graph, initial_state, config
        ):
            yield state

        self._jobs_processed += 1

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
            config=WMConfig(
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
            self._todo_manager = TodoManager(
                workspace_manager=self._workspace_manager,
            )

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

        # Copy document to workspace if provided
        updated_metadata = dict(metadata)
        if metadata.get("document_path"):
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

        # Create todo manager for this workspace
        self._todo_manager = TodoManager(
            workspace_manager=self._workspace_manager,
        )

        logger.debug(f"Workspace created at {self._workspace_manager.path}")

        return updated_metadata

    async def _setup_job_tools(self) -> None:
        """Set up tools for the current job.

        Loads tools based on configuration and injects dependencies
        (workspace manager, todo manager, connections).
        """
        # Create tool context with dependencies
        context = ToolContext(
            workspace_manager=self._workspace_manager,
            todo_manager=self._todo_manager,
            postgres_conn=self.postgres_conn,
            neo4j_conn=self.neo4j_conn,
            config=self.config.extra,  # Agent-specific config
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

        # Bind tools to LLM
        self._llm_with_tools = self._llm.bind_tools(self._tools)

        logger.debug(f"Loaded {len(self._tools)} tools")

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
        """Extract metadata from a job record for processing."""
        # Common fields to extract
        metadata_fields = [
            "document_path", "prompt", "requirement_id", "requirement_data",
            "source_document", "config", "options",
        ]

        metadata = {}
        for field in metadata_fields:
            if field in job:
                metadata[field] = job[field]

        # Include job-specific data if present
        if "data" in job and isinstance(job["data"], dict):
            metadata.update(job["data"])

        return metadata

    async def shutdown(self) -> None:
        """Shutdown the agent and cleanup resources."""
        logger.info(f"Shutting down {self.config.display_name}...")
        self._shutdown_requested = True

        # Close database connections
        if self.postgres_conn:
            try:
                await self.postgres_conn.disconnect()
            except Exception as e:
                logger.warning(f"Error closing PostgreSQL: {e}")

        if self.neo4j_conn:
            try:
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

    @property
    def agent_id(self) -> str:
        """Get the agent ID."""
        return self.config.agent_id

    @property
    def display_name(self) -> str:
        """Get the display name."""
        return self.config.display_name
