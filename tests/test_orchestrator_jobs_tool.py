"""Tests for the ``create_worker_job`` orchestrator tool.

The tool runs inside the persistent agent and POSTs to ``/api/jobs`` on the
orchestrator. The orchestrator route has no auth, so the tool must propagate
the originating session's identity. Without the ``thread_id`` field in the
payload the orchestrator can't derive the calling user, dispatch skips the
user-preference injection block, and the worker boots with the YAML default
model + ``not-needed`` API key — which silently routes to api.openai.com and
401s. Regression coverage for that flow lives here.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

project_root = Path(__file__).parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from src.tools.context import ToolContext  # noqa: E402
from src.tools.orchestrator.jobs import create_orchestrator_tools  # noqa: E402


def _tool_by_name(tools, name):
    for t in tools:
        if t.name == name:
            return t
    raise AssertionError(f"tool {name!r} not in {[t.name for t in tools]}")


class _CapturingClient:
    """Minimal httpx.AsyncClient stand-in that records POST payloads."""

    def __init__(self):
        self.posted: list[tuple[str, dict]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, json=None, **kwargs):
        self.posted.append((url, json or {}))
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json = MagicMock(return_value={"id": "new-job-uuid"})
        return resp


@pytest.fixture
def capturing_client(monkeypatch):
    cap = _CapturingClient()
    monkeypatch.setattr("src.tools.orchestrator.jobs._get_client", lambda: cap)
    return cap


@pytest.mark.asyncio
async def test_create_worker_job_includes_thread_id_from_context(capturing_client):
    """When the calling tool context carries a thread_id (i.e., the caller
    is a persistent session), it must be forwarded so the orchestrator can
    derive the owning user_id and apply their model preferences."""
    ctx = ToolContext(_thread_id="thread-abc-123")
    tools = create_orchestrator_tools(ctx)
    create = _tool_by_name(tools, "create_worker_job")

    await create.ainvoke({"description": "test job"})

    assert len(capturing_client.posted) == 1
    _, body = capturing_client.posted[0]
    assert body["thread_id"] == "thread-abc-123"


@pytest.mark.asyncio
async def test_create_worker_job_omits_thread_id_when_not_in_context(
    capturing_client,
):
    """Worker-mode callers (no session) must not send a thread_id field."""
    ctx = ToolContext()
    tools = create_orchestrator_tools(ctx)
    create = _tool_by_name(tools, "create_worker_job")

    await create.ainvoke({"description": "worker job"})

    _, body = capturing_client.posted[0]
    assert "thread_id" not in body


@pytest.mark.asyncio
async def test_create_worker_job_inherits_project_id_from_context(
    capturing_client,
):
    """Session's project scope flows through unless the caller overrides it."""
    ctx = ToolContext(_thread_id="thread-1", _project_id="proj-from-session")
    tools = create_orchestrator_tools(ctx)
    create = _tool_by_name(tools, "create_worker_job")

    await create.ainvoke({"description": "test"})

    _, body = capturing_client.posted[0]
    assert body["project_id"] == "proj-from-session"


@pytest.mark.asyncio
async def test_explicit_project_id_argument_wins_over_context(capturing_client):
    """An explicit project_id passed to the tool overrides the session's."""
    ctx = ToolContext(_thread_id="thread-1", _project_id="proj-from-session")
    tools = create_orchestrator_tools(ctx)
    create = _tool_by_name(tools, "create_worker_job")

    await create.ainvoke({"description": "test", "project_id": "explicit-proj-id"})

    _, body = capturing_client.posted[0]
    assert body["project_id"] == "explicit-proj-id"
