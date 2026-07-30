"""Tests for the ``create_worker_job`` orchestrator tool.

The tool runs inside persistent and worker agents and POSTs to ``/api/jobs`` on
the orchestrator. Internal transport auth is not a user/project grant, so the
tool must propagate an authoritative ``thread_id`` and/or ``parent_job_id``.
Without that origin the route rejects the request; accepting it would let an
agent select arbitrary project/OKF datasource UUIDs under the shared key.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import httpx
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
        self.puts: list[tuple[str, dict]] = []

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

    async def put(self, url, json=None, **kwargs):
        self.puts.append((url, json or {}))
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json = MagicMock(return_value={"status": "ok"})
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
async def test_worker_create_job_includes_authoritative_parent_job_id(
    capturing_client,
):
    parent_job_id = "11111111-2222-3333-4444-555555555555"
    ctx = ToolContext(_job_metadata={"job_id": parent_job_id})
    tools = create_orchestrator_tools(ctx)
    create = _tool_by_name(tools, "create_worker_job")

    await create.ainvoke(
        {
            "description": "worker child",
            "project_id": "model-selected-project",
            "datasource_ids": ["model-selected-datasource"],
        }
    )

    _, body = capturing_client.posted[0]
    assert body["parent_job_id"] == parent_job_id


@pytest.mark.asyncio
async def test_create_worker_job_can_send_thread_and_parent_origins(
    capturing_client,
):
    parent_job_id = "11111111-2222-3333-4444-555555555555"
    ctx = ToolContext(
        _thread_id="thread-1",
        _job_metadata={"job_id": parent_job_id},
    )
    tools = create_orchestrator_tools(ctx)
    create = _tool_by_name(tools, "create_worker_job")

    await create.ainvoke({"description": "scoped child"})

    _, body = capturing_client.posted[0]
    assert body["thread_id"] == "thread-1"
    assert body["parent_job_id"] == parent_job_id


@pytest.mark.asyncio
async def test_create_worker_job_omits_malformed_parent_metadata(capturing_client):
    ctx = ToolContext(_job_metadata={"job_id": "not-a-uuid"})
    tools = create_orchestrator_tools(ctx)
    create = _tool_by_name(tools, "create_worker_job")

    await create.ainvoke({"description": "worker job"})

    _, body = capturing_client.posted[0]
    assert "parent_job_id" not in body


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
async def test_list_worker_jobs_includes_full_job_id(monkeypatch):
    """Regression: list output must expose a usable full UUID."""
    job_id = "19707fa1-0000-4000-8000-000000000001"

    class _Client(_CapturingClient):
        async def get(self, url, params=None, **kwargs):
            self.gets.append((url, params or {}))
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.json = MagicMock(
                return_value=[
                    {
                        "id": job_id,
                        "status": "paused",
                        "description": "Hotel ERP developer job",
                        "config_name": "developer",
                    }
                ]
            )
            return resp

    cap = _Client()
    monkeypatch.setattr("src.tools.orchestrator.jobs._get_client", lambda **kw: cap)

    tools = create_orchestrator_tools(ToolContext(user_id="user-xyz"))
    lst = _tool_by_name(tools, "list_worker_jobs")

    result = await lst.ainvoke({})

    assert job_id in result
    assert "19707fa1..." not in result


@pytest.mark.asyncio
async def test_get_worker_job_resolves_visible_uuid_prefix(monkeypatch):
    """Action tools tolerate the short IDs shown in older list output/logs."""
    job_id = "19707fa1-0000-4000-8000-000000000001"

    class _Client(_CapturingClient):
        async def get(self, url, params=None, **kwargs):
            self.gets.append((url, params or {}))
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            if url.endswith("/api/jobs"):
                resp.json = MagicMock(return_value=[{"id": job_id, "status": "paused"}])
            else:
                resp.json = MagicMock(return_value={"id": job_id, "status": "paused"})
            return resp

    cap = _Client()
    monkeypatch.setattr("src.tools.orchestrator.jobs._get_client", lambda **kw: cap)

    tools = create_orchestrator_tools(ToolContext(user_id="user-xyz"))
    get = _tool_by_name(tools, "get_worker_job")

    result = await get.ainvoke({"job_id": "19707fa1..."})

    assert f"http://localhost:8085/api/jobs/{job_id}" in [url for url, _ in cap.gets]
    assert f"Job ID: {job_id}" in result


@pytest.mark.asyncio
async def test_pause_and_cancel_worker_job_use_put(monkeypatch):
    """Backend routes are PUT /pause and PUT /cancel."""
    job_id = "19707fa1-0000-4000-8000-000000000001"
    cap = _CapturingClient()
    monkeypatch.setattr("src.tools.orchestrator.jobs._get_client", lambda **kw: cap)

    tools = create_orchestrator_tools(ToolContext(user_id="user-xyz"))
    pause = _tool_by_name(tools, "pause_worker_job")
    cancel = _tool_by_name(tools, "cancel_worker_job")

    await pause.ainvoke({"job_id": job_id})
    await cancel.ainvoke({"job_id": job_id})

    assert [url for url, _ in cap.puts] == [
        f"http://localhost:8085/api/jobs/{job_id}/pause",
        f"http://localhost:8085/api/jobs/{job_id}/cancel",
    ]
    assert cap.posted == []


@pytest.mark.asyncio
async def test_get_session_context_reports_core_context():
    ctx = ToolContext(
        _thread_id="thread-abc",
        user_id="user-xyz",
        _project_id="project-123",
        datasources={"postgresql": object()},
    )
    tools = create_orchestrator_tools(ctx)
    get_context = _tool_by_name(tools, "get_session_context")

    result = await get_context.ainvoke({})

    assert "Thread ID: thread-abc" in result
    assert "User ID: user-xyz" in result
    assert "Primary project ID: project-123" in result
    assert "Connectors: postgresql" in result


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


# ---------------------------------------------------------------------------
# config_override / expert_id passthrough
#
# The tool deliberately takes config_override as a raw dict rather than typed
# params: the capability PDP (src/core/capability_grants.py) gates every
# escalation axis against the owner's grants, so the agent cannot exceed its
# owner regardless of the JSON it writes. Transport keys are the exception the
# PDP does not cover, so they are rejected here, before any HTTP.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_worker_job_forwards_config_override(capturing_client):
    """The override must ride into the body verbatim — the orchestrator merges
    it last (over project + expert defaults), so any mangling here silently
    changes what the worker runs."""
    ctx = ToolContext(_thread_id="thread-abc")
    tools = create_orchestrator_tools(ctx)
    create = _tool_by_name(tools, "create_worker_job")

    override = {"llm": {"model": "gpt-5.6"}, "workspace": {"backend": "vm"}}
    await create.ainvoke({"description": "heavy job", "config_override": override})

    _, body = capturing_client.posted[0]
    assert body["config_override"] == override


@pytest.mark.asyncio
async def test_create_worker_job_omits_absent_config_override(capturing_client):
    """No override => the key is absent, so the server's own resolution
    (project default, then expert) is untouched."""
    ctx = ToolContext(_thread_id="thread-abc")
    tools = create_orchestrator_tools(ctx)
    create = _tool_by_name(tools, "create_worker_job")

    await create.ainvoke({"description": "plain job"})

    _, body = capturing_client.posted[0]
    assert "config_override" not in body
    assert "expert_id" not in body


@pytest.mark.asyncio
async def test_create_worker_job_rejects_nested_transport_keys(capturing_client):
    """A base_url nested under llm would survive redaction and dispatch
    injection, letting a prompt-injected session route the worker's LLM to an
    arbitrary host. Reject before the request is sent, and name the path."""
    ctx = ToolContext(_thread_id="thread-abc")
    tools = create_orchestrator_tools(ctx)
    create = _tool_by_name(tools, "create_worker_job")

    result = await create.ainvoke(
        {
            "description": "exfil attempt",
            "config_override": {"llm": {"model": "x", "base_url": "http://evil"}},
        }
    )

    assert capturing_client.posted == []
    assert "llm.base_url" in result


@pytest.mark.asyncio
async def test_create_worker_job_rejects_env_keys_and_api_key_suffix(
    capturing_client,
):
    """env_keys carries transport for the auxiliary/embedding slots, and any
    *_api_key is a credential — both are refused with every offending path
    named, not just the first."""
    ctx = ToolContext(_thread_id="thread-abc")
    tools = create_orchestrator_tools(ctx)
    create = _tool_by_name(tools, "create_worker_job")

    result = await create.ainvoke(
        {
            "description": "job",
            "config_override": {
                "env_keys": {"EMBEDDING_BASE_URL": "http://evil"},
                "auxiliary": {"openai_api_key": "sk-x"},
            },
        }
    )

    assert capturing_client.posted == []
    assert "env_keys" in result
    assert "auxiliary.openai_api_key" in result


@pytest.mark.asyncio
async def test_create_worker_job_forwards_expert_id(capturing_client):
    ctx = ToolContext(_thread_id="thread-abc")
    tools = create_orchestrator_tools(ctx)
    create = _tool_by_name(tools, "create_worker_job")

    expert_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    await create.ainvoke({"description": "job", "expert_id": expert_id})

    _, body = capturing_client.posted[0]
    assert body["expert_id"] == expert_id


@pytest.mark.asyncio
async def test_create_worker_job_rejects_expert_id_with_bundled_config_name(
    capturing_client,
):
    """The API 400s on this pairing (one expert source at a time); catching it
    here turns an opaque status into a reason the agent can act on."""
    ctx = ToolContext(_thread_id="thread-abc")
    tools = create_orchestrator_tools(ctx)
    create = _tool_by_name(tools, "create_worker_job")

    result = await create.ainvoke(
        {
            "description": "job",
            "config_name": "developer",
            "expert_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        }
    )

    assert capturing_client.posted == []
    assert "expert_id" in result


# ---------------------------------------------------------------------------
# get_session_context discovery
# ---------------------------------------------------------------------------


class _DiscoveryClient(_CapturingClient):
    """Serves /api/models and /api/users/me/capabilities so the context tool can
    render the two lines a config_override needs."""

    async def get(self, url, params=None, **kwargs):
        self.gets.append((url, params or {}))
        if url.endswith("/api/models"):
            payload = {
                "groups": [
                    {"models": ["gpt-5.6", "claude-opus-5"]},
                    {"models": ["gemma-3"]},
                ]
            }
        elif url.endswith("/api/users/me/capabilities"):
            payload = {
                "is_admin": False,
                "grants": {"vm_workspace": False, "shell_tools": True},
            }
        else:
            payload = {}
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json = MagicMock(return_value=payload)
        return resp


@pytest.mark.asyncio
async def test_get_session_context_reports_models_and_grants(monkeypatch):
    """Model IDs are per-deployment and grants decide whether an override is
    accepted, so both must reach the agent before it writes one."""
    cap = _DiscoveryClient()
    monkeypatch.setattr("src.tools.orchestrator.jobs._get_client", lambda *a, **kw: cap)
    ctx = ToolContext(_thread_id="thread-abc", user_id="user-xyz")
    tools = create_orchestrator_tools(ctx)
    get_context = _tool_by_name(tools, "get_session_context")

    result = await get_context.ainvoke({})

    assert "Available chat models:" in result
    assert "gpt-5.6" in result and "gemma-3" in result
    assert "vm_workspace: False" in result
    assert "shell_tools: True" in result


@pytest.mark.asyncio
async def test_get_session_context_survives_lookup_failure(monkeypatch):
    """A context report must never break the session: an unreachable
    orchestrator drops the two discovery lines and keeps the rest."""

    class _ExplodingClient(_CapturingClient):
        async def get(self, url, params=None, **kwargs):
            raise RuntimeError("orchestrator down")

    monkeypatch.setattr(
        "src.tools.orchestrator.jobs._get_client",
        lambda *a, **kw: _ExplodingClient(),
    )
    ctx = ToolContext(_thread_id="thread-abc", user_id="user-xyz")
    tools = create_orchestrator_tools(ctx)
    get_context = _tool_by_name(tools, "get_session_context")

    result = await get_context.ainvoke({})

    assert "Thread ID: thread-abc" in result
    assert "Available chat models:" not in result


@pytest.mark.asyncio
async def test_get_session_context_reports_admin_as_unrestricted(monkeypatch):
    class _AdminClient(_CapturingClient):
        async def get(self, url, params=None, **kwargs):
            payload = (
                {"is_admin": True, "grants": None}
                if url.endswith("/capabilities")
                else {"groups": []}
            )
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.json = MagicMock(return_value=payload)
            return resp

    monkeypatch.setattr(
        "src.tools.orchestrator.jobs._get_client", lambda *a, **kw: _AdminClient()
    )
    ctx = ToolContext(user_id="admin-1")
    tools = create_orchestrator_tools(ctx)
    get_context = _tool_by_name(tools, "get_session_context")

    result = await get_context.ainvoke({})

    assert "admin (unrestricted)" in result


# ---------------------------------------------------------------------------
# Gitea-backed workspace reads
#
# get_job_workspace_file / list_job_workspace_files ride GET
# /api/jobs/{id}/repo/* (committed state as of the worker's last
# phase-boundary push), NOT the dead orchestrator-local /workspace/* routes.
# The staleness header keeps the officer from reasoning on hours-old bytes
# unknowingly, and a 404 must name what was searched instead of laundering
# a wrong guess into "the worker delivered nothing".
# ---------------------------------------------------------------------------

_REPO_JOB_ID = "19707fa1-0000-4000-8000-000000000001"
_HEAD_COMMIT = {
    "sha": "abcdef1234567890",
    "message": "phase 2 tactical complete\n\ndetails",
    "author": "worker",
    "date": "2026-07-30T09:00:00Z",
}


class _RepoClient(_CapturingClient):
    """Serves /repo/file, /repo/contents, and /repo/commits."""

    def __init__(
        self,
        *,
        file_status=200,
        content="line one\nline two",
        entries=None,
        commits_fail=False,
    ):
        super().__init__()
        self.file_status = file_status
        self.content = content
        self.entries = entries if entries is not None else []
        self.commits_fail = commits_fail

    async def get(self, url, params=None, **kwargs):
        self.gets.append((url, params or {}))
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        if url.endswith("/repo/commits"):
            if self.commits_fail:
                raise httpx.RequestError("commits lookup down")
            resp.json = MagicMock(
                return_value={"total_commits": 1, "commits": [_HEAD_COMMIT]}
            )
        elif url.endswith("/repo/file"):
            if self.file_status == 404:
                err = MagicMock()
                err.status_code = 404
                resp.raise_for_status = MagicMock(
                    side_effect=httpx.HTTPStatusError(
                        "404", request=MagicMock(), response=err
                    )
                )
            else:
                resp.json = MagicMock(
                    return_value={
                        "path": (params or {}).get("path"),
                        "content": self.content,
                        "size": len(self.content),
                    }
                )
        elif url.endswith("/repo/contents"):
            resp.json = MagicMock(return_value=self.entries)
        else:
            resp.json = MagicMock(return_value=[])
        return resp


@pytest.mark.asyncio
async def test_get_job_workspace_file_reads_repo_route_with_staleness_header(
    monkeypatch,
):
    """The read must hit /repo/file (Gitea) and lead with the head commit so
    the caller knows how fresh the bytes are."""
    cap = _RepoClient()
    monkeypatch.setattr("src.tools.orchestrator.jobs._get_client", lambda **kw: cap)
    tools = create_orchestrator_tools(ToolContext(user_id="user-xyz"))
    read = _tool_by_name(tools, "get_job_workspace_file")

    result = await read.ainvoke({"job_id": _REPO_JOB_ID, "path": "plan.md"})

    file_calls = [(u, p) for u, p in cap.gets if u.endswith("/repo/file")]
    assert file_calls == [
        (
            f"http://localhost:8085/api/jobs/{_REPO_JOB_ID}/repo/file",
            {"path": "plan.md"},
        )
    ]
    commits_calls = [(u, p) for u, p in cap.gets if u.endswith("/repo/commits")]
    assert commits_calls == [
        (
            f"http://localhost:8085/api/jobs/{_REPO_JOB_ID}/repo/commits",
            {"limit": 1},
        )
    ]
    assert result.startswith(
        "[repo head: abcdef1 2026-07-30T09:00:00Z — phase 2 tactical complete]"
    )
    assert "=== plan.md ===" in result
    assert "line one\nline two" in result


@pytest.mark.asyncio
async def test_get_job_workspace_file_passes_ref_and_names_it(monkeypatch):
    """An explicit ref rides as a query param to both /repo/file and the
    commits lookup, and the header names the ref instead of the branch head."""
    cap = _RepoClient()
    monkeypatch.setattr("src.tools.orchestrator.jobs._get_client", lambda **kw: cap)
    tools = create_orchestrator_tools(ToolContext(user_id="user-xyz"))
    read = _tool_by_name(tools, "get_job_workspace_file")

    tag = "19707fa1-phase-2-tactical-complete"
    result = await read.ainvoke({"job_id": _REPO_JOB_ID, "path": "plan.md", "ref": tag})

    file_calls = [(u, p) for u, p in cap.gets if u.endswith("/repo/file")]
    assert file_calls[0][1] == {"path": "plan.md", "ref": tag}
    commits_calls = [(u, p) for u, p in cap.gets if u.endswith("/repo/commits")]
    assert commits_calls[0][1] == {"limit": 1, "sha": tag}
    assert result.startswith(f"[ref '{tag}': abcdef1")


@pytest.mark.asyncio
async def test_get_job_workspace_file_404_names_ref_and_suggests_lister(
    monkeypatch,
):
    """The old message ('File not found') laundered the tool's own URL bug
    into a worker-didn't-deliver narrative. Not-found must say what was
    searched and point at the browse tool."""
    cap = _RepoClient(file_status=404)
    monkeypatch.setattr("src.tools.orchestrator.jobs._get_client", lambda **kw: cap)
    tools = create_orchestrator_tools(ToolContext(user_id="user-xyz"))
    read = _tool_by_name(tools, "get_job_workspace_file")

    result = await read.ainvoke({"job_id": _REPO_JOB_ID, "path": "output/report.md"})

    assert "'output/report.md' not found" in result
    assert "the job branch head" in result
    assert "list_job_workspace_files" in result

    result = await read.ainvoke(
        {"job_id": _REPO_JOB_ID, "path": "output/report.md", "ref": "some-tag"}
    )

    assert "ref 'some-tag'" in result
    assert "list_job_workspace_files" in result


@pytest.mark.asyncio
async def test_get_job_workspace_file_survives_commits_failure(monkeypatch):
    """The staleness header is best-effort: a dead commits lookup must not
    block the file read itself."""
    cap = _RepoClient(commits_fail=True)
    monkeypatch.setattr("src.tools.orchestrator.jobs._get_client", lambda **kw: cap)
    tools = create_orchestrator_tools(ToolContext(user_id="user-xyz"))
    read = _tool_by_name(tools, "get_job_workspace_file")

    result = await read.ainvoke({"job_id": _REPO_JOB_ID, "path": "plan.md"})

    assert result.startswith("=== plan.md ===")
    assert "line one\nline two" in result


@pytest.mark.asyncio
async def test_list_job_workspace_files_lists_directory(monkeypatch):
    """The lister exists so the officer can browse instead of guessing
    filenames into 404s; dirs first, sizes when known, same header."""
    entries = [
        {"name": "plan.md", "path": "plan.md", "type": "file", "size": 1532},
        {"name": "notes", "path": "notes", "type": "dir", "size": 0},
    ]
    cap = _RepoClient(entries=entries)
    monkeypatch.setattr("src.tools.orchestrator.jobs._get_client", lambda **kw: cap)
    tools = create_orchestrator_tools(ToolContext(user_id="user-xyz"))
    lister = _tool_by_name(tools, "list_job_workspace_files")

    result = await lister.ainvoke({"job_id": _REPO_JOB_ID})

    contents_calls = [(u, p) for u, p in cap.gets if u.endswith("/repo/contents")]
    assert contents_calls == [
        (
            f"http://localhost:8085/api/jobs/{_REPO_JOB_ID}/repo/contents",
            {"path": ""},
        )
    ]
    assert result.startswith("[repo head: abcdef1")
    assert "Contents of '/' (2 entries):" in result
    assert "\n  notes/\n" in result
    assert "plan.md (1532 bytes)" in result


@pytest.mark.asyncio
async def test_list_job_workspace_files_passes_path_and_ref(monkeypatch):
    cap = _RepoClient(entries=[])
    monkeypatch.setattr("src.tools.orchestrator.jobs._get_client", lambda **kw: cap)
    tools = create_orchestrator_tools(ToolContext(user_id="user-xyz"))
    lister = _tool_by_name(tools, "list_job_workspace_files")

    result = await lister.ainvoke(
        {"job_id": _REPO_JOB_ID, "path": "notes", "ref": "tag-1"}
    )

    contents_calls = [(u, p) for u, p in cap.gets if u.endswith("/repo/contents")]
    assert contents_calls[0][1] == {"path": "notes", "ref": "tag-1"}
    assert "Directory 'notes' is empty." in result
