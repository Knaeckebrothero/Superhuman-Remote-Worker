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

    def __init__(self, workspace_path: Path):
        """Initialize GitManager for a workspace directory.

        Args:
            workspace_path: Path to the workspace directory
        """
        self._workspace_path = Path(workspace_path)
        self._git_available = self._check_git_available()

        if not self._git_available:
            logger.warning("Git not available, workspace versioning disabled")

    @property
    def is_active(self) -> bool:
        """Check if git versioning is active for this workspace.

        Returns True only if git is available AND the workspace has been
        initialized as a git repository.
        """
        return self._git_available and (self._workspace_path / ".git").exists()

    @property
    def workspace_path(self) -> Path:
        """Get the workspace path."""
        return self._workspace_path

    def init_repository(self, ignore_patterns: Optional[List[str]] = None) -> bool:
        """Initialize git repository with .gitignore.

        Creates a new git repository in the workspace directory, configures
        a local git user, creates .gitignore from patterns, and makes an
        initial commit.

        If a .git directory already exists, this is a no-op and returns True.

        Args:
            ignore_patterns: List of patterns for .gitignore
                            (e.g., ["*.db", "*.log", "__pycache__/"])

        Returns:
            True if successful or already initialized, False on error
        """
        if not self._git_available:
            logger.warning("Cannot initialize repository: git not available")
            return False

        # Skip if already initialized
        if (self._workspace_path / ".git").exists():
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

            # Create .gitignore
            if ignore_patterns:
                gitignore_path = self._workspace_path / ".gitignore"
                gitignore_content = "\n".join(ignore_patterns) + "\n"
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
                if "nothing to commit" in result.stdout or "nothing to commit" in result.stderr:
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
            branch = branch_result.stdout.strip() if branch_result.returncode == 0 else "unknown"

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
            args = ["tag"]
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

    def _run_git(
        self, args: List[str], timeout: int = DEFAULT_TIMEOUT
    ) -> subprocess.CompletedProcess:
        """Execute git command in workspace directory with timeout.

        Args:
            args: Git command arguments (without 'git' prefix)
            timeout: Command timeout in seconds (default: 60)

        Returns:
            CompletedProcess with stdout, stderr, and returncode
        """
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
            # Return a fake CompletedProcess with error
            return subprocess.CompletedProcess(
                cmd, returncode=1, stdout="", stderr=f"Command timed out after {timeout}s"
            )
        except Exception as e:
            logger.error(f"Git command failed: {e}")
            return subprocess.CompletedProcess(
                cmd, returncode=1, stdout="", stderr=str(e)
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

    def _check_git_available(self) -> bool:
        """Check if git is installed and available.

        Returns:
            True if git is available, False otherwise
        """
        return shutil.which("git") is not None
