"""Tests for ``services.builder_search.tavily_search``.

Tavily is a search engine, not an LLM, so its key lives in the
``TAVILY_API_KEY`` env var (sourced from a Helm/Vault secret in
production). These tests pin the contract: env-fallback resolution,
explicit ``api_key`` override → HTTP call with Bearer header, and a
graceful error string with no HTTP call when neither is set.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.services.builder_search import tavily_search


@pytest.mark.asyncio
async def test_returns_error_when_no_key(monkeypatch):
    """Missing key short-circuits — never opens an httpx client."""
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    with patch("orchestrator.services.builder_search.httpx.AsyncClient") as mock_client:
        result = await tavily_search("foo")
    assert "no Tavily API key configured" in result
    mock_client.assert_not_called()


@pytest.mark.asyncio
async def test_returns_error_when_key_is_empty_string(monkeypatch):
    """Empty string is treated the same as None."""
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    with patch("orchestrator.services.builder_search.httpx.AsyncClient") as mock_client:
        result = await tavily_search("foo", api_key="")
    assert "no Tavily API key configured" in result
    mock_client.assert_not_called()


@pytest.mark.asyncio
async def test_falls_back_to_env_var_when_kwarg_missing(monkeypatch):
    """When ``api_key`` is not passed, the helper reads ``TAVILY_API_KEY``."""
    monkeypatch.setenv("TAVILY_API_KEY", "tk-env-456")

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
        await tavily_search("hello")

    fake_client.post.assert_awaited_once()
    _, kwargs = fake_client.post.call_args
    assert kwargs["headers"]["Authorization"] == "Bearer tk-env-456"


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
