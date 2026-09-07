"""The legacy keyed provider must seed before the bundled components."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.seed import llm_config


@pytest.mark.asyncio
async def test_research_seed_runs_tavily_before_the_bundled_components(monkeypatch):
    db = MagicMock()
    db.connect = AsyncMock()
    db.close = AsyncMock()
    monkeypatch.setattr(llm_config, "PostgresDB", lambda: db)

    events: list[str] = []

    async def ensure_tavily(_db):
        events.append("tavily")
        return True

    async def ensure_searxng(_db):
        events.append("searxng")
        return True

    # Tavily serves search AND fetch, so it must claim both empty slots before
    # either bundled component is allowed to fill one.
    async def ensure_crawl4ai(_db):
        events.append("crawl4ai")
        return True

    monkeypatch.setattr(llm_config, "ensure_tavily_search_endpoint", ensure_tavily)
    monkeypatch.setattr(llm_config, "ensure_searxng_search_endpoint", ensure_searxng)
    monkeypatch.setattr(llm_config, "ensure_crawl4ai_fetch_endpoint", ensure_crawl4ai)

    await llm_config.run_research_provider_seed()

    assert events == ["tavily", "searxng", "crawl4ai"]
    db.connect.assert_awaited_once()
    db.close.assert_awaited_once()
