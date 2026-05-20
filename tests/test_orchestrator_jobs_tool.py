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
from src.tools.orchestrator import jobs as jobs_module  # noqa: E402
from src.tools.orchestrator.jobs import create_orchestrator_tools  # noqa: E402


def _tool_by_name(tools, name):
    for t in tools:
        if t.name == name:
            return t
    raise AssertionError(f"tool {name!r} not in {[t.name for t in tools]}")


class _CapturingClient:
    """Minimal httpx.AsyncClient stand-in that records POST and GET calls."""

    def __init__(self):
        self.posted: list[tuple[str, dict]] = []
        self.gets: list[tuple[str, dict]] = []

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

    async def get(self, url, params=None, **kwargs):
        self.gets.append((url, params or {}))
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json = MagicMock(return_value=[])
        return resp


@pytest.fixture
def capturing_client(monkeypatch):
    cap = _CapturingClient()
    # _get_client gained an optional ``user_id`` kwarg so the agent can
    # forward the originating user's identity via X-MCP-User-Id. Accept
    # kwargs here so this fixture works with the new signature.
    monkeypatch.setattr(
        "src.tools.orchestrator.jobs._get_client",
        lambda *a, **kw: cap,
    )
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


# ---------------------------------------------------------------------------
# X-MCP-User-Id forwarding — fixes the agent's read-job 401.
#
# The persistent agent's job-read tools (list_worker_jobs / get_worker_job)
# hit /api/jobs and /api/jobs/{id}, which both require an approved user.
# X-Internal-Key alone is not sufficient — the orchestrator's
# _get_user_from_mcp_headers requires BOTH X-Internal-Key (matching env
# MCP_INTERNAL_KEY) AND X-MCP-User-Id. The agent reads the owning user_id
# from its ToolContext and forwards it on every orchestrator call.
# ---------------------------------------------------------------------------


class TestGetClientHeaders:
    """``_get_client`` must emit X-MCP-User-Id alongside X-Internal-Key
    when the caller supplies a ``user_id``. Without ``user_id`` (worker
    mode, lifecycle calls) only X-Internal-Key is attached — preserves
    back-compat with the require_internal endpoints."""

    def test_includes_user_id_header_when_supplied(self, monkeypatch):
        monkeypatch.setenv("MCP_INTERNAL_KEY", "test-internal-key")

        client = jobs_module._get_client(user_id="user-abc")
        try:
            assert client.headers.get("X-Internal-Key") == "test-internal-key"
            assert client.headers.get("X-MCP-User-Id") == "user-abc"
        finally:
            # httpx.AsyncClient holds resources even before aclose; this
            # avoids ResourceWarning noise in the test output.
            pass

    def test_omits_user_id_header_when_not_supplied(self, monkeypatch):
        monkeypatch.setenv("MCP_INTERNAL_KEY", "test-internal-key")

        client = jobs_module._get_client()
        assert client.headers.get("X-Internal-Key") == "test-internal-key"
        assert "X-MCP-User-Id" not in client.headers


@pytest.mark.asyncio
async def test_list_worker_jobs_forwards_user_id_from_context(monkeypatch):
    """list_worker_jobs hits GET /api/jobs, which requires a real user.
    The tool must forward context.user_id so _get_user_from_mcp_headers
    resolves the user instead of 401-ing."""
    monkeypatch.setenv("MCP_INTERNAL_KEY", "test-internal-key")
    captured: dict = {}

    def _factory(*, user_id=None):
        cap = _CapturingClient()
        captured["user_id"] = user_id
        return cap

    monkeypatch.setattr("src.tools.orchestrator.jobs._get_client", _factory)

    ctx = ToolContext(user_id="user-xyz")
    tools = create_orchestrator_tools(ctx)
    lst = _tool_by_name(tools, "list_worker_jobs")

    await lst.ainvoke({})

    assert captured["user_id"] == "user-xyz"


@pytest.mark.asyncio
async def test_get_worker_job_forwards_user_id_from_context(monkeypatch):
    """get_worker_job hits GET /api/jobs/{id} (require_job_access). Same
    requirement as list — must forward user_id from the context."""
    monkeypatch.setenv("MCP_INTERNAL_KEY", "test-internal-key")
    captured: dict = {}

    def _factory(*, user_id=None):
        cap = _CapturingClient()
        captured["user_id"] = user_id
        # Return a job dict so the tool's response code path completes.
        cap.gets = []  # reset

        async def _get(url, **kwargs):
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.json = MagicMock(return_value={"id": "j-1", "status": "running"})
            return resp

        cap.get = _get
        return cap

    monkeypatch.setattr("src.tools.orchestrator.jobs._get_client", _factory)

    ctx = ToolContext(user_id="user-xyz")
    tools = create_orchestrator_tools(ctx)
    get = _tool_by_name(tools, "get_worker_job")

    await get.ainvoke({"job_id": "j-1"})

    assert captured["user_id"] == "user-xyz"
