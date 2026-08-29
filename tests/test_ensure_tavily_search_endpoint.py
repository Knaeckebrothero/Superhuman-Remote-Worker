"""Boot conversion of the legacy Tavily secret into catalog rows."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, call

import pytest

from orchestrator.seed.llm_config import (
    TAVILY_ENDPOINT_LABEL,
    TAVILY_MODEL_ID,
    ensure_tavily_search_endpoint,
)


def _db(*, search_rows=None, endpoints=None, defaults=None):
    db = MagicMock()
    db.list_models = AsyncMock(return_value=list(search_rows or []))
    db.list_system_llm_endpoints = AsyncMock(return_value=list(endpoints or []))
    db.create_system_llm_endpoint = AsyncMock(
        return_value={"id": "11111111-1111-1111-1111-111111111111"}
    )
    db.create_model = AsyncMock(return_value={"model_id": TAVILY_MODEL_ID})
    defaults = defaults or {}
    db.get_default_llm_model = AsyncMock(
        side_effect=lambda capability: defaults.get(capability)
    )
    db.set_default_llm_model = AsyncMock()
    return db


@pytest.mark.asyncio
async def test_key_creates_endpoint_catalog_row_and_empty_defaults(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test-secret")
    db = _db()

    assert await ensure_tavily_search_endpoint(db) is True

    endpoint = db.create_system_llm_endpoint.await_args.kwargs
    assert endpoint["label"] == TAVILY_ENDPOINT_LABEL
    assert endpoint["base_url"] == "https://api.tavily.com"
    assert endpoint["api_key"] == "tvly-test-secret"
    model = db.create_model.await_args.kwargs
    assert model["model_id"] == TAVILY_MODEL_ID
    assert model["capabilities"] == ["search", "fetch"]
    assert model["params_json"] == {
        "provider": "tavily",
        "ops": ["search", "extract", "crawl", "map"],
    }
    assert db.set_default_llm_model.await_args_list == [
        call("search", TAVILY_MODEL_ID),
        call("fetch", TAVILY_MODEL_ID),
    ]


@pytest.mark.asyncio
async def test_key_absent_is_no_op(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    db = _db()

    assert await ensure_tavily_search_endpoint(db) is False
    db.list_models.assert_not_awaited()


@pytest.mark.asyncio
async def test_second_boot_with_search_row_is_idempotent(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test-secret")
    db = _db(search_rows=[{"model_id": TAVILY_MODEL_ID}])

    assert await ensure_tavily_search_endpoint(db) is False
    db.create_system_llm_endpoint.assert_not_awaited()
    db.create_model.assert_not_awaited()


@pytest.mark.asyncio
async def test_admin_deleted_model_is_not_recreated(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test-secret")
    db = _db(
        search_rows=[],
        endpoints=[{"id": "endpoint-id", "label": TAVILY_ENDPOINT_LABEL}],
    )

    assert await ensure_tavily_search_endpoint(db) is False
    db.create_model.assert_not_awaited()


@pytest.mark.asyncio
async def test_existing_admin_default_is_not_clobbered(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test-secret")
    db = _db(defaults={"search": "brave"})

    assert await ensure_tavily_search_endpoint(db) is True
    assert db.set_default_llm_model.await_args_list == [call("fetch", TAVILY_MODEL_ID)]
