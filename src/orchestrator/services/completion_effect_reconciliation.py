"""Effect-specific reconciliation for durable job completion.

Only effects whose external system can answer an exact, command-keyed probe
belong here.  A missing database marker is not evidence that an external
action did not commit; these helpers distinguish a proven absence from an
ambiguous probe failure before a finalizer is allowed to repeat the action.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID


_COMMAND_TRAILER = "SRW-Completion-Command"
_EFFECT_TRAILER = "SRW-Completion-Effect"
_GRAFT_OUTPUT_TRAILER = "SRW-Graft-Output"
_SAFE_GRAFT_OUTPUT = re.compile(r"^outputs/[A-Za-z0-9._/-]+$")
_SAFE_EFFECT_KIND = re.compile(r"^[a-z][a-z0-9-]{0,63}$")


class CompletionEffectProbeError(RuntimeError):
    """An external effect probe was ambiguous and the action must not repeat."""


@dataclass(frozen=True, slots=True)
class GraftCommitProbe:
    """Exact evidence that one completion command already committed a graft."""

    commit_sha: str
    output_path: str


@dataclass(frozen=True, slots=True)
class CompletionCommitProbe:
    """Exact evidence that a command-keyed external commit already landed."""

    commit_sha: str


@dataclass(frozen=True, slots=True)
class CompletionPullRequestProbe:
    """Exact evidence that a command-keyed pull request already exists."""

    pr_index: int
    state: str


def _canonical_key(command_id: str, effect_kind: str) -> tuple[str, str]:
    canonical_command_id = str(UUID(str(command_id)))
    if not _SAFE_EFFECT_KIND.fullmatch(effect_kind):
        raise ValueError("unsafe completion effect kind")
    return canonical_command_id, effect_kind


def completion_pr_title(
    title: str, *, command_id: str, effect_kind: str, max_length: int = 240
) -> str:
    """Add a visible, exact command key to a durable completion PR title."""

    canonical_command_id, canonical_effect = _canonical_key(command_id, effect_kind)
    marker = (
        f" [SRW-Completion-Command: {canonical_command_id}; Effect: {canonical_effect}]"
    )
    if max_length <= len(marker):
        raise ValueError("PR title limit is too short for completion marker")
    return f"{title[: max_length - len(marker)].rstrip()}{marker}"


def completion_pr_body(body: str, *, command_id: str, effect_kind: str) -> str:
    """Add machine-readable command/effect trailers to a durable PR body."""

    canonical_command_id, canonical_effect = _canonical_key(command_id, effect_kind)
    return (
        f"{body.rstrip()}\n\n"
        f"{_COMMAND_TRAILER}: {canonical_command_id}\n"
        f"{_EFFECT_TRAILER}: {canonical_effect}"
    )


def completion_commit_message(
    message: str, *, command_id: str, effect_kind: str
) -> str:
    """Add the natural key used to reconcile a durable external commit."""

    canonical_command_id, canonical_effect = _canonical_key(command_id, effect_kind)
    return (
        f"{message.rstrip()}\n\n"
        f"{_COMMAND_TRAILER}: {canonical_command_id}\n"
        f"{_EFFECT_TRAILER}: {canonical_effect}"
    )


def graft_commit_message(
    *, output_path: str, subjob_short_id: str, command_id: str
) -> str:
    """Build the commit message whose trailers form S26's durable natural key."""

    canonical_command_id = str(UUID(str(command_id)))
    if not _SAFE_GRAFT_OUTPUT.fullmatch(output_path):
        raise ValueError("unsafe graft output path")
    return (
        f"Graft {output_path} from subjob {subjob_short_id}\n\n"
        f"{_COMMAND_TRAILER}: {canonical_command_id}\n"
        f"{_GRAFT_OUTPUT_TRAILER}: {output_path}"
    )


def _trailers(message: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for line in message.splitlines():
        key, separator, value = line.partition(": ")
        if separator and key in {
            _COMMAND_TRAILER,
            _EFFECT_TRAILER,
            _GRAFT_OUTPUT_TRAILER,
        }:
            parsed[key] = value.strip()
    return parsed


async def probe_completion_commit(
    gitea: Any,
    *,
    repo_name: str,
    branch: str,
    command_id: str,
    effect_kind: str,
    page_size: int = 50,
) -> CompletionCommitProbe | None:
    """Find one exact command/effect commit in an exhaustively read history.

    ``None`` is a proven absence.  An unavailable page, a malformed keyed
    commit, or multiple commits carrying the same natural key is ambiguous and
    must park/retry rather than authorize another external write.
    """

    canonical_command_id, canonical_effect = _canonical_key(command_id, effect_kind)
    found: CompletionCommitProbe | None = None
    page = 1
    while True:
        commits = await gitea.get_commits(
            repo_name,
            sha=branch,
            page=page,
            limit=page_size,
        )
        if commits is None:
            raise CompletionEffectProbeError(
                f"cannot reconcile {canonical_effect} command "
                f"{canonical_command_id}: Gitea commit history is unavailable"
            )
        if not commits:
            return found
        for commit in commits:
            trailers = _trailers(str(commit.get("message") or ""))
            if trailers.get(_COMMAND_TRAILER) != canonical_command_id:
                continue
            if trailers.get(_EFFECT_TRAILER) != canonical_effect:
                raise CompletionEffectProbeError(
                    f"command {canonical_command_id} has a commit with a "
                    "missing or mismatched completion-effect trailer"
                )
            commit_sha = str(commit.get("sha") or "")
            if not commit_sha:
                raise CompletionEffectProbeError(
                    f"{canonical_effect} command {canonical_command_id} has "
                    "no commit SHA"
                )
            if found is not None:
                raise CompletionEffectProbeError(
                    f"{canonical_effect} command {canonical_command_id} has "
                    "multiple matching commits"
                )
            found = CompletionCommitProbe(commit_sha=commit_sha)
        page += 1


async def probe_completion_pull_request(
    gitea: Any,
    *,
    repo_name: str,
    head: str,
    base: str,
    command_id: str,
    effect_kind: str,
    page_size: int = 50,
) -> CompletionPullRequestProbe | None:
    """Find one exact command/effect PR, including closed and merged PRs.

    The command must be present in both the title and body, and the PR must
    match the exact head/base pair.  A partially matching or duplicated key is
    corruption, not absence, because creating another PR would recreate S33's
    create-before-intent crash window.
    """

    canonical_command_id, canonical_effect = _canonical_key(command_id, effect_kind)
    title_marker = (
        f"[SRW-Completion-Command: {canonical_command_id}; Effect: {canonical_effect}]"
    )
    found: CompletionPullRequestProbe | None = None
    page = 1
    while True:
        pulls = await gitea.list_pull_requests(
            repo_name,
            state="all",
            page=page,
            limit=page_size,
        )
        if pulls is None:
            raise CompletionEffectProbeError(
                f"cannot reconcile {canonical_effect} command "
                f"{canonical_command_id}: Gitea pull-request list is unavailable"
            )
        if not pulls:
            return found
        for pull in pulls:
            title_matches = str(pull.get("title") or "").endswith(title_marker)
            trailers = _trailers(str(pull.get("body") or ""))
            body_mentions_command = (
                trailers.get(_COMMAND_TRAILER) == canonical_command_id
            )
            if not title_matches and not body_mentions_command:
                continue
            valid = (
                title_matches
                and body_mentions_command
                and trailers.get(_EFFECT_TRAILER) == canonical_effect
                and pull.get("head") == head
                and pull.get("base") == base
            )
            pr_index = pull.get("number")
            valid = (
                valid
                and isinstance(pr_index, int)
                and not isinstance(pr_index, bool)
                and pr_index > 0
            )
            if not valid:
                raise CompletionEffectProbeError(
                    f"{canonical_effect} command {canonical_command_id} has a "
                    "pull request with mismatched marker, head, base, or index"
                )
            if found is not None:
                raise CompletionEffectProbeError(
                    f"{canonical_effect} command {canonical_command_id} has "
                    "multiple matching pull requests"
                )
            found = CompletionPullRequestProbe(
                pr_index=pr_index,
                state=str(pull.get("state") or ""),
            )
        page += 1


async def probe_graft_commit(
    gitea: Any,
    *,
    repo_name: str,
    branch: str,
    command_id: str,
    page_size: int = 50,
) -> GraftCommitProbe | None:
    """Find an S26 commit by its exact command trailer.

    ``None`` means the branch history was exhaustively read and the key was
    absent.  A Gitea error is deliberately an exception rather than absence:
    repeating the graft after an ambiguous probe recreates the original
    commit-before-marker double-graft window.
    """

    canonical_command_id = str(UUID(str(command_id)))
    page = 1
    while True:
        commits = await gitea.get_commits(
            repo_name,
            sha=branch,
            page=page,
            limit=page_size,
        )
        if commits is None:
            raise CompletionEffectProbeError(
                f"cannot reconcile graft command {canonical_command_id}: "
                "Gitea commit history is unavailable"
            )
        if not commits:
            return None
        for commit in commits:
            trailers = _trailers(str(commit.get("message") or ""))
            if trailers.get(_COMMAND_TRAILER) != canonical_command_id:
                continue
            output_path = trailers.get(_GRAFT_OUTPUT_TRAILER, "")
            if not _SAFE_GRAFT_OUTPUT.fullmatch(output_path):
                raise CompletionEffectProbeError(
                    f"graft command {canonical_command_id} has an invalid "
                    "or missing output-path trailer"
                )
            commit_sha = str(commit.get("sha") or "")
            if not commit_sha:
                raise CompletionEffectProbeError(
                    f"graft command {canonical_command_id} has no commit SHA"
                )
            return GraftCommitProbe(
                commit_sha=commit_sha,
                output_path=output_path,
            )
        page += 1
