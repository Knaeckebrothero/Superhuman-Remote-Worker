"""Tests for ProgressCommitter (src/core/progress_commit.py).

The behaviour under test is a durability guarantee, not a performance
optimisation: an outside observer must be able to see that a running job is
producing work without waiting for a phase boundary. The cases that matter
most are the unhappy ones — a stuck agent, a down remote — so those get the
most coverage here.
"""

import pytest

from agent.core.progress_commit import ProgressCommitter, _subject


class FakeClock:
    """Manually advanced monotonic clock."""

    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeGit:
    """Minimal GitManager stand-in recording commits and pushes."""

    def __init__(self, *, active=True, dirty=True, unpushed=True):
        self.is_active = active
        self._dirty = dirty
        self._unpushed = unpushed
        self.commits = []
        self.pushes = 0
        self.push_attempts = 0
        self.commit_raises = None
        self.push_raises = None

    def commit(self, message, allow_empty=True):
        if self.commit_raises:
            raise self.commit_raises
        if not allow_empty and not self._dirty:
            return False
        self.commits.append(message)
        self._dirty = False
        self._unpushed = True
        return True

    def push(self, remote="origin", branch=None, tags=True):
        self.push_attempts += 1
        if self.push_raises:
            raise self.push_raises
        self.pushes += 1
        self._unpushed = False
        return True

    def has_unpushed_commits(self, remote="origin", branch=None):
        return self._unpushed

    def has_uncommitted_changes(self):
        return self._dirty

    def dirty(self):
        """Simulate the agent writing files."""
        self._dirty = True


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def git():
    return FakeGit()


def make(git, clock, **kwargs):
    kwargs.setdefault("push_interval", 60.0)
    kwargs.setdefault("wip_after", 300.0)
    # Provider callable, mirroring how agent.py wires the live workspace.
    return ProgressCommitter(lambda: git, job_id="job-1", clock=clock, **kwargs)


class TestTodoCommits:
    def test_commits_with_semantic_message(self, git, clock):
        c = make(git, clock)
        c.on_todo_complete("todo_3", "Add retry to the fetch path")
        assert git.commits == ["todo_3: Add retry to the fetch path"]

    def test_first_completion_pushes(self, git, clock):
        c = make(git, clock)
        # The push clock starts at construction, so nothing is due yet.
        c.on_todo_complete("todo_1", "one")
        assert git.pushes == 0

        clock.advance(61)
        git.dirty()
        c.on_todo_complete("todo_2", "two")
        assert git.pushes == 1

    def test_rapid_completions_commit_every_time_but_push_once(self, git, clock):
        """Commit is local and free; push is a Gitea round-trip and is throttled."""
        c = make(git, clock)
        clock.advance(61)
        for i in range(5):
            git.dirty()
            clock.advance(1)
            c.on_todo_complete(f"todo_{i}", f"task {i}")

        assert len(git.commits) == 5
        assert git.pushes == 1

    def test_clean_tree_produces_no_commit(self, git, clock):
        c = make(git, clock)
        git._dirty = False
        c.on_todo_complete("todo_1", "nothing changed on disk")
        assert git.commits == []

    def test_no_push_when_nothing_unpushed(self, git, clock):
        """A boundary push may have just run; don't spend a round-trip on nothing."""
        c = make(git, clock)
        git._dirty = False
        git._unpushed = False
        clock.advance(120)
        c.on_todo_complete("todo_1", "already pushed by the boundary")
        assert git.pushes == 0


class TestWipFloor:
    def test_no_op_before_interval(self, git, clock):
        c = make(git, clock)
        clock.advance(299)
        c.on_turn()
        assert git.commits == []

    def test_commits_after_interval(self, git, clock):
        c = make(git, clock)
        clock.advance(301)
        c.on_turn()
        assert git.commits == ["WIP: work in progress"]

    def test_stuck_agent_still_becomes_visible(self, git, clock):
        """The regression this whole module exists for.

        An agent grinding on one hard todo never calls todo_complete. Under the
        old phase-boundary-only policy it produced no commits for the entire
        phase, so the officer read an empty workspace while the step count
        climbed — evidence of no work rather than missing evidence.
        """
        c = make(git, clock)
        for _ in range(120):  # many turns, zero completions
            clock.advance(10)
            git.dirty()
            c.on_turn()

        assert len(git.commits) == 4  # 1200s elapsed / 300s floor
        assert all(m.startswith("WIP") for m in git.commits)
        assert git.pushes >= 1

    def test_healthy_job_never_trips_the_floor(self, git, clock):
        """Completing todos keeps resetting the commit clock."""
        c = make(git, clock)
        for i in range(10):
            clock.advance(100)
            git.dirty()
            c.on_todo_complete(f"todo_{i}", f"task {i}")
            clock.advance(1)
            c.on_turn()

        assert all(not m.startswith("WIP") for m in git.commits)

    def test_zero_disables_floor(self, git, clock):
        c = make(git, clock, wip_after=0)
        clock.advance(10_000)
        c.on_turn()
        assert git.commits == []

    def test_clean_tree_does_not_retry_git_every_turn(self, git, clock):
        """A clean tree resets the clock so we don't burn an SSH round-trip per turn."""
        c = make(git, clock)
        git._dirty = False
        clock.advance(301)
        c.on_turn()
        clock.advance(1)
        c.on_turn()
        assert git.commits == []


class TestFailuresAreNonFatal:
    def test_commit_exception_swallowed(self, git, clock):
        c = make(git, clock)
        git.commit_raises = RuntimeError("git index locked")
        c.on_todo_complete("todo_1", "work")  # must not raise

    def test_push_exception_swallowed(self, git, clock):
        c = make(git, clock)
        git.push_raises = RuntimeError("gitea unreachable")
        clock.advance(61)
        c.on_todo_complete("todo_1", "work")  # must not raise
        assert git.commits  # the commit still landed locally

    def test_failed_push_retries_once_per_interval_not_per_turn(self, git, clock):
        """A down remote should cost one attempt per interval, not one per call."""
        c = make(git, clock)
        git.push_raises = RuntimeError("gitea unreachable")
        clock.advance(61)

        # Ten completions inside one throttle window.
        for i in range(10):
            clock.advance(1)
            git.dirty()
            c.on_todo_complete(f"todo_{i}", "work")

        assert len(git.commits) == 10  # every commit lands locally
        assert git.push_attempts == 1  # the failure did not un-throttle us

        # Once the interval elapses, we try again — exactly once more.
        clock.advance(61)
        git.dirty()
        c.on_todo_complete("todo_next", "work")
        assert git.push_attempts == 2

    def test_inactive_git_is_a_no_op(self, clock):
        git = FakeGit(active=False)
        c = make(git, clock)
        c.on_todo_complete("todo_1", "work")
        clock.advance(10_000)
        c.on_turn()
        assert git.commits == []
        assert git.pushes == 0

    def test_missing_git_manager_is_a_no_op(self, clock):
        c = ProgressCommitter(lambda: None, job_id="job-1", clock=clock)
        assert c.active is False
        c.on_todo_complete("todo_1", "work")  # must not raise
        c.on_turn()
        c.flush()


class TestFlush:
    def test_flush_ignores_the_throttle(self, git, clock):
        c = make(git, clock)
        c.flush("Job frozen: waiting for reply")
        assert git.commits == ["Job frozen: waiting for reply"]
        assert git.pushes == 1

    def test_flush_still_skips_a_no_op_push(self, git, clock):
        c = make(git, clock)
        git._dirty = False
        git._unpushed = False
        c.flush()
        assert git.pushes == 0


class TestLazyGitResolution:
    """The manager is resolved per call, not captured at construction.

    A workspace can be re-initialised or tier-upgraded mid-job, which replaces
    WorkspaceManager._git_manager. A committer holding the original handle
    would keep committing against a dead workspace, or stop committing without
    ever saying so.
    """

    def test_picks_up_a_swapped_manager(self, clock):
        first, second = FakeGit(), FakeGit()
        current = {"git": first}
        c = ProgressCommitter(
            lambda: current["git"], job_id="job-1", clock=clock, push_interval=60
        )

        c.on_todo_complete("todo_1", "before upgrade")
        current["git"] = second  # workspace tier upgrade swaps the manager
        c.on_todo_complete("todo_2", "after upgrade")

        assert first.commits == ["todo_1: before upgrade"]
        assert second.commits == ["todo_2: after upgrade"]

    def test_provider_returning_none_is_a_no_op(self, clock):
        current = {"git": FakeGit()}
        c = ProgressCommitter(lambda: current["git"], job_id="job-1", clock=clock)
        current["git"] = None
        c.on_todo_complete("todo_1", "workspace went away")  # must not raise
        clock.advance(10_000)
        c.on_turn()

    def test_provider_raising_is_a_no_op(self, clock):
        def boom():
            raise RuntimeError("workspace not initialised")

        c = ProgressCommitter(boom, job_id="job-1", clock=clock)
        assert c.active is False
        c.on_todo_complete("todo_1", "work")  # must not raise
        c.on_turn()
        c.flush()


class TestTodoToolWiring:
    """The seam between the todo_complete tool and the committer.

    Unit-testing the committer proves the policy; this proves it is actually
    reached. That wiring is the part most likely to rot silently, because
    nothing else fails when a progress commit quietly stops happening.
    """

    def _tool(self, context):
        from agent.tools.core.todo import create_todo_tools

        return next(t for t in create_todo_tools(context) if t.name == "todo_complete")

    def _context(self, todo_mgr, committer):
        from unittest.mock import MagicMock

        from agent.tools.context import ToolContext

        ctx = ToolContext(workspace_manager=MagicMock(), todo_manager=todo_mgr)
        ctx.recall_store = None
        ctx.progress_committer = committer
        return ctx

    def _todo_manager(self):
        from unittest.mock import MagicMock

        from agent.managers.todo import TodoManager

        mgr = TodoManager(workspace=MagicMock())
        # TodoManager enforces a min_todos floor (default 5).
        mgr.stage_tactical_todos(
            [f"Do the thing number {i} properly" for i in range(5)],
        )
        mgr.apply_staged_todos()
        return mgr

    def test_explicit_id_path_commits(self, git, clock):
        committer = make(git, clock)
        mgr = self._todo_manager()
        tool = self._tool(self._context(mgr, committer))

        first = mgr.list_all()[0]
        tool.invoke({"todo_id": first.id})

        assert git.commits == [f"{first.id}: {first.content}"]

    def test_implicit_first_pending_path_commits(self, git, clock):
        """The no-argument path has to carry the id/content through too."""
        committer = make(git, clock)
        mgr = self._todo_manager()
        tool = self._tool(self._context(mgr, committer))

        expected = mgr.list_all()[0]
        tool.invoke({})

        assert git.commits == [f"{expected.id}: {expected.content}"]

    def test_explicit_completion_note_is_durable_and_injected(self, git, clock):
        committer = make(git, clock)
        mgr = self._todo_manager()
        tool = self._tool(self._context(mgr, committer))

        first = mgr.list_all()[0]
        result = tool.invoke(
            {
                "todo_id": first.id,
                "completion_note": "GAPS: missing the limitations section",
            }
        )

        assert first.notes == ["GAPS: missing the limitations section"]
        assert "Recorded completion note" in result
        injection = mgr.format_for_injection()
        assert "Outcome: GAPS: missing the limitations section" in injection

    def test_implicit_completion_note_is_persisted(self, git, clock):
        committer = make(git, clock)
        mgr = self._todo_manager()
        tool = self._tool(self._context(mgr, committer))

        first = mgr.list_all()[0]
        tool.invoke({"completion_note": "PASS: all requested outputs verified"})

        assert first.notes == ["PASS: all requested outputs verified"]

    def test_oversized_completion_note_is_truncated_and_accepted(self, git, clock):
        from agent.tools.core.todo import MAX_COMPLETION_NOTE_CHARS

        committer = make(git, clock)
        mgr = self._todo_manager()
        tool = self._tool(self._context(mgr, committer))

        first = mgr.list_all()[0]
        original_len = MAX_COMPLETION_NOTE_CHARS + 387
        result = tool.invoke(
            {
                "todo_id": first.id,
                "completion_note": "x" * original_len,
            }
        )

        assert first.status.value == "completed"
        (stored,) = first.notes
        assert len(stored) <= MAX_COMPLETION_NOTE_CHARS
        assert stored.endswith(f"…[truncated from {original_len} chars]")
        assert "was truncated" in result
        assert git.commits == [f"{first.id}: {first.content}"]

    def test_completion_note_at_cap_is_stored_untouched(self, git, clock):
        from agent.tools.core.todo import MAX_COMPLETION_NOTE_CHARS

        committer = make(git, clock)
        mgr = self._todo_manager()
        tool = self._tool(self._context(mgr, committer))

        first = mgr.list_all()[0]
        note = "x" * MAX_COMPLETION_NOTE_CHARS
        result = tool.invoke({"todo_id": first.id, "completion_note": note})

        assert first.status.value == "completed"
        assert first.notes == [note]
        assert "truncated" not in result

    def test_no_committer_configured_is_harmless(self, git):
        """Persistent sessions leave progress_committer unset."""
        mgr = self._todo_manager()
        tool = self._tool(self._context(mgr, None))
        result = tool.invoke({})
        assert "Completed" in result
        assert git.commits == []

    def test_git_failure_does_not_fail_the_tool(self, git, clock):
        committer = make(git, clock)
        git.commit_raises = RuntimeError("git index locked")
        mgr = self._todo_manager()
        tool = self._tool(self._context(mgr, committer))

        result = tool.invoke({})
        assert "Error completing task" not in result


class TestSubject:
    def test_flattens_newlines(self):
        assert _subject("line one\n  line two") == "line one line two"

    def test_truncates_long_content(self):
        out = _subject("x" * 200)
        assert len(out) <= 72
        assert out.endswith("…")

    def test_empty_falls_back(self):
        assert _subject("") == "progress"
        assert _subject(None) == "progress"


class TestConcurrencyGuard:
    def test_reentrant_call_is_skipped(self, git, clock):
        """Tool batches can dispatch more than one call per turn.

        Two interleaved commits would race on the same worktree index. Skipping
        is safe rather than lossy: commit() stages the whole tree, so the call
        that wins picks up the other's changes too.
        """
        c = make(git, clock)
        calls = []

        original = git.commit

        def reentrant(message, allow_empty=True):
            calls.append(message)
            if len(calls) == 1:
                c.on_todo_complete("todo_2", "nested")
            return original(message, allow_empty=allow_empty)

        git.commit = reentrant
        c.on_todo_complete("todo_1", "outer")

        assert calls == ["todo_1: outer"]
