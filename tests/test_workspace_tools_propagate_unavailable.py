"""Workspace/shell tools must let WorkspaceUnavailableError propagate (not
flatten it into a string result), so a dead workspace freezes the job cleanly.
Regression guard for knowledge-base/knowledge/issues/agent_fast_freeze_on_dead_workspace.md."""

import pytest

from shared.runtime.core.workspace_backend import WorkspaceUnavailableError
from agent.tools.context import ToolContext
from agent.tools.workspace.filesystem import create_filesystem_tools
from agent.tools.workspace.files import create_file_tools


class _DeadWorkspace:
    """A WorkspaceManager stand-in whose every method call raises
    WorkspaceUnavailableError (the workspace pod is gone)."""

    is_initialized = True

    def __getattr__(self, name):
        def _raise(*args, **kwargs):
            raise WorkspaceUnavailableError("workspace gone")

        return _raise


def _ctx():
    return ToolContext(workspace_manager=_DeadWorkspace())


def _tool(tools, name):
    return next(t for t in tools if t.name == name)


def test_list_files_reraises_workspace_unavailable():
    tools = create_filesystem_tools(_ctx())
    with pytest.raises(WorkspaceUnavailableError):
        _tool(tools, "list_files").invoke({"path": ""})


def test_read_file_reraises_workspace_unavailable():
    tools = create_file_tools(_ctx())
    with pytest.raises(WorkspaceUnavailableError):
        _tool(tools, "read_file").invoke({"path": "x.txt"})
