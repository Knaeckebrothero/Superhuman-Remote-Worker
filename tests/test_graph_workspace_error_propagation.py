"""ToolNode must re-raise WorkspaceUnavailableError (so it propagates to the
job error path) while still stringifying ordinary tool errors."""

import pytest

# langgraph's private error-handling dispatch — tested directly because a
# standalone ToolNode.ainvoke needs a graph runtime this fix doesn't touch.
# These are the exact seams that make removing the substring watchdog safe.
from langgraph.prebuilt.tool_node import _handle_tool_error, _infer_handled_types

from src.core.workspace_backend import WorkspaceUnavailableError
from src.graph import _handle_tool_errors_reraise_workspace


def test_handler_reraises_workspace_error():
    with pytest.raises(WorkspaceUnavailableError):
        _handle_tool_errors_reraise_workspace(WorkspaceUnavailableError("gone"))


def test_handler_stringifies_other_errors():
    msg = _handle_tool_errors_reraise_workspace(ValueError("bad arg"))
    assert "ValueError" in msg  # repr(e) in the template


def test_handler_is_routed_all_exception_types():
    """Annotating (e: Exception) makes langgraph route ALL exceptions — including
    WorkspaceUnavailableError — to our handler, so it gets the chance to re-raise
    (rather than langgraph short-circuiting to a re-raise or a ToolMessage)."""
    assert _infer_handled_types(_handle_tool_errors_reraise_workspace) == (Exception,)


def test_langgraph_dispatch_reraises_workspace_error():
    """langgraph calls flag(e) with no surrounding try/except, so a raising handler
    propagates out of ToolNode instead of becoming a ToolMessage."""
    with pytest.raises(WorkspaceUnavailableError):
        _handle_tool_error(
            WorkspaceUnavailableError("gone"),
            flag=_handle_tool_errors_reraise_workspace,
        )


def test_langgraph_dispatch_stringifies_ordinary_error():
    content = _handle_tool_error(
        ValueError("nope"), flag=_handle_tool_errors_reraise_workspace
    )
    assert "ValueError" in content
