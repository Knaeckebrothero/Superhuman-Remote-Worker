"""Brave Search API adapter."""

from __future__ import annotations

from typing import Any, Mapping

import httpx

from .base import Result
from .errors import ProviderAuthError, ProviderError, ProviderRequestError
from .http import configured_endpoint, raise_for_provider_status, transport_error

BRAVE_SEARCH_BASE_URL = "https://api.search.brave.com"


class BraveAdapter:
    """Search-only client for Brave's fixed web-search endpoint."""

    provider = "brave"
    supported_ops = frozenset({"search"})

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None,
        ops: frozenset[str] | None = None,
    ) -> None:
        self.endpoint = configured_endpoint(
            base_url or BRAVE_SEARCH_BASE_URL,
            "res/v1/web/search",
        )
        self.api_key = api_key.strip() if isinstance(api_key, str) else None
        self.ops = ops if ops is not None else self.supported_ops

    def _require_search(self) -> str:
        if "search" not in self.ops:
            raise ProviderRequestError(
                "Brave adapter does not declare the 'search' operation"
            )
        if not self.api_key:
            raise ProviderAuthError("Brave Search API key is not configured")
        return self.api_key

    def search(self, query: str, max_results: int, **kw: Any) -> list[Result]:
        api_key = self._require_search()
        params: dict[str, Any] = {"q": query, "count": min(max(max_results, 1), 20)}
        freshness = {
            "day": "pd",
            "week": "pw",
            "month": "pm",
            "year": "py",
        }.get(kw.get("time_range"))
        if freshness:
            params["freshness"] = freshness
        try:
            with httpx.Client(timeout=30.0, follow_redirects=False) as client:
                response = client.get(
                    self.endpoint,
                    params=params,
                    headers={
                        "Accept": "application/json",
                        "X-Subscription-Token": api_key,
                    },
                )
            raise_for_provider_status("Brave", response)
            payload = response.json()
            if not isinstance(payload, Mapping):
                raise ValueError("response is not an object")
            web = payload.get("web")
            items = web.get("results", []) if isinstance(web, Mapping) else []
            return [
                Result(
                    title=str(item.get("title") or "Untitled"),
                    url=str(item.get("url") or ""),
                    snippet=str(item.get("description") or ""),
                )
                for item in items
                if isinstance(item, Mapping)
            ]
        except ProviderError:
            raise
        except Exception as exc:
            raise transport_error("Brave", exc) from exc

    def extract(self, urls: list[str], **kw: Any):
        del urls, kw
        raise ProviderRequestError("Brave does not support extract")

    def crawl(self, url: str, **kw: Any):
        del url, kw
        raise ProviderRequestError("Brave does not support crawl")

    def map(self, url: str, **kw: Any):
        del url, kw
        raise ProviderRequestError("Brave does not support map")
