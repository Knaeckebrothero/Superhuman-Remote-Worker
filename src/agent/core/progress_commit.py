"""Progress-driven commit and push for worker jobs.

The worker historically committed and pushed only at phase boundaries — all
six ``git_mgr.push()`` calls live in :mod:`agent.core.phase`. Every external
view of a running job (the orchestrator, the cockpit, the MCP workspace
tools, the officer) is therefore defined as *"committed state as of the
worker's last phase-boundary push"*.

That was tolerable while phases were frequent — a boundary every few minutes
acted as an incidental heartbeat. It stops being tolerable as phases get
larger: a single long execution phase means hours in which the step count
climbs while every artifact an observer reads says nothing has happened.
That combination is worse than a visible stall, because it reads as
*evidence of no work* rather than as missing evidence, and it is what
produced the night-2 false convictions
(``knowledge-base/knowledge/issues/`` — officer findings F7/F8).

This module decouples durability from phase structure using two triggers:

**Semantic** — :meth:`ProgressCommitter.on_todo_complete` commits when a todo
is finished. A todo is a real unit of work, so history gets meaningful
messages instead of ``Auto-commit after turn 47``.

**Wall-clock floor** — :meth:`ProgressCommitter.on_turn` commits work in
progress once nothing has been committed for a while. This exists precisely
because the semantic trigger is *anti-correlated with need*: an agent
grinding on one hard todo emits no completions, so a todo-only policy goes
quiet exactly when an observer most needs to see movement. It is also the
backstop for the model simply forgetting to call ``todo_complete``, which is
a live failure mode — durability must not depend on a tool the model may
skip.

Commit and push are deliberately separated. Commit is local and cheap; push
is a network round-trip to Gitea, where we have already had to do
performance work. So we commit on every semantic event and push on a
throttle, which keeps history granular without amplifying load.

Nothing here is allowed to fail a job. Every git operation is wrapped, and
push failures are logged and forgotten — the same posture as the existing
boundary sites (``src/agent/core/phase.py:1146`` treats the push result as
advisory).
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# Commit subjects are a summary line, not a changelog. Longer todo bodies are
# truncated rather than wrapped so ``git log --oneline`` stays readable.
_MAX_SUBJECT_CHARS = 72


def _subject(text: str) -> str:
    """Flatten arbitrary todo text into a one-line commit subject."""
    flat = " ".join(str(text or "").split())
    if not flat:
        return "progress"
    if len(flat) <= _MAX_SUBJECT_CHARS:
        return flat
    return flat[: _MAX_SUBJECT_CHARS - 1].rstrip() + "…"


class ProgressCommitter:
    """Commits and pushes job progress independently of phase boundaries.

    A single instance is shared by the tool layer (``todo_complete``) and the
    graph's turn loop, so the two triggers share one push clock and cannot
    double-push. It is created per job alongside the workspace.

    Args:
        git_provider: Zero-argument callable returning the workspace's current
            ``GitManager``, or None when there is no git versioning. This is
            deliberately a callable rather than the manager itself: a workspace
            can be re-initialised or tier-upgraded mid-job, which replaces
            ``WorkspaceManager._git_manager`` (see src/agent/core/workspace.py). A
            handle captured once would then commit against a dead workspace, or
            silently stop committing at all.
        job_id: Used only for log correlation.
        push_interval: Minimum seconds between pushes. Commits are unaffected.
        wip_after: Seconds without a commit before :meth:`on_turn` commits
            work in progress. 0 or negative disables the floor.
        clock: Monotonic time source, injectable for tests.
    """

    def __init__(
        self,
        git_provider: Callable[[], Optional[Any]],
        *,
        job_id: str = "unknown",
        push_interval: float = 60.0,
        wip_after: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._git_provider = git_provider
        self._job_id = job_id
        self._push_interval = max(0.0, float(push_interval))
        self._wip_after = float(wip_after)
        self._clock = clock

        now = clock()
        self._last_push = now
        self._last_commit = now

        # Guards against concurrent git operations. Tool batches can dispatch
        # more than one call per turn, and two interleaved commits against the
        # same worktree would race on the index. Skipping is safe rather than
        # lossy: commit() stages the whole tree, so whichever call wins picks
        # up the other's changes too.
        self._busy = False

    # -- state ---------------------------------------------------------

    def _git(self) -> Optional[Any]:
        """Resolve the workspace's current GitManager, tolerating failure."""
        try:
            return self._git_provider()
        except Exception as e:  # noqa: BLE001 - never fail a job on git
            logger.debug(f"[{self._job_id}] git manager unavailable: {e}")
            return None

    @property
    def active(self) -> bool:
        """Whether there is a usable git manager behind this committer."""
        return self._git() is not None

    # -- triggers ------------------------------------------------------

    def on_todo_complete(self, todo_id: str, content: str) -> None:
        """Commit the work a finished todo produced, then push if due.

        Called from the ``todo_complete`` tool. Safe to call when git is
        unavailable, when the tree is clean, or when the remote is down.
        """
        label = _subject(content)
        message = f"{todo_id}: {label}" if todo_id else label
        self._commit_and_maybe_push(message, reason="todo")

    def on_turn(self) -> None:
        """Wall-clock floor — commit work in progress if the tree has gone stale.

        Cheap by construction: the elapsed check is local, so a job ticking
        todos at a healthy rate never reaches git here. Only an agent that has
        gone quiet — stuck, looping, or working a single long todo — trips it.

        Deliberately hooked on the tools node rather than every LLM turn: a
        text-only response cannot have dirtied the workspace, so there is
        nothing it could commit. Tool batches that fail or time out still reach
        it, which is the case that matters.
        """
        if self._wip_after <= 0:
            return
        if (self._clock() - self._last_commit) < self._wip_after:
            return
        self._commit_and_maybe_push("WIP: work in progress", reason="wip")

    def flush(self, message: str = "Progress checkpoint") -> None:
        """Commit and push unconditionally, ignoring the throttle.

        For callers that need the remote current before yielding control —
        drain, freeze, or handing a workspace to another observer.
        """
        self._commit_and_maybe_push(message, reason="flush", force_push=True)

    # -- internals -----------------------------------------------------

    def _commit_and_maybe_push(
        self, message: str, *, reason: str, force_push: bool = False
    ) -> None:
        if self._busy:
            return
        git = self._git()
        if git is None:
            return

        self._busy = True
        try:
            # Reset the commit clock regardless of outcome. A clean tree means
            # there is genuinely nothing to record, and retrying the git call
            # every turn would burn an SSH round-trip to learn that again.
            self._last_commit = self._clock()

            try:
                if not git.is_active:
                    return
                # allow_empty=False: an empty commit here would be noise. The
                # boundary sites in phase.py deliberately allow empty ones —
                # they mark a lifecycle event even for read-only work — but a
                # progress commit with no progress is not worth a rev.
                committed = git.commit(message, allow_empty=False)
            except Exception as e:  # noqa: BLE001 - never fail a job on git
                logger.warning(
                    f"[{self._job_id}] progress commit failed ({reason}): {e}"
                )
                committed = False

            if committed:
                logger.debug(f"[{self._job_id}] progress commit ({reason}): {message}")

            self._maybe_push(git, force=force_push, reason=reason)
        finally:
            self._busy = False

    def _maybe_push(self, git: Any, *, force: bool, reason: str) -> None:
        if not force and (self._clock() - self._last_push) < self._push_interval:
            return

        try:
            # Local-ref comparison, no network. Skipping a no-op push matters
            # because the boundary sites in phase.py push on their own clock;
            # without this check a todo completing just after a boundary would
            # spend a round-trip shipping nothing.
            if not git.has_unpushed_commits():
                self._last_push = self._clock()
                return
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[{self._job_id}] unpushed-commit check failed: {e}")

        try:
            git.push()
            logger.debug(f"[{self._job_id}] progress push ({reason})")
        except Exception as e:  # noqa: BLE001 - push failures are advisory
            logger.warning(f"[{self._job_id}] progress push failed ({reason}): {e}")
        finally:
            # Reset even on failure. A down remote should cost one attempt per
            # interval, not one per turn.
            self._last_push = self._clock()
