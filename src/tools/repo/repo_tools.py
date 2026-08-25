"""Repository write tools for attached repository datasources.

These operate on the clones under ``repos/<name>/`` registered in
``workspace_manager.source_repos``, using the forge metadata recorded
alongside them in ``source_repo_meta``.

``read_only`` gating here prevents honest mistakes. It is NOT a security
boundary — the agent has a shell and can run git directly.
"""

import logging
from typing import Any, List, Optional
from uuid import UUID

from langchain_core.tools import tool

from ...services.forge import (
    ForgeError,
    ForgeRepo,
    get_pull_request_status,
    open_pull_request,
)
from ..context import ToolContext

logger = logging.getLogger(__name__)

REPO_TOOLS_METADATA = {
    "repo_checkout": {
        "category": "repo",
        "description": (
            "Switch an attached repository to a branch, optionally creating it."
        ),
        "short_description": "Switch (or create) a branch in an attached repository.",
    },
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
    "repo_pr_status": {
        "category": "repo",
        "description": (
            "Read the live open, merged, or closed state of a pull request "
            "(merge request on GitLab) in an attached repository."
        ),
        "short_description": "Read live pull/merge request status.",
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

    def _refuse_write(meta: dict, repo: str) -> Optional[str]:
        """Refusal reason for a write on ``repo``, or None if it may proceed.

        Fails CLOSED on missing metadata. An empty dict is not proof that the
        repository is writable — it is proof that the clone could not record
        anything, e.g. an SSH-form connection_url whose host cannot be parsed.
        Treating that as "not read-only" turned a metadata bug into an
        unguarded push.
        """
        if not meta:
            return (
                f"Repository {repo!r} has no recorded connector metadata, so "
                "writes cannot be authorized. This usually means its "
                "connection URL is in SSH form (git@host:owner/repo) or its "
                "forge is unset — fix the connector and re-attach it. "
                "repo_pull still works, and the shell can drive git directly."
            )
        if meta.get("read_only"):
            return (
                f"Repository {repo!r} is attached read-only; "
                "only repo_pull is available."
            )
        return None

    @tool
    async def repo_checkout(repo: str, branch: str, create: bool = False) -> str:
        """Switch an attached repository to a branch, optionally creating it.

        Args:
            repo: Clone-directory name, as listed in datasources.md.
            branch: Branch to check out.
            create: Create the branch if it does not exist yet.
        """
        resolved = _resolve(repo)
        if isinstance(resolved, str):
            return resolved
        git_mgr, meta = resolved
        refusal = _refuse_write(meta, repo)
        if refusal:
            return refusal
        existed = git_mgr.rev_parse(branch) is not None
        if git_mgr.checkout_branch(branch, create=create):
            landed = git_mgr.current_branch() or branch
            if create and not existed:
                return f"Created and switched {repo} to branch '{landed}'."
            return f"Switched {repo} to branch '{landed}'."
        if create:
            return (
                f"Could not create or switch to branch '{branch}' in {repo} "
                "(invalid name, or conflicting local changes?)."
            )
        return (
            f"Could not switch {repo} to branch '{branch}'. If it does not "
            "exist yet, retry with create=True."
        )

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
        refusal = _refuse_write(meta, repo)
        if refusal:
            return refusal
        # allow_empty defaults to True on GitManager.commit — that would
        # manufacture an empty commit and report success, contradicting the
        # "nothing to commit" answer below.
        if git_mgr.commit(message, allow_empty=False):
            return (
                f"Committed on branch '{git_mgr.current_branch()}' in {repo}: {message}"
            )
        return (
            f"Nothing to commit in {repo} (or the commit failed). Inspect the "
            f"clone with the shell: `git -C repos/{repo} status`."
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
        refusal = _refuse_write(meta, repo)
        if refusal:
            return refusal
        target = branch or git_mgr.current_branch()
        # A ':' or leading '+' makes target a raw refspec pass-through;
        # "origin/<target>" would not resolve, so skip outcome verification.
        plain = bool(target) and ":" not in target and not target.startswith("+")
        local_sha = git_mgr.rev_parse(target) if plain else None
        before = git_mgr.rev_parse(f"origin/{target}") if local_sha else None
        if git_mgr.push(branch=target):
            after = git_mgr.rev_parse(f"origin/{target}") if local_sha else None
            if after is None:
                return f"Pushed {target} to {repo}'s remote."
            if after == before:
                return (
                    f"Push of '{target}' was a NO-OP: remote already at "
                    f"{before[:12]}; nothing was transferred. Your commits "
                    "may be on a different branch — check git_log."
                )
            old = before[:12] if before else "(new branch)"
            return f"Pushed '{target}' to {repo}'s remote: {old} -> {after[:12]}."
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
        refusal = _refuse_write(meta, repo)
        if refusal:
            return refusal
        if not meta.get("forge"):
            return (
                f"Repository {repo!r} has no forge recorded, so its API cannot be "
                "called. Set 'forge' on the connector and re-run the job."
            )

        checked_out = git_mgr.current_branch()
        requested_head = head.strip() if isinstance(head, str) else None
        if not checked_out:
            return f"Could not determine the checked-out branch in {repo!r}."
        if requested_head and requested_head != checked_out:
            return (
                "The PR source must be the branch currently checked out in "
                f"{repo!r} ({checked_out!r}); switch branches before retrying."
            )
        source = checked_out
        source_revision = git_mgr.rev_parse(source)
        pushed_revision = git_mgr.rev_parse(f"origin/{source}")
        if (
            not source_revision
            or not pushed_revision
            or source_revision != pushed_revision
        ):
            return (
                f"Branch {source!r} in {repo!r} is not proven at the pushed "
                "remote revision. Push it successfully before opening the PR."
            )
        normalized_base = base.strip()
        target = ForgeRepo(
            forge=meta["forge"],
            api_base=meta["api_base"],
            owner=meta["owner"],
            repo=meta["repo"],
            token=meta.get("token", ""),
        )
        try:
            result = await open_pull_request(
                target, title=title, head=source, base=normalized_base, body=body
            )
        except ForgeError as exc:
            return f"Could not open the pull request: {exc}"

        try:
            live = await get_pull_request_status(target, int(result["number"]))
        except ForgeError as exc:
            return (
                f"Opened #{result['number']}: {result['url']}\n"
                "Warning: the forge could not attest the opened PR identity "
                f"for completion: {exc}"
            )
        if (
            str(live.get("head") or "") != source
            or str(live.get("base") or "") != normalized_base
            or str(live.get("head_sha") or "").lower() != source_revision.lower()
        ):
            return (
                f"Opened #{result['number']}: {result['url']}\n"
                "Warning: the forge returned a different or incomplete source/base "
                "identity, so this PR was not recorded for completion."
            )

        pull_request = {
            "forge": target.forge,
            "repo": f"{target.owner}/{target.repo}",
            "number": result["number"],
            "url": str(live.get("url") or result["url"]),
            "head": source,
            "base": normalized_base,
        }
        recorded = False
        try:
            if context.postgres_db is not None and context.job_id:
                recorded = await context.postgres_db.jobs.record_pull_request(
                    UUID(str(context.job_id)),
                    UUID(str(meta.get("datasource_id") or "")),
                    pull_request,
                    source_revision=source_revision,
                )
        except Exception:  # noqa: BLE001 - the PR already exists; preserve its URL
            logger.exception(
                "Opened pull request %s for %s but could not persist it against job %s",
                result["number"],
                repo,
                context.job_id,
            )

        opened = (
            f"Opened #{result['number']} ({source} → {normalized_base}): "
            f"{result['url']}"
        )
        if recorded:
            return opened
        return (
            f"{opened}\nWarning: the pull request could not be recorded against "
            "this job. Keep the URL above and do not open a duplicate."
        )

    @tool
    async def repo_pr_status(repo: str, number: int) -> str:
        """Read a pull request's live state from its forge.

        This is a read operation and remains available on read-only repository
        connectors.

        Args:
            repo: Clone-directory name, as listed in datasources.md.
            number: Pull/merge request number in that repository.
        """
        resolved = _resolve(repo)
        if isinstance(resolved, str):
            return resolved
        _git_mgr, meta = resolved
        if not meta:
            return (
                f"Repository {repo!r} has no recorded connector metadata, so "
                "its pull request status cannot be read. Fix the connector's "
                "connection URL/forge and re-attach it."
            )
        if not meta.get("forge"):
            return (
                f"Repository {repo!r} has no forge recorded, so its pull "
                "request status cannot be read."
            )

        target = ForgeRepo(
            forge=meta["forge"],
            api_base=meta["api_base"],
            owner=meta["owner"],
            repo=meta["repo"],
            token=meta.get("token", ""),
        )
        try:
            result = await get_pull_request_status(target, number)
        except ForgeError as exc:
            return f"Could not read the pull request status: {exc}"

        draft = " draft" if result["draft"] else ""
        refs = ""
        if result["head"] or result["base"]:
            refs = f" ({result['head'] or '?'} → {result['base'] or '?'})"
        link = f": {result['url']}" if result["url"] else ""
        return (
            f"Pull request #{result['number']} is{draft} "
            f"{result['state'].upper()}{refs}{link}"
        )

    return [
        repo_checkout,
        repo_commit,
        repo_push,
        repo_pull,
        repo_open_pr,
        repo_pr_status,
    ]
