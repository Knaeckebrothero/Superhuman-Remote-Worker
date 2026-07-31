"""Tests for request_replan — the in-flight adaptation path.

This replaced `todo_rewind`, which was a genuine rewind: it archived every
todo (including the completed ones) into `archive/failed_<time>.md` and
emptied the list, so the strategic phase that followed inherited no record of
what had actually been achieved and had to reconstruct it. It also only
reached the strategic phase *indirectly*, by leaving the todo list empty until
`check_todos` hit its "no todos in tactical phase — forcing phase complete to
recover" branch, which exists for resume bugs.

The replacement keeps every todo with its real status and asks for the phase
boundary explicitly. That matters more as tactical phases grow: with roughly
three phases per job it is the only way to adapt mid-job.
"""

from unittest.mock import MagicMock

import pytest

from src.graph import create_check_todos_node, create_handle_transition_node
from src.managers.todo import TodoManager
from src.tools.context import ToolContext


def make_config():
    config = MagicMock()
    config.extra = {}  # must be a real dict — MagicMock loops yaml.safe_load
    config.phase_settings = MagicMock(min_todos=5, max_todos=20)
    return config


def make_todo_manager(completed=0, total=5):
    mgr = TodoManager(workspace=MagicMock())
    mgr.stage_tactical_todos(
        [f"Do the thing number {i} properly" for i in range(total)]
    )
    mgr.apply_staged_todos()
    for todo in mgr.list_all()[:completed]:
        mgr.complete(todo.id)
    return mgr


def make_context(todo_mgr):
    ws = MagicMock()
    ws.exists.return_value = True
    ws.read_file.return_value = "x" * 100
    return ToolContext(workspace_manager=ws, todo_manager=todo_mgr, _job_id="job-1")


def get_tool(context):
    from src.tools.core.todo import create_todo_tools

    return next(t for t in create_todo_tools(context) if t.name == "request_replan")


class TestNothingIsLost:
    """The whole point: a replan is not an undo."""

    def test_active_todos_survive(self):
        mgr = make_todo_manager(completed=2, total=5)
        ctx = make_context(mgr)

        get_tool(ctx).invoke({"reason": "The API caps batches at 100 items"})

        assert len(mgr.list_all()) == 5

    def test_completed_todos_stay_completed(self):
        """The old tool archived these as 'failed' along with everything else."""
        mgr = make_todo_manager(completed=3, total=5)
        ctx = make_context(mgr)

        get_tool(ctx).invoke({"reason": "found a blocker"})

        statuses = [t.status.value for t in mgr.list_all()]
        assert statuses.count("completed") == 3

    def test_pending_todos_stay_pending(self):
        mgr = make_todo_manager(completed=1, total=5)
        ctx = make_context(mgr)

        get_tool(ctx).invoke({"reason": "found a blocker"})

        statuses = [t.status.value for t in mgr.list_all()]
        assert statuses.count("completed") == 1
        assert len(statuses) == 5

    def test_no_failure_archive_is_written(self):
        """archive_phase records the real statuses at the boundary instead."""
        mgr = make_todo_manager(completed=2, total=5)
        mgr.archive_with_failure_note = MagicMock()
        ctx = make_context(mgr)

        get_tool(ctx).invoke({"reason": "found a blocker"})

        mgr.archive_with_failure_note.assert_not_called()

    def test_progress_is_reported_back(self):
        mgr = make_todo_manager(completed=3, total=5)
        ctx = make_context(mgr)

        result = get_tool(ctx).invoke({"reason": "found a blocker"})

        assert "3/5" in result
        assert "nothing was" in result.lower()


class TestRequestSignal:
    def test_reason_is_parked_on_the_context(self):
        mgr = make_todo_manager()
        ctx = make_context(mgr)

        get_tool(ctx).invoke({"reason": "the schema changed underneath us"})

        assert ctx.consume_replan_request() == "the schema changed underneath us"

    def test_reason_is_consumed_once(self):
        mgr = make_todo_manager()
        ctx = make_context(mgr)
        get_tool(ctx).invoke({"reason": "x marks the spot"})

        assert ctx.consume_replan_request() == "x marks the spot"
        assert ctx.consume_replan_request() is None

    def test_empty_reason_is_rejected_and_sets_nothing(self):
        mgr = make_todo_manager()
        ctx = make_context(mgr)

        result = get_tool(ctx).invoke({"reason": "   "})

        assert "Error" in result
        assert ctx.consume_replan_request() is None


class TestCheckTodosEndsThePhase:
    def test_replan_completes_the_phase_with_todos_outstanding(self):
        """Without a replan, incomplete todos keep the phase running."""
        mgr = make_todo_manager(completed=1, total=5)
        ctx = make_context(mgr)
        node = create_check_todos_node(mgr, make_config(), tool_context=ctx)

        # Baseline: still work to do, phase continues.
        assert (
            node({"job_id": "job-1", "is_strategic_phase": False})["phase_complete"]
            is False
        )

        get_tool(ctx).invoke({"reason": "approach is wrong"})
        result = node({"job_id": "job-1", "is_strategic_phase": False})

        assert result["phase_complete"] is True
        assert result["replan_reason"] == "approach is wrong"

    def test_replan_exports_todo_state_for_the_checkpoint(self):
        mgr = make_todo_manager(completed=2, total=5)
        ctx = make_context(mgr)
        node = create_check_todos_node(mgr, make_config(), tool_context=ctx)
        get_tool(ctx).invoke({"reason": "approach is wrong"})

        result = node({"job_id": "job-1", "is_strategic_phase": False})

        assert len(result["todos"]) == 5

    def test_no_tool_context_is_harmless(self):
        mgr = make_todo_manager(completed=5, total=5)
        node = create_check_todos_node(mgr, make_config(), tool_context=None)

        assert (
            node({"job_id": "job-1", "is_strategic_phase": False})["phase_complete"]
            is True
        )


class TestTransitionCarriesTheReason:
    def _node(self):
        return create_handle_transition_node(
            MagicMock(),
            make_todo_manager(),
            make_config(),
            min_todos=5,
            max_todos=20,
        )

    @pytest.mark.asyncio
    async def test_reason_reaches_the_strategic_phase(self):
        result = await self._node()(
            {
                "job_id": "job-1",
                "is_strategic_phase": False,
                "phase_number": 2,
                "iteration": 5,
                "replan_reason": "the API caps batches at 100 items",
            }
        )

        bodies = [getattr(m, "content", "") for m in result.get("messages", [])]
        replan = [b for b in bodies if "[REPLAN REQUESTED]" in b]
        assert replan, "the incoming strategic phase was told nothing"
        assert "the API caps batches at 100 items" in replan[0]

    @pytest.mark.asyncio
    async def test_reason_is_cleared_so_it_cannot_steer_a_later_phase(self):
        result = await self._node()(
            {
                "job_id": "job-1",
                "is_strategic_phase": False,
                "phase_number": 2,
                "iteration": 5,
                "replan_reason": "one-time reason",
            }
        )

        assert result.get("replan_reason") is None

    @pytest.mark.asyncio
    async def test_ordinary_transition_says_nothing_about_replanning(self):
        result = await self._node()(
            {
                "job_id": "job-1",
                "is_strategic_phase": False,
                "phase_number": 2,
                "iteration": 5,
            }
        )

        bodies = [getattr(m, "content", "") for m in result.get("messages", [])]
        assert not any("[REPLAN REQUESTED]" in b for b in bodies)


class TestRegistration:
    def test_tool_is_tactical_only(self):
        from src.tools.core.todo import TODO_TOOLS_METADATA

        assert TODO_TOOLS_METADATA["request_replan"]["phases"] == ["tactical"]

    def test_old_name_is_gone(self):
        from src.tools.core.todo import TODO_TOOLS_METADATA

        assert "todo_rewind" not in TODO_TOOLS_METADATA
