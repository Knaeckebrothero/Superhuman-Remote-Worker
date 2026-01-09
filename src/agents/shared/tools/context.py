"""Tool context for dependency injection.

Provides a container for dependencies that tools need access to,
such as workspace managers, database connections, and configuration.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from src.agents.shared.workspace_manager import WorkspaceManager


@dataclass
class ToolContext:
    """Context container for tool dependencies.

    This class holds all dependencies that tools may need during execution.
    It's passed to tool creation functions to enable dependency injection
    without global state.

    Attributes:
        workspace_manager: WorkspaceManager for file operations
        todo_manager: TodoManager for task tracking (optional)
        postgres_conn: PostgreSQL connection for database operations
        neo4j_conn: Neo4j connection for graph operations
        config: Additional configuration dictionary
        job_id: Override job ID (if not using workspace_manager)

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
    postgres_conn: Optional[Any] = None
    neo4j_conn: Optional[Any] = None
    config: Dict[str, Any] = field(default_factory=dict)
    _job_id: Optional[str] = None  # Direct job_id override

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
        return self.postgres_conn is not None

    def has_neo4j(self) -> bool:
        """Check if Neo4j connection is available."""
        return self.neo4j_conn is not None

    def get_config(self, key: str, default: Any = None) -> Any:
        """Get a configuration value.

        Args:
            key: Configuration key
            default: Default value if key not found

        Returns:
            Configuration value or default
        """
        return self.config.get(key, default)
