"""Tests for staged todos clearing in job_complete and request_replan.

Verifies that:
- job_complete auto-clears staged todos instead of rejecting (deadlock fix)
- request_replan clears staged todos but keeps active ones
"""

import pytest

from tests._tool_invoke import invoke_tool
import sys
from pathlib import Path
from unittest.mock import MagicMock

# Add project root to path
project_root = Path(__file__).parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from src.managers.todo import TodoManager  # noqa: E402
from src.tools.context import ToolContext  # noqa: E402


# =============================================================================
# Helpers
# =============================================================================


def make_context(todo_manager: TodoManager) -> ToolContext:
    """Create a ToolContext with a real TodoManager and mock workspace."""
    ws = MagicMock()
    ws.exists.return_value = True
    ws.read_file.return_value = "x" * 100  # passes the >50 byte check
    ws.write_file = MagicMock()

    ctx = ToolContext(
        workspace_manager=ws,
        todo_manager=todo_manager,
        _job_id="test-job",
    )
    return ctx


def stage_some_todos(todo_mgr: TodoManager) -> None:
    """Stage a valid set of tactical todos."""
    todo_mgr.stage_tactical_todos(
        [f"Todo item {i} with enough characters" for i in range(5)],
        phase_name="test phase",
    )
    assert todo_mgr.has_staged_todos()


# =============================================================================
# Test: job_complete auto-clears staged todos
# =============================================================================


class TestJobCompleteClears:
    """Verify job_complete clears stale staged todos instead of rejecting."""

    def _get_job_complete(self, context: ToolContext):
        """Create and return the job_complete tool function."""
        from src.tools.core.job import create_job_tools

        tools = create_job_tools(context)
        return next(t for t in tools if t.name == "job_complete")

    @pytest.mark.asyncio
    async def test_clears_staged_todos_and_proceeds(self):
        """job_complete should clear staged todos and mark phase as final."""
        todo_mgr = TodoManager(workspace=MagicMock())
        todo_mgr.is_strategic_phase = True
        stage_some_todos(todo_mgr)

        ctx = make_context(todo_mgr)
        job_complete = self._get_job_complete(ctx)

        result = await invoke_tool(
            job_complete,
            {
                "summary": "Job is done.",
                "deliverables": ["output/result.md"],
                "confidence": 0.9,
            },
        )

        # Should NOT contain the old rejection error
        assert "ERROR" not in result
        assert "staged todos" not in result.lower() or "final" in result.lower()

        # Staged todos should be cleared
        assert not todo_mgr.has_staged_todos()

    @pytest.mark.asyncio
    async def test_proceeds_without_staged_todos(self):
        """job_complete works normally when no staged todos exist."""
        todo_mgr = TodoManager(workspace=MagicMock())
        todo_mgr.is_strategic_phase = True

        ctx = make_context(todo_mgr)
        job_complete = self._get_job_complete(ctx)

        result = await invoke_tool(
            job_complete,
            {
                "summary": "Job is done.",
                "deliverables": ["output/result.md"],
                "confidence": 0.9,
            },
        )

        assert "ERROR" not in result
        assert not todo_mgr.has_staged_todos()


# =============================================================================
# Test: request_replan clears staged todos but keeps active ones
# =============================================================================


class TestRequestReplanClearsStaged:
    """Verify request_replan drops the staged batch but preserves live work.

    Staged todos are a bet on the plan being revised, so they go. Active todos
    are the *record* of what this phase did and must survive — the incoming
    strategic phase reads their real statuses to decide what to carry forward.
    """

    def _get_request_replan(self, context: ToolContext):
        """Create and return the request_replan tool function."""
        from src.tools.core.todo import create_todo_tools

        tools = create_todo_tools(context)
        return next(t for t in tools if t.name == "request_replan")

    def test_clears_staged_todos(self):
        """request_replan should clear staged todos and mention it."""
        todo_mgr = TodoManager(workspace=MagicMock())

        todo_mgr.stage_tactical_todos(
            [f"Active todo {i} long enough" for i in range(5)],
        )
        todo_mgr.apply_staged_todos()
        assert len(todo_mgr.list_all()) == 5

        # Now stage new todos for the next phase
        stage_some_todos(todo_mgr)
        assert todo_mgr.has_staged_todos()

        ctx = make_context(todo_mgr)
        request_replan = self._get_request_replan(ctx)

        result = request_replan.invoke(
            {"reason": "The API caps batches at 100, so the bulk todos are wrong"}
        )

        # Staged todos should be cleared
        assert not todo_mgr.has_staged_todos()
        assert "staged todos were cleared" in result
        # ...but the active todos must NOT be — they are the phase record.
        assert len(todo_mgr.list_all()) == 5

    def test_no_staged_todos_no_message(self):
        """request_replan without staged todos doesn't mention clearing."""
        todo_mgr = TodoManager(workspace=MagicMock())

        todo_mgr.stage_tactical_todos(
            [f"Active todo {i} long enough" for i in range(5)],
        )
        todo_mgr.apply_staged_todos()

        ctx = make_context(todo_mgr)
        request_replan = self._get_request_replan(ctx)

        result = request_replan.invoke({"reason": "Need a different approach"})

        assert "staged todos were cleared" not in result
        assert not todo_mgr.has_staged_todos()

    def test_replan_with_only_staged_no_active(self):
        """request_replan with staged but no active todos still clears staged."""
        todo_mgr = TodoManager(workspace=MagicMock())
        stage_some_todos(todo_mgr)
        # No active todos, only staged

        ctx = make_context(todo_mgr)
        request_replan = self._get_request_replan(ctx)

        result = request_replan.invoke({"reason": "Changed my mind before executing"})

        assert not todo_mgr.has_staged_todos()
        assert "staged todos were cleared" in result
