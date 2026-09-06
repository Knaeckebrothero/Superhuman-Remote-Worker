"""SearXNG JSON API adapter."""

from __future__ import annotations

from typing import Any, Mapping

import httpx

from agent.tools.research.search.base import Result
from agent.tools.research.search.errors import ProviderError, ProviderRequestError
from agent.tools.research.search.http import (
    configured_endpoint,
    raise_for_provider_status,
    transport_error,
)


class SearxngAdapter:
    """Search-only client for an admin-configured SearXNG service."""

    provider = "searxng"
    supported_ops = frozenset({"search"})

    def __init__(
        self,
        *,
        base_url: str | None,
        api_key: str | None = None,
        ops: frozenset[str] | None = None,
    ) -> None:
        del api_key  # SearXNG's JSON endpoint is keyless.
        self.endpoint = configured_endpoint(base_url, "search")
        self.ops = ops if ops is not None else self.supported_ops

    def _require_search(self) -> None:
        if "search" not in self.ops:
            raise ProviderRequestError(
                "SearXNG adapter does not declare the 'search' operation"
            )

    def search(self, query: str, max_results: int, **kw: Any) -> list[Result]:
        self._require_search()
        params: dict[str, Any] = {"q": query, "format": "json"}
        if kw.get("time_range"):
            params["time_range"] = kw["time_range"]
        try:
            with httpx.Client(timeout=30.0, follow_redirects=False) as client:
                response = client.get(
                    self.endpoint,
                    params=params,
                    headers={"Accept": "application/json"},
                )
            raise_for_provider_status("SearXNG", response)
            payload = response.json()
            if not isinstance(payload, Mapping):
                raise ValueError("response is not an object")
            return [
                Result(
                    title=str(item.get("title") or "Untitled"),
                    url=str(item.get("url") or ""),
                    snippet=str(item.get("content") or ""),
                )
                for item in payload.get("results", [])[:max_results]
                if isinstance(item, Mapping)
            ]
        except ProviderError:
            raise
        except Exception as exc:
            raise transport_error("SearXNG", exc) from exc

    def extract(self, urls: list[str], **kw: Any):
        del urls, kw
        raise ProviderRequestError("SearXNG does not support extract")

    def crawl(self, url: str, **kw: Any):
        del url, kw
        raise ProviderRequestError("SearXNG does not support crawl")

    def map(self, url: str, **kw: Any):
        del url, kw
        raise ProviderRequestError("SearXNG does not support map")
