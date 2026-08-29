"""Boot registration and default-slot behavior for bundled SearXNG."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, call

import pytest

from orchestrator.seed.llm_config import (
    SEARXNG_ENDPOINT_LABEL,
    SEARXNG_MODEL_ID,
    ensure_searxng_search_endpoint,
)


def _db(*, endpoints=None, defaults=None, inserted=True):
    db = MagicMock()
    db.list_system_llm_endpoints = AsyncMock(return_value=list(endpoints or []))
    db.create_system_llm_endpoint = AsyncMock(
        return_value={"id": "22222222-2222-2222-2222-222222222222"}
    )
    db.create_model = AsyncMock(
        return_value={"model_id": SEARXNG_MODEL_ID} if inserted else None
    )
    defaults = defaults or {}
    db.get_default_llm_model = AsyncMock(
        side_effect=lambda capability: defaults.get(capability)
    )
    db.set_default_llm_model = AsyncMock()
    return db


@pytest.mark.asyncio
async def test_fresh_install_creates_keyless_search_row_and_primary(monkeypatch):
    monkeypatch.setenv("SEARXNG_BASE_URL", "http://srw-searxng:8080/")
    db = _db()

    assert await ensure_searxng_search_endpoint(db) is True

    endpoint = db.create_system_llm_endpoint.await_args.kwargs
    assert endpoint == {
        "label": SEARXNG_ENDPOINT_LABEL,
        "base_url": "http://srw-searxng:8080",
        "api_key": None,
        "key_prefix": None,
    }
    model = db.create_model.await_args.kwargs
    assert model["model_id"] == SEARXNG_MODEL_ID
    assert model["capabilities"] == ["search"]
    assert model["params_json"] == {
        "provider": "searxng",
        "ops": ["search"],
    }
    assert db.set_default_llm_model.await_args_list == [
        call("search", SEARXNG_MODEL_ID)
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("primary", ["tavily", "brave"])
async def test_existing_primary_places_searxng_in_empty_fallback(primary):
    db = _db(defaults={"search": primary})

    assert (
        await ensure_searxng_search_endpoint(
            db, base_url="http://srw-searxng:8080"
        )
        is True
    )

    assert db.set_default_llm_model.await_args_list == [
        call("search_fallback", SEARXNG_MODEL_ID)
    ]


@pytest.mark.asyncio
async def test_existing_fallback_is_not_clobbered():
    db = _db(defaults={"search": "brave", "search_fallback": "second-brave"})

    assert (
        await ensure_searxng_search_endpoint(
            db, base_url="http://srw-searxng:8080"
        )
        is True
    )

    db.set_default_llm_model.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_service_url_is_no_op(monkeypatch):
    monkeypatch.delenv("SEARXNG_BASE_URL", raising=False)
    db = _db()

    assert await ensure_searxng_search_endpoint(db) is False
    db.list_system_llm_endpoints.assert_not_awaited()


@pytest.mark.asyncio
async def test_second_boot_and_admin_deleted_model_are_not_recreated():
    db = _db(
        endpoints=[
            {
                "id": "22222222-2222-2222-2222-222222222222",
                "label": SEARXNG_ENDPOINT_LABEL,
            }
        ]
    )

    assert (
        await ensure_searxng_search_endpoint(
            db, base_url="http://srw-searxng:8080"
        )
        is False
    )
    db.create_system_llm_endpoint.assert_not_awaited()
    db.create_model.assert_not_awaited()
    db.set_default_llm_model.assert_not_awaited()


@pytest.mark.asyncio
async def test_catalog_conflict_does_not_write_defaults():
    db = _db(inserted=False)

    assert (
        await ensure_searxng_search_endpoint(
            db, base_url="http://srw-searxng:8080"
        )
        is False
    )
    db.set_default_llm_model.assert_not_awaited()
