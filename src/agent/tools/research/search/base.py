"""Provider-neutral contracts for web search and fetch adapters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


SEARCH_OPS = frozenset({"search", "extract", "crawl", "map"})


@dataclass(frozen=True, slots=True)
class Result:
    """One normalized ranked web-search result."""

    title: str
    url: str
    snippet: str
    raw_content: str | None = None


@dataclass(frozen=True, slots=True)
class Page:
    """One normalized fetched page or provider-reported page failure."""

    url: str
    content: str
    failed: str | None = None


class SearchAdapter(Protocol):
    """Fixed-destination provider client used by the web research tools."""

    ops: frozenset[str]
    provider: str

    def search(self, query: str, max_results: int, **kw: Any) -> list[Result]: ...

    def extract(self, urls: list[str], **kw: Any) -> list[Page]: ...

    def crawl(self, url: str, **kw: Any) -> list[Page]: ...

    def map(self, url: str, **kw: Any) -> list[str]: ...
