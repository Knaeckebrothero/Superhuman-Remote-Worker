"""Tests for the ``send_message`` communication tool.

When the agent is running inside a persistent session, ``ToolContext``
carries the originating user's id. The orchestrator's messages-send
endpoint should accept these calls via ``_get_user_from_mcp_headers``
(``X-Internal-Key`` + ``X-MCP-User-Id``) so the same identity model
applies as for the rest of the agent's orchestrator surface. Without
``X-MCP-User-Id`` the call only authenticates as ``require_internal``,
which keeps working today but loses the per-user audit trail and breaks
the dual-callable paths that branch on ``user`` vs. internal.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

project_root = Path(__file__).parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from src.tools.communication.messaging import create_communication_tools  # noqa: E402
from src.tools.context import ToolContext  # noqa: E402


def _tool_by_name(tools, name):
    for t in tools:
        if t.name == name:
            return t
    raise AssertionError(f"tool {name!r} not in {[t.name for t in tools]}")


class _CapturingAsyncClient:
    """Captures the constructor headers and the first POST call.

    httpx.AsyncClient is normally used as ``async with`` — this stand-in
    mimics that contract.
    """

    last_init_headers: dict[str, str] | None = None

    def __init__(self, *args, **kwargs):
        type(self).last_init_headers = dict(kwargs.get("headers") or {})

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, json=None, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        resp.json = MagicMock(
            return_value={
                "thread_id": json.get("thread_id") if json else "t",
                "status": "delivered",
            }
        )
        resp.text = ""
        return resp


@pytest.mark.asyncio
async def test_send_message_forwards_user_id_from_context(monkeypatch):
    """A persistent-session context (user_id set) must produce a POST
    whose headers carry X-MCP-User-Id alongside X-Internal-Key."""
    monkeypatch.setenv("MCP_INTERNAL_KEY", "test-internal-key")
    _CapturingAsyncClient.last_init_headers = None
    monkeypatch.setattr(
        "src.tools.communication.messaging.httpx.AsyncClient",
        _CapturingAsyncClient,
    )

    ctx = ToolContext(_job_id="job-1", user_id="user-xyz")
    tools = create_communication_tools(ctx)
    send = _tool_by_name(tools, "send_message")

    result = await send.ainvoke(
        {"to": "user", "subject": "hi", "message": "hello there", "mode": "async"}
    )

    # The tool may still report partial success; we only care about headers.
    assert isinstance(result, str)
    headers = _CapturingAsyncClient.last_init_headers
    assert headers is not None, "httpx.AsyncClient was never constructed"
    assert headers.get("X-Internal-Key") == "test-internal-key"
    assert headers.get("X-MCP-User-Id") == "user-xyz"


@pytest.mark.asyncio
async def test_send_message_omits_user_id_header_when_context_lacks_user(monkeypatch):
    """Worker-mode (no user_id) keeps the old behavior — X-Internal-Key
    only. The orchestrator's require_internal gate on messages-send is
    happy with that."""
    monkeypatch.setenv("MCP_INTERNAL_KEY", "test-internal-key")
    _CapturingAsyncClient.last_init_headers = None
    monkeypatch.setattr(
        "src.tools.communication.messaging.httpx.AsyncClient",
        _CapturingAsyncClient,
    )

    ctx = ToolContext(_job_id="job-1")
    tools = create_communication_tools(ctx)
    send = _tool_by_name(tools, "send_message")

    await send.ainvoke(
        {"to": "user", "subject": "hi", "message": "hello there", "mode": "async"}
    )

    headers = _CapturingAsyncClient.last_init_headers
    assert headers is not None
    assert headers.get("X-Internal-Key") == "test-internal-key"
    assert "X-MCP-User-Id" not in headers
