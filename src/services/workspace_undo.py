"""Durable, crash-recoverable workspace undo for persistent sessions.

Conversation rewind has a separate transcript/tombstone workflow. This module
implements the narrower ``undo`` action: restore the workspace tree to the
preceding durable turn checkpoint without rewriting conversation history.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from src.managers.git_manager import (
    WorkspaceUndoCommit,
    WorkspaceUndoInvariantViolation,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class WorkspaceUndoResult:
    """Durable details published in ``files.restored``."""

    paths: tuple[str, ...]
    restored_to_sha: str
    restore_commit_sha: str

    def event_params(self) -> dict[str, Any]:
        return {
            "paths": list(self.paths),
            "restored_to_sha": self.restored_to_sha,
            "restore_commit_sha": self.restore_commit_sha,
        }


class WorkspaceUndoUnavailable(RuntimeError):
    """A deterministic, safe-to-journal refusal before any workspace effect."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class WorkspaceUndoRetryable(RuntimeError):
    """An ambiguous/transient failure whose request must remain pending."""


async def _previous_distinct_ledger_commit(
    git_manager: Any,
    ledger_commits: tuple[str, ...],
) -> str | None:
    """Newest ledger commit whose file tree differs from current ``HEAD``."""

    head_tree = await asyncio.to_thread(git_manager.resolve_tree, "HEAD")
    if not head_tree:
        raise WorkspaceUndoRetryable(
            "Could not resolve the current workspace tree before undo"
        )

    for candidate_sha in ledger_commits:
        candidate_tree = await asyncio.to_thread(
            git_manager.resolve_tree,
            candidate_sha,
        )
        if not candidate_tree:
            raise WorkspaceUndoUnavailable(
                "workspace_undo_checkpoint_missing",
                "A workspace checkpoint in the durable undo chain is not present "
                "in Git",
            )
        if candidate_tree != head_tree:
            return candidate_sha
    return None


async def _current_undo_cursor(
    git_manager: Any,
    ledger_commits: tuple[str, ...],
) -> tuple[str, int] | None:
    """Return ``(target_sha, oldest_ledger_index)`` for a live undo cursor.

    Consecutive effects overwrite the newest transcript-position mapping, so
    scanning the full ledger by tree would oscillate A -> B after C -> B -> A.
    While HEAD still has the latest undo marker's target tree, its exact target
    SHA is the logical stack cursor. Only rows older than that target's oldest
    ledger occurrence are eligible. A later real file state changes HEAD (or
    leaves a dirty workspace handled before this helper) and resumes ordinary
    newest-distinct behavior.
    """

    try:
        latest_effect = await asyncio.to_thread(
            git_manager.find_latest_workspace_undo_commit
        )
    except WorkspaceUndoInvariantViolation as exc:
        raise WorkspaceUndoRetryable(
            "Workspace undo has an ambiguous latest Git marker"
        ) from exc
    if latest_effect is None:
        return None

    head_tree = await asyncio.to_thread(git_manager.resolve_tree, "HEAD")
    target_tree = await asyncio.to_thread(
        git_manager.resolve_tree,
        latest_effect.target_sha,
    )
    if not head_tree:
        raise WorkspaceUndoRetryable(
            "Could not resolve the current workspace tree before undo"
        )
    if not target_tree:
        raise WorkspaceUndoUnavailable(
            "workspace_undo_checkpoint_missing",
            "The latest workspace undo target is not present in Git",
        )
    if head_tree != target_tree:
        return None

    occurrences = [
        index
        for index, commit_sha in enumerate(ledger_commits)
        if commit_sha == latest_effect.target_sha
    ]
    if not occurrences:
        raise WorkspaceUndoUnavailable(
            "workspace_undo_checkpoint_missing",
            "The latest workspace undo target is absent from the durable ledger",
        )
    return latest_effect.target_sha, max(occurrences)


async def _ledger_before_current_undo_cursor(
    git_manager: Any,
    ledger_commits: tuple[str, ...],
) -> tuple[str, ...]:
    """Trim rows at/newer than the current validated undo target."""

    cursor = await _current_undo_cursor(git_manager, ledger_commits)
    if cursor is None:
        return ledger_commits
    _target_sha, oldest_index = cursor
    return ledger_commits[oldest_index + 1 :]


async def _current_logical_workspace_commit(
    git_manager: Any,
    ledger_commits: tuple[str, ...],
) -> str:
    """Durable state immediately before a fresh dirty workspace state."""

    cursor = await _current_undo_cursor(git_manager, ledger_commits)
    if cursor is not None:
        return cursor[0]

    head_tree = await asyncio.to_thread(git_manager.resolve_tree, "HEAD")
    if not head_tree:
        raise WorkspaceUndoRetryable(
            "Could not resolve the current workspace tree before undo"
        )
    for candidate_sha in ledger_commits:
        candidate_tree = await asyncio.to_thread(
            git_manager.resolve_tree,
            candidate_sha,
        )
        if not candidate_tree:
            raise WorkspaceUndoUnavailable(
                "workspace_undo_checkpoint_missing",
                "A workspace checkpoint in the durable undo chain is not present "
                "in Git",
            )
        if candidate_tree == head_tree:
            return candidate_sha
    raise WorkspaceUndoUnavailable(
        "workspace_undo_checkpoint_missing",
        "The current Git state is absent from the durable workspace ledger",
    )


async def _undo_effect_is_covered_by_ledger(
    git_manager: Any,
    effect: WorkspaceUndoCommit,
    ledger_commits: tuple[str, ...],
) -> bool:
    """Whether this marker is at/before a later durably mapped commit.

    ``record_turn_commit`` intentionally keeps one row per transcript seq, so
    consecutive undo effects overwrite that position.  A validated marker is
    nevertheless durably complete when it is an ancestor of any mapped
    descendant. This makes an old request UUID replayable after newer undos
    without repointing the current ledger row or changing today's worktree.
    """

    for candidate_sha in ledger_commits:
        if candidate_sha == effect.commit_sha:
            return True
        if await asyncio.to_thread(
            git_manager.is_ancestor,
            effect.commit_sha,
            candidate_sha,
        ):
            return True
    return False


async def apply_workspace_undo(
    *,
    thread_id: str,
    request_id: UUID | str,
    postgres: Any,
    workspace_manager: Any,
) -> WorkspaceUndoResult:
    """Apply or recover one exact workspace undo request.

    The Git effect commit is the idempotency record for the filesystem side of
    the operation. The caller still owns the run-queue lease fence used for its
    journal INSERT/finalization. Any failure after mutation remains retryable:
    a successor finds the marker, pushes it, records the turn mapping, and only
    then acknowledges the control.
    """

    try:
        canonical_request_id = UUID(str(request_id))
    except (TypeError, ValueError, AttributeError) as exc:
        raise WorkspaceUndoUnavailable(
            "workspace_undo_invalid_request",
            "Workspace undo requires a valid request identity",
        ) from exc

    git_manager = (
        getattr(workspace_manager, "git_manager", None)
        if workspace_manager is not None
        else None
    )
    git_active = (
        await asyncio.to_thread(lambda: git_manager.is_active)
        if git_manager is not None
        else False
    )
    if git_manager is None or not git_active:
        raise WorkspaceUndoUnavailable(
            "workspace_undo_unsupported",
            "This workspace tier has no durable Git history for undo",
        )
    if postgres is None or not hasattr(postgres, "list_workspace_turn_commits"):
        raise WorkspaceUndoUnavailable(
            "workspace_undo_unsupported",
            "This session has no durable turn ledger for undo",
        )

    raw_ledger_commits = await postgres.list_workspace_turn_commits(thread_id)
    if not isinstance(raw_ledger_commits, (list, tuple)) or any(
        not isinstance(value, str) or not value.strip() for value in raw_ledger_commits
    ):
        raise WorkspaceUndoUnavailable(
            "workspace_undo_checkpoint_missing",
            "The durable workspace checkpoint chain is invalid",
        )
    ledger_commits = tuple(value.strip() for value in raw_ledger_commits)

    try:
        effect = await asyncio.to_thread(
            git_manager.find_workspace_undo_commit, canonical_request_id
        )
    except WorkspaceUndoInvariantViolation as exc:
        raise WorkspaceUndoRetryable(
            "Workspace undo has an ambiguous Git idempotency marker"
        ) from exc

    effect_covered = False
    if effect is None:
        try:
            preparation = await asyncio.to_thread(
                git_manager.find_workspace_undo_preparation,
                canonical_request_id,
            )
        except WorkspaceUndoInvariantViolation as exc:
            raise WorkspaceUndoRetryable(
                "Workspace undo has an ambiguous Git preparation marker"
            ) from exc

        if preparation is not None:
            source_sha = await asyncio.to_thread(git_manager.get_current_commit)
            if source_sha != preparation.commit_sha:
                raise WorkspaceUndoRetryable(
                    "Workspace advanced beyond its undo preparation"
                )
            if preparation.target_sha not in ledger_commits:
                raise WorkspaceUndoRetryable(
                    "Workspace undo preparation target left the durable ledger"
                )
            target_sha = preparation.target_sha
            already_restored = await asyncio.to_thread(
                git_manager.workspace_matches_tree,
                target_sha,
            )
            if not already_restored and not await asyncio.to_thread(
                git_manager.workspace_matches_tree,
                preparation.commit_sha,
            ):
                raise WorkspaceUndoRetryable(
                    "Workspace changed after its undo preparation"
                )
        else:
            workspace_matches_head = await asyncio.to_thread(
                git_manager.workspace_matches_tree,
                "HEAD",
            )
            if workspace_matches_head:
                eligible_commits = await _ledger_before_current_undo_cursor(
                    git_manager,
                    ledger_commits,
                )
                target_sha = await _previous_distinct_ledger_commit(
                    git_manager,
                    eligible_commits,
                )
                if not target_sha:
                    raise WorkspaceUndoUnavailable(
                        "workspace_undo_no_checkpoint",
                        "No preceding durable workspace checkpoint is available",
                    )
            else:
                # Dirty files are a fresh logical state even when their bytes
                # happen to equal an older checkpoint. Only this request's
                # strict preparation marker may identify a restore crash.
                target_sha = await _current_logical_workspace_commit(
                    git_manager,
                    ledger_commits,
                )

            source_sha = await asyncio.to_thread(
                git_manager.commit_workspace_undo_preparation,
                request_id=canonical_request_id,
                target_sha=target_sha,
            )
            if not source_sha:
                raise WorkspaceUndoRetryable(
                    "Could not snapshot the current workspace before undo"
                )
            already_restored = False

        if not already_restored:
            restored = await asyncio.to_thread(git_manager.restore_tree, target_sha)
            if not restored:
                raise WorkspaceUndoRetryable(
                    "Could not restore the preceding workspace checkpoint"
                )

        effect_sha = await asyncio.to_thread(
            git_manager.commit_workspace_undo,
            request_id=canonical_request_id,
            source_sha=source_sha,
            target_sha=target_sha,
        )
        if not effect_sha:
            raise WorkspaceUndoRetryable(
                "Could not commit the restored workspace checkpoint"
            )
        try:
            effect = await asyncio.to_thread(
                git_manager.find_workspace_undo_commit, canonical_request_id
            )
        except WorkspaceUndoInvariantViolation as exc:
            raise WorkspaceUndoRetryable(
                "Workspace undo marker could not be verified after commit"
            ) from exc
        if effect is None or effect.commit_sha != effect_sha:
            raise WorkspaceUndoRetryable(
                "Workspace undo marker disappeared after commit"
            )
    else:
        effect_covered = await _undo_effect_is_covered_by_ledger(
            git_manager,
            effect,
            ledger_commits,
        )

    if not effect_covered:
        # Pending controls are drained before user input, so no valid work may
        # advance the tree beyond an as-yet-unmapped effect. Empty bookkeeping
        # commits are allowed; tracked/untracked workspace changes are not.
        head_matches = await asyncio.to_thread(
            git_manager.trees_match, "HEAD", effect.commit_sha
        )
        workspace_matches = await asyncio.to_thread(
            git_manager.workspace_matches_tree, effect.commit_sha
        )
        if not head_matches or not workspace_matches:
            raise WorkspaceUndoRetryable(
                "Workspace advanced beyond an unacknowledged undo effect"
            )

    paths = await asyncio.to_thread(
        git_manager.changed_paths, effect.source_sha, effect.commit_sha
    )
    if not paths:
        source_matches_target = await asyncio.to_thread(
            git_manager.trees_match, effect.source_sha, effect.commit_sha
        )
        if not source_matches_target:
            raise WorkspaceUndoRetryable(
                "Could not enumerate files changed by workspace undo"
            )

    # A failed/ambiguous push or mapping write leaves the request pending. The
    # same request marker makes both retry paths idempotent on another pod.
    if not await asyncio.to_thread(git_manager.push):
        raise WorkspaceUndoRetryable(
            "Workspace undo commit could not be pushed to durable history"
        )
    if not effect_covered:
        try:
            await postgres.record_turn_commit(thread_id, effect.commit_sha)
        except Exception as exc:
            raise WorkspaceUndoRetryable(
                "Workspace undo turn mapping could not be recorded"
            ) from exc

    logger.info(
        "workspace undo ready for acknowledgement: thread=%s request=%s "
        "paths=%d target=%s effect=%s",
        thread_id,
        canonical_request_id,
        len(paths),
        effect.target_sha,
        effect.commit_sha,
    )
    return WorkspaceUndoResult(
        paths=tuple(paths),
        restored_to_sha=effect.target_sha,
        restore_commit_sha=effect.commit_sha,
    )


__all__ = [
    "WorkspaceUndoResult",
    "WorkspaceUndoRetryable",
    "WorkspaceUndoUnavailable",
    "apply_workspace_undo",
]
