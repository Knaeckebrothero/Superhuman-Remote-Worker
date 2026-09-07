"""Transport contract for delegated Dynamic Canvas client calls."""

from __future__ import annotations

import httpx
import pytest

from agent.api.orchestrator_client import (
    CANVAS_REQUEST_TIMEOUT_SECONDS,
    CanvasClearResult,
    CanvasClientError,
    CanvasSetResult,
    OrchestratorClient,
)


def _client() -> OrchestratorClient:
    return OrchestratorClient(
        orchestrator_url="http://orchestrator.test",
        pod_ip="127.0.0.1",
        pod_port=8002,
        hostname="agent-test",
        config_name="persistent",
        user_id="user-1",
    )


@pytest.mark.asyncio
async def test_short_lived_client_attaches_internal_and_delegated_headers(monkeypatch):
    monkeypatch.setenv("MCP_INTERNAL_KEY", "internal-test-key")
    client = _client()
    await client.connect()
    try:
        assert client._client is not None
        assert client._client.headers["X-Internal-Key"] == "internal-test-key"
        assert client._client.headers["X-MCP-User-Id"] == "user-1"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_canvas_methods_use_canonical_public_and_internal_routes():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(
            200,
            json={
                "canvas_id": "main",
                "source": {"type": "workspace_file", "path": "output/a.md"},
                "presentation_revision": 1,
            },
        )

    client = _client()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        await client.get_thread_canvas("thread-1")
        await client.set_thread_canvas(
            "thread-1",
            {
                "source_type": "workspace_file",
                "path": "output/a.md",
                "renderer": "auto",
            },
        )
        assert await client.clear_thread_canvas("thread-1") is None
    finally:
        await client.close()

    assert [(request.method, request.url.path) for request in requests] == [
        ("GET", "/api/internal/persistent/threads/thread-1/canvases/main"),
        (
            "POST",
            "/api/internal/persistent/threads/thread-1/canvases/main/set",
        ),
        ("DELETE", "/api/internal/persistent/threads/thread-1/canvases/main"),
    ]
    assert requests[1].read() == (
        b'{"source_type":"workspace_file","path":"output/a.md","renderer":"auto"}'
    )
    assert all(
        request.extensions["timeout"]["read"] == CANVAS_REQUEST_TIMEOUT_SECONDS
        for request in requests
    )
    assert CANVAS_REQUEST_TIMEOUT_SECONDS > 50


@pytest.mark.asyncio
async def test_clear_result_carries_non_model_visible_changed_header():
    state = {
        "canvas_id": "main",
        "source": None,
        "status": "cleared",
        "presentation_revision": 4,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"X-Canvas-Mutation-Changed": "false"},
            json=state,
        )

    client = _client()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        result = await client.clear_thread_canvas("thread-1")
    finally:
        await client.close()

    assert result == CanvasClearResult(state=state, changed=False)


@pytest.mark.parametrize(
    ("header", "expected_changed"),
    [("true", True), ("false", False), (None, True)],
)
@pytest.mark.asyncio
async def test_set_result_parses_closed_mutation_header(
    header: str | None, expected_changed: bool
):
    state = {
        "canvas_id": "main",
        "source": {"type": "browser"},
        "presentation_revision": 4,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        headers = {"X-Canvas-Mutation-Changed": header} if header is not None else {}
        return httpx.Response(200, headers=headers, json=state)

    client = _client()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        result = await client.set_thread_canvas(
            "thread-1",
            {"source_type": "browser", "browser_id": "current"},
        )
    finally:
        await client.close()

    assert result == CanvasSetResult(state=state, changed=expected_changed)


@pytest.mark.parametrize("header", ["TRUE", "False", " true", "", "1"])
@pytest.mark.asyncio
async def test_set_result_rejects_malformed_mutation_header(header: str):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"X-Canvas-Mutation-Changed": header},
            json={"canvas_id": "main", "source": {"type": "browser"}},
        )

    client = _client()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(CanvasClientError) as error:
            await client.set_thread_canvas(
                "thread-1",
                {"source_type": "browser", "browser_id": "current"},
            )
    finally:
        await client.close()

    assert error.value.code == "invalid_canvas_response"


@pytest.mark.asyncio
async def test_canvas_typed_client_error_uses_fixed_message_without_request_details():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422,
            json={
                "detail": {
                    "code": "invalid_canvas_path",
                    "message": (
                        "sensitive-thread-id failed on private-service "
                        "while reading /srv/workspaces/private/missing.md"
                    ),
                }
            },
        )

    client = _client()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(CanvasClientError) as error:
            await client.set_thread_canvas(
                "sensitive-thread-id",
                {"source_type": "workspace_file", "path": "missing.md"},
            )
    finally:
        await client.close()

    assert error.value.code == "invalid_canvas_path"
    assert error.value.status_code == 422
    assert "File path is invalid" in str(error.value)
    assert "orchestrator.test" not in str(error.value)
    assert "sensitive-thread-id" not in str(error.value)
    assert "private-service" not in str(error.value)
    assert "/srv/workspaces" not in str(error.value)
    assert "/api/internal" not in str(error.value)


@pytest.mark.parametrize(
    ("code", "public_message"),
    [
        ("invalid_canvas_port", "Canvas application port is invalid"),
        ("canvas_port_reserved", "Canvas application port is reserved"),
        (
            "invalid_canvas_entry_path",
            "Canvas application entry path is invalid",
        ),
    ],
)
@pytest.mark.asyncio
async def test_canvas_app_errors_use_closed_public_messages(code, public_message):
    private_detail = (
        "sensitive-thread-id failed at private-workspace.test:30022 /srv/private"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422,
            json={"detail": {"code": code, "message": private_detail}},
        )

    client = _client()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(CanvasClientError) as error:
            await client.set_thread_canvas(
                "sensitive-thread-id",
                {"source_type": "workspace_port", "port": 8501},
            )
    finally:
        await client.close()

    rendered = str(error.value)
    assert error.value.code == code
    assert error.value.status_code == 422
    assert public_message in rendered
    assert private_detail not in rendered
    assert "sensitive-thread-id" not in rendered
    assert "private-workspace.test" not in rendered
    assert "/srv/private" not in rendered


@pytest.mark.asyncio
async def test_canvas_typed_client_error_rejects_unrecognized_backend_code():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422,
            json={
                "detail": {
                    "code": "private_service_failure",
                    "message": "sensitive-thread-id at private-service /srv/private",
                }
            },
        )

    client = _client()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(CanvasClientError) as error:
            await client.get_thread_canvas("sensitive-thread-id")
    finally:
        await client.close()

    rendered = str(error.value)
    assert error.value.code == "invalid_canvas_request"
    assert "Canvas request was rejected" in rendered
    assert "private_service_failure" not in rendered
    assert "sensitive-thread-id" not in rendered
    assert "private-service" not in rendered
    assert "/srv/private" not in rendered


@pytest.mark.asyncio
async def test_canvas_server_error_does_not_relay_response_body():
    secret = "http://private-service/api/internal/threads/secret-thread"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            json={
                "detail": {
                    "code": "backend_failure",
                    "message": f"failed while calling {secret}",
                }
            },
        )

    client = _client()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(CanvasClientError) as error:
            await client.get_thread_canvas("sensitive-thread-id")
    finally:
        await client.close()

    assert error.value.code == "canvas_service_unavailable"
    assert error.value.status_code == 500
    assert secret not in str(error.value)
    assert "private-service" not in str(error.value)
    assert "sensitive-thread-id" not in str(error.value)


@pytest.mark.asyncio
async def test_canvas_network_error_does_not_relay_transport_or_request_details():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(
            "dial to private-service failed for sensitive-thread-id", request=request
        )

    client = _client()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(CanvasClientError) as error:
            await client.clear_thread_canvas("sensitive-thread-id")
    finally:
        await client.close()

    assert error.value.code == "canvas_service_unavailable"
    assert error.value.status_code is None
    assert "private-service" not in str(error.value)
    assert "sensitive-thread-id" not in str(error.value)
    assert "orchestrator.test" not in str(error.value)


@pytest.mark.asyncio
async def test_canvas_invalid_success_body_is_sanitized():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="secret internal response")

    client = _client()
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(CanvasClientError) as error:
            await client.get_thread_canvas("sensitive-thread-id")
    finally:
        await client.close()

    assert error.value.code == "invalid_canvas_response"
    assert "secret internal response" not in str(error.value)
    assert "sensitive-thread-id" not in str(error.value)
