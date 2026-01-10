"""Workspace management for agent file-based storage.

This module provides a filesystem-based workspace abstraction for agents.
Each job gets an isolated workspace directory where agents can store plans,
documents, notes, and intermediate work products.

Environment-aware path resolution:
- Container mode: /workspace/job_{uuid}/
- Dev mode: ./workspace/job_{uuid}/
"""

import logging
import os
import shutil
from pathlib import Path
from typing import Optional, List
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class WorkspaceConfig:
    """Configuration for workspace management."""

    # Base path for all workspaces
    base_path: Optional[str] = None

    # Standard subdirectories to create for each job
    structure: List[str] = field(default_factory=lambda: [
        "plans",
        "archive",
        "documents",
        "notes",
        "chunks",
        "candidates",
        "requirements",
        "output",
    ])

    # Template file to copy as instructions.md (optional)
    instructions_template: Optional[str] = None

    @classmethod
    def from_dict(cls, data: dict) -> "WorkspaceConfig":
        """Create config from dictionary."""
        return cls(
            base_path=data.get("base_path"),
            structure=data.get("structure", cls.__dataclass_fields__["structure"].default_factory()),
            instructions_template=data.get("instructions_template"),
        )


def get_workspace_base_path() -> Path:
    """Get the base path for workspaces based on environment.

    Priority:
    1. WORKSPACE_PATH environment variable
    2. /workspace if running in container (detected by existence)
    3. ./workspace in project root for development

    Returns:
        Path to workspace base directory
    """
    # Check environment variable first
    env_path = os.getenv("WORKSPACE_PATH")
    if env_path:
        return Path(env_path)

    # Check if running in container (standard container workspace path)
    container_path = Path("/workspace")
    if container_path.exists() and container_path.is_dir():
        return container_path

    # Development mode: use ./workspace relative to project root
    from src.core.config import get_project_root
    return get_project_root() / "workspace"


class WorkspaceManager:
    """Manages file-based workspaces for agent jobs.

    Each job gets an isolated workspace directory with a standard structure.
    The workspace provides persistent storage for plans, documents, notes,
    and intermediate work products.

    Example:
        ```python
        # Create workspace for a job
        ws = WorkspaceManager(job_id="abc123")
        await ws.initialize()

        # Access paths
        plans_dir = ws.get_path("plans")
        output_file = ws.get_path("output/results.json")

        # Read/write files
        content = ws.read_file("plans/main_plan.md")
        ws.write_file("notes/research.md", "# Research Notes\\n...")

        # List contents
        files = ws.list_files("chunks")

        # Cleanup when done
        ws.cleanup()
        ```
    """

    def __init__(
        self,
        job_id: str,
        config: Optional[WorkspaceConfig] = None,
        base_path: Optional[Path] = None,
    ):
        """Initialize workspace manager.

        Args:
            job_id: Unique job identifier (usually UUID)
            config: Optional workspace configuration
            base_path: Override base path (for testing)
        """
        self.job_id = job_id
        self.config = config or WorkspaceConfig()

        # Determine base path
        if base_path:
            self._base_path = Path(base_path)
        elif self.config.base_path:
            self._base_path = Path(self.config.base_path)
        else:
            self._base_path = get_workspace_base_path()

        # Job-specific workspace path
        self._workspace_path = self._base_path / f"job_{job_id}"
        self._initialized = False

    @property
    def path(self) -> Path:
        """Get the root path of this workspace."""
        return self._workspace_path

    @property
    def is_initialized(self) -> bool:
        """Check if workspace has been initialized."""
        return self._initialized or self._workspace_path.exists()

    def initialize(self) -> None:
        """Initialize the workspace directory structure.

        Creates the workspace root and all configured subdirectories.
        Safe to call multiple times - will not overwrite existing files.
        """
        # Create workspace root
        self._workspace_path.mkdir(parents=True, exist_ok=True)
        logger.info(f"Initialized workspace at {self._workspace_path}")

        # Create subdirectories
        for subdir in self.config.structure:
            dir_path = self._workspace_path / subdir
            dir_path.mkdir(parents=True, exist_ok=True)
            logger.debug(f"Created directory: {subdir}")

        # Copy instructions template if configured
        if self.config.instructions_template:
            self._copy_instructions_template()

        self._initialized = True

    def _copy_instructions_template(self) -> None:
        """Copy instructions template to workspace."""
        from src.core.config import get_project_root

        template_path = get_project_root() / "config" / "prompts" / self.config.instructions_template
        dest_path = self._workspace_path / "instructions.md"

        if template_path.exists() and not dest_path.exists():
            shutil.copy(template_path, dest_path)
            logger.debug(f"Copied instructions template to {dest_path}")
        elif not template_path.exists():
            logger.warning(f"Instructions template not found: {template_path}")

    def get_path(self, relative_path: str = "") -> Path:
        """Get absolute path within workspace.

        Args:
            relative_path: Path relative to workspace root

        Returns:
            Absolute path within workspace

        Raises:
            ValueError: If path attempts to escape workspace
        """
        if not relative_path:
            return self._workspace_path

        # Resolve the path and ensure it stays within workspace
        full_path = (self._workspace_path / relative_path).resolve()
        workspace_resolved = self._workspace_path.resolve()

        # Security check: prevent path traversal
        try:
            full_path.relative_to(workspace_resolved)
        except ValueError:
            raise ValueError(f"Path '{relative_path}' escapes workspace boundary")

        return full_path

    def exists(self, relative_path: str) -> bool:
        """Check if a file or directory exists in workspace.

        Args:
            relative_path: Path relative to workspace root

        Returns:
            True if path exists
        """
        return self.get_path(relative_path).exists()

    def read_file(self, relative_path: str) -> str:
        """Read a file from the workspace.

        Args:
            relative_path: Path relative to workspace root

        Returns:
            File contents as string

        Raises:
            FileNotFoundError: If file doesn't exist
            ValueError: If path escapes workspace
        """
        file_path = self.get_path(relative_path)

        if not file_path.exists():
            raise FileNotFoundError(f"File not found: {relative_path}")

        if not file_path.is_file():
            raise ValueError(f"Not a file: {relative_path}")

        return file_path.read_text(encoding="utf-8")

    def write_file(self, relative_path: str, content: str) -> Path:
        """Write content to a file in the workspace.

        Creates parent directories if they don't exist.

        Args:
            relative_path: Path relative to workspace root
            content: Content to write

        Returns:
            Absolute path to written file

        Raises:
            ValueError: If path escapes workspace
        """
        file_path = self.get_path(relative_path)

        # Create parent directories
        file_path.parent.mkdir(parents=True, exist_ok=True)

        # Write content
        file_path.write_text(content, encoding="utf-8")
        logger.debug(f"Wrote file: {relative_path}")

        return file_path

    def append_file(self, relative_path: str, content: str) -> Path:
        """Append content to a file in the workspace.

        Creates the file if it doesn't exist.

        Args:
            relative_path: Path relative to workspace root
            content: Content to append

        Returns:
            Absolute path to file
        """
        file_path = self.get_path(relative_path)
        file_path.parent.mkdir(parents=True, exist_ok=True)

        with open(file_path, "a", encoding="utf-8") as f:
            f.write(content)

        return file_path

    def delete_file(self, relative_path: str) -> bool:
        """Delete a file or empty directory from workspace.

        Args:
            relative_path: Path relative to workspace root

        Returns:
            True if deleted, False if didn't exist

        Raises:
            ValueError: If trying to delete non-empty directory
        """
        file_path = self.get_path(relative_path)

        if not file_path.exists():
            return False

        if file_path.is_file():
            file_path.unlink()
            logger.debug(f"Deleted file: {relative_path}")
            return True

        if file_path.is_dir():
            if any(file_path.iterdir()):
                raise ValueError(f"Cannot delete non-empty directory: {relative_path}")
            file_path.rmdir()
            logger.debug(f"Deleted directory: {relative_path}")
            return True

        return False

    def list_files(self, relative_path: str = "", pattern: str = "*") -> List[str]:
        """List files in a workspace directory.

        Args:
            relative_path: Path relative to workspace root
            pattern: Glob pattern to filter files (default: "*")

        Returns:
            List of relative paths to files/directories
        """
        dir_path = self.get_path(relative_path)

        if not dir_path.exists():
            return []

        if not dir_path.is_dir():
            return [relative_path]

        results = []
        workspace_resolved = self._workspace_path.resolve()
        for item in dir_path.glob(pattern):
            # Get path relative to workspace root
            rel = item.relative_to(workspace_resolved)
            # Add trailing slash for directories
            if item.is_dir():
                results.append(str(rel) + "/")
            else:
                results.append(str(rel))

        return sorted(results)

    def search_files(self, query: str, path: str = "", case_sensitive: bool = False) -> List[dict]:
        """Search for text in workspace files.

        Args:
            query: Text to search for
            path: Directory to search in (default: entire workspace)
            case_sensitive: Whether search is case-sensitive

        Returns:
            List of dicts with 'path', 'line_number', and 'line' for each match
        """
        search_path = self.get_path(path)
        results = []
        workspace_resolved = self._workspace_path.resolve()

        if not case_sensitive:
            query = query.lower()

        # Recursively search text files
        for file_path in search_path.rglob("*"):
            if not file_path.is_file():
                continue

            # Skip binary files (simple heuristic)
            if file_path.suffix in [".pdf", ".docx", ".png", ".jpg", ".gif", ".zip"]:
                continue

            try:
                content = file_path.read_text(encoding="utf-8")
                lines = content.splitlines()

                for i, line in enumerate(lines, 1):
                    search_line = line if case_sensitive else line.lower()
                    if query in search_line:
                        rel_path = str(file_path.relative_to(workspace_resolved))
                        results.append({
                            "path": rel_path,
                            "line_number": i,
                            "line": line.strip(),
                        })
            except (UnicodeDecodeError, IOError):
                # Skip files that can't be read as text
                continue

        return results

    def get_size(self, relative_path: str = "") -> int:
        """Get size of a file or directory in bytes.

        Args:
            relative_path: Path relative to workspace root

        Returns:
            Size in bytes
        """
        target_path = self.get_path(relative_path)

        if not target_path.exists():
            return 0

        if target_path.is_file():
            return target_path.stat().st_size

        # Sum up all files in directory
        total = 0
        for file_path in target_path.rglob("*"):
            if file_path.is_file():
                total += file_path.stat().st_size

        return total

    def cleanup(self) -> bool:
        """Remove the entire workspace directory.

        Returns:
            True if workspace was removed, False if it didn't exist
        """
        if not self._workspace_path.exists():
            return False

        shutil.rmtree(self._workspace_path)
        logger.info(f"Cleaned up workspace: {self._workspace_path}")
        self._initialized = False

        return True

    def get_summary(self) -> dict:
        """Get a summary of workspace contents.

        Returns:
            Dictionary with file counts and sizes by directory
        """
        summary = {
            "job_id": self.job_id,
            "path": "/",  # Always return "/" - agent sees workspace as root
            "exists": self._workspace_path.exists(),
            "directories": {},
            "total_files": 0,
            "total_size_bytes": 0,
        }

        if not self._workspace_path.exists():
            return summary

        for subdir in self.config.structure:
            dir_path = self._workspace_path / subdir.rstrip("/")
            if dir_path.exists():
                files = list(dir_path.glob("*"))
                file_count = len([f for f in files if f.is_file()])
                dir_size = self.get_size(subdir)

                summary["directories"][subdir] = {
                    "file_count": file_count,
                    "size_bytes": dir_size,
                }
                summary["total_files"] += file_count
                summary["total_size_bytes"] += dir_size

        return summary

    def __repr__(self) -> str:
        return f"WorkspaceManager(job_id='{self.job_id}', path='{self._workspace_path}')"
