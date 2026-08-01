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
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Generator, Optional, List
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from ..managers.git_manager import GitManager
    from .backends.overlay import VirtualOverlayBackend
    from .workspace_backend import WorkspaceBackend

logger = logging.getLogger(__name__)

# Bounded retry for the jobs-repo clone. GitManager.clone returns None on any
# failure without distinguishing transient (a momentary reachability blip during
# an image rollout — the common case) from permanent (bad auth/URL), so we retry
# blindly a few times: a permanent failure only costs a couple of cheap extra
# attempts before the caller's F29 hard-fail. Removes the single-shot clone that
# turned a rollout-window blip into a dead project job.
# docs/done/coincident_infra_error_overrides_reported_job_outcome.md
_CLONE_ATTEMPTS = 3
_CLONE_BACKOFF_SECONDS = (2.0, 5.0)  # waits before attempts 2 and 3


def _clone_repo_with_retry(
    url: str,
    target_path: "Path",
    *,
    backend=None,
    remote_cwd=None,
    attempts: int = _CLONE_ATTEMPTS,
) -> Optional["GitManager"]:
    """Clone with a bounded retry + backoff; returns the GitManager or None.

    ``_CLONE_BACKOFF_SECONDS`` and ``time.sleep`` are read at call time (not bound
    as defaults) so tests can stub the backoff to zero.
    """
    try:
        from ..managers.git_manager import GitManager
    except ImportError:
        from src.managers.git_manager import GitManager

    mgr = None
    for attempt in range(1, attempts + 1):
        mgr = GitManager.clone(url, target_path, backend=backend, remote_cwd=remote_cwd)
        if mgr is not None:
            if attempt > 1:
                logger.info("Clone succeeded on attempt %d/%d", attempt, attempts)
            return mgr
        if attempt < attempts:
            delay = _CLONE_BACKOFF_SECONDS[
                min(attempt - 1, len(_CLONE_BACKOFF_SECONDS) - 1)
            ]
            logger.warning(
                "Clone attempt %d/%d failed; retrying in %.0fs",
                attempt,
                attempts,
                delay,
            )
            time.sleep(delay)
    return mgr


def _normalize_repo_url(url: str) -> str:
    """Normalize a repo URL for equivalence checks.

    Drops credentials (tokens rotate between dispatches), a trailing slash,
    and an optional ``.git`` suffix. Non-URL forms (scp-like ``git@host:path``)
    fall back to the raw string.
    """
    raw = (url or "").strip()
    try:
        parts = urlsplit(raw)
        if not parts.scheme or not parts.hostname:
            return raw
        port = f":{parts.port}" if parts.port else ""
        path = parts.path.rstrip("/")
        if path.endswith(".git"):
            path = path[: -len(".git")]
        return f"{parts.scheme}://{parts.hostname}{port}{path}"
    except ValueError:
        return raw


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


_FALLBACK_BASE_WARNED = False


def get_workspace_base_path() -> Path:
    """Get the base path for the agent's local control-plane state.

    This is the agent process's own state directory (LangGraph checkpoints,
    per-job log files, phase snapshot tarballs) — NOT the job workspace
    where the LLM does its work. Job workspaces are always remote
    (SSH/SFTP into a workspace container/VM); see `WorkspaceManager`.

    Priority:
    1. WORKSPACE_PATH environment variable (set by container/pod manifests)
    2. /workspace if it exists (container/pod with PVC mounted there)
    3. ~/.cache/srw-agent/workspace for bare-metal dev (with a one-time warning)

    Returns:
        Path to workspace base directory
    """
    env_path = os.getenv("WORKSPACE_PATH")
    if env_path:
        return Path(env_path)

    container_path = Path("/workspace")
    if container_path.exists() and container_path.is_dir():
        return container_path

    global _FALLBACK_BASE_WARNED
    fallback = Path.home() / ".cache" / "srw-agent" / "workspace"
    if not _FALLBACK_BASE_WARNED:
        logger.warning(
            "WORKSPACE_PATH not set and /workspace not mounted; "
            "falling back to %s for agent state. "
            "Set WORKSPACE_PATH explicitly to silence this warning.",
            fallback,
        )
        _FALLBACK_BASE_WARNED = True
    return fallback


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

        # Virtual directories: wrap the real backend so registered prefixes
        # (tools/, contacts/, instructions.md) are served from live state.
        # See docs/features/virtual_directories.md.
        self._virtual_overlay: Optional["VirtualOverlayBackend"] = None
        if backend is not None and os.getenv(
            "VIRTUAL_DIRS_ENABLED", "true"
        ).lower() not in ("false", "0", "no"):
            try:
                from .backends.overlay import VirtualOverlayBackend
            except ImportError:
                # Loaded directly (tests use spec_from_file_location to bypass
                # package __init__ side effects), so there is no parent package.
                from src.core.backends.overlay import VirtualOverlayBackend

            self._virtual_overlay = VirtualOverlayBackend(backend)
            self._backend = self._virtual_overlay
        else:
            self._backend = backend

        # Git manager (created during initialize if git_versioning enabled)
        self._git_manager: Optional["GitManager"] = None

        # Source/reference repo git managers, keyed by repo name
        self._source_repos: dict[str, "GitManager"] = {}

        # Forge/auth metadata per cloned repo, keyed like _source_repos
        self._source_repo_meta: dict[str, dict] = {}

    @property
    def backend(self) -> "WorkspaceBackend":
        """Get the workspace backend."""
        return self._backend

    @property
    def virtual_overlay(self):
        """The virtual overlay, or None when VIRTUAL_DIRS_ENABLED is off."""
        return self._virtual_overlay

    def register_virtual_provider(self, provider) -> None:
        """Register a virtual directory provider. No-op when disabled."""
        if self._virtual_overlay is None:
            logger.debug(
                "VIRTUAL_DIRS_ENABLED is off — ignoring provider %s",
                getattr(provider, "prefix", "?"),
            )
            return
        self._virtual_overlay.register(provider)

    def swap_backend(self, new_backend: "WorkspaceBackend") -> None:
        """Replace the real backend, keeping the virtual overlay in front of it.

        Workspace-tier upgrades (virtual -> sandbox in ``src/agent.py``,
        container -> VM in ``src/api/persistent_session.py``) hand the manager a
        different backend mid-run. Assigning ``self._backend`` directly drops
        the overlay: ``_virtual_overlay`` keeps wrapping the OLD, now
        disconnected backend, ``backend`` becomes the raw new one, and every
        virtual path 404s from then on — including the deferred-tool full docs.
        Re-registering providers cannot repair that, because they land on an
        overlay nothing reads through any more.

        Rebinding keeps the same overlay object (and therefore every registered
        provider) in place over the new backend.

        Args:
            new_backend: The connected replacement backend. Must be a real
                backend, never another overlay.
        """
        if new_backend is None:
            raise TypeError("swap_backend requires a backend")
        # Defensive: an overlay-in-an-overlay would double-route virtual paths.
        # Via the isinstance-typed helper, never getattr(_, "inner", _): a
        # MagicMock auto-creates `.inner`, so duck-typing here would swap in a
        # child mock instead of the backend the caller passed.
        try:
            from .backends.overlay import unwrap_backend
        except ImportError:  # module loaded directly — see __init__
            from src.core.backends.overlay import unwrap_backend

        new_backend = unwrap_backend(new_backend)
        if self._virtual_overlay is not None:
            self._virtual_overlay.rebind(new_backend)
            self._backend = self._virtual_overlay
        else:
            self._backend = new_backend

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
    def source_repo_meta(self) -> dict[str, dict]:
        """Forge/auth metadata per cloned repo, keyed like ``source_repos``.

        ``source_repos`` holds GitManager instances, which know how to run git
        but nothing about the forge's API. The repo tools need both.
        """
        return self._source_repo_meta

    def get_head_commit(self) -> Optional[str]:
        """Best-effort HEAD commit SHA at the workspace root.

        Used by the critic verdict tools to detect whether a target job's
        deliverable changed between review rounds (progress detection).
        Deliberately independent of ``git_manager``/``git_versioning``: a repo
        can be present at the workspace root (e.g. a project job's cloned jobs
        repo) even when workspace-level versioning was never enabled, so this
        always probes the root directly rather than relying on
        ``self._git_manager`` being set.

        This is a heuristic, not a correctness dependency — it must never
        raise. Any failure (no repo, git unavailable, backend error) returns
        None.
        """
        try:
            from ..managers.git_manager import GitManager
        except ImportError:
            from src.managers.git_manager import GitManager

        try:
            return GitManager(
                self._workspace_path, backend=self._backend
            ).get_current_commit()
        except Exception:
            return None

    def get_content_tree(self) -> Optional[str]:
        """Best-effort content hash of the committed workspace at HEAD.

        This — not ``get_head_commit`` — is what the verification gate's
        no-progress guard compares between rounds. A commit SHA moves on every
        round (both freeze branches commit with ``allow_empty=True``) and
        reverts on a re-clone after a failed push, so it is simultaneously
        unable to fire and able to fire backwards. See
        :meth:`GitManager.get_content_tree`.

        Probes the workspace root directly for the same reason
        ``get_head_commit`` does: a repo can be present there even when
        workspace-level versioning was never enabled. Never raises — any
        failure (no repo, git unavailable, backend error) returns None.
        """
        try:
            from ..managers.git_manager import GitManager
        except ImportError:
            from src.managers.git_manager import GitManager

        try:
            return GitManager(
                self._workspace_path, backend=self._backend
            ).get_content_tree()
        except Exception:
            return None

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

        # 1. Reuse or clone the jobs repo as workspace root.
        #
        # A FIRST dispatch can land on an already-populated root: on the
        # container backend the scholar subjob provisions the parent's shared
        # workspace pod and fully initializes it (jobs-repo clone + research)
        # before the parent ever runs, and critic/product-qa subjobs inherit a
        # populated parent workspace the same way. The agent-side reattach
        # gates are resume-only, so this path must cope on its own: cloning
        # into a non-empty root can never succeed, and retrying it just
        # produces the misleading reachability error below.
        git_mgr = self._attach_existing_jobs_repo(jobs_repo)

        # Empty root: clone (bounded retry+backoff — a momentary reachability
        # blip during an image rollout must not hard-fail the job on the first
        # miss; docs/done/coincident_infra_error_overrides_reported_job_outcome.md)
        if git_mgr is None:
            git_mgr = _clone_repo_with_retry(
                jobs_repo["repo_url"], self._workspace_path, backend=self._backend
            )
        if not git_mgr:
            # F29 hardening: do NOT silently git-init here. A project job whose
            # jobs repo won't clone is disconnected from `main`; proceeding would
            # rebuild from scratch in a throwaway local git and lose every push
            # on teardown — the exact run-5/6 failure, invisible because it was a
            # warning. Fail loud instead: the raise propagates to agent._run's
            # job-error handler (status=failed), so the loop advance counts it and
            # rotates on rather than burning a night on work that can't land.
            # (repo name only in the message — repo_url carries credentials.)
            repo_name = jobs_repo.get("name", "<unknown>")
            raise RuntimeError(
                f"Failed to clone project jobs repo '{repo_name}' — refusing to "
                "fall back to a disconnected git init (work would be lost on "
                "teardown). Check jobs-repo URL reachability from this backend."
            )

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

    def _attach_existing_jobs_repo(self, jobs_repo: dict) -> Optional["GitManager"]:
        """Attach to a pre-existing jobs-repo clone at the workspace root.

        Returns None when the root is empty (the normal clone path applies).
        Raises RuntimeError when the root is populated but cannot be reused:
        a clone could never succeed there, so fail immediately with an
        accurate message instead of burning the clone retries and reporting
        a reachability problem that doesn't exist.
        """
        try:
            from ..managers.git_manager import GitManager
        except ImportError:
            from src.managers.git_manager import GitManager

        repo_name = jobs_repo.get("name", "<unknown>")
        expected_url = jobs_repo["repo_url"]

        if not self._backend.exists(".git"):
            try:
                entries = self._backend.list_dir("")
            except Exception as e:
                logger.warning(
                    f"Workspace root listing failed ({e}); assuming empty root"
                )
                entries = []
            if entries:
                raise RuntimeError(
                    f"Workspace root is not empty ({len(entries)} entries) and has "
                    f"no git repo — refusing to clone jobs repo '{repo_name}' over "
                    "existing content. This usually means a pre-seeded or inherited "
                    "workspace was dispatched without resume handling."
                )
            return None

        git_mgr = GitManager(self._workspace_path, backend=self._backend)
        origin = git_mgr.remote_url("origin")
        if origin and _normalize_repo_url(origin) != _normalize_repo_url(expected_url):
            # A PRESENT-but-different origin is a genuine identity conflict:
            # pushing would land this job's work in a foreign repo. Fail loud.
            raise RuntimeError(
                f"Workspace root holds a git repo whose origin "
                f"({GitManager._mask_url_static(origin)}) does not match "
                f"project jobs repo '{repo_name}' — refusing to clone over "
                "it or push into it."
            )
        if not origin:
            # Unset/unreadable origin is NOT a conflict: `git remote get-url`
            # can fail transiently on a fresh session, and killing the job
            # here would lose the pre-seeded work (the exact failure F29
            # guards against). Log the raw git result for diagnosis, then
            # reuse the repo — add_remote below restores push connectivity.
            probe = git_mgr._run_git(["remote", "get-url", "origin"])
            logger.warning(
                "Existing jobs repo at workspace root has no readable origin "
                "(rc=%s, stdout=%r, stderr=%r) — attaching and setting origin "
                "to the project jobs repo '%s'",
                probe.returncode,
                GitManager._mask_url_static(probe.stdout.strip())[-200:],
                GitManager._mask_url_static(probe.stderr.strip())[-200:],
                repo_name,
            )
        # Refresh origin so credentials rotated since the original clone
        # land in the repo config before any push.
        git_mgr.add_remote("origin", expected_url)
        logger.info(
            "Reusing existing jobs-repo clone at workspace root "
            "(pre-initialized workspace, e.g. scholar-provisioned pod)"
        )
        return git_mgr

    def _clone_auxiliary_repos(self) -> None:
        """Clone source/reference repositories into repos/ subdirectory.

        Two roles are deliberately never cloned: ``jobs`` (it *is* the
        workspace root) and ``knowledge`` (the KB vault is server-side —
        notes are read through the KB tools and written via the
        orchestrator's materialisation endpoint, so a checkout would be a
        stale second copy nobody reads;
        docs/features/knowledge_base_repo_separation.md §3).
        """
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
            if repo["role"] == "knowledge":
                continue  # KB vault is server-side; never checked out

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
        self,
        query: str,
        path: str = "",
        case_sensitive: bool = False,
        exclude_dirs: list[str] | None = None,
    ) -> List[dict]:
        """Search for text in workspace files.

        Args:
            query: Text to search for
            path: Directory to search in (default: entire workspace)
            case_sensitive: Whether search is case-sensitive
            exclude_dirs: Optional list of directory names to skip with grep

        Returns:
            List of dicts with 'path', 'line_number', and 'line' for each match
        """
        return self._backend.search_files(
            query,
            path,
            case_sensitive,
            exclude_dirs=exclude_dirs,
        )

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
