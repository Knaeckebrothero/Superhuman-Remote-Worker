"""Dispatch-time delivery of catalog-resolved search/fetch providers."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

import pytest

os.environ.setdefault("VECTOR_DB_URL", "postgresql://test@localhost/test")

import orchestrator.main  # noqa: E402
from orchestrator.services.capability_credentials import CapabilityCredentials  # noqa: E402


@pytest.mark.asyncio
async def test_injects_primary_search_and_fetch_sections():
    async def resolve(*, capability, **kwargs):
        del kwargs
        if capability == "search":
            return CapabilityCredentials(
                model="searxng",
                base_url="http://searxng.svc:8080",
                api_key=None,
                provider="searxng",
                params={"provider": "searxng", "ops": ["search"]},
                catalog_id="search-row",
            )
        return CapabilityCredentials(
            model="tavily",
            base_url="https://api.tavily.com",
            api_key="tvly-secret",
            provider="tavily",
            params={
                "provider": "tavily",
                "ops": ["extract", "crawl", "map"],
            },
            catalog_id="fetch-row",
        )

    config = {}
    with patch(
        "orchestrator.services.capability_credentials.resolve_capability_credentials",
        AsyncMock(side_effect=resolve),
    ):
        result = await orchestrator.main._inject_search_credentials(
            config,
            user_settings={"default_search_model": "searxng"},
            user_id="user-1",
            resolved_keys={},
        )

    assert result["research"] == {
        "search": {
            "provider": "searxng",
            "base_url": "http://searxng.svc:8080",
            "api_key": None,
            "ops": ["search"],
        },
        "fetch": {
            "provider": "tavily",
            "base_url": "https://api.tavily.com",
            "api_key": "tvly-secret",
            "ops": ["extract", "crawl", "map"],
        },
    }


@pytest.mark.asyncio
async def test_removes_stale_sections_when_nothing_resolves():
    config = {
        "research": {
            "search": {"provider": "stale"},
            "fetch": {"provider": "stale"},
        }
    }
    with patch(
        "orchestrator.services.capability_credentials.resolve_capability_credentials",
        AsyncMock(return_value=None),
    ):
        await orchestrator.main._inject_search_credentials(
            config,
            user_settings={},
            user_id="user-1",
            resolved_keys={},
        )

    assert "research" not in config


@pytest.mark.asyncio
async def test_malformed_provider_params_degrade_without_credentials():
    creds = CapabilityCredentials(
        model="broken-row",
        base_url="https://provider.invalid",
        api_key="must-not-survive",
        provider=None,
        params={"ops": ["search"]},
    )
    config = {}
    with patch(
        "orchestrator.services.capability_credentials.resolve_capability_credentials",
        AsyncMock(return_value=creds),
    ):
        await orchestrator.main._inject_search_credentials(
            config,
            user_settings={},
            user_id="user-1",
            resolved_keys={},
        )

    assert "research" not in config


@pytest.mark.asyncio
async def test_different_catalog_row_is_injected_as_search_fallback():
    primary = CapabilityCredentials(
        model="tavily",
        base_url="https://api.tavily.com",
        api_key="primary-key",
        provider="tavily",
        params={"provider": "tavily", "ops": ["search"]},
        catalog_id="primary-row",
    )
    fallback = CapabilityCredentials(
        model="searxng",
        base_url="http://searxng.svc:8080",
        provider="searxng",
        params={"provider": "searxng", "ops": ["search"]},
        catalog_id="fallback-row",
    )

    async def resolve(*, capability, setting_key=None, **kwargs):
        del kwargs
        if setting_key == "default_search_fallback_model":
            return fallback
        return primary if capability == "search" else None

    config = {}
    with patch(
        "orchestrator.services.capability_credentials.resolve_capability_credentials",
        AsyncMock(side_effect=resolve),
    ):
        await orchestrator.main._inject_search_credentials(
            config,
            user_settings={},
            user_id="user-1",
            resolved_keys={},
        )

    assert config["research"]["search_fallback"] == {
        "provider": "searxng",
        "base_url": "http://searxng.svc:8080",
        "api_key": None,
        "ops": ["search"],
    }


@pytest.mark.asyncio
async def test_same_catalog_row_is_not_injected_as_its_own_fallback():
    same = CapabilityCredentials(
        model="tavily",
        base_url="https://api.tavily.com",
        api_key="key",
        provider="tavily",
        params={"provider": "tavily", "ops": ["search"]},
        catalog_id="same-row",
    )

    async def resolve(*, capability, **kwargs):
        del kwargs
        return same if capability == "search" else None

    config = {}
    with patch(
        "orchestrator.services.capability_credentials.resolve_capability_credentials",
        AsyncMock(side_effect=resolve),
    ):
        await orchestrator.main._inject_search_credentials(
            config,
            user_settings={},
            user_id="user-1",
            resolved_keys={},
        )

    assert "search_fallback" not in config["research"]
