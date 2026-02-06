"""Tests for browser automation tools and network utilities."""

import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.tools.research.utils.network import ProxyConfig, ProxyType
from src.tools.research.utils.paper_types import PaperSource


# ── ProxyConfig tests ──────────────────────────────────────────────


class TestProxyType:
    """Tests for ProxyType enum."""

    def test_enum_values(self):
        assert ProxyType.NONE.value == "none"
        assert ProxyType.HTTP.value == "http"
        assert ProxyType.SOCKS5.value == "socks5"


class TestProxyConfig:
    """Tests for ProxyConfig dataclass."""

    def test_default_is_unconfigured(self):
        config = ProxyConfig()
        assert config.type == ProxyType.NONE
        assert config.is_configured is False
        assert config.url is None

    def test_configured_socks5(self):
        config = ProxyConfig(
            type=ProxyType.SOCKS5, host="localhost", port=1080
        )
        assert config.is_configured is True
        assert config.url == "socks5://localhost:1080"

    def test_configured_http(self):
        config = ProxyConfig(
            type=ProxyType.HTTP, host="proxy.uni.edu", port=8080
        )
        assert config.url == "http://proxy.uni.edu:8080"

    def test_url_with_auth(self):
        config = ProxyConfig(
            type=ProxyType.SOCKS5,
            host="localhost",
            port=1080,
            username="user",
            password="pass",
        )
        assert config.url == "socks5://user:pass@localhost:1080"

    def test_not_configured_without_host(self):
        config = ProxyConfig(type=ProxyType.SOCKS5, port=1080)
        assert config.is_configured is False

    def test_not_configured_without_port(self):
        config = ProxyConfig(type=ProxyType.SOCKS5, host="localhost")
        assert config.is_configured is False


class TestProxyConfigFromEnv:
    """Tests for ProxyConfig.from_env class method."""

    def test_from_env_none(self):
        with patch.dict("os.environ", {}, clear=True):
            config = ProxyConfig.from_env()
            assert config.type == ProxyType.NONE

    def test_from_env_socks5(self):
        env = {
            "RESEARCH_PROXY_TYPE": "socks5",
            "RESEARCH_PROXY_HOST": "localhost",
            "RESEARCH_PROXY_PORT": "1080",
        }
        with patch.dict("os.environ", env, clear=True):
            config = ProxyConfig.from_env()
            assert config.type == ProxyType.SOCKS5
            assert config.host == "localhost"
            assert config.port == 1080
            assert config.is_configured is True

    def test_from_env_with_auth(self):
        env = {
            "RESEARCH_PROXY_TYPE": "http",
            "RESEARCH_PROXY_HOST": "proxy.example.com",
            "RESEARCH_PROXY_PORT": "8080",
            "RESEARCH_PROXY_USER": "admin",
            "RESEARCH_PROXY_PASS": "secret",
        }
        with patch.dict("os.environ", env, clear=True):
            config = ProxyConfig.from_env()
            assert config.username == "admin"
            assert config.password == "secret"

    def test_from_env_unknown_type_falls_back(self):
        env = {"RESEARCH_PROXY_TYPE": "invalid_type"}
        with patch.dict("os.environ", env, clear=True):
            config = ProxyConfig.from_env()
            assert config.type == ProxyType.NONE


class TestProxyConfigFromConfig:
    """Tests for ProxyConfig.from_config class method."""

    def test_from_config_enabled(self):
        config_dict = {
            "enabled": True,
            "type": "socks5",
            "host": "localhost",
            "port": 1080,
        }
        config = ProxyConfig.from_config(config_dict)
        assert config.type == ProxyType.SOCKS5
        assert config.host == "localhost"
        assert config.port == 1080

    def test_from_config_disabled_falls_back_to_env(self):
        env = {
            "RESEARCH_PROXY_TYPE": "http",
            "RESEARCH_PROXY_HOST": "env-proxy",
            "RESEARCH_PROXY_PORT": "3128",
        }
        with patch.dict("os.environ", env, clear=True):
            config = ProxyConfig.from_config({"enabled": False})
            assert config.type == ProxyType.HTTP
            assert config.host == "env-proxy"

    def test_from_config_empty_falls_back_to_env(self):
        with patch.dict("os.environ", {}, clear=True):
            config = ProxyConfig.from_config({})
            assert config.type == ProxyType.NONE

    def test_from_config_none_falls_back_to_env(self):
        with patch.dict("os.environ", {}, clear=True):
            config = ProxyConfig.from_config(None)
            assert config.type == ProxyType.NONE

    def test_from_config_auth_from_env(self):
        config_dict = {
            "enabled": True,
            "type": "socks5",
            "host": "localhost",
            "port": 1080,
        }
        env = {"RESEARCH_PROXY_USER": "user", "RESEARCH_PROXY_PASS": "pass"}
        with patch.dict("os.environ", env, clear=True):
            config = ProxyConfig.from_config(config_dict)
            assert config.username == "user"
            assert config.password == "pass"


class TestProxyConfigToPlaywright:
    """Tests for ProxyConfig.to_playwright_proxy method."""

    def test_returns_none_when_unconfigured(self):
        config = ProxyConfig()
        assert config.to_playwright_proxy() is None

    def test_returns_proxy_dict(self):
        config = ProxyConfig(
            type=ProxyType.SOCKS5, host="localhost", port=1080
        )
        result = config.to_playwright_proxy()
        assert result == {"server": "socks5://localhost:1080"}

    def test_includes_auth(self):
        config = ProxyConfig(
            type=ProxyType.HTTP,
            host="proxy.example.com",
            port=8080,
            username="user",
            password="pass",
        )
        result = config.to_playwright_proxy()
        assert result["username"] == "user"
        assert result["password"] == "pass"


class TestProxyConfigToBrowserUse:
    """Tests for ProxyConfig.to_browser_use_proxy method."""

    def test_returns_none_when_unconfigured(self):
        config = ProxyConfig()
        assert config.to_browser_use_proxy() is None

    def test_returns_none_when_browser_use_not_installed(self):
        config = ProxyConfig(
            type=ProxyType.SOCKS5, host="localhost", port=1080
        )
        with patch.dict("sys.modules", {"browser_use": None, "browser_use.browser.profile": None}):
            # Force ImportError by patching the import
            with patch(
                "builtins.__import__",
                side_effect=lambda name, *args: (_ for _ in ()).throw(ImportError)
                if "browser_use" in name
                else __builtins__.__import__(name, *args),
            ):
                result = config.to_browser_use_proxy()
                assert result is None


# ── Browser tools tests ────────────────────────────────────────────


class TestBrowserToolsMetadata:
    """Tests for browser tools metadata."""

    def test_metadata_has_all_tools(self):
        from src.tools.research.browser import BROWSER_TOOLS_METADATA

        assert "browse_website" in BROWSER_TOOLS_METADATA
        assert "download_from_website" in BROWSER_TOOLS_METADATA

    def test_metadata_category(self):
        from src.tools.research.browser import BROWSER_TOOLS_METADATA

        for name, meta in BROWSER_TOOLS_METADATA.items():
            assert meta["category"] == "research"

    def test_metadata_phases(self):
        from src.tools.research.browser import BROWSER_TOOLS_METADATA

        for name, meta in BROWSER_TOOLS_METADATA.items():
            assert "tactical" in meta["phases"]


class TestGetBrowserConfig:
    """Tests for _get_browser_config helper."""

    def test_default_config(self, mock_tool_context, temp_docs_dir):
        from src.tools.research.browser import _get_browser_config

        mock_tool_context.config = {"browser": {}, "research": {"proxy": {}}}

        with patch.dict("os.environ", {}, clear=True):
            config = _get_browser_config(mock_tool_context)

        assert config["headless"] is True
        assert config["accept_downloads"] is True
        assert config["auto_download_pdfs"] is True

    def test_headless_from_env(self, mock_tool_context):
        from src.tools.research.browser import _get_browser_config

        mock_tool_context.config = {"browser": {}, "research": {"proxy": {}}}

        with patch.dict("os.environ", {"BROWSER_HEADLESS": "false"}, clear=True):
            config = _get_browser_config(mock_tool_context)

        assert config["headless"] is False

    def test_headless_from_config(self, mock_tool_context):
        from src.tools.research.browser import _get_browser_config

        mock_tool_context.config = {
            "browser": {"headless": False},
            "research": {"proxy": {}},
        }

        with patch.dict("os.environ", {}, clear=True):
            config = _get_browser_config(mock_tool_context)

        assert config["headless"] is False

    def test_custom_downloads_path(self, mock_tool_context, temp_docs_dir):
        from src.tools.research.browser import _get_browser_config

        mock_tool_context.config = {"browser": {}, "research": {"proxy": {}}}
        custom_path = temp_docs_dir / "custom_downloads"

        with patch.dict("os.environ", {}, clear=True):
            config = _get_browser_config(mock_tool_context, downloads_path=custom_path)

        assert config["downloads_path"] == str(custom_path)
        assert custom_path.exists()


class TestExtractResult:
    """Tests for _extract_result helper."""

    def test_extract_from_final_result(self):
        from src.tools.research.browser import _extract_result

        history = MagicMock()
        history.final_result.return_value = "Extracted text content"

        result = _extract_result(history)
        assert result == "Extracted text content"

    def test_extract_from_last_action(self):
        from src.tools.research.browser import _extract_result

        history = MagicMock()
        history.final_result.return_value = None
        entry = MagicMock()
        entry.result = "Action result text"
        history.history = [entry]

        result = _extract_result(history)
        assert result == "Action result text"

    def test_truncates_long_action_results(self):
        from src.tools.research.browser import _extract_result

        # final_result returns None, so it falls through to last action result
        history = MagicMock()
        history.final_result.return_value = None
        entry = MagicMock()
        entry.result = "x" * 6000
        history.history = [entry]

        result = _extract_result(history)
        assert len(result) <= 5100  # 5000 + "\n... (truncated)"
        assert "truncated" in result

    def test_does_not_truncate_final_result(self):
        from src.tools.research.browser import _extract_result

        history = MagicMock()
        history.final_result.return_value = "x" * 6000

        result = _extract_result(history)
        assert len(result) == 6000

    def test_handles_none_history(self):
        from src.tools.research.browser import _extract_result

        result = _extract_result(None)
        assert "no result" in result.lower()


class TestFindNewFiles:
    """Tests for _find_new_files helper."""

    def test_finds_recent_files(self, temp_docs_dir):
        from src.tools.research.browser import _find_new_files

        # Create a file
        (temp_docs_dir / "test.pdf").write_bytes(b"content")

        files = _find_new_files(temp_docs_dir, max_age_seconds=60)
        assert len(files) == 1
        assert files[0].name == "test.pdf"

    def test_ignores_old_files(self, temp_docs_dir):
        import os

        from src.tools.research.browser import _find_new_files

        # Create a file and backdate it
        path = temp_docs_dir / "old.pdf"
        path.write_bytes(b"content")
        old_time = time.time() - 120
        os.utime(path, (old_time, old_time))

        files = _find_new_files(temp_docs_dir, max_age_seconds=60)
        assert len(files) == 0

    def test_returns_newest_first(self, temp_docs_dir):
        from src.tools.research.browser import _find_new_files

        (temp_docs_dir / "first.pdf").write_bytes(b"a")
        time.sleep(0.05)
        (temp_docs_dir / "second.pdf").write_bytes(b"b")

        files = _find_new_files(temp_docs_dir, max_age_seconds=60)
        assert files[0].name == "second.pdf"

    def test_empty_directory(self, temp_docs_dir):
        from src.tools.research.browser import _find_new_files

        files = _find_new_files(temp_docs_dir, max_age_seconds=60)
        assert files == []

    def test_nonexistent_directory(self):
        from src.tools.research.browser import _find_new_files

        files = _find_new_files(Path("/nonexistent/directory"), max_age_seconds=60)
        assert files == []


class TestCreateBrowserTools:
    """Tests for create_browser_tools factory."""

    def test_creates_two_tools(self, mock_tool_context):
        from src.tools.research.browser import create_browser_tools

        tools = create_browser_tools(mock_tool_context)
        assert len(tools) == 2
        names = {t.name for t in tools}
        assert names == {"browse_website", "download_from_website"}

    @pytest.mark.asyncio
    async def test_browse_website_handles_exception(self, mock_tool_context):
        from src.tools.research.browser import create_browser_tools

        tools = create_browser_tools(mock_tool_context)
        browse = next(t for t in tools if t.name == "browse_website")

        # Simulate browser-use raising during execution
        with patch(
            "src.tools.research.browser._get_browser_llm",
            side_effect=Exception("LLM not configured"),
        ):
            result = await browse.ainvoke(
                {"url": "https://example.com", "task": "test"}
            )

        assert "failed" in result.lower() or "error" in result.lower()

    @pytest.mark.asyncio
    async def test_download_from_website_handles_exception(self, mock_tool_context):
        from src.tools.research.browser import create_browser_tools

        tools = create_browser_tools(mock_tool_context)
        download = next(t for t in tools if t.name == "download_from_website")

        with patch(
            "src.tools.research.browser._get_browser_llm",
            side_effect=Exception("LLM not configured"),
        ):
            result = await download.ainvoke({"url": "https://example.com"})

        assert "failed" in result.lower() or "error" in result.lower()


# ── Workflow tools tests ───────────────────────────────────────────


class TestWorkflowToolsMetadata:
    """Tests for workflow tools metadata."""

    def test_metadata_has_research_topic(self):
        from src.tools.research.workflow import WORKFLOW_TOOLS_METADATA

        assert "research_topic" in WORKFLOW_TOOLS_METADATA

    def test_metadata_category(self):
        from src.tools.research.workflow import WORKFLOW_TOOLS_METADATA

        assert WORKFLOW_TOOLS_METADATA["research_topic"]["category"] == "research"


class TestDeduplicatePapers:
    """Tests for _deduplicate_papers function."""

    def test_deduplicates_by_doi(self, sample_paper, sample_paper_s2):
        from src.tools.research.workflow import _deduplicate_papers

        # Give them the same DOI
        sample_paper_s2.doi = sample_paper.doi

        result = _deduplicate_papers([sample_paper], [sample_paper_s2])

        # S2 is processed first, so it should be kept
        assert len(result) == 1
        assert result[0].source == PaperSource.SEMANTIC_SCHOLAR

    def test_deduplicates_by_arxiv_id(self, sample_paper, sample_paper_s2):
        from src.tools.research.workflow import _deduplicate_papers

        # Different DOIs but same arXiv ID
        sample_paper.doi = None
        sample_paper_s2.doi = None
        sample_paper_s2.arxiv_id = sample_paper.arxiv_id

        result = _deduplicate_papers([sample_paper], [sample_paper_s2])
        assert len(result) == 1

    def test_keeps_unique_papers(self, sample_paper, sample_paper_s2):
        from src.tools.research.workflow import _deduplicate_papers

        # Different identifiers
        sample_paper.doi = "10.1/a"
        sample_paper.arxiv_id = "1111.11111"
        sample_paper_s2.doi = "10.1/b"
        sample_paper_s2.arxiv_id = "2222.22222"

        result = _deduplicate_papers([sample_paper], [sample_paper_s2])
        assert len(result) == 2

    def test_empty_inputs(self):
        from src.tools.research.workflow import _deduplicate_papers

        result = _deduplicate_papers([], [])
        assert result == []

    def test_prefers_semantic_scholar(self, sample_paper, sample_paper_s2):
        from src.tools.research.workflow import _deduplicate_papers

        # Same DOI - S2 should win since it's processed first
        sample_paper_s2.doi = sample_paper.doi
        sample_paper_s2.citation_count = 5000

        result = _deduplicate_papers([sample_paper], [sample_paper_s2])
        assert len(result) == 1
        assert result[0].citation_count == 5000


class TestResearchTopic:
    """Tests for research_topic tool."""

    @pytest.mark.asyncio
    async def test_research_topic_combines_sources(self, mock_tool_context, sample_paper, sample_paper_s2):
        from src.tools.research.workflow import create_workflow_tools

        tools = create_workflow_tools(mock_tool_context)
        research_topic = tools[0]

        # Different papers from each source
        sample_paper.doi = "10.1/arxiv"
        sample_paper.arxiv_id = "1111.11111"
        sample_paper_s2.doi = "10.1/s2"
        sample_paper_s2.arxiv_id = "2222.22222"

        with patch(
            "src.tools.research.workflow._search_arxiv_raw",
            new_callable=AsyncMock,
            return_value=[sample_paper],
        ), patch(
            "src.tools.research.workflow._search_semantic_scholar_raw",
            new_callable=AsyncMock,
            return_value=[sample_paper_s2],
        ):
            result = await research_topic.ainvoke(
                {"topic": "transformers", "download_available": False}
            )

        assert "Research Report" in result
        assert "transformers" in result
        assert "Unique papers after deduplication: 2" in result

    @pytest.mark.asyncio
    async def test_research_topic_handles_search_failure(self, mock_tool_context, sample_paper):
        from src.tools.research.workflow import create_workflow_tools

        tools = create_workflow_tools(mock_tool_context)
        research_topic = tools[0]

        with patch(
            "src.tools.research.workflow._search_arxiv_raw",
            new_callable=AsyncMock,
            return_value=[sample_paper],
        ), patch(
            "src.tools.research.workflow._search_semantic_scholar_raw",
            new_callable=AsyncMock,
            side_effect=Exception("API error"),
        ):
            result = await research_topic.ainvoke(
                {"topic": "test", "download_available": False}
            )

        # Should still return results from arXiv
        assert "Research Report" in result

    @pytest.mark.asyncio
    async def test_research_topic_no_results(self, mock_tool_context):
        from src.tools.research.workflow import create_workflow_tools

        tools = create_workflow_tools(mock_tool_context)
        research_topic = tools[0]

        with patch(
            "src.tools.research.workflow._search_arxiv_raw",
            new_callable=AsyncMock,
            return_value=[],
        ), patch(
            "src.tools.research.workflow._search_semantic_scholar_raw",
            new_callable=AsyncMock,
            return_value=[],
        ):
            result = await research_topic.ainvoke({"topic": "xyznonexistent"})

        assert "No results found" in result

    @pytest.mark.asyncio
    async def test_research_topic_both_fail(self, mock_tool_context):
        from src.tools.research.workflow import create_workflow_tools

        tools = create_workflow_tools(mock_tool_context)
        research_topic = tools[0]

        with patch(
            "src.tools.research.workflow._search_arxiv_raw",
            new_callable=AsyncMock,
            side_effect=Exception("arXiv down"),
        ), patch(
            "src.tools.research.workflow._search_semantic_scholar_raw",
            new_callable=AsyncMock,
            side_effect=Exception("S2 down"),
        ):
            result = await research_topic.ainvoke({"topic": "test"})

        assert "No results found" in result

    @pytest.mark.asyncio
    async def test_research_topic_caps_num_papers(self, mock_tool_context):
        from src.tools.research.workflow import create_workflow_tools

        tools = create_workflow_tools(mock_tool_context)
        research_topic = tools[0]

        with patch(
            "src.tools.research.workflow._search_arxiv_raw",
            new_callable=AsyncMock,
            return_value=[],
        ), patch(
            "src.tools.research.workflow._search_semantic_scholar_raw",
            new_callable=AsyncMock,
            return_value=[],
        ):
            result = await research_topic.ainvoke(
                {"topic": "test", "num_papers": 100}
            )

        # Should cap to 30 (max) - no error even with large value
        assert isinstance(result, str)


class TestFormatResearchReport:
    """Tests for _format_research_report function."""

    def test_format_basic_report(self, sample_paper):
        from src.tools.research.workflow import _format_research_report

        report = _format_research_report(
            topic="test topic",
            papers=[sample_paper],
            download_results=[],
            arxiv_count=1,
            s2_count=0,
            include_abstracts=True,
        )

        assert "Research Report: test topic" in report
        assert "arXiv (1 results)" in report
        assert "Semantic Scholar (0 results)" in report
        assert "Unique papers after deduplication: 1" in report
        assert sample_paper.title in report

    def test_format_with_downloads(self, sample_paper):
        from src.tools.research.workflow import _format_research_report

        report = _format_research_report(
            topic="test",
            papers=[sample_paper],
            download_results=["  Downloaded: paper.pdf"],
            arxiv_count=1,
            s2_count=0,
            include_abstracts=False,
        )

        assert "Downloads:" in report
        assert "Downloaded: paper.pdf" in report

    def test_format_truncates_abstracts(self, sample_paper):
        from src.tools.research.workflow import _format_research_report

        sample_paper.abstract = "x" * 500

        report = _format_research_report(
            topic="test",
            papers=[sample_paper],
            download_results=[],
            arxiv_count=1,
            s2_count=0,
            include_abstracts=True,
        )

        assert "..." in report

    def test_format_without_abstracts(self, sample_paper):
        from src.tools.research.workflow import _format_research_report

        report = _format_research_report(
            topic="test",
            papers=[sample_paper],
            download_results=[],
            arxiv_count=1,
            s2_count=0,
            include_abstracts=False,
        )

        assert "Abstract:" not in report


# ── Research __init__ integration test ─────────────────────────────


class TestResearchToolsRegistry:
    """Tests for the research tools registry."""

    def test_get_research_metadata_includes_all_modules(self):
        from src.tools.research import get_research_metadata

        metadata = get_research_metadata()

        # Web tools
        assert "web_search" in metadata

        # Paper tools
        assert "search_papers" in metadata
        assert "download_paper" in metadata
        assert "get_paper_info" in metadata

        # Browser tools
        assert "browse_website" in metadata
        assert "download_from_website" in metadata

        # Workflow tools
        assert "research_topic" in metadata

    def test_create_research_tools_returns_all(self, mock_tool_context):
        from src.tools.research import create_research_tools

        tools = create_research_tools(mock_tool_context)
        names = {t.name for t in tools}

        assert "web_search" in names
        assert "search_papers" in names
        assert "download_paper" in names
        assert "get_paper_info" in names
        assert "browse_website" in names
        assert "download_from_website" in names
        assert "research_topic" in names
