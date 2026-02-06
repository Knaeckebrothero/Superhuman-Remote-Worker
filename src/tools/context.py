"""Tool context for dependency injection.

Provides a container for dependencies that tools need access to,
such as workspace managers, database connections, and configuration.
"""

from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Deque, Dict, Optional

from ..core.workspace import WorkspaceManager

# Avoid circular imports with TYPE_CHECKING
if TYPE_CHECKING:
    from ..database.postgres_db import PostgresDB
    from ..database.neo4j_db import Neo4jDB


@dataclass
class ToolContext:
    """Context container for tool dependencies.

    This class holds all dependencies that tools may need during execution.
    It's passed to tool creation functions to enable dependency injection
    without global state.

    Attributes:
        workspace_manager: WorkspaceManager for file operations
        todo_manager: TodoManager for task tracking (optional)
        postgres_db: PostgresDB instance for database operations
        neo4j_db: Neo4jDB instance for graph operations
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
    todo_manager: Optional[Any] = None  # TodoManager, imported later to avoid circular deps
    postgres_db: Optional["PostgresDB"] = None
    neo4j_db: Optional["Neo4jDB"] = None
    config: Dict[str, Any] = field(default_factory=dict)
    _job_id: Optional[str] = None  # Direct job_id override
    citation_engine: Optional[Any] = None  # CitationEngine, imported lazily
    _source_registry: Dict[str, int] = field(default_factory=dict)  # path/url -> source_id
    _inaccessible_sources: Dict[str, str] = field(default_factory=dict)  # url -> error message
    _recent_reads: Deque[str] = field(default_factory=lambda: deque(maxlen=10))  # Recently read file paths

    def __post_init__(self):
        """Validate context after initialization."""
        # Workspace manager is required for workspace tools
        if self.workspace_manager is not None and not self.workspace_manager.is_initialized:
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

    def has_workspace(self) -> bool:
        """Check if workspace manager is available."""
        return self.workspace_manager is not None

    def has_todo(self) -> bool:
        """Check if todo manager is available."""
        return self.todo_manager is not None

    def has_postgres(self) -> bool:
        """Check if PostgreSQL connection is available."""
        return self.postgres_db is not None

    def has_neo4j(self) -> bool:
        """Check if Neo4j connection is available."""
        return self.neo4j_db is not None

    def has_git(self) -> bool:
        """Check if git manager is available and active.

        Returns True only if workspace_manager exists, has a git_manager,
        and the git_manager is active (git available and repo initialized).
        """
        if not self.has_workspace():
            return False
        gm = self.workspace_manager.git_manager
        return gm is not None and gm.is_active

    @property
    def db(self) -> Optional["PostgresDB"]:
        """Get PostgresDB instance.

        Returns:
            PostgresDB instance if available, None otherwise
        """
        return self.postgres_db

    @property
    def graph(self) -> Optional["Neo4jDB"]:
        """Get Neo4jDB instance.

        Returns:
            Neo4jDB instance if available, None otherwise
        """
        return self.neo4j_db

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
        Uses multi-agent mode (PostgreSQL) via CITATION_DB_URL environment variable.

        Returns:
            CitationEngine instance

        Raises:
            ImportError: If citation_engine package is not installed
        """
        if self.citation_engine is None:
            from citation_engine import CitationEngine, CitationContext

            # Create context for audit trails using job_id as session
            ctx = CitationContext(
                session_id=self.job_id or "unknown",
                agent_id=self.config.get("agent_id", "unknown"),
            )

            # Use multi-agent mode (PostgreSQL) - reads CITATION_DB_URL from env
            self.citation_engine = CitationEngine(mode="multi-agent", context=ctx)
            self.citation_engine._connect()

        return self.citation_engine

    def get_or_register_doc_source(self, file_path: str, name: Optional[str] = None) -> int:
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

    async def update_job_status(
        self,
        status: str,
        completed_at: bool = False,
        error_message: Optional[str] = None,
    ) -> bool:
        """Update job status in PostgreSQL (async version).

        FIXED: Converted from sync psycopg2-style to async asyncpg.
        This method now properly uses asyncpg's execute() method instead of
        the old sync cursor() pattern that was causing runtime errors.

        Args:
            status: New status value (e.g., 'completed', 'failed')
            completed_at: Whether to set completed_at to NOW()
            error_message: Optional error message for failed status

        Returns:
            True if update was successful, False otherwise

        Raises:
            ValueError: If no job_id is available
        """
        if not self.job_id:
            raise ValueError("No job_id available for status update")

        if not self.has_postgres():
            return False

        try:
            import logging
            logger = logging.getLogger(__name__)

            # Use asyncpg's execute method (no cursor needed)
            # asyncpg uses $1, $2, $3 placeholders instead of %s
            if completed_at:
                if error_message:
                    await self.postgres_db.execute(
                        """
                        UPDATE jobs
                        SET status = $1, completed_at = NOW(), error_message = $2
                        WHERE id = $3::uuid
                        """,
                        status, error_message, self.job_id,
                    )
                else:
                    await self.postgres_db.execute(
                        """
                        UPDATE jobs
                        SET status = $1, completed_at = NOW()
                        WHERE id = $2::uuid
                        """,
                        status, self.job_id,
                    )
            else:
                await self.postgres_db.execute(
                    """
                    UPDATE jobs
                    SET status = $1
                    WHERE id = $2::uuid
                    """,
                    status, self.job_id,
                )

            logger.info(f"Updated job {self.job_id} status to '{status}'")
            return True

        except Exception as e:
            import logging
            logger = logging.getLogger(__name__)
            logger.error(f"Failed to update job status: {e}")
            return False
