"""Tests for arXiv client utility."""

import sys
from unittest.mock import MagicMock, patch

import pytest

from agent.tools.research.utils.arxiv_client import ArxivClient, extract_arxiv_id
from agent.tools.research.utils.paper_types import AccessStatus, PaperSource


@pytest.fixture
def mock_arxiv_module():
    """Create a mock arxiv module and inject it into sys.modules."""
    mock_mod = MagicMock()
    mock_mod.SortCriterion.Relevance = "relevance"
    mock_mod.Client.return_value.results.return_value = []
    with patch.dict(sys.modules, {"arxiv": mock_mod}):
        yield mock_mod


def test_installed_arxiv_package_exposes_client_results():
    """Pin the API contract provided by the dependency installed in CI/images.

    The old tests mocked ``Search.results`` and stayed green after arxiv 4.0
    removed it.  This test imports the actual installed package without making
    a network request.
    """
    import arxiv

    assert callable(getattr(arxiv.Client, "results", None))


class TestExtractArxivId:
    """Tests for extract_arxiv_id function."""

    def test_plain_id(self):
        assert extract_arxiv_id("1706.03762") == "1706.03762"

    def test_id_with_version(self):
        assert extract_arxiv_id("1706.03762v7") == "1706.03762v7"

    def test_abs_url(self):
        assert extract_arxiv_id("https://arxiv.org/abs/2408.08921") == "2408.08921"

    def test_pdf_url(self):
        assert extract_arxiv_id("https://arxiv.org/pdf/2408.08921v1") == "2408.08921v1"

    def test_five_digit_id(self):
        assert extract_arxiv_id("2301.12345") == "2301.12345"

    def test_no_match(self):
        assert extract_arxiv_id("not-an-arxiv-id") is None

    def test_empty_string(self):
        assert extract_arxiv_id("") is None

    def test_doi_does_not_match(self):
        assert extract_arxiv_id("10.1038/nature12373") is None


class TestArxivClientSearch:
    """Tests for ArxivClient.search method."""

    @pytest.mark.asyncio
    async def test_search_returns_papers(self, mock_arxiv_module, mock_arxiv_result):
        client = ArxivClient(rate_limit_seconds=0)

        mock_search = MagicMock()
        mock_arxiv_module.Search.return_value = mock_search
        mock_arxiv_module.Client.return_value.results.return_value = [mock_arxiv_result]

        papers = await client.search("attention mechanism", max_results=5)

        assert len(papers) == 1
        paper = papers[0]
        assert paper.title == "Attention Is All You Need"
        assert paper.source == PaperSource.ARXIV
        assert paper.access_status == AccessStatus.OPEN_ACCESS
        assert paper.year == 2017
        mock_arxiv_module.Client.return_value.results.assert_called_once_with(
            mock_search
        )

    @pytest.mark.asyncio
    async def test_search_empty_results(self, mock_arxiv_module):
        client = ArxivClient(rate_limit_seconds=0)

        mock_search = MagicMock()
        mock_arxiv_module.Search.return_value = mock_search

        papers = await client.search("nonexistent topic xyz")
        assert papers == []

    @pytest.mark.asyncio
    async def test_search_passes_max_results(self, mock_arxiv_module):
        client = ArxivClient(rate_limit_seconds=0)

        mock_search = MagicMock()
        mock_arxiv_module.Search.return_value = mock_search

        await client.search("test", max_results=25)

        mock_arxiv_module.Search.assert_called_once_with(
            query="test", max_results=25, sort_by="relevance"
        )
        mock_arxiv_module.Client.return_value.results.assert_called_once_with(
            mock_search
        )


class TestArxivClientGetPaper:
    """Tests for ArxivClient.get_paper method."""

    @pytest.mark.asyncio
    async def test_get_paper_found(self, mock_arxiv_module, mock_arxiv_result):
        client = ArxivClient(rate_limit_seconds=0)

        mock_search = MagicMock()
        mock_arxiv_module.Search.return_value = mock_search
        mock_arxiv_module.Client.return_value.results.return_value = [mock_arxiv_result]

        paper = await client.get_paper("1706.03762")

        assert paper is not None
        assert paper.title == "Attention Is All You Need"
        mock_arxiv_module.Search.assert_called_once_with(id_list=["1706.03762"])
        mock_arxiv_module.Client.return_value.results.assert_called_once_with(
            mock_search
        )

    @pytest.mark.asyncio
    async def test_get_paper_not_found(self, mock_arxiv_module):
        client = ArxivClient(rate_limit_seconds=0)

        mock_search = MagicMock()
        mock_arxiv_module.Search.return_value = mock_search

        paper = await client.get_paper("0000.00000")
        assert paper is None

    @pytest.mark.asyncio
    async def test_get_paper_extracts_id_from_url(
        self, mock_arxiv_module, mock_arxiv_result
    ):
        client = ArxivClient(rate_limit_seconds=0)

        mock_search = MagicMock()
        mock_arxiv_module.Search.return_value = mock_search
        mock_arxiv_module.Client.return_value.results.return_value = [mock_arxiv_result]

        await client.get_paper("https://arxiv.org/abs/1706.03762v7")

        mock_arxiv_module.Search.assert_called_once_with(id_list=["1706.03762v7"])


class TestArxivClientDownload:
    """Tests for ArxivClient.download method."""

    @pytest.mark.asyncio
    async def test_download_success(
        self, temp_docs_dir, mock_arxiv_module, mock_arxiv_result
    ):
        client = ArxivClient(rate_limit_seconds=0)

        mock_search = MagicMock()
        mock_arxiv_module.Search.return_value = mock_search
        mock_arxiv_module.Client.return_value.results.return_value = [mock_arxiv_result]

        result = await client.download("1706.03762", temp_docs_dir)

        assert result.success is True
        assert result.path == temp_docs_dir / "1706.03762.pdf"
        assert result.source == PaperSource.ARXIV
        assert result.paper is not None
        assert result.paper.title == "Attention Is All You Need"
        mock_arxiv_module.Client.return_value.results.assert_called_once_with(
            mock_search
        )
        mock_arxiv_result.download_pdf.assert_called_once_with(
            dirpath=str(temp_docs_dir), filename="1706.03762.pdf"
        )

    @pytest.mark.asyncio
    async def test_download_not_found(self, temp_docs_dir, mock_arxiv_module):
        client = ArxivClient(rate_limit_seconds=0)

        mock_search = MagicMock()
        mock_arxiv_module.Search.return_value = mock_search

        result = await client.download("0000.00000", temp_docs_dir)

        assert result.success is False
        assert "not found" in result.error

    @pytest.mark.asyncio
    async def test_download_exception(
        self, temp_docs_dir, mock_arxiv_module, mock_arxiv_result
    ):
        client = ArxivClient(rate_limit_seconds=0)

        mock_search = MagicMock()
        mock_arxiv_result.download_pdf.side_effect = Exception("Network error")
        mock_arxiv_module.Search.return_value = mock_search
        mock_arxiv_module.Client.return_value.results.return_value = [mock_arxiv_result]

        result = await client.download("1706.03762", temp_docs_dir)

        assert result.success is False
        assert "Network error" in result.error


class TestArxivClientReuse:
    """The official client owns request pacing and is reused by the wrapper."""

    @pytest.mark.asyncio
    async def test_reuses_one_client_across_requests(self, mock_arxiv_module):
        client = ArxivClient(rate_limit_seconds=0)

        mock_search = MagicMock()
        mock_arxiv_module.Search.return_value = mock_search

        await client.search("test1")
        await client.search("test2")

        mock_arxiv_module.Client.assert_called_once_with(delay_seconds=0)
        assert mock_arxiv_module.Client.return_value.results.call_count == 2

    @pytest.mark.asyncio
    async def test_passes_rate_limit_to_official_client(self, mock_arxiv_module):
        client = ArxivClient(rate_limit_seconds=5.0)

        mock_search = MagicMock()
        mock_arxiv_module.Search.return_value = mock_search

        await client.search("test")

        mock_arxiv_module.Client.assert_called_once_with(delay_seconds=5.0)


class TestArxivClientToPaper:
    """Tests for ArxivClient._to_paper conversion."""

    def test_converts_all_fields(self, mock_arxiv_result):
        client = ArxivClient()
        paper = client._to_paper(mock_arxiv_result)

        assert paper.title == "Attention Is All You Need"
        assert paper.authors == ["Ashish Vaswani", "Noam Shazeer"]
        assert paper.abstract == "The dominant sequence transduction models..."
        assert paper.doi == "10.48550/arXiv.1706.03762"
        assert paper.arxiv_id == "1706.03762v7"
        assert paper.url == "http://arxiv.org/abs/1706.03762v7"
        assert paper.pdf_url == "http://arxiv.org/pdf/1706.03762v7"
        assert paper.source == PaperSource.ARXIV
        assert paper.access_status == AccessStatus.OPEN_ACCESS
        assert paper.year == 2017

    def test_handles_no_published_date(self, mock_arxiv_result):
        mock_arxiv_result.published = None
        client = ArxivClient()
        paper = client._to_paper(mock_arxiv_result)
        assert paper.year is None
