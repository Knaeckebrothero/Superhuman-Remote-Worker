"""Fail-closed MCP auth-context behavior.

Two deliberately different anonymous shapes exist on the MCP surface:

* stdio transport is the documented internal mode (docker/Dockerfile.mcp):
  no token middleware, requests authenticate with ``MCP_INTERNAL_KEY`` alone.
* http transport's only identity source is the verified bearer token. When it
  cannot be resolved, the invocation must go out with NO identity headers at
  all — never the internal key as an error fallback — so guarded orchestrator
  endpoints 401 (the pre-unification fail-closed behavior), and the tool
  result must name the auth context failure.

The wire-level mechanics are tested in-process against the shared surface;
everything touching ``orchestrator.mcp.server`` runs in a clean interpreter
(the conftest path setup shadows the ``mcp`` SDK with ``orchestrator/mcp``,
same reason ``test_mcp_capabilities.py`` uses subprocesses).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import httpx
import pytest

from src.shared.orch_surface.client import AsyncCockpitClient
from src.shared.orch_surface.jobs import (
    AUTH_CONTEXT_FAILURE_NOTICE,
    CallerCtx,
    get_descriptor,
    make_bound_handler,
)

ROOT = Path(__file__).parent.parent

_IDENTITY_HEADERS = ("X-Internal-Key", "X-MCP-User-Id", "X-MCP-Scope")


class HeaderRecorder:
    """Models a dual-callable orchestrator endpoint: identity in, 401 out."""

    def __init__(self) -> None:
        self.headers: list[dict[str, str | None]] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.headers.append(
            {name: request.headers.get(name) for name in _IDENTITY_HEADERS}
        )
        authenticated = request.headers.get("X-Internal-Key") or request.headers.get(
            "X-MCP-User-Id"
        )
        if not authenticated:
            return httpx.Response(401, json={"detail": "Not authenticated"})
        if request.method == "GET" and request.url.path == "/api/jobs":
            return httpx.Response(200, json=[])
        return httpx.Response(200, json={"status": "ok"})


def _client(recorder: HeaderRecorder) -> AsyncCockpitClient:
    return AsyncCockpitClient(
        base_url="http://orchestrator.test",
        transport=httpx.MockTransport(recorder),
    )


# ---------------------------------------------------------------------------
# Wire mechanics (shared surface, in-process)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_anonymous_internal_caller_keeps_internal_key_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """stdio's caller shape (anonymous, auth_failed=False) stays internal."""
    monkeypatch.setenv("MCP_INTERNAL_KEY", "internal-test-key")
    recorder = HeaderRecorder()
    client = _client(recorder)
    invoke = make_bound_handler(
        get_descriptor("list_jobs"),
        client_provider=lambda: client,
        caller_provider=lambda: CallerCtx(kind="mcp"),
    )
    try:
        result = await invoke()
    finally:
        await client.close()

    assert recorder.headers == [
        {
            "X-Internal-Key": "internal-test-key",
            "X-MCP-User-Id": None,
            "X-MCP-Scope": None,
        }
    ]
    assert AUTH_CONTEXT_FAILURE_NOTICE not in result


@pytest.mark.asyncio
async def test_auth_failed_caller_sends_no_identity_headers_and_reports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The security property: an auth-context failure must never be silently
    upgraded to internal-grade access on dual-callable endpoints."""
    monkeypatch.setenv("MCP_INTERNAL_KEY", "internal-test-key")
    recorder = HeaderRecorder()
    client = _client(recorder)
    invoke = make_bound_handler(
        get_descriptor("list_jobs"),
        client_provider=lambda: client,
        caller_provider=lambda: CallerCtx(kind="mcp", auth_failed=True),
    )
    try:
        result = await invoke()
    finally:
        await client.close()

    # The request went out (the orchestrator is the enforcement point and
    # 401s it) with none of the three identity headers — the internal key is
    # never attached as an error fallback.
    assert recorder.headers == [
        {"X-Internal-Key": None, "X-MCP-User-Id": None, "X-MCP-Scope": None}
    ]
    assert result.startswith(AUTH_CONTEXT_FAILURE_NOTICE)
    assert "auth context failure" in result.lower()
    assert "401" in result


@pytest.mark.asyncio
async def test_session_lane_never_sends_scope_header() -> None:
    """The session/agent lane never sends X-MCP-Scope (server-side project
    fencing hid NULL-project jobs); officer_supervision_surface E2 owns any
    deliberate reintroduction."""
    recorder = HeaderRecorder()
    client = _client(recorder)
    invoke = make_bound_handler(
        get_descriptor("list_jobs"),
        client_provider=lambda: client,
        caller_provider=lambda: CallerCtx(
            kind="session",
            user_id="user-1",
            project_ids=("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",),
        ),
    )
    try:
        await invoke()
    finally:
        await client.close()

    assert recorder.headers[0]["X-MCP-Scope"] is None
    assert recorder.headers[0]["X-MCP-User-Id"] == "user-1"


@pytest.mark.asyncio
async def test_mcp_lane_still_stamps_scope_header() -> None:
    recorder = HeaderRecorder()
    client = _client(recorder)
    invoke = make_bound_handler(
        get_descriptor("list_jobs"),
        client_provider=lambda: client,
        caller_provider=lambda: CallerCtx(
            kind="mcp",
            user_id="user-1",
            project_ids=("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",),
        ),
    )
    try:
        await invoke()
    finally:
        await client.close()

    assert (
        recorder.headers[0]["X-MCP-Scope"]
        == "project:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    )


# ---------------------------------------------------------------------------
# Server-side _get_mcp_caller_ctx + the hand-written mcp_tool wrapper
# (clean interpreter; the SDK's ``mcp`` package must not be shadowed)
# ---------------------------------------------------------------------------

_SERVER_SCRIPT = """
import asyncio
import json
import os
from types import SimpleNamespace

import httpx

os.environ["MCP_INTERNAL_KEY"] = "internal-test-key"
os.environ["MCP_TRANSPORT"] = "stdio"  # import-safe; flipped per case below

from orchestrator.mcp import server
from src.shared.orch_surface.client import AsyncCockpitClient

import mcp.server.auth.middleware.auth_context as auth_context

report = {}

# stdio: the documented internal lane.
server._transport = "stdio"
ctx = server._get_mcp_caller_ctx()
report["stdio"] = {"auth_failed": ctx.auth_failed, "user_id": ctx.user_id}

# http, no resolvable token (outside middleware get_access_token() is None).
server._transport = "http"
ctx = server._get_mcp_caller_ctx()
report["http_missing_token"] = {"auth_failed": ctx.auth_failed, "user_id": ctx.user_id}

# http, middleware raises.
def boom():
    raise RuntimeError("middleware exploded")
original = auth_context.get_access_token
auth_context.get_access_token = boom
ctx = server._get_mcp_caller_ctx()
report["http_middleware_error"] = {
    "auth_failed": ctx.auth_failed,
    "user_id": ctx.user_id,
}

# http, resolved token keeps full identity.
token = SimpleNamespace(
    client_id="user-1",
    scopes=["project:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"],
)
auth_context.get_access_token = lambda: token
ctx = server._get_mcp_caller_ctx()
report["http_token"] = {
    "auth_failed": ctx.auth_failed,
    "user_id": ctx.user_id,
    "scope": ctx.scope_header,
}
auth_context.get_access_token = original

# Hand-written mcp_tool wrapper under http auth failure: request goes out
# with no identity headers; result names the auth context failure.
observed = []

async def handler(request):
    observed.append(
        {
            name: request.headers.get(name)
            for name in ("X-Internal-Key", "X-MCP-User-Id", "X-MCP-Scope")
        }
    )
    return httpx.Response(401, json={"detail": "Not authenticated"})

server._client = AsyncCockpitClient(
    base_url="http://orchestrator.test",
    transport=httpx.MockTransport(handler),
)

async def run_tool():
    tool = server.test_datasource
    function = getattr(tool, "fn", tool)
    return await function(datasource_id="connector-1")

result = asyncio.run(run_tool())
report["handwritten_tool"] = {"headers": observed, "result": result}

print(json.dumps(report))
"""


@pytest.fixture(scope="module")
def server_report() -> dict:
    env = dict(os.environ)
    env["MCP_TRANSPORT"] = "stdio"
    completed = subprocess.run(
        [sys.executable, "-c", _SERVER_SCRIPT],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_stdio_transport_is_deliberate_internal_operation(server_report) -> None:
    assert server_report["stdio"] == {"auth_failed": False, "user_id": None}


def test_http_missing_token_marks_auth_context_failed(server_report) -> None:
    assert server_report["http_missing_token"] == {
        "auth_failed": True,
        "user_id": None,
    }


def test_http_middleware_error_marks_auth_context_failed(server_report) -> None:
    assert server_report["http_middleware_error"] == {
        "auth_failed": True,
        "user_id": None,
    }


def test_http_resolved_token_keeps_full_identity(server_report) -> None:
    assert server_report["http_token"] == {
        "auth_failed": False,
        "user_id": "user-1",
        "scope": "project:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    }


def test_handwritten_mcp_tool_fails_closed_on_http_auth_failure(
    server_report,
) -> None:
    outcome = server_report["handwritten_tool"]
    assert outcome["headers"] == [
        {"X-Internal-Key": None, "X-MCP-User-Id": None, "X-MCP-Scope": None}
    ]
    assert outcome["result"].startswith(AUTH_CONTEXT_FAILURE_NOTICE)
