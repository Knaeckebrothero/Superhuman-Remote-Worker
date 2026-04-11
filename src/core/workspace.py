"""Workspace management for agent file-based storage.

This module provides a filesystem-based workspace abstraction for agents.
Each job gets an isolated workspace (container or VM) where agents can
store plans, documents, notes, and intermediate work products.

The workspace IS the base path — no per-job subdirectories. Isolation is
provided by the container/VM boundary, not directory structure.

Git versioning:
- Optional git repository per workspace for automatic change tracking
- Commits on todo completion for audit trail
- Phase tags for milestone tracking

Backend abstraction:
- File I/O is delegated to a WorkspaceBackend implementation
- RemoteBackend (SSH/SFTP) is the only production backend
- See docs/features/vm_backend.md for the full design
"""

import logging
import os
import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Generator, Optional, List

if TYPE_CHECKING:
    from ..managers.git_manager import GitManager
    from .workspace_backend import WorkspaceBackend

logger = logging.getLogger(__name__)


@dataclass
class WorkspaceManagerConfig:
    """Configuration for WorkspaceManager.

    Note: This is distinct from loader.py:WorkspaceConfig which is used
    for AgentConfig JSON parsing. This class configures the runtime
    WorkspaceManager behavior.
    """

    # Base path for all workspaces
    base_path: Optional[str] = None

    # Standard subdirectories to create for each job
    structure: List[str] = field(
        default_factory=lambda: [
            "archive",
            "documents",
            "chunks",
            "candidates",
            "requirements",
            "output",
        ]
    )

    # Git versioning settings
    git_versioning: bool = True  # Enable git versioning for workspace history

    # Git remote URL for workspace delivery (set by orchestrator via Gitea)
    git_remote_url: Optional[str] = None

    # Project repository info
    branch_name: Optional[str] = None
    repositories: Optional[List[dict]] = None

    @classmethod
    def from_dict(cls, data: dict) -> "WorkspaceManagerConfig":
        """Create config from dictionary."""
        return cls(
            base_path=data.get("base_path"),
            structure=data.get(
                "structure", cls.__dataclass_fields__["structure"].default_factory()
            ),
            git_versioning=data.get("git_versioning", True),
            git_remote_url=data.get("git_remote_url"),
            branch_name=data.get("branch_name"),
            repositories=data.get("repositories"),
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
    from src.utils.config import get_project_root

    return get_project_root() / "workspace"


def get_checkpoints_path() -> Path:
    """Get path for LangGraph checkpoint storage.

    Checkpoints are stored in a shared directory outside individual job workspaces:
        workspace/checkpoints/job_<id>.db

    Returns:
        Path to checkpoints directory (created if it doesn't exist)
    """
    base = get_workspace_base_path()
    checkpoints_dir = base / "checkpoints"
    checkpoints_dir.mkdir(parents=True, exist_ok=True)
    return checkpoints_dir


def get_logs_path() -> Path:
    """Get path for job log file storage.

    Logs are stored in a shared directory outside individual job workspaces:
        workspace/logs/job_<id>.log

    Returns:
        Path to logs directory (created if it doesn't exist)
    """
    base = get_workspace_base_path()
    logs_dir = base / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    return logs_dir


class WorkspaceManager:
    """Manages file-based workspaces for agent jobs.

    Each job gets an isolated workspace directory with a standard structure.
    The workspace provides persistent storage for plans, documents, notes,
    and intermediate work products.

    File I/O is delegated to a WorkspaceBackend instance, which the caller
    must provide. Production constructs a RemoteBackend that SSHes into a
    workspace container or VM. Tests use a filesystem-backed backend from
    tests/_fs_backend.py.

    Example:
        ```python
        # Create workspace for a job
        ws = WorkspaceManager(job_id="abc123", backend=backend)
        await ws.initialize()

        # Access paths
        plans_dir = ws.get_path("plans")
        output_file = ws.get_path("output/results.json")

        # Read/write files
        content = ws.read_file("plan.md")
        ws.write_file("research.md", "# Research Notes\\n...")

        # List contents
        files = ws.list_files("chunks")

        # Cleanup when done
        ws.cleanup()
        ```
    """

    def __init__(
        self,
        job_id: str,
        backend: "WorkspaceBackend",
        config: Optional[WorkspaceManagerConfig] = None,
        base_path: Optional[Path] = None,
    ):
        """Initialize workspace manager.

        Args:
            job_id: Unique job identifier (usually UUID)
            backend: Workspace backend. Production must pass a RemoteBackend;
                     the agent process never operates on its own filesystem.
            config: Optional workspace configuration
            base_path: Override base path (for testing)
        """
        if backend is None:
            raise TypeError(
                "WorkspaceManager requires a backend. Production must pass a "
                "RemoteBackend; tests should import FilesystemTestBackend from "
                "tests/_fs_backend.py."
            )

        self.job_id = job_id
        self.config = config or WorkspaceManagerConfig()

        # Determine base path
        if base_path:
            self._base_path = Path(base_path)
        elif self.config.base_path:
            self._base_path = Path(self.config.base_path)
        else:
            self._base_path = get_workspace_base_path()

        # Workspace path — the base path IS the workspace.
        # Isolation is provided by the container/VM, not subdirectories.
        self._workspace_path = self._base_path
        self._initialized = False

        self._backend = backend

        # Git manager (created during initialize if git_versioning enabled)
        self._git_manager: Optional["GitManager"] = None

        # Source/reference repo git managers, keyed by repo name
        self._source_repos: dict[str, "GitManager"] = {}

    @property
    def backend(self) -> "WorkspaceBackend":
        """Get the workspace backend."""
        return self._backend

    @property
    def path(self) -> Path:
        """Get the root path of this workspace."""
        return self._workspace_path

    @property
    def is_initialized(self) -> bool:
        """Check if workspace has been initialized."""
        return self._initialized or self._workspace_path.exists()

    @property
    def git_manager(self) -> Optional["GitManager"]:
        """Get the GitManager for this workspace.

        Returns None if git versioning is not enabled or initialization failed.
        """
        return self._git_manager

    @property
    def source_repos(self) -> dict[str, "GitManager"]:
        """Git managers for source/reference repos, keyed by repo name."""
        return self._source_repos

    @property
    def _backend_has_shell(self) -> bool:
        """Check if the workspace backend supports shell execution."""
        return getattr(self._backend, "supports_shell", False)

    def initialize(self) -> None:
        """Initialize the workspace directory structure.

        Creates the workspace root and all configured subdirectories.
        Safe to call multiple times - will not overwrite existing files.

        If git_versioning is enabled, also:
        - Creates a GitManager instance
        - Initializes a git repository with .gitignore
        - Creates initial phase_state.yaml
        - Makes an initial commit

        When a git_remote_url is configured, the workspace is cloned from
        the remote BEFORE creating subdirectories so that the local history
        extends the remote's (required for pushes and subjob branches).
        """
        # When we have a remote, clone first (needs empty/non-existent dir),
        # then layer subdirectories on top.
        if self.config.git_versioning and self.config.git_remote_url:
            if not self._backend_has_shell:
                self._workspace_path.parent.mkdir(parents=True, exist_ok=True)
            else:
                # Remote backend: workspace dir may have leftover files from a
                # previous session (static container pool reuse). Clear it so
                # git clone has an empty target directory.
                self._backend.shell_run(
                    f"rm -rf {self._backend.root}/* {self._backend.root}/.[!.]* 2>/dev/null || true",
                    timeout=30,
                    tab_name="git",
                )
            self._initialize_git()
            # Create any subdirectories the clone didn't provide
            for subdir in self.config.structure:
                self._backend.mkdir(subdir)
            self._initialized = True
            return

        # No remote — standard path: create dirs, then git init
        if self._backend_has_shell:
            # Remote backend: workspace dir may have leftover files from a
            # previous session (static container pool reuse). Clear it so
            # the new session starts with a clean workspace.
            self._backend.shell_run(
                f"rm -rf {self._backend.root}/* {self._backend.root}/.[!.]* 2>/dev/null || true",
                timeout=30,
                tab_name="init",
            )
        self._workspace_path.mkdir(parents=True, exist_ok=True)
        logger.info(f"Initialized workspace at {self._workspace_path}")

        for subdir in self.config.structure:
            self._backend.mkdir(subdir)

        if self.config.git_versioning:
            self._initialize_git()

        self._initialized = True

    def _initialize_git(self) -> None:
        """Initialize git versioning for the workspace.

        If a git_remote_url is configured, clones the remote repo so that
        local history extends the remote's — this ensures pushes succeed and
        subjob branches (forked from the remote) inherit the workspace files.

        Falls back to ``git init`` + ``add_remote`` only when no remote URL
        is available.
        """
        try:
            from ..managers.git_manager import GitManager
        except ImportError:
            from src.managers.git_manager import GitManager

        if self.config.git_remote_url:
            # Clone from remote so histories stay connected.
            git_mgr = GitManager.clone(
                self.config.git_remote_url,
                self._workspace_path,
                backend=self._backend,
            )
            if git_mgr:
                self._git_manager = git_mgr
                # Checkout job branch if specified
                if self.config.branch_name:
                    if git_mgr.checkout_branch(self.config.branch_name, create=True):
                        logger.info(f"Checked out branch: {self.config.branch_name}")
                    else:
                        logger.warning(
                            f"Failed to checkout branch {self.config.branch_name}"
                        )
                logger.info("Git versioning enabled (cloned from remote)")
                return
            # Clone failed — fall through to git init
            logger.warning("Failed to clone remote repo, falling back to git init")

        # No remote URL or clone failed: plain git init
        self._git_manager = GitManager(self._workspace_path, backend=self._backend)
        success = self._git_manager.init_repository()

        if success:
            logger.info("Git versioning enabled for workspace")
            if self.config.git_remote_url:
                self._git_manager.add_remote("origin", self.config.git_remote_url)
        else:
            logger.warning("Failed to initialize git repository")
            self._git_manager = None

    def initialize_project_workspace(self) -> None:
        """Initialize workspace from project repositories.

        For project jobs, the jobs repo IS the workspace root:
        1. Clone jobs repo → workspace root
        2. Checkout job branch (create if needed)
        3. Create subdirectories
        4. Clone source/reference repos → repos/ subdirectory
        5. Update .gitignore to exclude cloned repos
        """
        if not self.config.repositories:
            logger.warning(
                "initialize_project_workspace called without repositories, falling back"
            )
            self.initialize()
            return

        # Find the jobs repo
        jobs_repo = next(
            (r for r in self.config.repositories if r["role"] == "jobs"),
            None,
        )
        if not jobs_repo or not jobs_repo.get("repo_url"):
            logger.warning(
                "No jobs repo found in repositories, falling back to standard init"
            )
            self.initialize()
            return

        try:
            from ..managers.git_manager import GitManager
        except ImportError:
            from src.managers.git_manager import GitManager

        # 1. Clone jobs repo as workspace root
        git_mgr = GitManager.clone(
            jobs_repo["repo_url"], self._workspace_path, backend=self._backend
        )
        if not git_mgr:
            logger.warning("Failed to clone jobs repo, falling back to standard init")
            self.initialize()
            return

        self._git_manager = git_mgr
        logger.info(f"Cloned jobs repo as workspace root: {self._workspace_path}")

        # 2. Checkout job branch
        branch = self.config.branch_name
        if branch:
            success = git_mgr.checkout_branch(branch, create=True)
            if success:
                logger.info(f"Checked out branch: {branch}")
            else:
                logger.warning(
                    f"Failed to checkout branch {branch}, continuing on default"
                )

        # 3. Create subdirectories
        for subdir in self.config.structure:
            self._backend.mkdir(subdir)

        # 4. Clone source/reference repos
        self._clone_auxiliary_repos()

        self._initialized = True
        logger.info("Project workspace initialized successfully")

    def _clone_auxiliary_repos(self) -> None:
        """Clone source/reference repositories into repos/ subdirectory."""
        if not self.config.repositories:
            return

        try:
            from ..managers.git_manager import GitManager
        except ImportError:
            from src.managers.git_manager import GitManager

        self._backend.mkdir("repos")
        repos_dir = self._workspace_path / "repos"

        for repo in self.config.repositories:
            if repo["role"] == "jobs":
                continue  # Jobs repo IS the workspace root

            repo_name = repo["name"]
            target = repos_dir / repo_name
            remote_cwd = f"repos/{repo_name}"

            if self._backend.exists(remote_cwd):
                logger.debug(f"Repo {repo_name} already cloned, skipping")
                continue

            repo_url = repo.get("repo_url")
            if not repo_url:
                logger.warning(f"Repo {repo_name} has no URL, skipping")
                continue

            try:
                git_mgr = GitManager.clone(
                    repo_url,
                    target,
                    backend=self._backend,
                    remote_cwd=remote_cwd,
                )
                if git_mgr:
                    branch = repo.get("branch", "main")
                    if branch and branch != "main":
                        git_mgr.checkout_branch(branch)
                    self._source_repos[repo_name] = git_mgr
                    logger.info(f"Cloned {repo['role']} repo: {repo_name}")
                else:
                    logger.warning(f"Failed to clone repo: {repo_name}")
            except Exception as e:
                logger.warning(f"Error cloning repo {repo_name}: {e}")

        # Update .gitignore to exclude repos/ directory
        if self._backend.exists(".gitignore"):
            content = self._backend.read_file(".gitignore")
            if "repos/" not in content:
                self._backend.append_file(
                    ".gitignore", "\n# Cloned project repositories\nrepos/\n"
                )
        else:
            self._backend.write_file(
                ".gitignore", "# Cloned project repositories\nrepos/\n"
            )

        if self._git_manager:
            self._git_manager.commit("Add repos/ to .gitignore", allow_empty=False)

    def get_path(self, relative_path: str = "") -> Path:
        """Get absolute path within workspace.

        Delegates path validation to the backend, returns a Path object
        for backward compatibility with callers that need filesystem paths.

        Args:
            relative_path: Path relative to workspace root

        Returns:
            Absolute path within workspace

        Raises:
            ValueError: If path attempts to escape workspace
        """
        return Path(self._backend.resolve_path(relative_path))

    @contextmanager
    def local_copy(self, relative_path: str) -> Generator[Path, None, None]:
        """Yield a local filesystem path to a workspace file.

        For local backends the resolved path already exists on the local
        filesystem, so it is yielded directly (no copy, no cleanup).

        For remote backends the file is downloaded via SFTP to a temporary
        file which is cleaned up when the context manager exits.

        Use this whenever a service (AudioHelper, VisionHelper, PDFReader,
        etc.) needs to open a file with local I/O.

        Args:
            relative_path: Path relative to workspace root

        Yields:
            A Path on the local filesystem containing the file data
        """
        local_path = self.get_path(relative_path)
        if local_path.exists():
            yield local_path
            return

        # Remote file — download to a local temp file
        data = self._backend.read_file(relative_path, binary=True)
        suffix = Path(relative_path).suffix
        tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
        try:
            tmp.write(data)
            tmp.close()
            yield Path(tmp.name)
        finally:
            Path(tmp.name).unlink(missing_ok=True)

    def exists(self, relative_path: str) -> bool:
        """Check if a file or directory exists in workspace.

        Args:
            relative_path: Path relative to workspace root

        Returns:
            True if path exists
        """
        return self._backend.exists(relative_path)

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
        return self._backend.read_file(relative_path)

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
        self._backend.write_file(relative_path, content)
        return self.get_path(relative_path)

    def append_file(self, relative_path: str, content: str) -> Path:
        """Append content to a file in the workspace.

        Creates the file if it doesn't exist.

        Args:
            relative_path: Path relative to workspace root
            content: Content to append

        Returns:
            Absolute path to file
        """
        self._backend.append_file(relative_path, content)
        return self.get_path(relative_path)

    def create_directory(self, relative_path: str) -> Path:
        """Create a directory (and parents) in workspace.

        Args:
            relative_path: Path relative to workspace root

        Returns:
            Absolute path to created directory

        Raises:
            ValueError: If path escapes workspace
        """
        self._backend.mkdir(relative_path)
        return self.get_path(relative_path)

    def delete_directory(self, relative_path: str) -> bool:
        """Delete a directory and all its contents.

        Args:
            relative_path: Path relative to workspace root

        Returns:
            True if deleted, False if didn't exist

        Raises:
            ValueError: If path escapes workspace or is the workspace root
        """
        return self._backend.delete_directory(relative_path)

    def delete_file(self, relative_path: str) -> bool:
        """Delete a file or empty directory from workspace.

        Args:
            relative_path: Path relative to workspace root

        Returns:
            True if deleted, False if didn't exist

        Raises:
            ValueError: If trying to delete non-empty directory
        """
        return self._backend.delete_file(relative_path)

    def move_file(self, source: str, dest: str) -> Path:
        """Move a file or directory within the workspace.

        Creates parent directories for destination if needed.
        Can also be used to rename files.

        Args:
            source: Source path relative to workspace root
            dest: Destination path relative to workspace root

        Returns:
            Absolute path to the moved file/directory

        Raises:
            FileNotFoundError: If source doesn't exist
            ValueError: If paths escape workspace boundary
        """
        self._backend.move(source, dest)
        return self.get_path(dest)

    def copy_file(self, source: str, dest: str) -> Path:
        """Copy a file within the workspace.

        Creates parent directories for destination if needed.

        Args:
            source: Source path relative to workspace root
            dest: Destination path relative to workspace root

        Returns:
            Absolute path to the copied file

        Raises:
            FileNotFoundError: If source doesn't exist
            ValueError: If paths escape workspace boundary or source is a directory
        """
        self._backend.copy(source, dest)
        return self.get_path(dest)

    def list_files(self, relative_path: str = "", pattern: str = "*") -> List[str]:
        """List files in a workspace directory.

        Args:
            relative_path: Path relative to workspace root
            pattern: Glob pattern to filter files (default: "*")

        Returns:
            List of relative paths to files/directories
        """
        return self._backend.list_dir(relative_path, pattern)

    def search_files(
        self, query: str, path: str = "", case_sensitive: bool = False
    ) -> List[dict]:
        """Search for text in workspace files.

        Args:
            query: Text to search for
            path: Directory to search in (default: entire workspace)
            case_sensitive: Whether search is case-sensitive

        Returns:
            List of dicts with 'path', 'line_number', and 'line' for each match
        """
        return self._backend.search_files(query, path, case_sensitive)

    def get_size(self, relative_path: str = "") -> int:
        """Get size of a file or directory in bytes.

        Args:
            relative_path: Path relative to workspace root

        Returns:
            Size in bytes
        """
        return self._backend.stat(relative_path)

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
            subdir_clean = subdir.rstrip("/")
            if self._backend.is_dir(subdir_clean):
                entries = self._backend.list_dir(subdir_clean)
                # Count only files (entries without trailing /)
                file_count = len([e for e in entries if not e.endswith("/")])
                dir_size = self._backend.stat(subdir_clean)

                summary["directories"][subdir] = {
                    "file_count": file_count,
                    "size_bytes": dir_size,
                }
                summary["total_files"] += file_count
                summary["total_size_bytes"] += dir_size

        return summary

    def __repr__(self) -> str:
        return (
            f"WorkspaceManager(job_id='{self.job_id}', path='{self._workspace_path}')"
        )
