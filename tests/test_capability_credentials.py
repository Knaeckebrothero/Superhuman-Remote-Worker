"""Catalog-backed capability credential resolution."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.services.capability_credentials import (
    CapabilityCredentials,
    resolve_capability_credentials,
)


def _db(*, default=None, row=None):
    db = MagicMock()
    db.resolve_default_for_capability = AsyncMock(return_value=default)
    db.resolve_catalog_model = AsyncMock(return_value=row)
    db.get_user_llm_endpoint = AsyncMock(return_value=None)
    return db


def _row(**overrides):
    row = {
        "catalog_id": "11111111-1111-1111-1111-111111111111",
        "provider_kind": "endpoint",
        "provider_ref": "22222222-2222-2222-2222-222222222222",
        "model_id": "searxng",
        "endpoint_base_url": "http://searxng.svc:8080",
        "api_key": None,
        "params_json": {"provider": "searxng", "ops": ["search"]},
    }
    row.update(overrides)
    return row


@pytest.mark.asyncio
async def test_user_default_beats_system_default():
    db = _db(default="system-search", row=_row())

    result = await resolve_capability_credentials(
        capability="search",
        user_settings={"default_search_model": "searxng"},
        user_id="user-1",
        resolved_keys={},
        postgres_db=db,
    )

    assert result == CapabilityCredentials(
        model="searxng",
        base_url="http://searxng.svc:8080",
        api_key=None,
        provider="searxng",
        params={"provider": "searxng", "ops": ["search"]},
        catalog_id="11111111-1111-1111-1111-111111111111",
    )
    db.resolve_default_for_capability.assert_not_awaited()
    db.resolve_catalog_model.assert_awaited_once_with("searxng", capability="search")


@pytest.mark.asyncio
async def test_system_default_is_used_when_user_has_none():
    db = _db(default="searxng", row=_row())

    result = await resolve_capability_credentials(
        capability="search",
        user_settings={},
        user_id="user-1",
        resolved_keys={},
        postgres_db=db,
    )

    assert result is not None and result.model == "searxng"
    db.resolve_default_for_capability.assert_awaited_once_with("search")


@pytest.mark.asyncio
async def test_none_when_no_user_or_system_default():
    db = _db(default=None)

    result = await resolve_capability_credentials(
        capability="search",
        user_settings={},
        user_id="user-1",
        resolved_keys={},
        postgres_db=db,
    )

    assert result is None
    db.resolve_catalog_model.assert_not_awaited()


@pytest.mark.asyncio
async def test_null_endpoint_api_key_resolves_cleanly():
    db = _db(default="searxng", row=_row(api_key=None))

    result = await resolve_capability_credentials(
        capability="search",
        user_settings={},
        user_id="user-1",
        resolved_keys={},
        postgres_db=db,
    )

    assert result is not None
    assert result.api_key is None
    assert result.provider == "searxng"


@pytest.mark.asyncio
async def test_system_provider_uses_per_user_resolved_key():
    db = _db(
        default="brave",
        row=_row(
            provider_kind="system",
            provider_ref="brave",
            model_id="brave",
            endpoint_base_url=None,
            api_key="system-key",
            params_json={"provider": "brave", "ops": ["search"]},
        ),
    )

    result = await resolve_capability_credentials(
        capability="search",
        user_settings={},
        user_id="user-1",
        resolved_keys={"brave": "user-key"},
        postgres_db=db,
    )

    assert result is not None and result.api_key == "user-key"
