"""Tests for Unpaywall client utility."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.tools.research.utils.paper_types import AccessStatus, PaperSource
from src.tools.research.utils.unpaywall_client import UnpaywallClient

# Patch target: research_request is imported locally inside UnpaywallClient methods
_RESEARCH_REQUEST_PATCH = "src.tools.research.utils.network.research_request"


# Sample Unpaywall API response for an open access paper
SAMPLE_OA_RESPONSE = {
    "doi": "10.1038/nature12373",
    "doi_url": "https://doi.org/10.1038/nature12373",
    "title": "Genomic analysis of a key innovation in an experimental E. coli population",
    "year": 2012,
    "is_oa": True,
    "best_oa_location": {
        "url_for_pdf": "https://europepmc.org/articles/pmc4329834?pdf=render",
        "evidence": "oa repository",
    },
    "z_authors": [
        {"given": "Zachary", "family": "Blount"},
        {"given": "Christina", "family": "Borland"},
    ],
}

# Sample response for a paywalled paper
SAMPLE_PAYWALLED_RESPONSE = {
    "doi": "10.1016/j.cell.2021.01.001",
    "doi_url": "https://doi.org/10.1016/j.cell.2021.01.001",
    "title": "Some Paywalled Paper",
    "year": 2021,
    "is_oa": False,
    "best_oa_location": None,
    "z_authors": [{"given": "Jane", "family": "Doe"}],
}


def _make_mock_response(status=200, json_data=None, content=None):
    """Create a mock aiohttp response."""
    resp = MagicMock()
    resp.status = status
    resp.raise_for_status = MagicMock()
    if json_data is not None:
        resp.json = AsyncMock(return_value=json_data)
    if content is not None:
        resp.read = AsyncMock(return_value=content)
    return resp


def _mock_research_request(responses):
    """Create a mock research_request context manager that yields responses in order.

    Args:
        responses: List of mock responses (or a single response) to yield
                   from research_request() calls in order.
    """
    if not isinstance(responses, list):
        responses = [responses]

    call_idx = [0]

    @asynccontextmanager
    async def mock_request(method, url, **kwargs):
        idx = min(call_idx[0], len(responses) - 1)
        call_idx[0] += 1
        yield responses[idx]

    return mock_request


class TestUnpaywallClientConfiguration:
    """Tests for UnpaywallClient configuration."""

    def test_is_configured_with_email(self):
        client = UnpaywallClient(email="test@example.com")
        assert client.is_configured() is True

    def test_is_configured_without_email(self):
        with patch.dict("os.environ", {}, clear=True):
            client = UnpaywallClient(email=None)
            assert client.is_configured() is False

    def test_reads_email_from_env(self):
        with patch.dict("os.environ", {"UNPAYWALL_EMAIL": "env@example.com"}):
            client = UnpaywallClient()
            assert client.email == "env@example.com"
            assert client.is_configured() is True

    def test_explicit_email_takes_precedence(self):
        with patch.dict("os.environ", {"UNPAYWALL_EMAIL": "env@example.com"}):
            client = UnpaywallClient(email="explicit@example.com")
            assert client.email == "explicit@example.com"


class TestUnpaywallGetPaper:
    """Tests for UnpaywallClient.get_paper method."""

    @pytest.mark.asyncio
    async def test_get_paper_open_access(self):
        client = UnpaywallClient(email="test@example.com")
        mock_resp = _make_mock_response(200, json_data=SAMPLE_OA_RESPONSE)

        with patch(_RESEARCH_REQUEST_PATCH, _mock_research_request(mock_resp)):
            paper = await client.get_paper("10.1038/nature12373")

        assert paper is not None
        assert paper.title == SAMPLE_OA_RESPONSE["title"]
        assert paper.access_status == AccessStatus.OPEN_ACCESS
        assert paper.pdf_url == "https://europepmc.org/articles/pmc4329834?pdf=render"
        assert paper.doi == "10.1038/nature12373"
        assert paper.source == PaperSource.UNPAYWALL
        assert paper.year == 2012
        assert paper.authors == ["Zachary Blount", "Christina Borland"]

    @pytest.mark.asyncio
    async def test_get_paper_paywalled(self):
        client = UnpaywallClient(email="test@example.com")
        mock_resp = _make_mock_response(200, json_data=SAMPLE_PAYWALLED_RESPONSE)

        with patch(_RESEARCH_REQUEST_PATCH, _mock_research_request(mock_resp)):
            paper = await client.get_paper("10.1016/j.cell.2021.01.001")

        assert paper is not None
        assert paper.access_status == AccessStatus.PAYWALLED
        assert paper.pdf_url is None

    @pytest.mark.asyncio
    async def test_get_paper_404(self):
        client = UnpaywallClient(email="test@example.com")
        mock_resp = _make_mock_response(404)

        with patch(_RESEARCH_REQUEST_PATCH, _mock_research_request(mock_resp)):
            paper = await client.get_paper("10.9999/nonexistent")

        assert paper is None

    @pytest.mark.asyncio
    async def test_get_paper_422_invalid_doi(self):
        client = UnpaywallClient(email="test@example.com")
        mock_resp = _make_mock_response(422)

        with patch(_RESEARCH_REQUEST_PATCH, _mock_research_request(mock_resp)):
            paper = await client.get_paper("invalid-doi-format")

        assert paper is None

    @pytest.mark.asyncio
    async def test_get_paper_no_email_returns_none(self):
        with patch.dict("os.environ", {}, clear=True):
            client = UnpaywallClient(email=None)
            paper = await client.get_paper("10.1038/nature12373")
            assert paper is None


class TestUnpaywallGetPdfUrl:
    """Tests for UnpaywallClient.get_pdf_url method."""

    @pytest.mark.asyncio
    async def test_returns_url_for_oa_paper(self):
        client = UnpaywallClient(email="test@example.com")
        mock_resp = _make_mock_response(200, json_data=SAMPLE_OA_RESPONSE)

        with patch(_RESEARCH_REQUEST_PATCH, _mock_research_request(mock_resp)):
            url = await client.get_pdf_url("10.1038/nature12373")

        assert url == "https://europepmc.org/articles/pmc4329834?pdf=render"

    @pytest.mark.asyncio
    async def test_returns_none_for_paywalled(self):
        client = UnpaywallClient(email="test@example.com")
        mock_resp = _make_mock_response(200, json_data=SAMPLE_PAYWALLED_RESPONSE)

        with patch(_RESEARCH_REQUEST_PATCH, _mock_research_request(mock_resp)):
            url = await client.get_pdf_url("10.1016/j.cell.2021.01.001")

        assert url is None


class TestUnpaywallDownload:
    """Tests for UnpaywallClient.download method."""

    @pytest.mark.asyncio
    async def test_download_success(self, temp_docs_dir):
        client = UnpaywallClient(email="test@example.com")
        pdf_content = b"%PDF-1.4 fake pdf content"

        # First call: get_paper (returns OA response)
        # Second call: download PDF
        mock_paper_resp = _make_mock_response(200, json_data=SAMPLE_OA_RESPONSE)
        mock_pdf_resp = _make_mock_response(200, content=pdf_content)

        with patch(
            _RESEARCH_REQUEST_PATCH,
            _mock_research_request([mock_paper_resp, mock_pdf_resp]),
        ):
            result = await client.download("10.1038/nature12373", temp_docs_dir)

        assert result.success is True
        assert result.source == PaperSource.UNPAYWALL
        assert result.paper is not None

    @pytest.mark.asyncio
    async def test_download_doi_not_found(self, temp_docs_dir):
        client = UnpaywallClient(email="test@example.com")
        mock_resp = _make_mock_response(404)

        with patch(_RESEARCH_REQUEST_PATCH, _mock_research_request(mock_resp)):
            result = await client.download("10.9999/nonexistent", temp_docs_dir)

        assert result.success is False
        assert "not found" in result.error.lower()

    @pytest.mark.asyncio
    async def test_download_paywalled_no_pdf(self, temp_docs_dir):
        client = UnpaywallClient(email="test@example.com")
        mock_resp = _make_mock_response(200, json_data=SAMPLE_PAYWALLED_RESPONSE)

        with patch(_RESEARCH_REQUEST_PATCH, _mock_research_request(mock_resp)):
            result = await client.download("10.1016/j.cell.2021.01.001", temp_docs_dir)

        assert result.success is False
        assert "No OA PDF" in result.error
        assert result.paper is not None
        assert result.paper.access_status == AccessStatus.PAYWALLED


class TestUnpaywallToPaper:
    """Tests for UnpaywallClient._to_paper conversion."""

    def test_converts_oa_response(self):
        client = UnpaywallClient(email="test@example.com")
        paper = client._to_paper(SAMPLE_OA_RESPONSE)

        assert paper.title == SAMPLE_OA_RESPONSE["title"]
        assert paper.doi == "10.1038/nature12373"
        assert paper.access_status == AccessStatus.OPEN_ACCESS
        assert paper.pdf_url is not None
        assert paper.source == PaperSource.UNPAYWALL
        assert paper.year == 2012
        assert len(paper.authors) == 2
        assert paper.authors[0] == "Zachary Blount"

    def test_converts_paywalled_response(self):
        client = UnpaywallClient(email="test@example.com")
        paper = client._to_paper(SAMPLE_PAYWALLED_RESPONSE)

        assert paper.access_status == AccessStatus.PAYWALLED
        assert paper.pdf_url is None

    def test_handles_missing_authors(self):
        client = UnpaywallClient(email="test@example.com")
        data = {**SAMPLE_OA_RESPONSE, "z_authors": None}
        paper = client._to_paper(data)
        assert paper.authors == []

    def test_handles_missing_title(self):
        client = UnpaywallClient(email="test@example.com")
        data = {**SAMPLE_OA_RESPONSE, "title": None}
        paper = client._to_paper(data)
        assert paper.title == "Unknown"
