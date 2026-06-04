"""Git manager for workspace versioning.

Provides a GitManager class that encapsulates all git operations for
agent workspaces. This enables automatic versioning of workspace changes,
queryable history, and phase tracking via git tags.

Usage:
    from src.managers import GitManager

    git_mgr = GitManager(workspace_path)
    if git_mgr.is_active:
        git_mgr.commit("Complete todo_1: Extract requirements")
        git_mgr.tag("phase-1-tactical-complete")
"""

import logging
import posixpath
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)


class GitManager:
    """Manages git operations for a workspace directory.

    This class provides a high-level interface for git operations within
    agent workspaces. It handles:
    - Repository initialization with .gitignore
    - Auto-commits on todo completion
    - History queries (log, show, diff)
    - Phase tracking via tags

    The manager gracefully degrades when git is not available - all methods
    return appropriate defaults rather than raising exceptions.

    Attributes:
        DEFAULT_TIMEOUT: Subprocess timeout for git commands (seconds)
    """

    # Subprocess timeout for git commands (seconds)
    DEFAULT_TIMEOUT = 60

    # Default truncation limits
    DEFAULT_MAX_LINES = 500
    DEFAULT_MAX_WORDS = 10000

    def __init__(self, workspace_path: Path, backend=None, remote_cwd=None):
        """Initialize GitManager for a workspace directory.

        Args:
            workspace_path: Path to the workspace directory
            backend: Optional WorkspaceBackend instance. When provided and
                the backend supports shell execution, git commands are
                delegated to the backend (e.g. via SSH on a remote VM)
                instead of running locally via subprocess.
            remote_cwd: Relative path within the backend root to use as the
                git working directory. Used for auxiliary repos cloned into
                subdirectories (e.g. "repos/my-repo"). When None, commands
                run in the backend's root directory.
        """
        self._workspace_path = Path(workspace_path)
        self._backend = backend
        self._remote_cwd = remote_cwd
        self._use_backend = backend is not None and getattr(
            backend, "supports_shell", False
        )

        if self._use_backend:
            # Git is available on the remote — skip local check
            self._git_available = True
        else:
            self._git_available = self._check_git_available()

        if not self._git_available:
            logger.warning("Git not available, workspace versioning disabled")

    @property
    def is_active(self) -> bool:
        """Check if git versioning is active for this workspace.

        Returns True only if git is available AND the workspace has been
        initialized as a git repository.
        """
        if not self._git_available:
            return False
        if self._use_backend:
            git_path = (
                posixpath.join(self._remote_cwd, ".git") if self._remote_cwd else ".git"
            )
            return self._backend.exists(git_path)
        return (self._workspace_path / ".git").exists()

    @property
    def workspace_path(self) -> Path:
        """Get the workspace path."""
        return self._workspace_path

    # Default .gitignore patterns for workspace repositories
    DEFAULT_IGNORE_PATTERNS = [
        "*.db",
        "*.log",
        "__pycache__/",
        ".DS_Store",
        "*.pyc",
    ]

    def init_repository(self) -> bool:
        """Initialize git repository with .gitignore.

        Creates a new git repository in the workspace directory, configures
        a local git user, creates .gitignore with sensible defaults, and
        makes an initial commit.

        If a .git directory already exists, this is a no-op and returns True.

        Returns:
            True if successful or already initialized, False on error
        """
        if not self._git_available:
            logger.warning("Cannot initialize repository: git not available")
            return False

        # Skip if already initialized
        if self._use_backend:
            git_path = (
                posixpath.join(self._remote_cwd, ".git") if self._remote_cwd else ".git"
            )
            already_init = self._backend.exists(git_path)
        else:
            already_init = (self._workspace_path / ".git").exists()

        if already_init:
            logger.info("Git repository already exists, skipping initialization")
            return True

        try:
            # Initialize repository
            result = self._run_git(["init"])
            if result.returncode != 0:
                logger.error(f"git init failed: {result.stderr}")
                return False

            # Configure local git user (avoid global config issues)
            self._run_git(["config", "user.email", "agent@workspace.local"])
            self._run_git(["config", "user.name", "Agent"])

            # Create .gitignore with sensible defaults
            gitignore_content = "\n".join(self.DEFAULT_IGNORE_PATTERNS) + "\n"
            if self._use_backend:
                gitignore_path = (
                    posixpath.join(self._remote_cwd, ".gitignore")
                    if self._remote_cwd
                    else ".gitignore"
                )
                self._backend.write_file(gitignore_path, gitignore_content)
            else:
                gitignore_path = self._workspace_path / ".gitignore"
                gitignore_path.write_text(gitignore_content)

            # Stage all files
            result = self._run_git(["add", "-A"])
            if result.returncode != 0:
                logger.warning(f"git add failed: {result.stderr}")

            # Create initial commit
            result = self._run_git(
                ["commit", "-m", "Initialize workspace", "--allow-empty"]
            )
            if result.returncode != 0:
                logger.error(f"Initial commit failed: {result.stderr}")
                return False

            logger.info(f"Initialized git repository in {self._workspace_path}")
            return True

        except Exception as e:
            logger.error(f"Failed to initialize git repository: {e}")
            return False

    def commit(self, message: str, allow_empty: bool = True) -> bool:
        """Stage all changes and commit.

        Stages all modified, added, and deleted files, then creates a commit
        with the provided message.

        Args:
            message: Commit message
            allow_empty: If True, allow commits with no changes (default: True)
                        Empty commits maintain the audit trail even for
                        read-only analysis tasks.

        Returns:
            True if commit succeeded, False otherwise
        """
        if not self.is_active:
            logger.debug("Git not active, skipping commit")
            return False

        try:
            # Stage all changes
            result = self._run_git(["add", "-A"])
            if result.returncode != 0:
                logger.warning(f"git add failed: {result.stderr}")
                return False

            # Create commit
            args = ["commit", "-m", message]
            if allow_empty:
                args.append("--allow-empty")

            result = self._run_git(args)

            # Check for "nothing to commit" which is OK if allow_empty=False
            if result.returncode != 0:
                if (
                    "nothing to commit" in result.stdout
                    or "nothing to commit" in result.stderr
                ):
                    if allow_empty:
                        logger.debug("No changes to commit (allow_empty=True)")
                        return True
                    else:
                        logger.debug("No changes to commit")
                        return False
                logger.warning(f"git commit failed: {result.stderr}")
                return False

            logger.debug(f"Committed: {message[:50]}...")
            return True

        except Exception as e:
            logger.error(f"Failed to commit: {e}")
            return False

    def log(self, max_count: int = 10, oneline: bool = True) -> str:
        """Get commit history.

        Args:
            max_count: Maximum number of commits to return (default: 10)
            oneline: If True, show compact one-line format (default: True)

        Returns:
            Formatted commit history, or error message if failed
        """
        if not self.is_active:
            return "Git versioning not available for this workspace"

        try:
            args = ["log", f"-{max_count}"]
            if oneline:
                args.append("--oneline")

            result = self._run_git(args)
            if result.returncode != 0:
                return f"Error: {result.stderr}"

            output = result.stdout.strip()
            return output if output else "No commits yet"

        except Exception as e:
            return f"Error getting log: {e}"

    def show(
        self,
        commit_ref: str = "HEAD",
        stat_only: bool = False,
        max_lines: int = DEFAULT_MAX_LINES,
    ) -> str:
        """Show commit details.

        Args:
            commit_ref: Commit reference (default: "HEAD")
            stat_only: If True, show only file statistics without diff
            max_lines: Maximum output lines before truncation (default: 500)

        Returns:
            Commit details and diff, or error message if failed
        """
        if not self.is_active:
            return "Git versioning not available for this workspace"

        try:
            args = ["show", commit_ref]
            if stat_only:
                args.append("--stat")

            result = self._run_git(args)
            if result.returncode != 0:
                return f"Error: {result.stderr}"

            return self._truncate_output(result.stdout, max_lines=max_lines)

        except Exception as e:
            return f"Error showing commit: {e}"

    def diff(
        self,
        ref1: Optional[str] = None,
        ref2: Optional[str] = None,
        file_path: Optional[str] = None,
        max_lines: int = DEFAULT_MAX_LINES,
    ) -> str:
        """Show differences.

        Supports multiple modes:
        - No args: Show uncommitted changes (working directory vs HEAD)
        - One ref: Compare that ref to working directory
        - Two refs: Compare between refs

        Args:
            ref1: First reference (optional)
            ref2: Second reference (optional)
            file_path: Limit diff to specific file (optional)
            max_lines: Maximum output lines before truncation (default: 500)

        Returns:
            Diff output, or error message if failed
        """
        if not self.is_active:
            return "Git versioning not available for this workspace"

        try:
            args = ["diff"]

            if ref1 and ref2:
                args.extend([ref1, ref2])
            elif ref1:
                args.append(ref1)
            # No refs = uncommitted changes

            if file_path:
                args.extend(["--", file_path])

            result = self._run_git(args)
            if result.returncode != 0:
                return f"Error: {result.stderr}"

            output = result.stdout
            if not output.strip():
                return "No differences"

            return self._truncate_output(output, max_lines=max_lines)

        except Exception as e:
            return f"Error getting diff: {e}"

    def status(self) -> str:
        """Get current status with clear dirty/clean indication.

        Returns a human-readable status including:
        - Current branch
        - Whether workspace is clean or dirty
        - List of modified/staged/untracked files

        Returns:
            Formatted status string
        """
        if not self.is_active:
            return "Git versioning not available for this workspace"

        try:
            # Get porcelain status for parsing
            result = self._run_git(["status", "--porcelain"])
            if result.returncode != 0:
                return f"Error: {result.stderr}"

            # Get branch name
            branch_result = self._run_git(["branch", "--show-current"])
            branch = (
                branch_result.stdout.strip()
                if branch_result.returncode == 0
                else "unknown"
            )

            lines = result.stdout.strip().split("\n") if result.stdout.strip() else []

            if not lines:
                return f"Branch: {branch}\nStatus: clean (no uncommitted changes)"

            # Parse status
            staged = []
            modified = []
            untracked = []

            for line in lines:
                if len(line) < 2:
                    continue
                index_status = line[0]
                worktree_status = line[1]
                filename = line[3:]

                if index_status in "MADRC":
                    staged.append(filename)
                if worktree_status in "MD":
                    modified.append(filename)
                if index_status == "?" and worktree_status == "?":
                    untracked.append(filename)

            output = [f"Branch: {branch}", "Status: dirty (uncommitted changes)"]

            if staged:
                output.append(f"\nStaged ({len(staged)}):")
                for f in staged[:10]:  # Limit to 10
                    output.append(f"  + {f}")
                if len(staged) > 10:
                    output.append(f"  ... and {len(staged) - 10} more")

            if modified:
                output.append(f"\nModified ({len(modified)}):")
                for f in modified[:10]:
                    output.append(f"  M {f}")
                if len(modified) > 10:
                    output.append(f"  ... and {len(modified) - 10} more")

            if untracked:
                output.append(f"\nUntracked ({len(untracked)}):")
                for f in untracked[:10]:
                    output.append(f"  ? {f}")
                if len(untracked) > 10:
                    output.append(f"  ... and {len(untracked) - 10} more")

            return "\n".join(output)

        except Exception as e:
            return f"Error getting status: {e}"

    def has_uncommitted_changes(self) -> bool:
        """Check if there are uncommitted changes in the workspace.

        Returns:
            True if there are uncommitted changes, False if clean or inactive
        """
        if not self.is_active:
            return False

        try:
            result = self._run_git(["status", "--porcelain"])
            return bool(result.stdout.strip())
        except Exception:
            return False

    def tag(self, tag_name: str, message: Optional[str] = None) -> bool:
        """Create a git tag at current HEAD.

        Args:
            tag_name: Tag name (e.g., "phase-2-tactical-complete")
            message: Optional tag message (creates annotated tag if provided)

        Returns:
            True if successful, False otherwise
        """
        if not self.is_active:
            logger.debug("Git not active, skipping tag")
            return False

        try:
            args = ["tag", "-f"]
            if message:
                args.extend(["-a", tag_name, "-m", message])
            else:
                args.append(tag_name)

            result = self._run_git(args)
            if result.returncode != 0:
                logger.warning(f"git tag failed: {result.stderr}")
                return False

            logger.debug(f"Created tag: {tag_name}")
            return True

        except Exception as e:
            logger.error(f"Failed to create tag: {e}")
            return False

    def list_tags(self, pattern: Optional[str] = None) -> List[str]:
        """List git tags, optionally filtered by pattern.

        Args:
            pattern: Glob pattern (e.g., "phase-*")

        Returns:
            List of tag names, empty list if none or error
        """
        if not self.is_active:
            return []

        try:
            args = ["tag", "-l"]
            if pattern:
                args.append(pattern)

            result = self._run_git(args)
            if result.returncode != 0:
                return []

            output = result.stdout.strip()
            return output.split("\n") if output else []

        except Exception:
            return []

    # =========================================================================
    # Branch Operations
    # =========================================================================

    def checkout_branch(self, branch_name: str, create: bool = False) -> bool:
        """Checkout a branch. If create=True and branch doesn't exist, create it.

        Args:
            branch_name: Branch name to checkout
            create: If True, create the branch if it doesn't exist

        Returns:
            True if checkout succeeded, False otherwise
        """
        if not self.is_active:
            return False

        try:
            if create:
                # Check if branch exists locally
                result = self._run_git(["branch", "--list", branch_name])
                if not result.stdout.strip():
                    # Check if it exists on remote
                    result = self._run_git(
                        ["branch", "-r", "--list", f"origin/{branch_name}"]
                    )
                    if result.stdout.strip():
                        # Track remote branch
                        result = self._run_git(
                            ["checkout", "-b", branch_name, f"origin/{branch_name}"]
                        )
                        return result.returncode == 0
                    else:
                        # Create new local branch
                        result = self._run_git(["checkout", "-b", branch_name])
                        return result.returncode == 0

            # Branch exists locally, just checkout
            result = self._run_git(["checkout", branch_name])
            return result.returncode == 0

        except Exception as e:
            logger.error(f"Failed to checkout branch {branch_name}: {e}")
            return False

    def current_branch(self) -> str | None:
        """Get current branch name.

        Returns:
            Current branch name, or None if not in a git repo
        """
        if not self.is_active:
            return None

        try:
            result = self._run_git(["rev-parse", "--abbrev-ref", "HEAD"])
            return result.stdout.strip() if result.returncode == 0 else None
        except Exception:
            return None

    # =========================================================================
    # Remote Operations
    # =========================================================================

    def has_remote(self, name: str = "origin") -> bool:
        """Check if a named remote is configured.

        Args:
            name: Remote name (default: "origin")

        Returns:
            True if remote exists, False otherwise
        """
        if not self.is_active:
            return False

        try:
            result = self._run_git(["remote", "get-url", name])
            return result.returncode == 0
        except Exception:
            return False

    def add_remote(self, name: str, url: str) -> bool:
        """Add or update a git remote.

        If the remote already exists, updates its URL instead.

        Args:
            name: Remote name (e.g. "origin")
            url: Remote URL (may contain credentials)

        Returns:
            True if successful, False otherwise
        """
        if not self.is_active:
            return False

        masked = self._mask_url(url)

        try:
            if self.has_remote(name):
                result = self._run_git(["remote", "set-url", name, url])
                if result.returncode == 0:
                    logger.debug(f"Updated remote '{name}' -> {masked}")
                    return True
            else:
                result = self._run_git(["remote", "add", name, url])
                if result.returncode == 0:
                    logger.debug(f"Added remote '{name}' -> {masked}")
                    return True

            logger.warning(f"Failed to configure remote '{name}': {result.stderr}")
            return False

        except Exception as e:
            logger.error(f"Failed to configure remote '{name}': {e}")
            return False

    def push(
        self, remote: str = "origin", branch: Optional[str] = None, tags: bool = True
    ) -> bool:
        """Push commits and optionally tags to remote.

        Gracefully returns False if no remote is configured.

        Args:
            remote: Remote name (default: "origin")
            branch: Branch to push (auto-detected if None)
            tags: Also push tags (default: True)

        Returns:
            True if push succeeded, False otherwise
        """
        if not self.is_active or not self.has_remote(remote):
            return False

        try:
            # Auto-detect branch
            if branch is None:
                result = self._run_git(["branch", "--show-current"])
                branch = result.stdout.strip() if result.returncode == 0 else "main"

            # Push branch
            result = self._run_git(
                ["push", "-u", remote, branch],
                timeout=120,
            )
            if result.returncode != 0:
                logger.warning(f"git push failed: {result.stderr}")
                return False

            # Push tags
            if tags:
                tag_result = self._run_git(
                    ["push", remote, "--tags"],
                    timeout=120,
                )
                if tag_result.returncode != 0:
                    logger.warning(f"git push --tags failed: {tag_result.stderr}")
                    # Don't fail overall push if only tags failed

            logger.debug(f"Pushed to {remote}/{branch}")
            return True

        except Exception as e:
            logger.warning(f"Push failed: {e}")
            return False

    def has_unpushed_commits(
        self, remote: str = "origin", branch: Optional[str] = None
    ) -> bool:
        """Check whether the local branch has commits not yet on the remote.

        Compares HEAD against the local remote-tracking ref (e.g.
        ``origin/main``), so it does NOT perform a network round-trip — it only
        reads refs already on disk. Used to decide whether an end-of-turn push
        is worthwhile.

        Returns:
            True if at least one local commit is not on the remote. False if
            git is inactive, no remote is configured (nothing to push to), or
            the branch is already fully pushed.
        """
        if not self.is_active or not self.has_remote(remote):
            return False

        try:
            if branch is None:
                result = self._run_git(["branch", "--show-current"])
                branch = (
                    result.stdout.strip()
                    if result.returncode == 0 and result.stdout.strip()
                    else "main"
                )

            # Count commits reachable from HEAD but not from the remote-tracking
            # ref. Reads the local ref only — no fetch required.
            result = self._run_git(
                ["rev-list", "--count", f"{remote}/{branch}..HEAD"]
            )
            if result.returncode != 0:
                # The remote-tracking ref doesn't exist yet (nothing has ever
                # been pushed). Any commit on HEAD is therefore unpushed.
                head = self._run_git(["rev-parse", "--verify", "HEAD"])
                return head.returncode == 0
            return int((result.stdout or "").strip() or "0") > 0
        except Exception as e:
            # Be conservative: if we can't tell, assume there may be unpushed
            # work so the caller attempts a push rather than silently stranding
            # commits on the workspace pod.
            logger.debug(f"has_unpushed_commits check failed: {e}")
            return True

    def pull(self, remote: str = "origin", branch: Optional[str] = None) -> bool:
        """Pull from remote (fast-forward only).

        Gracefully returns False if no remote is configured.

        Args:
            remote: Remote name (default: "origin")
            branch: Branch to pull (auto-detected if None)

        Returns:
            True if pull succeeded, False otherwise
        """
        if not self.is_active or not self.has_remote(remote):
            return False

        try:
            if branch is None:
                result = self._run_git(["branch", "--show-current"])
                branch = result.stdout.strip() if result.returncode == 0 else "main"

            result = self._run_git(
                ["pull", remote, branch, "--ff-only"],
                timeout=120,
            )
            if result.returncode != 0:
                logger.warning(f"git pull failed: {result.stderr}")
                return False

            logger.debug(f"Pulled from {remote}/{branch}")
            return True

        except Exception as e:
            logger.warning(f"Pull failed: {e}")
            return False

    @classmethod
    def clone(
        cls,
        url: str,
        target_path: Path,
        backend=None,
        remote_cwd=None,
    ) -> Optional["GitManager"]:
        """Clone a repository to a target path.

        Args:
            url: Repository URL (may contain credentials)
            target_path: Local path to clone into (used for the GitManager
                instance; ignored for the actual clone when backend is active)
            backend: Optional WorkspaceBackend for remote execution
            remote_cwd: Relative path within backend root for the clone
                target (e.g. "repos/my-repo"). When None, clones into
                the backend's root directory.

        Returns:
            GitManager instance for the cloned repo, or None on failure
        """
        masked = cls._mask_url_static(url)
        use_backend = backend is not None and getattr(backend, "supports_shell", False)

        if use_backend:
            try:
                if remote_cwd:
                    remote_target = posixpath.join(backend.root, remote_cwd)
                else:
                    remote_target = backend.root

                cmd = f"git clone {shlex.quote(url)} {shlex.quote(remote_target)}"
                output = backend.shell_run(cmd, timeout=120, tab_name="git")

                # Parse exit code
                first_line = output.split("\n", 1)[0].strip()
                if not first_line.startswith("Exit code: 0"):
                    logger.warning(f"git clone failed for {masked}: {output}")
                    return None

                mgr = cls(target_path, backend=backend, remote_cwd=remote_cwd)
                mgr._run_git(["config", "user.email", "agent@workspace.local"])
                mgr._run_git(["config", "user.name", "Agent"])

                logger.info(f"Cloned {masked} to {remote_target}")
                return mgr

            except Exception as e:
                logger.warning(f"Failed to clone {masked}: {e}")
                return None

        # --- Local subprocess path (no backend) ---

        if shutil.which("git") is None:
            logger.warning("Cannot clone: git not available")
            return None

        try:
            result = subprocess.run(
                ["git", "clone", url, str(target_path)],
                capture_output=True,
                text=True,
                timeout=120,
            )

            if result.returncode != 0:
                logger.warning(f"git clone failed for {masked}: {result.stderr}")
                return None

            mgr = cls(target_path)
            # Configure local user for the cloned repo
            mgr._run_git(["config", "user.email", "agent@workspace.local"])
            mgr._run_git(["config", "user.name", "Agent"])

            logger.info(f"Cloned {masked} to {target_path}")
            return mgr

        except subprocess.TimeoutExpired:
            logger.warning(f"git clone timed out for {masked}")
            return None
        except Exception as e:
            logger.warning(f"Failed to clone {masked}: {e}")
            return None

    @classmethod
    def from_worktree(
        cls, worktree_path: Path, backend=None, remote_cwd=None
    ) -> Optional["GitManager"]:
        """Create a GitManager for an existing git worktree.

        Git worktrees have a `.git` file (not directory) that points to the
        parent repo's `.git` directory. This method validates the worktree
        exists and configures local user identity.

        Args:
            worktree_path: Path to the worktree directory
            backend: Optional WorkspaceBackend for remote execution
            remote_cwd: Relative path within backend root for the worktree

        Returns:
            GitManager instance, or None if path is not a valid git worktree
        """
        worktree_path = Path(worktree_path)
        use_backend = backend is not None and getattr(backend, "supports_shell", False)

        if use_backend:
            git_path = posixpath.join(remote_cwd, ".git") if remote_cwd else ".git"
            if not backend.exists(git_path):
                logger.warning(f"Not a git worktree (no .git): {worktree_path}")
                return None
        else:
            git_marker = worktree_path / ".git"
            if not git_marker.exists():
                logger.warning(f"Not a git worktree (no .git): {worktree_path}")
                return None

        mgr = cls(worktree_path, backend=backend, remote_cwd=remote_cwd)
        if not mgr._git_available:
            return None

        # Configure local user for the worktree
        mgr._run_git(["config", "user.email", "agent@workspace.local"])
        mgr._run_git(["config", "user.name", "Agent"])

        logger.info(f"GitManager initialized from worktree at {worktree_path}")
        return mgr

    @staticmethod
    def _mask_url_static(url: str) -> str:
        """Mask credentials in a URL for safe logging."""
        import re

        return re.sub(r"://([^:]+):[^@]+@", r"://\1:***@", url)

    def _mask_url(self, url: str) -> str:
        """Instance method wrapper for URL masking."""
        return self._mask_url_static(url)

    def _run_git(
        self, args: List[str], timeout: int = DEFAULT_TIMEOUT
    ) -> subprocess.CompletedProcess:
        """Execute git command in workspace directory with timeout.

        When a workspace backend with shell support is available, the command
        is delegated to the backend (e.g. executed via SSH on a remote VM).
        Otherwise falls back to local subprocess execution.

        Args:
            args: Git command arguments (without 'git' prefix)
            timeout: Command timeout in seconds (default: 60)

        Returns:
            CompletedProcess with stdout, stderr, and returncode
        """
        if self._use_backend:
            cmd_str = "git " + " ".join(shlex.quote(a) for a in args)
            try:
                output = self._backend.shell_run(
                    cmd_str,
                    timeout=timeout,
                    tab_name="git",
                    working_dir=self._remote_cwd,
                )
                return self._parse_shell_run_output(output, args)
            except Exception as e:
                logger.error(f"Backend git command failed: {e}")
                return subprocess.CompletedProcess(
                    ["git"] + args, returncode=1, stdout="", stderr=str(e)
                )

        # --- Local subprocess path (no backend) ---
        cmd = ["git"] + args
        try:
            result = subprocess.run(
                cmd,
                cwd=self._workspace_path,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return result
        except subprocess.TimeoutExpired:
            logger.error(f"Git command timed out after {timeout}s: {' '.join(cmd)}")
            return subprocess.CompletedProcess(
                cmd,
                returncode=1,
                stdout="",
                stderr=f"Command timed out after {timeout}s",
            )
        except Exception as e:
            logger.error(f"Git command failed: {e}")
            return subprocess.CompletedProcess(
                cmd, returncode=1, stdout="", stderr=str(e)
            )

    def _parse_shell_run_output(
        self, output: str, args: Optional[List[str]] = None
    ) -> subprocess.CompletedProcess:
        """Parse shell_run formatted output into a CompletedProcess.

        The backend's shell_run returns formatted strings like:
            "Exit code: 0\\n--- stdout ---\\nactual output..."
            "Exit code: 1\\n--- stdout ---\\nerror message..."
            "Exit code: 0\\n(no output)"

        Error cases (timeout, interactive prompt) lack the "Exit code:" prefix
        and are treated as failures.

        Args:
            output: Raw shell_run output string
            args: Git command args for the CompletedProcess.args field

        Returns:
            CompletedProcess with parsed returncode, stdout, and stderr
        """
        cmd = ["git"] + (args or [])
        lines = output.split("\n", 1)
        first_line = lines[0].strip()

        # A still-running result (soft/hard no-change timeout) is not a normal
        # completion. Surface it as a non-zero result with a clear stderr so git
        # callers don't mistake the explanatory text for command stdout.
        if "--- still running ---" in output:
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=-1,
                stdout="",
                stderr="git command did not complete (still running / timed out).\n"
                + output,
            )

        if first_line.startswith("Exit code:"):
            try:
                exit_code = int(first_line.split(":", 1)[1].strip())
            except (ValueError, IndexError):
                exit_code = 1

            rest = lines[1] if len(lines) > 1 else ""
            stdout_marker = "--- stdout ---\n"
            if rest.startswith(stdout_marker):
                stdout = rest[len(stdout_marker) :]
            elif rest.strip() == "(no output)":
                stdout = ""
            else:
                stdout = rest

            return subprocess.CompletedProcess(
                args=cmd,
                returncode=exit_code,
                stdout=stdout,
                stderr="",
            )

        # No "Exit code:" prefix — timeout, stall, or other error
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=1,
            stdout="",
            stderr=output,
        )

    def _truncate_output(
        self,
        output: str,
        max_lines: int = DEFAULT_MAX_LINES,
        max_words: int = DEFAULT_MAX_WORDS,
    ) -> str:
        """Truncate output to prevent context bloat.

        Args:
            output: Output string to truncate
            max_lines: Maximum number of lines (default: 500)
            max_words: Maximum number of words (default: 10000)

        Returns:
            Truncated output with count indicator if truncated
        """
        lines = output.splitlines()
        words = output.split()

        original_lines = len(lines)
        original_words = len(words)

        truncated = False
        result = output

        # Check lines first
        if len(lines) > max_lines:
            result = "\n".join(lines[:max_lines])
            truncated = True

        # Then check words (on potentially already-truncated result)
        result_words = result.split()
        if len(result_words) > max_words:
            result = " ".join(result_words[:max_words])
            truncated = True

        if truncated:
            result += f"\n\n... truncated ({original_lines} lines, {original_words} words total)"

        return result

    # --- Worktree management (for subagent delegation) ---

    def worktree_add(self, path: str, branch: str, create_branch: bool = True) -> bool:
        """Create a git worktree at the given path on the specified branch.

        Args:
            path: Relative path for the worktree (e.g., '.worktrees/subagent_0')
            branch: Branch name (e.g., 'subagent/0')
            create_branch: If True, create a new branch; if False, use existing

        Returns:
            True if worktree was created successfully
        """
        if not self.is_active:
            return False

        args = ["worktree", "add"]
        if create_branch:
            args.extend(["-b", branch, path])
        else:
            args.extend([path, branch])

        result = self._run_git(args)
        if result.returncode == 0:
            logger.info(f"Created worktree at {path} on branch {branch}")
            return True
        else:
            logger.error(f"Failed to create worktree at {path}: {result.stderr}")
            return False

    def worktree_remove(self, path: str, force: bool = False) -> bool:
        """Remove a git worktree.

        Args:
            path: Path to the worktree to remove
            force: If True, force removal even with uncommitted changes

        Returns:
            True if worktree was removed successfully
        """
        if not self.is_active:
            return False

        args = ["worktree", "remove"]
        if force:
            args.append("--force")
        args.append(path)

        result = self._run_git(args)
        if result.returncode == 0:
            logger.info(f"Removed worktree at {path}")
            return True
        else:
            logger.error(f"Failed to remove worktree at {path}: {result.stderr}")
            return False

    def worktree_list(self) -> list[dict]:
        """List all worktrees.

        Returns:
            List of dicts with 'path', 'head', and 'branch' keys
        """
        if not self.is_active:
            return []

        result = self._run_git(["worktree", "list", "--porcelain"])
        if result.returncode != 0:
            logger.warning(f"Failed to list worktrees: {result.stderr}")
            return []

        worktrees = []
        current: dict = {}
        for line in result.stdout.strip().splitlines():
            if line.startswith("worktree "):
                if current:
                    worktrees.append(current)
                current = {"path": line[len("worktree ") :].strip()}
            elif line.startswith("HEAD "):
                current["head"] = line[len("HEAD ") :].strip()
            elif line.startswith("branch "):
                current["branch"] = line[len("branch ") :].strip()
            elif line == "bare":
                current["bare"] = True
            elif line == "detached":
                current["detached"] = True
        if current:
            worktrees.append(current)

        return worktrees

    def merge_squash(self, branch: str) -> tuple[bool, str]:
        """Squash-merge the given branch into current HEAD.

        Performs `git merge --squash <branch>` then commits with a descriptive
        message. Does NOT delete the source branch — caller handles cleanup.

        Args:
            branch: Branch name to squash-merge

        Returns:
            Tuple of (success, message). Message contains the commit info on
            success, or the error/conflict details on failure.
        """
        if not self.is_active:
            return False, "Git not active"

        # Perform squash merge
        result = self._run_git(["merge", "--squash", branch])
        if result.returncode != 0:
            stderr = result.stderr.strip()
            if "CONFLICT" in result.stdout or "CONFLICT" in stderr:
                return False, f"Merge conflicts detected:\n{result.stdout}\n{stderr}"
            return False, f"Squash merge failed: {stderr}"

        # Check if merge reported no changes
        if "Already up to date" in result.stdout:
            return True, f"No changes from {branch} (already up to date)"

        # Commit the squash merge
        commit_msg = f"Squash merge {branch}"
        commit_result = self._run_git(["commit", "-m", commit_msg, "--no-edit"])
        if commit_result.returncode != 0:
            # Check if there's nothing to commit (no changes from branch)
            combined = commit_result.stdout + commit_result.stderr
            if "nothing to commit" in combined or "nothing added" in combined:
                return True, f"No changes from {branch} (already merged or empty)"
            return False, f"Commit after squash merge failed: {commit_result.stderr}"

        logger.info(f"Squash-merged branch {branch}")
        return True, f"Squash-merged {branch}: {commit_result.stdout.strip()}"

    def delete_branch(self, branch: str, force: bool = False) -> bool:
        """Delete a local branch.

        Args:
            branch: Branch name to delete
            force: If True, use -D (force delete); otherwise -d (safe delete)

        Returns:
            True if branch was deleted
        """
        if not self.is_active:
            return False

        flag = "-D" if force else "-d"
        result = self._run_git(["branch", flag, branch])
        if result.returncode == 0:
            logger.info(f"Deleted branch {branch}")
            return True
        else:
            logger.warning(f"Failed to delete branch {branch}: {result.stderr}")
            return False

    def cleanup_orphaned_worktrees(self) -> list[str]:
        """Remove worktrees in .worktrees/ that have no corresponding active branch.

        Scans git worktree list for entries under .worktrees/ and removes any
        that are no longer needed (branch deleted, or worktree in broken state).

        Returns:
            List of cleaned-up worktree paths
        """
        if not self.is_active:
            return []

        worktrees = self.worktree_list()
        cleaned = []

        for wt in worktrees:
            path = wt.get("path", "")
            if "/.worktrees/" not in path and "\\.worktrees\\" not in path:
                continue  # Not a delegation worktree

            # Check if worktree is in broken state (branch deleted, etc.)
            # git worktree list --porcelain shows "prunable" for stale entries
            # For simplicity, we use git worktree prune to clean up stale refs
            pass

        # Use git worktree prune to remove stale worktree refs
        prune_result = self._run_git(["worktree", "prune"])
        if prune_result.returncode == 0 and prune_result.stdout.strip():
            logger.info(f"Pruned stale worktree refs: {prune_result.stdout.strip()}")

        # Check for .worktrees/ directory entries that git no longer tracks
        worktrees_dir = self._workspace_path / ".worktrees"
        if worktrees_dir.exists() and worktrees_dir.is_dir():
            tracked_paths = {wt.get("path", "") for wt in self.worktree_list()}
            for child in worktrees_dir.iterdir():
                if child.is_dir() and str(child) not in tracked_paths:
                    # Orphaned directory — git doesn't know about it
                    try:
                        shutil.rmtree(child)
                        cleaned.append(str(child))
                        logger.info(f"Removed orphaned worktree directory: {child}")
                    except Exception as e:
                        logger.warning(
                            f"Failed to remove orphaned worktree {child}: {e}"
                        )

            # Remove .worktrees/ if empty
            if worktrees_dir.exists() and not any(worktrees_dir.iterdir()):
                try:
                    worktrees_dir.rmdir()
                    logger.info("Removed empty .worktrees/ directory")
                except Exception:
                    pass

        return cleaned

    def _check_git_available(self) -> bool:
        """Check if git is installed and available.

        Returns:
            True if git is available, False otherwise
        """
        return shutil.which("git") is not None
