"""Semantic Scholar transport classification and secret-safe health tests."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from src.tools.research.utils import semantic_scholar_client as client_module
from src.tools.research.utils import provider_health
from src.tools.research.utils.semantic_scholar_client import (
    SemanticScholarProviderError,
    get_semantic_scholar_health,
    get_semantic_scholar_paper,
    semantic_scholar_request_json,
)


class _Response:
    def __init__(self, status: int, payload=None):
        self.status = status
        self._payload = payload if payload is not None else {}

    async def json(self):
        return self._payload


def _install_response(monkeypatch, response: _Response):
    captured = {}

    @asynccontextmanager
    async def fake_request(method, url, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        yield response

    monkeypatch.setattr(client_module, "research_request", fake_request)
    return captured


@pytest.mark.asyncio
async def test_success_records_secret_free_health(monkeypatch):
    monkeypatch.setenv("SEMANTIC_SCHOLAR_API_KEY", "test-secret-value")
    captured = _install_response(monkeypatch, _Response(200, {"paperId": "abc"}))

    result = await semantic_scholar_request_json("paper/abc")

    assert result == {"paperId": "abc"}
    assert captured["headers"] == {"x-api-key": "test-secret-value"}
    health = get_semantic_scholar_health()
    assert health["state"] == "ready"
    assert health["key_configured"] is True
    assert "test-secret-value" not in str(health)


@pytest.mark.asyncio
async def test_403_is_non_retryable_authentication_failure(monkeypatch):
    monkeypatch.setenv("SEMANTIC_SCHOLAR_API_KEY", "rejected-secret-value")
    _install_response(monkeypatch, _Response(403, {"message": "Forbidden"}))

    with pytest.raises(SemanticScholarProviderError) as raised:
        await semantic_scholar_request_json("paper/search")

    error = raised.value
    assert error.category == "authentication"
    assert error.status_code == 403
    assert error.retryable is False
    assert "rotate or verify" in str(error)
    assert "rejected-secret-value" not in str(error)
    health = get_semantic_scholar_health()
    assert health["state"] == "authentication"
    assert "rejected-secret-value" not in str(health)


@pytest.mark.asyncio
async def test_429_is_retryable_rate_limit(monkeypatch):
    monkeypatch.delenv("SEMANTIC_SCHOLAR_API_KEY", raising=False)
    _install_response(monkeypatch, _Response(429))

    with pytest.raises(SemanticScholarProviderError) as raised:
        await semantic_scholar_request_json("paper/search")

    error = raised.value
    assert error.category == "rate_limit"
    assert error.status_code == 429
    assert error.retryable is True
    assert "shared anonymous pool" in str(error)


@pytest.mark.asyncio
async def test_allowed_404_proves_provider_handshake(monkeypatch):
    monkeypatch.delenv("SEMANTIC_SCHOLAR_API_KEY", raising=False)
    _install_response(monkeypatch, _Response(404))

    result = await semantic_scholar_request_json(
        "paper/not-found", allow_not_found=True
    )

    assert result is None
    assert get_semantic_scholar_health()["state"] == "ready"


@pytest.mark.asyncio
async def test_paper_identifier_is_path_encoded(monkeypatch):
    monkeypatch.delenv("SEMANTIC_SCHOLAR_API_KEY", raising=False)
    captured = _install_response(monkeypatch, _Response(404))

    result = await get_semantic_scholar_paper(
        "DOI:10.1000/example/path", fields="title"
    )

    assert result is None
    assert captured["url"].endswith("paper/DOI:10.1000%2Fexample%2Fpath")


def test_arxiv_health_checks_installed_client_contract():
    provider_health.get_arxiv_health.cache_clear()

    result = provider_health.get_arxiv_health()

    assert result["state"] == "ready"
    assert result["version"]
    assert "results(search)" in result["message"]


@pytest.mark.asyncio
async def test_combined_probe_is_secret_free(monkeypatch):
    monkeypatch.setattr(
        provider_health,
        "probe_semantic_scholar",
        AsyncMock(
            return_value={
                "state": "authentication",
                "key_configured": True,
                "status_code": 403,
                "message": "configured key rejected",
            }
        ),
    )

    result = await provider_health.probe_paper_providers(timeout=1)

    assert result["ready"] is False
    assert result["providers"]["arxiv"]["state"] == "ready"
    assert result["providers"]["semantic_scholar"]["status_code"] == 403
    assert "api_key" not in str(result).lower()
