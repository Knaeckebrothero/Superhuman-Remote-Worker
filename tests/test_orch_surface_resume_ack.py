"""The programmatic resume client surfaces drift instead of a bare HTTP error.

``AsyncCockpitClient.resume_persistent_thread`` must turn a 428 config-drift
response into a ``SessionConfigDriftError`` that names the drifted items, must
transmit an ``acknowledge`` list when the caller supplies one, and must leave
ordinary 200 and non-428-error handling unchanged. See
docs/features/session_config_drift_resume.md §4.6.

This repo has no ``httpx_mock``/pytest-httpx fixture; existing orch_surface
client tests (tests/test_mcp_client_contracts.py, tests/test_mcp_client_safety.py)
stub the transport directly with ``httpx.MockTransport``, so these tests follow
that pattern instead of adding a new test dependency.
"""

from __future__ import annotations

import json

import httpx
import pytest

from src.shared.orch_surface.client import AsyncCockpitClient, SessionConfigDriftError

RESUME_URL = "http://orchestrator.test/api/persistent/threads/t1/resume"


def _client(handler) -> AsyncCockpitClient:
    return AsyncCockpitClient(
        base_url="http://orchestrator.test",
        transport=httpx.MockTransport(handler),
    )


@pytest.mark.asyncio
async def test_428_raises_a_drift_error_naming_the_items() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == RESUME_URL
        return httpx.Response(
            428,
            json={
                "detail": {
                    "code": "config_drift",
                    "detail": (
                        "Parts of this session's configuration are no longer available"
                    ),
                    "drift": [
                        {
                            "id": "connector:abc",
                            "kind": "connector",
                            "reason": "deleted",
                            "label": "KurortEngine",
                        }
                    ],
                    "summary": ["KurortEngine"],
                }
            },
        )

    client = _client(handler)
    try:
        with pytest.raises(SessionConfigDriftError) as excinfo:
            await client.resume_persistent_thread("t1")
    finally:
        await client.close()

    assert "KurortEngine" in str(excinfo.value)
    assert excinfo.value.drift == [
        {
            "id": "connector:abc",
            "kind": "connector",
            "reason": "deleted",
            "label": "KurortEngine",
        }
    ]


@pytest.mark.asyncio
async def test_acknowledge_is_sent_in_the_body() -> None:
    captured: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"status": "created", "thread_id": "t1"})

    client = _client(handler)
    try:
        await client.resume_persistent_thread("t1", acknowledge=["connector:abc"])
    finally:
        await client.close()

    assert captured == [{"acknowledge": ["connector:abc"]}]


@pytest.mark.asyncio
async def test_200_without_acknowledge_returns_parsed_json_as_before() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "created", "thread_id": "t1"})

    client = _client(handler)
    try:
        result = await client.resume_persistent_thread("t1")
    finally:
        await client.close()

    assert result == {"status": "created", "thread_id": "t1"}


@pytest.mark.asyncio
async def test_non_428_error_still_raises_via_raise_for_status() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "boom"})

    client = _client(handler)
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await client.resume_persistent_thread("t1")
    finally:
        await client.close()
