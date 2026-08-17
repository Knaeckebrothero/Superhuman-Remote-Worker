"""Job completion tools must propagate WorkspaceUnavailableError, not stringify it.

Regression guard for Defect 8 of
knowledge-base/knowledge/issues/transient_db_error_hard_fails_job_and_destroys_vm.md.

Job c6dd288d called ``job_complete`` five times over 13 minutes against a VM
that had already been deleted. Every call was recorded ``Tool [ok] … success:
true`` with the result string ``Error marking job as final: Failed to connect
to workspace 100.64.0.106:22``, because a bare ``except Exception`` caught the
``WorkspaceUnavailableError`` and returned it as prose. The agent therefore did
not classify the workspace as dead until an unrelated ``file_exists`` call let
the exception propagate ~16 minutes later.

A dead workspace is a lifecycle signal owned by the fast-freeze path, not a
tool error for the model to read.
"""

from unittest.mock import MagicMock

import pytest

from tests._tool_invoke import invoke_tool

from src.core.workspace_backend import WorkspaceUnavailableError
from src.tools.core.job import _final_phase_data, create_job_tools


@pytest.fixture(autouse=True)
def _clear_final_phase_data():
    """``_final_phase_data`` is module-global; a stale entry short-circuits
    job_complete with "already marked as final" and hides the real path."""
    _final_phase_data.clear()
    yield
    _final_phase_data.clear()


def _tools(workspace, job_id="job-under-test"):
    """Build (mark_complete, job_complete) over a stubbed workspace."""
    context = MagicMock()
    context.job_id = job_id
    context.has_workspace.return_value = True
    context.workspace_manager = workspace
    context.has_todo.return_value = False
    return create_job_tools(context)


def _dead_workspace():
    """Workspace whose every operation reports the VM is unreachable."""
    boom = WorkspaceUnavailableError(
        "Failed to connect to workspace 100.64.0.106:22 after 2 attempt(s) "
        "[timeout]: timed out"
    )
    ws = MagicMock()
    ws.exists.side_effect = boom
    ws.read_file.side_effect = boom
    ws.write_file.side_effect = boom
    return ws


class TestWorkspaceDeathPropagates:
    @pytest.mark.asyncio
    async def test_job_complete_raises_instead_of_returning_error_string(self):
        """The exact incident shape: job_complete against a deleted VM."""
        mark_complete, job_complete = _tools(_dead_workspace())

        with pytest.raises(WorkspaceUnavailableError):
            await invoke_tool(
                job_complete,
                {
                    "summary": "Delivered the theme candidate.",
                    "deliverables": ["theme_program/theme_overview.md"],
                    "confidence": 0.98,
                },
            )

    @pytest.mark.asyncio
    async def test_mark_complete_raises_instead_of_returning_error_string(self):
        """mark_complete writes to the workspace too — same exposure."""
        mark_complete, job_complete = _tools(_dead_workspace())

        with pytest.raises(WorkspaceUnavailableError):
            await mark_complete.ainvoke(
                {
                    "summary": "Phase done.",
                    "deliverables": ["output/report.md"],
                }
            )

    @pytest.mark.asyncio
    async def test_dead_workspace_is_not_reported_as_a_bad_deliverable(self):
        """A live workspace that dies mid-validation must not degrade to a warning.

        The inner read_file guard used to catch every exception and append
        "could not be read", which would reject the job's own completion for
        deliverable problems that do not exist.
        """
        ws = MagicMock()
        ws.exists.return_value = True
        ws.read_file.side_effect = WorkspaceUnavailableError("VM gone mid-read")
        mark_complete, job_complete = _tools(ws)

        with pytest.raises(WorkspaceUnavailableError):
            await invoke_tool(
                job_complete,
                {
                    "summary": "Delivered the theme candidate.",
                    "deliverables": ["theme_program/theme_overview.md"],
                    "confidence": 0.98,
                },
            )

    @pytest.mark.asyncio
    async def test_ordinary_tool_errors_still_return_a_string(self):
        """Only workspace death is special — other failures stay model-visible."""
        ws = MagicMock()
        ws.exists.side_effect = RuntimeError("something mundane broke")
        mark_complete, job_complete = _tools(ws)

        result = await invoke_tool(
            job_complete,
            {
                "summary": "Delivered the theme candidate.",
                "deliverables": ["theme_program/theme_overview.md"],
                "confidence": 0.98,
            },
        )
        assert isinstance(result, str)
        assert "something mundane broke" in result
