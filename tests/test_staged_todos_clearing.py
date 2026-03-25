"""Tests for staged todos clearing in job_complete and todo_rewind.

Verifies that:
- job_complete auto-clears staged todos instead of rejecting (deadlock fix)
- todo_rewind clears staged todos when abandoning an approach
"""

import pytest
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

        result = await job_complete.ainvoke(
            {
                "summary": "Job is done.",
                "deliverables": ["output/result.md"],
                "confidence": 0.9,
            }
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

        result = await job_complete.ainvoke(
            {
                "summary": "Job is done.",
                "deliverables": ["output/result.md"],
                "confidence": 0.9,
            }
        )

        assert "ERROR" not in result
        assert not todo_mgr.has_staged_todos()


# =============================================================================
# Test: todo_rewind clears staged todos
# =============================================================================


class TestTodoRewindClears:
    """Verify todo_rewind clears staged todos when abandoning approach."""

    def _get_todo_rewind(self, context: ToolContext):
        """Create and return the todo_rewind tool function."""
        from src.tools.core.todo import create_todo_tools

        tools = create_todo_tools(context)
        return next(t for t in tools if t.name == "todo_rewind")

    def test_clears_staged_todos(self):
        """todo_rewind should clear staged todos and mention it."""
        todo_mgr = TodoManager(workspace=MagicMock())

        # Add active todos first (rewind archives these)
        todo_mgr.stage_tactical_todos(
            [f"Active todo {i} long enough" for i in range(5)],
        )
        todo_mgr.apply_staged_todos()
        assert len(todo_mgr.list_all()) == 5

        # Now stage new todos for the next phase
        stage_some_todos(todo_mgr)
        assert todo_mgr.has_staged_todos()

        ctx = make_context(todo_mgr)
        todo_rewind = self._get_todo_rewind(ctx)

        result = todo_rewind.invoke(
            {"issue": "Current approach is fundamentally broken"}
        )

        # Staged todos should be cleared
        assert not todo_mgr.has_staged_todos()
        # Result should mention the clearing
        assert "Cleared previously staged todos" in result

    def test_no_staged_todos_no_message(self):
        """todo_rewind without staged todos doesn't mention clearing."""
        todo_mgr = TodoManager(workspace=MagicMock())

        # Add active todos only
        todo_mgr.stage_tactical_todos(
            [f"Active todo {i} long enough" for i in range(5)],
        )
        todo_mgr.apply_staged_todos()

        ctx = make_context(todo_mgr)
        todo_rewind = self._get_todo_rewind(ctx)

        result = todo_rewind.invoke({"issue": "Need different approach"})

        assert "Cleared previously staged todos" not in result
        assert not todo_mgr.has_staged_todos()

    def test_rewind_with_only_staged_no_active(self):
        """todo_rewind with staged but no active todos still clears staged."""
        todo_mgr = TodoManager(workspace=MagicMock())
        stage_some_todos(todo_mgr)
        # No active todos, only staged

        ctx = make_context(todo_mgr)
        todo_rewind = self._get_todo_rewind(ctx)

        result = todo_rewind.invoke({"issue": "Changed my mind before executing"})

        assert not todo_mgr.has_staged_todos()
        assert "Cleared previously staged todos" in result
