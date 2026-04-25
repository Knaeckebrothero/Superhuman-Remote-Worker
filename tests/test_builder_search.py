"""Tests for ``services.builder_search.tavily_search``.

The helper migrated from reading ``TAVILY_API_KEY`` directly to accepting an
``api_key`` keyword arg — the orchestrator wiring resolves it from
``system_api_keys.tavily`` at dispatch time. These tests pin the new
contract: explicit key → HTTP call with Bearer header; missing key →
graceful error string, no HTTP call.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.services.builder_search import tavily_search


@pytest.mark.asyncio
async def test_returns_error_when_no_key():
    """Missing key short-circuits — never opens an httpx client."""
    with patch("orchestrator.services.builder_search.httpx.AsyncClient") as mock_client:
        result = await tavily_search("foo")
    assert "no Tavily API key configured" in result
    mock_client.assert_not_called()


@pytest.mark.asyncio
async def test_returns_error_when_key_is_empty_string():
    """Empty string is treated the same as None."""
    with patch("orchestrator.services.builder_search.httpx.AsyncClient") as mock_client:
        result = await tavily_search("foo", api_key="")
    assert "no Tavily API key configured" in result
    mock_client.assert_not_called()


@pytest.mark.asyncio
async def test_calls_tavily_with_bearer_header():
    """Explicit key is passed as ``Authorization: Bearer ...`` to the API."""
    fake_response = MagicMock()
    fake_response.raise_for_status = MagicMock()
    fake_response.json = MagicMock(
        return_value={
            "results": [{"title": "T", "url": "u", "content": "c"}],
            "answer": "summary",
        }
    )

    fake_client = MagicMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)
    fake_client.post = AsyncMock(return_value=fake_response)

    with patch(
        "orchestrator.services.builder_search.httpx.AsyncClient",
        return_value=fake_client,
    ):
        result = await tavily_search("hello world", api_key="tk-test-123")

    fake_client.post.assert_awaited_once()
    _, kwargs = fake_client.post.call_args
    assert kwargs["headers"]["Authorization"] == "Bearer tk-test-123"
    assert kwargs["json"]["query"] == "hello world"
    assert "summary" in result


@pytest.mark.asyncio
async def test_keeps_max_results_clamped():
    """``max_results`` is clamped to 1..10 inside the helper."""
    fake_response = MagicMock()
    fake_response.raise_for_status = MagicMock()
    fake_response.json = MagicMock(return_value={"results": [], "answer": ""})

    fake_client = MagicMock()
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)
    fake_client.post = AsyncMock(return_value=fake_response)

    with patch(
        "orchestrator.services.builder_search.httpx.AsyncClient",
        return_value=fake_client,
    ):
        await tavily_search("q", max_results=99, api_key="tk")

    sent = fake_client.post.call_args.kwargs["json"]
    assert sent["max_results"] == 10
