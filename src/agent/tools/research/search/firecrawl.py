"""Firecrawl v2 search and remote-fetch adapter."""

from __future__ import annotations

import time
from typing import Any, Mapping
from urllib.parse import quote, urlsplit

import httpx

from agent.tools.research.search.base import Page, Result, SEARCH_OPS
from agent.tools.research.search.errors import (
    ProviderAuthError,
    ProviderError,
    ProviderRequestError,
    ProviderUnavailableError,
)
from agent.tools.research.search.http import (
    configured_endpoint,
    raise_for_provider_status,
    transport_error,
)


FIRECRAWL_BASE_URL = "https://api.firecrawl.dev"
_CRAWL_POLL_INTERVAL_SECONDS = 0.5
_CRAWL_TIMEOUT_SECONDS = 120.0


def _firecrawl_endpoint(base_url: str, suffix: str) -> str:
    """Build a v2 endpoint while accepting roots with or without ``/v2``."""

    path = urlsplit(base_url).path.rstrip("/")
    api_suffix = suffix if path.endswith("/v2") else f"v2/{suffix}"
    return configured_endpoint(base_url, api_suffix)


class FirecrawlAdapter:
    """Typed client for a fixed hosted or self-hosted Firecrawl API origin."""

    provider = "firecrawl"
    supported_ops = SEARCH_OPS

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        ops: frozenset[str] | None = None,
    ) -> None:
        configured_base = str(base_url or "").strip()
        self.base_url = configured_base or FIRECRAWL_BASE_URL
        self.api_key = api_key.strip() if isinstance(api_key, str) else None
        self._hosted_default = not configured_base
        self.ops = ops if ops is not None else self.supported_ops
        self.search_endpoint = _firecrawl_endpoint(self.base_url, "search")
        self.scrape_endpoint = _firecrawl_endpoint(self.base_url, "scrape")
        self.crawl_endpoint = _firecrawl_endpoint(self.base_url, "crawl")
        self.map_endpoint = _firecrawl_endpoint(self.base_url, "map")

    def _require(self, op: str) -> None:
        if op not in self.ops or op not in self.supported_ops:
            raise ProviderRequestError(
                f"Firecrawl adapter does not declare the {op!r} operation"
            )
        # Firecrawl Cloud always requires a bearer token. An explicitly
        # configured self-hosted service may deliberately run without one.
        if self._hosted_default and not self.api_key:
            raise ProviderAuthError("Firecrawl API key is not configured")

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    @staticmethod
    def _payload(response: httpx.Response) -> Mapping[str, Any]:
        if response.status_code == 408:
            raise ProviderUnavailableError(
                "Firecrawl request timed out", status_code=408
            )
        raise_for_provider_status("Firecrawl", response)
        payload = response.json()
        if not isinstance(payload, Mapping):
            raise ProviderUnavailableError("Firecrawl returned an invalid response")
        if payload.get("success") is False:
            message = str(payload.get("error") or "Firecrawl request failed")
            raise ProviderUnavailableError(message)
        return payload

    @staticmethod
    def _page(item: Mapping[str, Any], fallback_url: str = "") -> Page:
        metadata_value = item.get("metadata")
        metadata = metadata_value if isinstance(metadata_value, Mapping) else {}
        url = str(
            metadata.get("sourceURL")
            or metadata.get("url")
            or item.get("url")
            or fallback_url
        )
        content = str(
            item.get("markdown")
            or item.get("content")
            or item.get("html")
            or item.get("rawHtml")
            or ""
        )
        error = metadata.get("error")
        status = metadata.get("statusCode") or metadata.get("pageStatusCode")
        failed = str(error) if error else None
        if failed is None:
            try:
                if status is not None and int(status) >= 400:
                    failed = f"HTTP {int(status)}"
            except (TypeError, ValueError):
                pass
        return Page(url=url, content=content, failed=failed)

    def search(self, query: str, max_results: int, **kw: Any) -> list[Result]:
        self._require("search")
        body: dict[str, Any] = {
            "query": query,
            "limit": min(max(max_results, 1), 100),
        }
        if kw.get("include_domains"):
            body["includeDomains"] = kw["include_domains"]
        if kw.get("exclude_domains"):
            body["excludeDomains"] = kw["exclude_domains"]
        time_filter = {
            "day": "qdr:d",
            "week": "qdr:w",
            "month": "qdr:m",
            "year": "qdr:y",
        }.get(kw.get("time_range"))
        if time_filter:
            body["tbs"] = time_filter
        if kw.get("include_raw_content"):
            body["scrapeOptions"] = {
                "formats": ["markdown"],
                "onlyMainContent": True,
            }

        try:
            with httpx.Client(timeout=60.0, follow_redirects=False) as client:
                response = client.post(
                    self.search_endpoint,
                    json=body,
                    headers=self._headers(),
                )
            payload = self._payload(response)
            data = payload.get("data", {})
            if isinstance(data, Mapping):
                items = data.get("web", [])
            else:
                # Firecrawl v1/self-host compatibility: search returned the
                # web-result array directly under data.
                items = data
            if not isinstance(items, list):
                raise ProviderUnavailableError("Firecrawl returned invalid search data")
            return [
                Result(
                    title=str(item.get("title") or "Untitled"),
                    url=str(item.get("url") or ""),
                    snippet=str(
                        item.get("description")
                        or item.get("snippet")
                        or item.get("markdown")
                        or ""
                    ),
                    raw_content=(
                        str(item["markdown"])
                        if item.get("markdown") is not None
                        else None
                    ),
                )
                for item in items
                if isinstance(item, Mapping)
            ]
        except ProviderError:
            raise
        except Exception as exc:
            raise transport_error("Firecrawl", exc) from exc

    def extract(self, urls: list[str], **kw: Any) -> list[Page]:
        self._require("extract")
        del kw
        pages: list[Page] = []
        try:
            with httpx.Client(timeout=60.0, follow_redirects=False) as client:
                for url in urls:
                    response = client.post(
                        self.scrape_endpoint,
                        json={
                            "url": url,
                            "formats": ["markdown"],
                            "onlyMainContent": True,
                        },
                        headers=self._headers(),
                    )
                    payload = self._payload(response)
                    item = payload.get("data")
                    if not isinstance(item, Mapping):
                        raise ProviderUnavailableError(
                            "Firecrawl returned invalid scrape data"
                        )
                    pages.append(self._page(item, url))
            return pages
        except ProviderError:
            raise
        except Exception as exc:
            raise transport_error("Firecrawl", exc) from exc

    def crawl(self, url: str, **kw: Any) -> list[Page]:
        self._require("crawl")
        body: dict[str, Any] = {
            "url": url,
            "maxDiscoveryDepth": kw.get("max_depth", 1),
            "limit": kw.get("limit", 20),
            "scrapeOptions": {
                "formats": ["markdown"],
                "onlyMainContent": True,
            },
        }
        if kw.get("instructions"):
            body["prompt"] = kw["instructions"]
        if kw.get("select_paths"):
            body["includePaths"] = kw["select_paths"]
        if kw.get("exclude_paths"):
            body["excludePaths"] = kw["exclude_paths"]

        try:
            with httpx.Client(timeout=60.0, follow_redirects=False) as client:
                started = self._payload(
                    client.post(
                        self.crawl_endpoint,
                        json=body,
                        headers=self._headers(),
                    )
                )
                job_id = str(started.get("id") or "").strip()
                if not job_id:
                    raise ProviderUnavailableError(
                        "Firecrawl crawl response did not include a job id"
                    )
                # Quote the provider-issued id into a path under the configured
                # origin. Never follow a provider-returned absolute `next` URL.
                status_endpoint = _firecrawl_endpoint(
                    self.base_url,
                    f"crawl/{quote(job_id, safe='')}",
                )
                deadline = time.monotonic() + _CRAWL_TIMEOUT_SECONDS
                while True:
                    status_payload = self._payload(
                        client.get(status_endpoint, headers=self._headers())
                    )
                    status = str(status_payload.get("status") or "").lower()
                    if status == "completed":
                        items = status_payload.get("data", [])
                        if not isinstance(items, list):
                            raise ProviderUnavailableError(
                                "Firecrawl returned invalid crawl data"
                            )
                        return [
                            self._page(item)
                            for item in items
                            if isinstance(item, Mapping)
                        ]
                    if status == "failed":
                        raise ProviderUnavailableError("Firecrawl crawl job failed")
                    if time.monotonic() >= deadline:
                        raise ProviderUnavailableError("Firecrawl crawl job timed out")
                    time.sleep(_CRAWL_POLL_INTERVAL_SECONDS)
        except ProviderError:
            raise
        except Exception as exc:
            raise transport_error("Firecrawl", exc) from exc

    def map(self, url: str, **kw: Any) -> list[str]:
        self._require("map")
        body: dict[str, Any] = {
            "url": url,
            "limit": kw.get("limit", 50),
        }
        if kw.get("instructions"):
            body["search"] = kw["instructions"]
        try:
            with httpx.Client(timeout=60.0, follow_redirects=False) as client:
                response = client.post(
                    self.map_endpoint,
                    json=body,
                    headers=self._headers(),
                )
            payload = self._payload(response)
            links = payload.get("links", [])
            if not isinstance(links, list):
                raise ProviderUnavailableError("Firecrawl returned invalid map data")
            return [
                str(item.get("url") or "") if isinstance(item, Mapping) else str(item)
                for item in links
            ]
        except ProviderError:
            raise
        except Exception as exc:
            raise transport_error("Firecrawl", exc) from exc
