"""Tavily adapter preserving the existing LangChain client behavior."""

from __future__ import annotations

import re
from typing import Any, Mapping

from agent.tools.research.search.base import Page, Result, SEARCH_OPS
from agent.tools.research.search.errors import (
    ProviderAuthError,
    ProviderError,
    ProviderQuotaError,
    ProviderRateLimitError,
    ProviderRequestError,
    ProviderUnavailableError,
)

_STATUS_RE = re.compile(r"(?:error|http|status(?: code)?)\s*[:=]?\s*(\d{3})", re.I)

# langchain_tavily raises ``ToolException("No <search|extracted|crawl> results
# found for ...")`` for a genuinely empty result set, and its clients ship with
# ``handle_tool_error=True``, so ``invoke`` hands that notice back as a bare
# string instead of a results dict. It is an answer, not a provider failure.
_EMPTY_NOTICE_RE = re.compile(r"^\s*No \w+ results found for\b")


def _status_code(value: Any) -> int | None:
    """Extract an HTTP-like status from vendor responses and exceptions."""

    for candidate in (
        getattr(value, "status_code", None),
        getattr(getattr(value, "response", None), "status_code", None),
        getattr(value, "status", None),
    ):
        try:
            if candidate is not None:
                return int(candidate)
        except (TypeError, ValueError):
            pass

    match = _STATUS_RE.search(str(value))
    return int(match.group(1)) if match else None


def classify_tavily_error(error: Any, *, redact: str | None = None) -> ProviderError:
    """Map a Tavily response/exception to the public provider taxonomy.

    ``error`` is whatever the client handed back in-band: the HTTP layer's
    ``ValueError("Error <status>: <detail>")`` itself on current releases, its
    text on older ones. ``redact`` is the API key, masked should the vendor
    ever echo it into a message that reaches the model or the logs.
    """

    if isinstance(error, ProviderError):
        return error

    message = str(error).strip() or "Tavily request failed"
    if redact:
        message = message.replace(redact, "***")
    status_code = _status_code(error)
    lowered = message.lower()

    if status_code in {401, 403} or any(
        marker in lowered
        for marker in ("invalid api key", "malformed key", "unauthorized")
    ):
        cls = ProviderAuthError
    elif status_code in {402, 432} or any(
        marker in lowered
        for marker in ("usage limit", "quota", "plan exhausted", "credit limit")
    ):
        cls = ProviderQuotaError
    elif status_code == 429 or "rate limit" in lowered or "rate-limit" in lowered:
        cls = ProviderRateLimitError
    elif (status_code is not None and status_code >= 500) or any(
        marker in lowered
        for marker in (
            "connection refused",
            "connection error",
            "timed out",
            "timeout",
            "temporarily unavailable",
        )
    ):
        cls = ProviderUnavailableError
    elif status_code is not None and 400 <= status_code < 500:
        cls = ProviderRequestError
    else:
        # LangChain can wrap transport failures without preserving a response
        # status. Treat an otherwise-unclassified vendor/client exception as
        # unavailable; malformed adapter inputs are raised explicitly below.
        cls = ProviderUnavailableError

    return cls(message, status_code=status_code)


class TavilyAdapter:
    """Typed adapter over ``langchain_tavily``'s four fixed API clients."""

    provider = "tavily"
    supported_ops = SEARCH_OPS

    def __init__(
        self,
        *,
        api_key: str | None,
        base_url: str | None = None,
        ops: frozenset[str] | None = None,
    ) -> None:
        self.api_key = api_key.strip() if isinstance(api_key, str) else None
        # Reserved for catalog shape parity. langchain_tavily does not expose a
        # base URL override, so accepting one must not change today's wire path.
        self.base_url = base_url
        self.ops = ops if ops is not None else self.supported_ops

    def _require(self, op: str) -> str:
        if op not in self.ops or op not in self.supported_ops:
            raise ProviderRequestError(
                f"Tavily adapter does not declare the {op!r} operation"
            )
        if not self.api_key:
            raise ProviderAuthError("Tavily API key is not configured")
        return self.api_key

    def _classify(self, error: Any) -> ProviderError:
        return classify_tavily_error(error, redact=self.api_key)

    def _response(self, response: Any) -> Mapping[str, Any]:
        if isinstance(response, str) and _EMPTY_NOTICE_RE.match(response):
            return {}
        if not isinstance(response, Mapping):
            raise ProviderUnavailableError("Tavily returned an invalid response")
        if response.get("error"):
            # langchain_tavily never raises on an HTTP failure: its ``_run``
            # catches the HTTP layer's error and returns it here, with no
            # ``results`` key. Read it before anything looks at ``results``,
            # or a quota/auth/rate-limit failure becomes "no results".
            raise self._classify(response["error"])
        return response

    def search(self, query: str, max_results: int, **kw: Any) -> list[Result]:
        api_key = self._require("search")
        try:
            from langchain_tavily import TavilySearch

            constructor_kwargs: dict[str, Any] = {
                "api_key": api_key,
                "max_results": max_results,
            }
            if kw.get("include_raw_content"):
                constructor_kwargs["include_raw_content"] = True
            search = TavilySearch(**constructor_kwargs)

            invoke_kwargs: dict[str, Any] = {"query": query}
            for name, default in (
                ("search_depth", "basic"),
                ("topic", "general"),
            ):
                value = kw.get(name, default)
                if value != default:
                    invoke_kwargs[name] = value
            if kw.get("time_range"):
                invoke_kwargs["time_range"] = kw["time_range"]
            if kw.get("include_domains"):
                invoke_kwargs["include_domains"] = kw["include_domains"]
            if kw.get("exclude_domains"):
                invoke_kwargs["exclude_domains"] = kw["exclude_domains"]

            response = self._response(search.invoke(invoke_kwargs))
            return [
                Result(
                    title=str(item.get("title") or "Untitled"),
                    url=str(item.get("url") or ""),
                    snippet=str(item.get("content") or item.get("raw_content") or ""),
                    raw_content=(
                        str(item["raw_content"])
                        if item.get("raw_content") is not None
                        else None
                    ),
                )
                for item in response.get("results", [])
                if isinstance(item, Mapping)
            ]
        except ImportError as exc:
            raise ProviderRequestError(
                "langchain-tavily package is not installed"
            ) from exc
        except ProviderError:
            raise
        except Exception as exc:
            raise self._classify(exc) from exc

    def extract(self, urls: list[str], **kw: Any) -> list[Page]:
        api_key = self._require("extract")
        try:
            from langchain_tavily import TavilyExtract

            extract = TavilyExtract(
                api_key=api_key,
                extract_depth=kw.get("extract_depth", "basic"),
            )
            invoke_kwargs: dict[str, Any] = {"urls": urls}
            if kw.get("query"):
                invoke_kwargs["query"] = kw["query"]
            response = self._response(extract.invoke(invoke_kwargs))

            pages = [
                Page(
                    url=str(item.get("url") or ""),
                    content=str(item.get("raw_content") or ""),
                )
                for item in response.get("results", [])
                if isinstance(item, Mapping)
            ]
            for failed in response.get("failed_results", []):
                if isinstance(failed, Mapping):
                    failed_url = str(failed.get("url") or "")
                    reason = str(failed.get("error") or "failed")
                else:
                    failed_url = str(failed)
                    reason = "failed"
                pages.append(Page(url=failed_url, content="", failed=reason))
            return pages
        except ImportError as exc:
            raise ProviderRequestError(
                "langchain-tavily package is not installed"
            ) from exc
        except ProviderError:
            raise
        except Exception as exc:
            raise self._classify(exc) from exc

    def crawl(self, url: str, **kw: Any) -> list[Page]:
        api_key = self._require("crawl")
        try:
            from langchain_tavily import TavilyCrawl

            crawl = TavilyCrawl(api_key=api_key)
            invoke_kwargs: dict[str, Any] = {
                "url": url,
                "max_depth": kw.get("max_depth", 1),
                "max_breadth": kw.get("max_breadth", 20),
                "limit": kw.get("limit", 20),
            }
            for name in ("instructions", "select_paths", "exclude_paths"):
                if kw.get(name):
                    invoke_kwargs[name] = kw[name]
            response = self._response(crawl.invoke(invoke_kwargs))
            return [
                Page(
                    url=str(item.get("url") or ""),
                    content=str(item.get("raw_content") or ""),
                )
                for item in response.get("results", [])
                if isinstance(item, Mapping)
            ]
        except ImportError as exc:
            raise ProviderRequestError(
                "langchain-tavily package is not installed"
            ) from exc
        except ProviderError:
            raise
        except Exception as exc:
            raise self._classify(exc) from exc

    def map(self, url: str, **kw: Any) -> list[str]:
        api_key = self._require("map")
        try:
            from langchain_tavily import TavilyMap

            mapper = TavilyMap(api_key=api_key)
            invoke_kwargs: dict[str, Any] = {
                "url": url,
                "max_depth": kw.get("max_depth", 2),
                "limit": kw.get("limit", 50),
            }
            for name in ("instructions", "select_paths", "exclude_paths"):
                if kw.get(name):
                    invoke_kwargs[name] = kw[name]
            response = self._response(mapper.invoke(invoke_kwargs))
            urls: list[str] = []
            for item in response.get("results", []):
                if isinstance(item, Mapping):
                    urls.append(str(item.get("url") or item))
                else:
                    urls.append(str(item))
            return urls
        except ImportError as exc:
            raise ProviderRequestError(
                "langchain-tavily package is not installed"
            ) from exc
        except ProviderError:
            raise
        except Exception as exc:
            raise self._classify(exc) from exc
