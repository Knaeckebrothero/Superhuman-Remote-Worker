"""Concurrency and retry-safety regressions for the orchestrator MCP client."""

from __future__ import annotations

import asyncio
import inspect

import httpx
import pytest

from shared.orch_surface.client import AsyncCockpitClient, MutationOutcomeUnknown
from shared.orch_surface.jobs import CallerCtx, get_descriptor, make_bound_handler


@pytest.mark.asyncio
async def test_bound_invocations_isolate_project_scope_and_reset_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One pooled client must never leak identity between concurrent tools."""
    monkeypatch.setenv("MCP_INTERNAL_KEY", "test-internal-key")
    observed: list[tuple[str, str]] = []
    both_arrived = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        observed.append(
            (
                request.headers.get("X-MCP-User-Id", ""),
                request.headers.get("X-MCP-Scope", ""),
            )
        )
        if len(observed) == 2:
            both_arrived.set()
        await asyncio.wait_for(both_arrived.wait(), timeout=1)
        return httpx.Response(200, json=[])

    client = AsyncCockpitClient(
        base_url="http://orchestrator.test",
        transport=httpx.MockTransport(handler),
    )
    descriptor = get_descriptor("list_jobs")
    # Only the MCP lane stamps X-MCP-Scope; a session caller with an identical
    # single-project binding must NOT (see make_bound_handler).
    mcp_call = make_bound_handler(
        descriptor,
        client_provider=lambda: client,
        caller_provider=lambda: CallerCtx(
            kind="mcp",
            user_id="user-mcp",
            project_ids=("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",),
        ),
    )
    session_call = make_bound_handler(
        descriptor,
        client_provider=lambda: client,
        caller_provider=lambda: CallerCtx(
            kind="session",
            user_id="user-session",
            project_ids=("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",),
        ),
    )

    try:
        await asyncio.gather(mcp_call(), session_call())
        # The descriptor wrapper has reset its ContextVar token. A direct read
        # therefore carries only the client's internal credential.
        direct = await client.list_jobs()
    finally:
        await client.close()

    assert set(observed[:2]) == {
        (
            "user-mcp",
            "project:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        ),
        ("user-session", ""),
    }
    assert observed[2] == ("", "")
    assert direct["jobs"] == []


@pytest.mark.asyncio
async def test_request_scope_headers_are_isolated_between_users_and_health(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Delayed concurrent calls must retain their own identity and scope."""
    monkeypatch.setenv("MCP_INTERNAL_KEY", "test-internal-key")
    observed: list[tuple[str, str, str, str]] = []
    both_users_arrived = asyncio.Event()
    arrivals = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal arrivals
        user_id = request.headers.get("X-MCP-User-Id", "")
        scope = request.headers.get("X-MCP-Scope", "")
        internal_key = request.headers.get("X-Internal-Key", "")
        observed.append((request.url.path, user_id, scope, internal_key))

        if request.url.path == "/api/health":
            return httpx.Response(200, json={"status": "healthy"})

        arrivals += 1
        if arrivals == 2:
            both_users_arrived.set()
        await asyncio.wait_for(both_users_arrived.wait(), timeout=1)
        return httpx.Response(200, json=[{"user_id": user_id, "scope": scope}])

    client = AsyncCockpitClient(
        base_url="http://orchestrator.test",
        transport=httpx.MockTransport(handler),
    )

    # F15: the imperative set_scope_headers/clear_scope_headers pair is gone;
    # invocation_scope is the one binding mechanism and must isolate
    # concurrent tasks exactly as the removed methods did.
    async def call_as(user_id: str, scope: str) -> list[dict[str, str]]:
        with client.invocation_scope(user_id=user_id, scope=scope):
            return await client.list_jobs()

    async def run_health_probe() -> dict[str, str]:
        await asyncio.sleep(0)
        with client.invocation_scope(unauthenticated=True):
            return await client.health_check()

    try:
        first, second, health = await asyncio.gather(
            call_as("user-a", "project:project-a"),
            call_as("user-b", "project:project-b"),
            run_health_probe(),
        )
    finally:
        await client.close()

    assert first["jobs"] == [{"user_id": "user-a", "scope": "project:project-a"}]
    assert second["jobs"] == [{"user_id": "user-b", "scope": "project:project-b"}]
    assert health == {"status": "healthy"}
    assert set(observed) == {
        ("/api/jobs", "user-a", "project:project-a", "test-internal-key"),
        ("/api/jobs", "user-b", "project:project-b", "test-internal-key"),
        ("/api/health", "", "", ""),
    }
    assert "X-MCP-User-Id" not in client._client.headers
    assert "X-MCP-Scope" not in client._client.headers
    assert "X-Internal-Key" not in client._client.headers


@pytest.mark.asyncio
async def test_delayed_mutation_is_sent_once() -> None:
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        await asyncio.sleep(0.02)
        return httpx.Response(201, json={"id": "job-1", "status": "created"})

    client = AsyncCockpitClient(
        base_url="http://orchestrator.test",
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await client.create_job("delayed job")
    finally:
        await client.close()

    assert result == {"id": "job-1", "status": "created"}
    assert attempts == 1


@pytest.mark.asyncio
async def test_ambiguous_mutation_timeout_is_not_retried_or_reported_failed() -> None:
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout("response arrived too late", request=request)

    client = AsyncCockpitClient(
        base_url="http://orchestrator.test",
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(MutationOutcomeUnknown) as exc_info:
            await client.create_job("ambiguous job")
    finally:
        await client.close()

    assert attempts == 1
    assert "Outcome unknown" in str(exc_info.value)
    assert "did not retry" in str(exc_info.value)
    assert "Verify current state" in str(exc_info.value)


@pytest.mark.asyncio
async def test_mutation_connect_failure_is_distinct_and_not_retried() -> None:
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectError("orchestrator unavailable", request=request)

    client = AsyncCockpitClient(
        base_url="http://orchestrator.test",
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(httpx.ConnectError):
            await client.create_job("not sent")
    finally:
        await client.close()

    assert attempts == 1


@pytest.mark.asyncio
async def test_non_get_read_is_single_attempt_without_mutation_ambiguity() -> None:
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout("read timed out", request=request)

    client = AsyncCockpitClient(
        base_url="http://orchestrator.test",
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(httpx.ReadTimeout):
            await client.test_datasource("connector-1")
    finally:
        await client.close()

    assert attempts == 1


def test_mutation_methods_do_not_use_the_read_retry_decorator() -> None:
    """Guard every mutation wrapper against accidentally restoring retries."""
    class_source = inspect.getsource(AsyncCockpitClient)
    for verb in ("post", "put", "patch", "delete"):
        assert f"self._client.{verb}(" not in class_source

    mutation_methods = {
        name
        for name, method in inspect.getmembers(
            AsyncCockpitClient, inspect.iscoroutinefunction
        )
        if "self._mutation_request(" in inspect.getsource(method)
    }

    assert {
        "create_job",
        "delete_job",
        "create_project",
        "create_datasource",
        "create_persistent_thread",
    } <= mutation_methods

    for method_name in mutation_methods:
        method = getattr(AsyncCockpitClient, method_name)
        assert not hasattr(method, "retry"), method_name
