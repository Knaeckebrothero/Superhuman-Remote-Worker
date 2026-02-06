"""Tests for web search tools (Tavily Search, Extract, Crawl, Map)."""

import sys
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

from src.tools.research.web import (
    MAX_RAW_CONTENT_WORDS,
    RESEARCH_TOOLS_METADATA,
    _get_tavily_api_key,
    _parse_comma_list,
    _truncate_content,
    create_web_tools,
)


@pytest.fixture
def mock_langchain_tavily():
    """Create a mock langchain_tavily module and inject into sys.modules."""
    mock_mod = ModuleType("langchain_tavily")
    mock_mod.TavilySearch = MagicMock()
    mock_mod.TavilyExtract = MagicMock()
    mock_mod.TavilyCrawl = MagicMock()
    mock_mod.TavilyMap = MagicMock()
    with patch.dict(sys.modules, {"langchain_tavily": mock_mod}):
        yield mock_mod


# ── Helpers ────────────────────────────────────────────────────────


class TestHelpers:
    """Tests for module-level helper functions."""

    def test_get_api_key_present(self, monkeypatch):
        monkeypatch.setenv("TAVILY_API_KEY", "tvly-test123")
        assert _get_tavily_api_key() == "tvly-test123"

    def test_get_api_key_missing(self, monkeypatch):
        monkeypatch.delenv("TAVILY_API_KEY", raising=False)
        assert _get_tavily_api_key() is None

    def test_parse_comma_list_single(self):
        assert _parse_comma_list("example.com") == ["example.com"]

    def test_parse_comma_list_multiple(self):
        assert _parse_comma_list("a.com, b.com, c.com") == ["a.com", "b.com", "c.com"]

    def test_parse_comma_list_none(self):
        assert _parse_comma_list(None) is None

    def test_parse_comma_list_empty(self):
        assert _parse_comma_list("") is None

    def test_parse_comma_list_whitespace_only(self):
        assert _parse_comma_list("  ,  , ") is None

    def test_truncate_content_within_limit(self):
        text = "word " * 100
        result = _truncate_content(text.strip(), max_words=200)
        assert "truncated" not in result

    def test_truncate_content_over_limit(self):
        text = "word " * 6000
        result = _truncate_content(text.strip(), max_words=5000)
        assert "truncated from 6000 words" in result
        assert len(result.split()) < 5010  # 5000 words + truncation note


# ── Metadata ───────────────────────────────────────────────────────


class TestWebToolsMetadata:
    """Tests for RESEARCH_TOOLS_METADATA entries."""

    def test_metadata_has_all_tools(self):
        expected = {"web_search", "extract_webpage", "crawl_website", "map_website"}
        assert set(RESEARCH_TOOLS_METADATA.keys()) == expected

    def test_metadata_category_is_research(self):
        for name, meta in RESEARCH_TOOLS_METADATA.items():
            assert meta["category"] == "research", f"{name} has wrong category"

    def test_metadata_phases_tactical(self):
        for name, meta in RESEARCH_TOOLS_METADATA.items():
            assert meta["phases"] == ["tactical"], f"{name} has wrong phases"


# ── Tool creation ──────────────────────────────────────────────────


class TestCreateWebTools:
    """Tests for create_web_tools factory."""

    def test_creates_four_tools(self, mock_tool_context):
        tools = create_web_tools(mock_tool_context)
        assert len(tools) == 4

    def test_tool_names(self, mock_tool_context):
        tools = create_web_tools(mock_tool_context)
        names = {t.name for t in tools}
        assert names == {"web_search", "extract_webpage", "crawl_website", "map_website"}


# ── web_search ─────────────────────────────────────────────────────


class TestWebSearch:
    """Tests for the enhanced web_search tool."""

    def _make_search_response(self, n=2, include_raw=False):
        results = []
        for i in range(n):
            r = {
                "url": f"https://example{i}.com",
                "title": f"Result {i}",
                "content": f"Short snippet for result {i}",
            }
            if include_raw:
                r["raw_content"] = f"Full content for result {i} " + ("word " * 100)
            results.append(r)
        return {"results": results}

    def test_basic_search(self, mock_tool_context, mock_langchain_tavily, monkeypatch):
        monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
        mock_instance = MagicMock()
        mock_instance.invoke.return_value = self._make_search_response()
        mock_langchain_tavily.TavilySearch.return_value = mock_instance

        tools = create_web_tools(mock_tool_context)
        ws = next(t for t in tools if t.name == "web_search")
        result = ws.invoke({"query": "test query"})

        assert "Web Search Results for: test query" in result
        assert "example0.com" in result
        assert "example1.com" in result

    def test_missing_api_key(self, mock_tool_context, monkeypatch):
        monkeypatch.delenv("TAVILY_API_KEY", raising=False)
        tools = create_web_tools(mock_tool_context)
        ws = next(t for t in tools if t.name == "web_search")
        result = ws.invoke({"query": "test"})
        assert "TAVILY_API_KEY not configured" in result

    def test_no_results(self, mock_tool_context, mock_langchain_tavily, monkeypatch):
        monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
        mock_instance = MagicMock()
        mock_instance.invoke.return_value = {"results": []}
        mock_langchain_tavily.TavilySearch.return_value = mock_instance

        tools = create_web_tools(mock_tool_context)
        ws = next(t for t in tools if t.name == "web_search")
        result = ws.invoke({"query": "obscure"})
        assert "No web results found" in result

    def test_search_depth_advanced(self, mock_tool_context, mock_langchain_tavily, monkeypatch):
        monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
        mock_instance = MagicMock()
        mock_instance.invoke.return_value = self._make_search_response()
        mock_langchain_tavily.TavilySearch.return_value = mock_instance

        tools = create_web_tools(mock_tool_context)
        ws = next(t for t in tools if t.name == "web_search")
        ws.invoke({"query": "test", "search_depth": "advanced"})

        call_kwargs = mock_instance.invoke.call_args[0][0]
        assert call_kwargs["search_depth"] == "advanced"

    def test_topic_news(self, mock_tool_context, mock_langchain_tavily, monkeypatch):
        monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
        mock_instance = MagicMock()
        mock_instance.invoke.return_value = self._make_search_response()
        mock_langchain_tavily.TavilySearch.return_value = mock_instance

        tools = create_web_tools(mock_tool_context)
        ws = next(t for t in tools if t.name == "web_search")
        ws.invoke({"query": "test", "topic": "news"})

        call_kwargs = mock_instance.invoke.call_args[0][0]
        assert call_kwargs["topic"] == "news"

    def test_time_range(self, mock_tool_context, mock_langchain_tavily, monkeypatch):
        monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
        mock_instance = MagicMock()
        mock_instance.invoke.return_value = self._make_search_response()
        mock_langchain_tavily.TavilySearch.return_value = mock_instance

        tools = create_web_tools(mock_tool_context)
        ws = next(t for t in tools if t.name == "web_search")
        ws.invoke({"query": "test", "time_range": "week"})

        call_kwargs = mock_instance.invoke.call_args[0][0]
        assert call_kwargs["time_range"] == "week"

    def test_include_domains(self, mock_tool_context, mock_langchain_tavily, monkeypatch):
        monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
        mock_instance = MagicMock()
        mock_instance.invoke.return_value = self._make_search_response()
        mock_langchain_tavily.TavilySearch.return_value = mock_instance

        tools = create_web_tools(mock_tool_context)
        ws = next(t for t in tools if t.name == "web_search")
        ws.invoke({"query": "test", "include_domains": "example.com, other.com"})

        call_kwargs = mock_instance.invoke.call_args[0][0]
        assert call_kwargs["include_domains"] == ["example.com", "other.com"]

    def test_exclude_domains(self, mock_tool_context, mock_langchain_tavily, monkeypatch):
        monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
        mock_instance = MagicMock()
        mock_instance.invoke.return_value = self._make_search_response()
        mock_langchain_tavily.TavilySearch.return_value = mock_instance

        tools = create_web_tools(mock_tool_context)
        ws = next(t for t in tools if t.name == "web_search")
        ws.invoke({"query": "test", "exclude_domains": "spam.com"})

        call_kwargs = mock_instance.invoke.call_args[0][0]
        assert call_kwargs["exclude_domains"] == ["spam.com"]

    def test_raw_content_creates_new_instance(self, mock_tool_context, mock_langchain_tavily, monkeypatch):
        monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
        mock_instance = MagicMock()
        mock_instance.invoke.return_value = self._make_search_response(include_raw=True)
        mock_langchain_tavily.TavilySearch.return_value = mock_instance

        tools = create_web_tools(mock_tool_context)
        ws = next(t for t in tools if t.name == "web_search")
        ws.invoke({"query": "test", "include_raw_content": True})

        constructor_kwargs = mock_langchain_tavily.TavilySearch.call_args[1]
        assert constructor_kwargs.get("include_raw_content") is True

    def test_raw_content_not_truncated_300(self, mock_tool_context, mock_langchain_tavily, monkeypatch):
        monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
        long_content = "A" * 500
        mock_instance = MagicMock()
        mock_instance.invoke.return_value = {
            "results": [{"url": "https://ex.com", "title": "T", "content": "short", "raw_content": long_content}]
        }
        mock_langchain_tavily.TavilySearch.return_value = mock_instance

        tools = create_web_tools(mock_tool_context)
        ws = next(t for t in tools if t.name == "web_search")
        result = ws.invoke({"query": "test", "include_raw_content": True})

        assert long_content in result

    def test_raw_content_word_limit(self, mock_tool_context, mock_langchain_tavily, monkeypatch):
        monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
        huge_content = "word " * 6000
        mock_instance = MagicMock()
        mock_instance.invoke.return_value = {
            "results": [{"url": "https://ex.com", "title": "T", "content": "short", "raw_content": huge_content}]
        }
        mock_langchain_tavily.TavilySearch.return_value = mock_instance

        tools = create_web_tools(mock_tool_context)
        ws = next(t for t in tools if t.name == "web_search")
        result = ws.invoke({"query": "test", "include_raw_content": True})

        assert "truncated from 6000 words" in result

    def test_citation_registration(self, mock_tool_context, mock_langchain_tavily, monkeypatch):
        monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
        mock_instance = MagicMock()
        mock_instance.invoke.return_value = self._make_search_response()
        mock_langchain_tavily.TavilySearch.return_value = mock_instance
        mock_tool_context.get_or_register_web_source.return_value = ("src-1", None)

        tools = create_web_tools(mock_tool_context)
        ws = next(t for t in tools if t.name == "web_search")
        result = ws.invoke({"query": "test"})

        assert mock_tool_context.get_or_register_web_source.called
        assert "archived" in result

    def test_inaccessible_sources_warning(self, mock_tool_context, mock_langchain_tavily, monkeypatch):
        monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
        mock_instance = MagicMock()
        mock_instance.invoke.return_value = self._make_search_response()
        mock_langchain_tavily.TavilySearch.return_value = mock_instance
        mock_tool_context.get_or_register_web_source.return_value = ("src-1", "HTTP 403")

        tools = create_web_tools(mock_tool_context)
        ws = next(t for t in tools if t.name == "web_search")
        result = ws.invoke({"query": "test"})

        assert "WARNING" in result
        assert "INACCESSIBLE" in result


# ── extract_webpage ────────────────────────────────────────────────


class TestExtractWebpage:
    """Tests for the extract_webpage tool."""

    def _setup_extract(self, mock_langchain_tavily, response=None):
        mock_instance = MagicMock()
        if response is None:
            response = {
                "results": [
                    {"url": "https://example.com/page1", "raw_content": "Full page content here"},
                ],
                "failed_results": [],
            }
        mock_instance.invoke.return_value = response
        mock_langchain_tavily.TavilyExtract.return_value = mock_instance
        return mock_instance

    def test_single_url(self, mock_tool_context, mock_langchain_tavily, monkeypatch):
        monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
        self._setup_extract(mock_langchain_tavily)
        mock_tool_context.get_or_register_web_source.return_value = ("src-1", None)

        tools = create_web_tools(mock_tool_context)
        t = next(t for t in tools if t.name == "extract_webpage")
        result = t.invoke({"urls": "https://example.com/page1"})

        assert "Extracted Content from 1 URL(s)" in result
        assert "Full page content here" in result

    def test_multiple_urls(self, mock_tool_context, mock_langchain_tavily, monkeypatch):
        monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
        mock_instance = self._setup_extract(mock_langchain_tavily, {
            "results": [
                {"url": "https://a.com", "raw_content": "Content A"},
                {"url": "https://b.com", "raw_content": "Content B"},
            ],
            "failed_results": [],
        })

        tools = create_web_tools(mock_tool_context)
        t = next(t for t in tools if t.name == "extract_webpage")
        result = t.invoke({"urls": "https://a.com, https://b.com"})

        call_kwargs = mock_instance.invoke.call_args[0][0]
        assert call_kwargs["urls"] == ["https://a.com", "https://b.com"]
        assert "Content A" in result
        assert "Content B" in result

    def test_with_query(self, mock_tool_context, mock_langchain_tavily, monkeypatch):
        monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
        mock_instance = self._setup_extract(mock_langchain_tavily)

        tools = create_web_tools(mock_tool_context)
        t = next(t for t in tools if t.name == "extract_webpage")
        t.invoke({"urls": "https://example.com", "query": "important info"})

        call_kwargs = mock_instance.invoke.call_args[0][0]
        assert call_kwargs["query"] == "important info"

    def test_advanced_depth(self, mock_tool_context, mock_langchain_tavily, monkeypatch):
        monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
        self._setup_extract(mock_langchain_tavily)

        tools = create_web_tools(mock_tool_context)
        t = next(t for t in tools if t.name == "extract_webpage")
        t.invoke({"urls": "https://example.com", "extract_depth": "advanced"})

        assert mock_langchain_tavily.TavilyExtract.call_args[1]["extract_depth"] == "advanced"

    def test_citation_registration(self, mock_tool_context, mock_langchain_tavily, monkeypatch):
        monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
        self._setup_extract(mock_langchain_tavily)
        mock_tool_context.get_or_register_web_source.return_value = ("src-1", None)

        tools = create_web_tools(mock_tool_context)
        t = next(t for t in tools if t.name == "extract_webpage")
        result = t.invoke({"urls": "https://example.com/page1"})

        mock_tool_context.get_or_register_web_source.assert_called()
        assert "archived" in result

    def test_missing_api_key(self, mock_tool_context, monkeypatch):
        monkeypatch.delenv("TAVILY_API_KEY", raising=False)
        tools = create_web_tools(mock_tool_context)
        t = next(t for t in tools if t.name == "extract_webpage")
        result = t.invoke({"urls": "https://example.com"})
        assert "TAVILY_API_KEY not configured" in result

    def test_content_word_limit(self, mock_tool_context, mock_langchain_tavily, monkeypatch):
        monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
        huge = "word " * 6000
        self._setup_extract(mock_langchain_tavily, {
            "results": [{"url": "https://ex.com", "raw_content": huge}],
            "failed_results": [],
        })

        tools = create_web_tools(mock_tool_context)
        t = next(t for t in tools if t.name == "extract_webpage")
        result = t.invoke({"urls": "https://ex.com"})

        assert "truncated from 6000 words" in result

    def test_failed_urls_reported(self, mock_tool_context, mock_langchain_tavily, monkeypatch):
        monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
        self._setup_extract(mock_langchain_tavily, {
            "results": [{"url": "https://a.com", "raw_content": "OK"}],
            "failed_results": ["https://bad.com"],
        })

        tools = create_web_tools(mock_tool_context)
        t = next(t for t in tools if t.name == "extract_webpage")
        result = t.invoke({"urls": "https://a.com, https://bad.com"})

        assert "1 failed" in result
        assert "https://bad.com" in result

    def test_too_many_urls(self, mock_tool_context, monkeypatch):
        monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
        tools = create_web_tools(mock_tool_context)
        t = next(t for t in tools if t.name == "extract_webpage")
        urls = ", ".join(f"https://url{i}.com" for i in range(21))
        result = t.invoke({"urls": urls})
        assert "Maximum 20 URLs" in result

    def test_empty_urls(self, mock_tool_context, monkeypatch):
        monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
        tools = create_web_tools(mock_tool_context)
        t = next(t for t in tools if t.name == "extract_webpage")
        result = t.invoke({"urls": ""})
        assert "No URLs provided" in result


# ── crawl_website ──────────────────────────────────────────────────


class TestCrawlWebsite:
    """Tests for the crawl_website tool."""

    def _setup_crawl(self, mock_langchain_tavily, response=None):
        mock_instance = MagicMock()
        if response is None:
            response = {
                "results": [
                    {"url": "https://docs.example.com/", "raw_content": "Homepage content"},
                    {"url": "https://docs.example.com/page2", "raw_content": "Page 2 content"},
                ],
            }
        mock_instance.invoke.return_value = response
        mock_langchain_tavily.TavilyCrawl.return_value = mock_instance
        return mock_instance

    def test_basic_crawl(self, mock_tool_context, mock_langchain_tavily, monkeypatch):
        monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
        self._setup_crawl(mock_langchain_tavily)
        mock_tool_context.get_or_register_web_source.return_value = ("src-1", None)

        tools = create_web_tools(mock_tool_context)
        t = next(t for t in tools if t.name == "crawl_website")
        result = t.invoke({"url": "https://docs.example.com/"})

        assert "Website Crawl Results" in result
        assert "Pages crawled: 2" in result
        assert "Homepage content" in result

    def test_with_instructions(self, mock_tool_context, mock_langchain_tavily, monkeypatch):
        monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
        mock_instance = self._setup_crawl(mock_langchain_tavily)

        tools = create_web_tools(mock_tool_context)
        t = next(t for t in tools if t.name == "crawl_website")
        t.invoke({"url": "https://docs.example.com/", "instructions": "find API docs"})

        call_kwargs = mock_instance.invoke.call_args[0][0]
        assert call_kwargs["instructions"] == "find API docs"

    def test_path_filters(self, mock_tool_context, mock_langchain_tavily, monkeypatch):
        monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
        mock_instance = self._setup_crawl(mock_langchain_tavily)

        tools = create_web_tools(mock_tool_context)
        t = next(t for t in tools if t.name == "crawl_website")
        t.invoke({
            "url": "https://docs.example.com/",
            "select_paths": "/api/.*, /docs/.*",
            "exclude_paths": "/blog/.*",
        })

        call_kwargs = mock_instance.invoke.call_args[0][0]
        assert call_kwargs["select_paths"] == ["/api/.*", "/docs/.*"]
        assert call_kwargs["exclude_paths"] == ["/blog/.*"]

    def test_depth_clamping_high(self, mock_tool_context, mock_langchain_tavily, monkeypatch):
        monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
        mock_instance = self._setup_crawl(mock_langchain_tavily)

        tools = create_web_tools(mock_tool_context)
        t = next(t for t in tools if t.name == "crawl_website")
        t.invoke({"url": "https://example.com", "max_depth": 10})

        call_kwargs = mock_instance.invoke.call_args[0][0]
        assert call_kwargs["max_depth"] == 5

    def test_depth_clamping_low(self, mock_tool_context, mock_langchain_tavily, monkeypatch):
        monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
        mock_instance = self._setup_crawl(mock_langchain_tavily)

        tools = create_web_tools(mock_tool_context)
        t = next(t for t in tools if t.name == "crawl_website")
        t.invoke({"url": "https://example.com", "max_depth": -1})

        call_kwargs = mock_instance.invoke.call_args[0][0]
        assert call_kwargs["max_depth"] == 1

    def test_citation_registration(self, mock_tool_context, mock_langchain_tavily, monkeypatch):
        monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
        self._setup_crawl(mock_langchain_tavily)
        mock_tool_context.get_or_register_web_source.return_value = ("src-1", None)

        tools = create_web_tools(mock_tool_context)
        t = next(t for t in tools if t.name == "crawl_website")
        result = t.invoke({"url": "https://docs.example.com/"})

        assert mock_tool_context.get_or_register_web_source.call_count == 2
        assert "archived" in result

    def test_missing_api_key(self, mock_tool_context, monkeypatch):
        monkeypatch.delenv("TAVILY_API_KEY", raising=False)
        tools = create_web_tools(mock_tool_context)
        t = next(t for t in tools if t.name == "crawl_website")
        result = t.invoke({"url": "https://example.com"})
        assert "TAVILY_API_KEY not configured" in result

    def test_content_word_limit(self, mock_tool_context, mock_langchain_tavily, monkeypatch):
        monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
        huge = "word " * 6000
        self._setup_crawl(mock_langchain_tavily, {
            "results": [{"url": "https://ex.com", "raw_content": huge}],
        })

        tools = create_web_tools(mock_tool_context)
        t = next(t for t in tools if t.name == "crawl_website")
        result = t.invoke({"url": "https://ex.com"})

        assert "truncated from 6000 words" in result

    def test_no_results(self, mock_tool_context, mock_langchain_tavily, monkeypatch):
        monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
        self._setup_crawl(mock_langchain_tavily, {"results": []})

        tools = create_web_tools(mock_tool_context)
        t = next(t for t in tools if t.name == "crawl_website")
        result = t.invoke({"url": "https://example.com"})

        assert "No pages could be crawled" in result


# ── map_website ────────────────────────────────────────────────────


class TestMapWebsite:
    """Tests for the map_website tool."""

    def _setup_map(self, mock_langchain_tavily, response=None):
        mock_instance = MagicMock()
        if response is None:
            response = {
                "results": [
                    "https://example.com/",
                    "https://example.com/about",
                    "https://example.com/docs",
                ],
            }
        mock_instance.invoke.return_value = response
        mock_langchain_tavily.TavilyMap.return_value = mock_instance
        return mock_instance

    def test_basic_map(self, mock_tool_context, mock_langchain_tavily, monkeypatch):
        monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
        self._setup_map(mock_langchain_tavily)

        tools = create_web_tools(mock_tool_context)
        t = next(t for t in tools if t.name == "map_website")
        result = t.invoke({"url": "https://example.com"})

        assert "Website Map for: https://example.com" in result
        assert "URLs discovered: 3" in result
        assert "https://example.com/about" in result

    def test_with_instructions(self, mock_tool_context, mock_langchain_tavily, monkeypatch):
        monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
        mock_instance = self._setup_map(mock_langchain_tavily)

        tools = create_web_tools(mock_tool_context)
        t = next(t for t in tools if t.name == "map_website")
        t.invoke({"url": "https://example.com", "instructions": "find docs"})

        call_kwargs = mock_instance.invoke.call_args[0][0]
        assert call_kwargs["instructions"] == "find docs"

    def test_path_filters(self, mock_tool_context, mock_langchain_tavily, monkeypatch):
        monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
        mock_instance = self._setup_map(mock_langchain_tavily)

        tools = create_web_tools(mock_tool_context)
        t = next(t for t in tools if t.name == "map_website")
        t.invoke({
            "url": "https://example.com",
            "select_paths": "/docs/.*",
            "exclude_paths": "/blog/.*",
        })

        call_kwargs = mock_instance.invoke.call_args[0][0]
        assert call_kwargs["select_paths"] == ["/docs/.*"]
        assert call_kwargs["exclude_paths"] == ["/blog/.*"]

    def test_no_citation_registration(self, mock_tool_context, mock_langchain_tavily, monkeypatch):
        monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
        self._setup_map(mock_langchain_tavily)

        tools = create_web_tools(mock_tool_context)
        t = next(t for t in tools if t.name == "map_website")
        t.invoke({"url": "https://example.com"})

        mock_tool_context.get_or_register_web_source.assert_not_called()

    def test_missing_api_key(self, mock_tool_context, monkeypatch):
        monkeypatch.delenv("TAVILY_API_KEY", raising=False)
        tools = create_web_tools(mock_tool_context)
        t = next(t for t in tools if t.name == "map_website")
        result = t.invoke({"url": "https://example.com"})
        assert "TAVILY_API_KEY not configured" in result

    def test_guidance_message(self, mock_tool_context, mock_langchain_tavily, monkeypatch):
        monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
        self._setup_map(mock_langchain_tavily)

        tools = create_web_tools(mock_tool_context)
        t = next(t for t in tools if t.name == "map_website")
        result = t.invoke({"url": "https://example.com"})

        assert "extract_webpage" in result
        assert "crawl_website" in result

    def test_dict_results(self, mock_tool_context, mock_langchain_tavily, monkeypatch):
        """Test handling when results are dicts instead of strings."""
        monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
        self._setup_map(mock_langchain_tavily, {
            "results": [
                {"url": "https://example.com/page1"},
                {"url": "https://example.com/page2"},
            ],
        })

        tools = create_web_tools(mock_tool_context)
        t = next(t for t in tools if t.name == "map_website")
        result = t.invoke({"url": "https://example.com"})

        assert "https://example.com/page1" in result
        assert "https://example.com/page2" in result

    def test_no_results(self, mock_tool_context, mock_langchain_tavily, monkeypatch):
        monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
        self._setup_map(mock_langchain_tavily, {"results": []})

        tools = create_web_tools(mock_tool_context)
        t = next(t for t in tools if t.name == "map_website")
        result = t.invoke({"url": "https://example.com"})

        assert "No URLs discovered" in result

    def test_depth_clamping(self, mock_tool_context, mock_langchain_tavily, monkeypatch):
        monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
        mock_instance = self._setup_map(mock_langchain_tavily)

        tools = create_web_tools(mock_tool_context)
        t = next(t for t in tools if t.name == "map_website")
        t.invoke({"url": "https://example.com", "max_depth": 99})

        call_kwargs = mock_instance.invoke.call_args[0][0]
        assert call_kwargs["max_depth"] == 5
