"""Repository write tools for attached repository datasources.

These operate on the clones under ``repos/<name>/`` registered in
``workspace_manager.source_repos``, using the forge metadata recorded
alongside them in ``source_repo_meta``.

``read_only`` gating here prevents honest mistakes. It is NOT a security
boundary — the agent has a shell and can run git directly.
"""

import logging
from typing import Any, List, Optional

from langchain_core.tools import tool

from ...services.forge import ForgeError, ForgeRepo, open_pull_request
from ..context import ToolContext

logger = logging.getLogger(__name__)

REPO_TOOLS_METADATA = {
    "repo_commit": {
        "category": "repo",
        "description": "Stage all changes and commit them in an attached repository.",
        "short_description": "Stage and commit changes in an attached repository.",
    },
    "repo_push": {
        "category": "repo",
        "description": "Push a branch of an attached repository to its remote.",
        "short_description": "Push the current branch of an attached repository.",
    },
    "repo_pull": {
        "category": "repo",
        "description": "Fast-forward pull in an attached repository.",
        "short_description": "Fast-forward pull in an attached repository.",
    },
    "repo_open_pr": {
        "category": "repo",
        "description": (
            "Open a pull request (merge request on GitLab) for an attached repository."
        ),
        "short_description": "Open a pull/merge request for an attached repository.",
    },
}


def create_repo_tools(context: ToolContext) -> List[Any]:
    """Create the repo_* tools bound to this job's cloned repositories."""
    ws = context.workspace_manager

    def _resolve(repo: str) -> tuple[Any, dict] | str:
        """Return (git_manager, meta) or an error string naming valid repos."""
        repos = getattr(ws, "source_repos", {}) or {}
        meta_all = getattr(ws, "source_repo_meta", {}) or {}
        if repo not in repos:
            known = ", ".join(sorted(repos)) or "(none attached)"
            return f"Unknown repository {repo!r}. Attached repositories: {known}"
        return repos[repo], meta_all.get(repo, {})

    def _refuse_if_read_only(meta: dict, repo: str) -> Optional[str]:
        if meta.get("read_only"):
            return (
                f"Repository {repo!r} is attached read-only; "
                "only repo_pull is available."
            )
        return None

    @tool
    async def repo_commit(repo: str, message: str) -> str:
        """Stage all changes and commit them in an attached repository.

        Args:
            repo: Clone-directory name, as listed in datasources.md.
            message: Commit message.
        """
        resolved = _resolve(repo)
        if isinstance(resolved, str):
            return resolved
        git_mgr, meta = resolved
        refusal = _refuse_if_read_only(meta, repo)
        if refusal:
            return refusal
        if git_mgr.commit(message):
            return f"Committed in {repo}: {message}"
        return (
            f"Nothing to commit in {repo} (or the commit failed — check repo_status)."
        )

    @tool
    async def repo_push(repo: str, branch: Optional[str] = None) -> str:
        """Push a branch of an attached repository to its remote.

        Args:
            repo: Clone-directory name.
            branch: Branch to push; defaults to the currently checked-out one.
        """
        resolved = _resolve(repo)
        if isinstance(resolved, str):
            return resolved
        git_mgr, meta = resolved
        refusal = _refuse_if_read_only(meta, repo)
        if refusal:
            return refusal
        target = branch or git_mgr.current_branch()
        if git_mgr.push(branch=target):
            return f"Pushed {target} to {repo}'s remote."
        return (
            f"Push of {target} to {repo} failed. If the remote rejected it, the "
            "branch is probably protected — push a job branch instead."
        )

    @tool
    async def repo_pull(repo: str, branch: Optional[str] = None) -> str:
        """Fast-forward pull in an attached repository.

        Args:
            repo: Clone-directory name.
            branch: Branch to pull; defaults to the current one.
        """
        resolved = _resolve(repo)
        if isinstance(resolved, str):
            return resolved
        git_mgr, _meta = resolved
        if git_mgr.pull(branch=branch):
            return f"Pulled latest changes into {repo}."
        return f"Pull in {repo} failed (diverged history, or no remote configured)."

    @tool
    async def repo_open_pr(
        repo: str,
        title: str,
        base: str,
        body: str = "",
        head: Optional[str] = None,
    ) -> str:
        """Open a pull request (merge request on GitLab) for an attached repository.

        Push the branch first — the forge rejects a PR whose head does not exist.

        Args:
            repo: Clone-directory name.
            title: PR title.
            base: Branch to merge INTO (e.g. "develop").
            body: PR description.
            head: Branch to merge FROM; defaults to the current one.
        """
        resolved = _resolve(repo)
        if isinstance(resolved, str):
            return resolved
        git_mgr, meta = resolved
        refusal = _refuse_if_read_only(meta, repo)
        if refusal:
            return refusal
        if not meta.get("forge"):
            return (
                f"Repository {repo!r} has no forge recorded, so its API cannot be "
                "called. Set 'forge' on the connector and re-run the job."
            )

        source = head or git_mgr.current_branch()
        target = ForgeRepo(
            forge=meta["forge"],
            api_base=meta["api_base"],
            owner=meta["owner"],
            repo=meta["repo"],
            token=meta.get("token", ""),
        )
        try:
            result = await open_pull_request(
                target, title=title, head=source, base=base, body=body
            )
        except ForgeError as exc:
            return f"Could not open the pull request: {exc}"
        return f"Opened #{result['number']} ({source} → {base}): {result['url']}"

    return [repo_commit, repo_push, repo_pull, repo_open_pr]
