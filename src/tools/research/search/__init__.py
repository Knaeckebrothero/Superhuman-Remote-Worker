"""Search/fetch adapter registry."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from .base import Page, Result, SearchAdapter
from .errors import (
    ProviderAuthError,
    ProviderError,
    ProviderQuotaError,
    ProviderRateLimitError,
    ProviderRequestError,
    ProviderUnavailableError,
)
from .tavily import TavilyAdapter

logger = logging.getLogger(__name__)

ADAPTER_NAMES = frozenset({"tavily"})


def create_search_adapter(config: Any) -> SearchAdapter | None:
    """Build one adapter from an orchestrator-injected research section."""

    if not isinstance(config, Mapping):
        return None
    provider = str(config.get("provider") or "").strip().lower()
    if not provider:
        return None

    requested_ops = config.get("ops")
    ops = None
    if isinstance(requested_ops, (list, tuple, set, frozenset)):
        ops = frozenset(str(op) for op in requested_ops)

    if provider == "tavily":
        return TavilyAdapter(
            api_key=config.get("api_key"),
            base_url=config.get("base_url"),
            ops=ops,
        )

    logger.warning("Unknown research provider %r; no web tools constructed", provider)
    return None


__all__ = [
    "ADAPTER_NAMES",
    "Page",
    "ProviderAuthError",
    "ProviderError",
    "ProviderQuotaError",
    "ProviderRateLimitError",
    "ProviderRequestError",
    "ProviderUnavailableError",
    "Result",
    "SearchAdapter",
    "TavilyAdapter",
    "create_search_adapter",
]
