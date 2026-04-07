"""Git tools for the Universal Agent.

Provides read-only git operations for workspace version control:
- git_log: View commit history
- git_show: Inspect specific commits
- git_diff: Compare changes
- git_status: Current workspace state
- git_tags: List phase milestone tags

All tools access GitManager via context.workspace_manager.git_manager.
"""

import logging
from typing import Any, Dict, List, Optional

from langchain_core.tools import tool

from ..context import ToolContext

logger = logging.getLogger(__name__)


# Tool metadata for registry
# Phase availability: git tools are available in BOTH phases (read-only)
GIT_TOOLS_METADATA: Dict[str, Dict[str, Any]] = {
    "git_log": {
        "module": "git.git_tools",
        "function": "git_log",
        "description": "View commit history with filtering",
        "category": "git",
        "short_description": "View commit history (default: last 10 commits).",
        "phases": ["strategic", "tactical"],
    },
    "git_show": {
        "module": "git.git_tools",
        "function": "git_show",
        "description": "Inspect a specific commit's changes",
        "category": "git",
        "short_description": "Show commit details and diff (use stat_only=true for summary).",
        "phases": ["strategic", "tactical"],
    },
    "git_diff": {
        "module": "git.git_tools",
        "function": "git_diff",
        "description": "Compare current state to previous commits",
        "category": "git",
        "short_description": "Show differences (uncommitted changes or between refs).",
        "phases": ["strategic", "tactical"],
    },
    "git_status": {
        "module": "git.git_tools",
        "function": "git_status",
        "description": "See uncommitted changes and workspace state",
        "category": "git",
        "short_description": "Show current branch and uncommitted changes.",
        "phases": ["strategic", "tactical"],
    },
    "git_tags": {
        "module": "git.git_tools",
        "function": "git_tags",
        "description": "List phase milestone tags",
        "category": "git",
        "short_description": "List git tags for this job (use all_jobs=True for all).",
        "phases": ["strategic", "tactical"],
    },
    "git_merge_squash": {
        "module": "git.git_tools",
        "function": "git_merge_squash",
        "description": "Squash-merge a branch into the current HEAD (for delegation review)",
        "category": "git",
        "short_description": "Squash-merge a branch into HEAD. Used to merge subagent results.",
        "phases": ["strategic", "tactical"],
    },
    "git_worktree_cleanup": {
        "module": "git.git_tools",
        "function": "git_worktree_cleanup",
        "description": "Remove a worktree directory and delete its branch (for delegation cleanup)",
        "category": "git",
        "short_description": "Remove a worktree and delete its branch after merge.",
        "phases": ["strategic", "tactical"],
    },
}


def create_git_tools(context: ToolContext) -> List[Any]:
    """Create git tools with injected context.

    Args:
        context: ToolContext with workspace_manager (which has git_manager)

    Returns:
        List of LangChain tool functions

    Raises:
        ValueError: If git manager not available
    """
    # Check if git is available via workspace manager
    if not context.has_workspace():
        raise ValueError("Git tools require workspace_manager in ToolContext")

    git_mgr = context.workspace_manager.git_manager
    if git_mgr is None:
        raise ValueError("Git tools require git_manager on workspace_manager")

    @tool
    def git_log(max_count: int = 10, oneline: bool = True) -> str:
        """View commit history.

        Use this to review what was accomplished in previous phases or to
        understand the history of workspace changes.

        Args:
            max_count: Maximum number of commits to show (default: 10)
            oneline: Show compact one-line format (default: True).
                     Set to False for full commit details.

        Returns:
            Formatted commit history

        Example:
            git_log()  # Last 10 commits in compact format
            git_log(max_count=5)  # Last 5 commits
            git_log(oneline=False)  # Full commit details
        """
        if not git_mgr.is_active:
            return "Git versioning not available for this workspace"

        return git_mgr.log(max_count=max_count, oneline=oneline)

    @tool
    def git_show(
        commit_ref: str = "HEAD",
        stat_only: bool = False,
        max_lines: int = 500,
    ) -> str:
        """Show details of a specific commit.

        Use this to inspect what changes were made in a particular commit,
        including the full diff or just file statistics.

        Args:
            commit_ref: Commit reference to show (default: "HEAD")
                       Examples: "HEAD", "HEAD~1", commit hash, tag name
            stat_only: If True, show only file statistics without full diff
                       (default: False). Use this for a quick overview.
            max_lines: Maximum output lines before truncation (default: 500)

        Returns:
            Commit details including message and diff (or stats)

        Example:
            git_show()  # Show latest commit with full diff
            git_show(stat_only=True)  # Show latest commit file list only
            git_show(commit_ref="HEAD~3")  # Show commit from 3 commits ago
            git_show(commit_ref="phase-1-tactical-complete")  # Show tagged commit
        """
        if not git_mgr.is_active:
            return "Git versioning not available for this workspace"

        return git_mgr.show(
            commit_ref=commit_ref,
            stat_only=stat_only,
            max_lines=max_lines,
        )

    @tool
    def git_diff(
        ref1: Optional[str] = None,
        ref2: Optional[str] = None,
        file_path: Optional[str] = None,
        max_lines: int = 500,
    ) -> str:
        """Show differences between commits or uncommitted changes.

        Supports multiple comparison modes:
        - No arguments: Show uncommitted changes (working directory vs HEAD)
        - One ref: Compare that ref to working directory
        - Two refs: Compare between refs (ref1..ref2)

        Args:
            ref1: First reference (optional). Examples: "HEAD~5", tag, hash
            ref2: Second reference (optional). Only used with ref1.
            file_path: Limit diff to a specific file (optional)
            max_lines: Maximum output lines before truncation (default: 500)

        Returns:
            Diff output showing changes

        Example:
            git_diff()  # Show uncommitted changes
            git_diff(ref1="HEAD~5")  # Changes in last 5 commits vs working dir
            git_diff(ref1="phase-1-tactical-complete", ref2="HEAD")  # Between tag and HEAD
            git_diff(file_path="workspace.md")  # Uncommitted changes to specific file
        """
        if not git_mgr.is_active:
            return "Git versioning not available for this workspace"

        return git_mgr.diff(
            ref1=ref1,
            ref2=ref2,
            file_path=file_path,
            max_lines=max_lines,
        )

    @tool
    def git_status() -> str:
        """Show current workspace git status.

        Provides a clear indication of:
        - Current branch name
        - Whether workspace is clean or dirty
        - List of staged, modified, and untracked files

        Use this before completing a todo to verify the workspace state,
        or to understand what files have been changed.

        Returns:
            Formatted status showing branch and file changes

        Example output (clean):
            Branch: master
            Status: clean (no uncommitted changes)

        Example output (dirty):
            Branch: master
            Status: dirty (uncommitted changes)

            Staged (2):
              + new_file.txt
              + updated.md

            Modified (1):
              M workspace.md
        """
        if not git_mgr.is_active:
            return "Git versioning not available for this workspace"

        return git_mgr.status()

    @tool
    def git_tags(pattern: str = "phase-*", all_jobs: bool = False) -> str:
        """List git tags, filtered by pattern.

        Use this to see phase milestones and understand progression,
        especially useful after context compaction when phase history
        may not be in conversation context.

        By default, only shows tags for the current job. Set all_jobs=True
        to see tags from all jobs in a shared project repository.

        Args:
            pattern: Glob pattern to filter tags (default: "phase-*")
                    Use "*" to list all tags.
            all_jobs: If True, show tags from all jobs in the repo.
                     If False (default), only show this job's tags.

        Returns:
            Comma-separated list of matching tags, or message if none found

        Example:
            git_tags()  # List all phase tags for this job
            git_tags(pattern="phase-1-*")  # Only phase 1 tags for this job
            git_tags(pattern="*")  # All tags for this job
            git_tags(all_jobs=True)  # Phase tags from all jobs in repo
        """
        if not git_mgr.is_active:
            return "Git versioning not available for this workspace"

        # Auto-scope to current job unless all_jobs requested
        if not all_jobs and context.job_id:
            pattern = f"{context.job_id[:8]}-{pattern}"

        tags = git_mgr.list_tags(pattern=pattern)

        if not tags or tags == [""]:
            return f"No tags matching pattern '{pattern}'"

        return ", ".join(tags)

    @tool
    def git_merge_squash(
        branch: str,
        commit_message: Optional[str] = None,
    ) -> str:
        """Squash-merge a branch into the current HEAD.

        Used during delegation review to merge a subagent's branch. Creates a
        single squash commit containing all changes from the branch. Does NOT
        delete the branch — use git_worktree_cleanup for that.

        Merge in creation order (subagent/0 first, then subagent/1, etc.) to
        handle conflicts incrementally.

        Args:
            branch: Branch name to squash-merge (e.g., "subagent/0")
            commit_message: Optional custom commit message. If not provided,
                           uses "Squash merge {branch}".

        Returns:
            Success message with commit info, or error details if merge failed.
            If merge conflicts occur, the message describes which files conflict.

        Example:
            git_merge_squash(branch="subagent/0")
            git_merge_squash(branch="subagent/1", commit_message="Merge research results")
        """
        if not git_mgr.is_active:
            return "Git versioning not available for this workspace"

        success, msg = git_mgr.merge_squash(branch)

        if not success and "conflict" in msg.lower():
            return (
                f"Merge conflicts when merging {branch}:\n{msg}\n\n"
                "Resolve conflicts manually using read_file/write_file, "
                "then run_command('git add .') and run_command('git commit')."
            )
        if not success:
            return f"Failed to merge {branch}: {msg}"

        # If custom commit message and the merge created a commit, amend it
        if commit_message and "No changes" not in msg:
            git_mgr._run_git(["commit", "--amend", "-m", commit_message])

        return f"Successfully squash-merged {branch}: {msg}"

    @tool
    def git_worktree_cleanup(
        branch: str,
        force: bool = False,
    ) -> str:
        """Remove a worktree and delete its branch after merge.

        Call this after squash-merging a subagent's branch to clean up. Removes
        the worktree directory and deletes the local branch.

        The worktree path is derived from the branch name:
        - branch "subagent/0" → worktree ".worktrees/subagent_0"

        Args:
            branch: Branch name to clean up (e.g., "subagent/0")
            force: Force removal even if worktree has uncommitted changes

        Returns:
            Status message about what was cleaned up.

        Example:
            git_worktree_cleanup(branch="subagent/0")
            git_worktree_cleanup(branch="subagent/2", force=True)
        """
        if not git_mgr.is_active:
            return "Git versioning not available for this workspace"

        # Derive worktree path from branch name: subagent/N → .worktrees/subagent_N
        wt_dir = branch.replace("/", "_")
        wt_path = f".worktrees/{wt_dir}"

        results = []

        # Remove worktree
        if git_mgr.worktree_remove(wt_path, force=force):
            results.append(f"Removed worktree {wt_path}")
        else:
            results.append(f"Failed to remove worktree {wt_path} (may not exist)")

        # Delete branch
        if git_mgr.delete_branch(branch, force=force):
            results.append(f"Deleted branch {branch}")
        else:
            results.append(f"Failed to delete branch {branch}")

        return ". ".join(results) + "."

    return [
        git_log,
        git_show,
        git_diff,
        git_status,
        git_tags,
        git_merge_squash,
        git_worktree_cleanup,
    ]
