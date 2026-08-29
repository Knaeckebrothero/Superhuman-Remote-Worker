"""Shared contracts and error classification for web-provider adapters."""

from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

from src.tools.research.search import (
    Page,
    ProviderAuthError,
    ProviderQuotaError,
    ProviderRateLimitError,
    ProviderRequestError,
    ProviderUnavailableError,
    Result,
    TavilyAdapter,
)


@pytest.fixture
def tavily_module():
    module = ModuleType("langchain_tavily")
    module.TavilySearch = MagicMock()
    module.TavilyExtract = MagicMock()
    module.TavilyCrawl = MagicMock()
    module.TavilyMap = MagicMock()
    with patch.dict("sys.modules", {"langchain_tavily": module}):
        yield module


def _client(module, name, response):
    instance = MagicMock()
    instance.invoke.return_value = response
    getattr(module, name).return_value = instance
    return instance


def test_tavily_declared_ops_return_normalized_shapes(tavily_module):
    _client(
        tavily_module,
        "TavilySearch",
        {
            "results": [
                {
                    "title": "One",
                    "url": "https://example.com/one",
                    "content": "Snippet",
                    "raw_content": "Body",
                }
            ]
        },
    )
    _client(
        tavily_module,
        "TavilyExtract",
        {
            "results": [{"url": "https://example.com/one", "raw_content": "Body"}],
            "failed_results": ["https://example.com/bad"],
        },
    )
    _client(
        tavily_module,
        "TavilyCrawl",
        {"results": [{"url": "https://example.com/two", "raw_content": "Page"}]},
    )
    _client(
        tavily_module,
        "TavilyMap",
        {"results": ["https://example.com/one", {"url": "https://example.com/two"}]},
    )
    adapter = TavilyAdapter(api_key="tvly-test")

    assert adapter.ops == frozenset({"search", "extract", "crawl", "map"})
    assert adapter.search("query", 5) == [
        Result(
            title="One",
            url="https://example.com/one",
            snippet="Snippet",
            raw_content="Body",
        )
    ]
    assert adapter.extract(["https://example.com/one"]) == [
        Page(url="https://example.com/one", content="Body"),
        Page(url="https://example.com/bad", content="", failed="failed"),
    ]
    assert adapter.crawl("https://example.com") == [
        Page(url="https://example.com/two", content="Page")
    ]
    assert adapter.map("https://example.com") == [
        "https://example.com/one",
        "https://example.com/two",
    ]


@pytest.mark.parametrize("op", ["extract", "crawl", "map"])
def test_tavily_undeclared_ops_raise(op):
    adapter = TavilyAdapter(api_key="tvly-test", ops=frozenset({"search"}))

    with pytest.raises(ProviderRequestError):
        if op == "extract":
            adapter.extract(["https://example.com"])
        else:
            getattr(adapter, op)("https://example.com")


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        ("Error 401: invalid API key", ProviderAuthError),
        ("Error 403: forbidden", ProviderAuthError),
        ("Error 402: payment required", ProviderQuotaError),
        ("Error 432: usage limit exceeded", ProviderQuotaError),
        ("Error 429: rate limit exceeded", ProviderRateLimitError),
        ("Error 503: unavailable", ProviderUnavailableError),
        ("Error 400: invalid request", ProviderRequestError),
    ],
)
def test_tavily_response_errors_are_classified(tavily_module, error, expected):
    _client(tavily_module, "TavilySearch", {"error": error})
    adapter = TavilyAdapter(api_key="tvly-test")

    with pytest.raises(expected):
        adapter.search("query", 5)


def test_tavily_432_regression_surfaces_quota_error(tavily_module):
    _client(
        tavily_module,
        "TavilySearch",
        {"error": "Error 432: This request exceeds your plan's usage limit"},
    )
    adapter = TavilyAdapter(api_key="tvly-test")

    with pytest.raises(ProviderQuotaError, match="usage limit"):
        adapter.search("query", 5)


def test_tavily_zero_results_is_an_answer(tavily_module):
    _client(tavily_module, "TavilySearch", {"results": []})

    assert TavilyAdapter(api_key="tvly-test").search("query", 5) == []


def test_model_supplied_fetch_url_is_only_a_provider_argument(tavily_module):
    instance = _client(
        tavily_module,
        "TavilyExtract",
        {"results": [], "failed_results": []},
    )
    adapter = TavilyAdapter(
        api_key="tvly-test",
        base_url="https://configured-provider.example/api",
    )

    adapter.extract(["http://169.254.169.254/latest/meta-data"])

    # The adapter constructs only Tavily's typed client. The influenced URL is
    # payload for the off-pod provider and never an in-process request origin.
    tavily_module.TavilyExtract.assert_called_once_with(
        api_key="tvly-test", extract_depth="basic"
    )
    assert instance.invoke.call_args.args[0]["urls"] == [
        "http://169.254.169.254/latest/meta-data"
    ]
