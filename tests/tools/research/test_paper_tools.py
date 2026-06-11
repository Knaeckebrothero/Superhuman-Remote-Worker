"""Tests for paper tools (search_papers, download_paper, get_paper_info)."""

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.tools.research.papers import (
    DOI_PATTERN,
    ARXIV_PATTERN,
    _detect_identifier_type,
    _is_remote_workspace,
    _get_local_documents_dir,
    _transfer_to_workspace,
    _maybe_transfer,
    create_paper_tools,
)
from src.tools.research.utils.paper_types import (
    AccessStatus,
    DownloadResult,
    Paper,
    PaperSource,
)


class TestDetectIdentifierType:
    """Tests for _detect_identifier_type function."""

    def test_detects_doi(self):
        assert _detect_identifier_type("10.1038/nature12373") == "doi"

    def test_detects_doi_with_prefix(self):
        assert _detect_identifier_type("https://doi.org/10.1038/nature12373") == "doi"

    def test_detects_arxiv_id(self):
        assert _detect_identifier_type("2408.08921") == "arxiv"

    def test_detects_arxiv_id_with_version(self):
        assert _detect_identifier_type("1706.03762v7") == "arxiv"

    def test_detects_arxiv_url(self):
        assert _detect_identifier_type("https://arxiv.org/abs/2408.08921") == "arxiv"

    def test_defaults_to_doi(self):
        assert _detect_identifier_type("some-unknown-identifier") == "doi"


class TestDoiPattern:
    """Tests for the DOI regex pattern."""

    def test_matches_standard_doi(self):
        assert DOI_PATTERN.search("10.1038/nature12373") is not None

    def test_matches_doi_with_slashes(self):
        assert DOI_PATTERN.search("10.18653/v1/N19-1423") is not None

    def test_no_match_for_arxiv(self):
        assert DOI_PATTERN.search("2408.08921") is None


class TestArxivPattern:
    """Tests for the arXiv ID regex pattern."""

    def test_matches_standard_id(self):
        assert ARXIV_PATTERN.search("2408.08921") is not None

    def test_matches_with_version(self):
        assert ARXIV_PATTERN.search("1706.03762v7") is not None

    def test_matches_five_digit(self):
        assert ARXIV_PATTERN.search("2301.12345") is not None


class TestCreatePaperTools:
    """Tests for create_paper_tools factory function."""

    def test_creates_three_tools(self, mock_tool_context):
        tools = create_paper_tools(mock_tool_context)
        assert len(tools) == 3
        names = {t.name for t in tools}
        assert names == {"search_papers", "download_paper", "get_paper_info"}


class TestSearchPapers:
    """Tests for search_papers tool."""

    @pytest.mark.asyncio
    async def test_search_arxiv(self, mock_tool_context, sample_paper):
        tools = create_paper_tools(mock_tool_context)
        search_papers = next(t for t in tools if t.name == "search_papers")

        with patch(
            "src.tools.research.papers._search_arxiv",
            new_callable=AsyncMock,
            return_value="arXiv Search Results for: test\nResults: 1",
        ) as mock_search:
            result = await search_papers.ainvoke({"query": "test", "source": "arxiv"})

        assert "arXiv Search Results" in result
        mock_search.assert_called_once_with("test", 10)

    @pytest.mark.asyncio
    async def test_search_semantic_scholar(self, mock_tool_context):
        tools = create_paper_tools(mock_tool_context)
        search_papers = next(t for t in tools if t.name == "search_papers")

        with patch(
            "src.tools.research.papers._search_semantic_scholar",
            new_callable=AsyncMock,
            return_value="Semantic Scholar Results for: test\nResults: 1",
        ):
            result = await search_papers.ainvoke(
                {"query": "test", "source": "semantic_scholar"}
            )

        assert "Semantic Scholar Results" in result

    @pytest.mark.asyncio
    async def test_search_unknown_source(self, mock_tool_context):
        tools = create_paper_tools(mock_tool_context)
        search_papers = next(t for t in tools if t.name == "search_papers")

        result = await search_papers.ainvoke({"query": "test", "source": "unknown_db"})
        assert "Unknown source" in result

    @pytest.mark.asyncio
    async def test_max_results_capped_at_50(self, mock_tool_context):
        tools = create_paper_tools(mock_tool_context)
        search_papers = next(t for t in tools if t.name == "search_papers")

        with patch(
            "src.tools.research.papers._search_arxiv",
            new_callable=AsyncMock,
            return_value="Results",
        ) as mock_search:
            await search_papers.ainvoke(
                {"query": "test", "source": "arxiv", "max_results": 100}
            )

        mock_search.assert_called_once_with("test", 50)


class TestDownloadPaper:
    """Tests for download_paper tool."""

    @pytest.mark.asyncio
    async def test_download_arxiv_success(
        self, mock_tool_context, sample_paper, temp_docs_dir
    ):
        tools = create_paper_tools(mock_tool_context)
        download_paper = next(t for t in tools if t.name == "download_paper")

        mock_result = DownloadResult(
            success=True,
            path=temp_docs_dir / "1706.03762.pdf",
            source=PaperSource.ARXIV,
            paper=sample_paper,
        )

        with patch(
            "src.tools.research.papers._try_arxiv_download",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            result = await download_paper.ainvoke(
                {"identifier": "1706.03762", "identifier_type": "arxiv"}
            )

        assert "Downloaded" in result
        assert "arXiv" in result

    @pytest.mark.asyncio
    async def test_download_doi_unpaywall_success(
        self, mock_tool_context, temp_docs_dir
    ):
        tools = create_paper_tools(mock_tool_context)
        download_paper = next(t for t in tools if t.name == "download_paper")

        paper = Paper(
            title="Test Paper",
            authors=["Author"],
            url="https://doi.org/10.1038/test",
            source=PaperSource.UNPAYWALL,
            access_status=AccessStatus.OPEN_ACCESS,
        )
        mock_result = DownloadResult(
            success=True,
            path=temp_docs_dir / "test.pdf",
            source=PaperSource.UNPAYWALL,
            paper=paper,
        )

        with (
            patch(
                "src.tools.research.papers._try_arxiv_download",
                new_callable=AsyncMock,
                return_value=DownloadResult(success=False, error="Not on arXiv"),
            ),
            patch(
                "src.tools.research.papers._try_unpaywall_download",
                new_callable=AsyncMock,
                return_value=mock_result,
            ),
        ):
            result = await download_paper.ainvoke(
                {"identifier": "10.1038/test", "identifier_type": "doi"}
            )

        assert "Downloaded" in result
        assert "Unpaywall" in result

    @pytest.mark.asyncio
    async def test_download_fallback_chain_all_fail(self, mock_tool_context):
        tools = create_paper_tools(mock_tool_context)
        download_paper = next(t for t in tools if t.name == "download_paper")

        with (
            patch(
                "src.tools.research.papers._try_arxiv_download",
                new_callable=AsyncMock,
                return_value=DownloadResult(success=False, error="Not found"),
            ),
            patch(
                "src.tools.research.papers._try_unpaywall_download",
                new_callable=AsyncMock,
                return_value=DownloadResult(success=False, error="Not found"),
            ),
        ):
            result = await download_paper.ainvoke(
                {"identifier": "10.1038/test", "identifier_type": "doi"}
            )

        assert "Could not download" in result
        assert "Suggestions" in result

    @pytest.mark.asyncio
    async def test_download_paywalled_message(self, mock_tool_context):
        tools = create_paper_tools(mock_tool_context)
        download_paper = next(t for t in tools if t.name == "download_paper")

        paywalled_paper = Paper(
            title="Paywalled Paper",
            authors=["Author"],
            url="https://doi.org/10.1016/test",
            source=PaperSource.UNPAYWALL,
            access_status=AccessStatus.PAYWALLED,
        )

        with (
            patch(
                "src.tools.research.papers._try_unpaywall_download",
                new_callable=AsyncMock,
                return_value=DownloadResult(
                    success=False,
                    error="No OA PDF",
                    paper=paywalled_paper,
                ),
            ),
        ):
            result = await download_paper.ainvoke(
                {"identifier": "10.1016/test", "identifier_type": "doi"}
            )

        assert "paywalled" in result.lower()
        assert "Paywalled Paper" in result

    @pytest.mark.asyncio
    async def test_download_auto_detects_arxiv(
        self, mock_tool_context, sample_paper, temp_docs_dir
    ):
        tools = create_paper_tools(mock_tool_context)
        download_paper = next(t for t in tools if t.name == "download_paper")

        mock_result = DownloadResult(
            success=True,
            path=temp_docs_dir / "1706.03762.pdf",
            source=PaperSource.ARXIV,
            paper=sample_paper,
        )

        with patch(
            "src.tools.research.papers._try_arxiv_download",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            result = await download_paper.ainvoke(
                {"identifier": "1706.03762"}  # auto-detect
            )

        assert "Downloaded" in result


class TestGetPaperInfo:
    """Tests for get_paper_info tool."""

    @pytest.mark.asyncio
    async def test_get_info_from_semantic_scholar(self, mock_tool_context):
        tools = create_paper_tools(mock_tool_context)
        get_paper_info = next(t for t in tools if t.name == "get_paper_info")

        with patch(
            "src.tools.research.papers._get_semantic_scholar_info",
            new_callable=AsyncMock,
            return_value="Paper: Test Paper\nAuthors: Alice\nCitations: 100",
        ):
            result = await get_paper_info.ainvoke({"identifier": "10.1038/nature12373"})

        assert "Test Paper" in result

    @pytest.mark.asyncio
    async def test_get_info_fallback_to_arxiv(self, mock_tool_context):
        tools = create_paper_tools(mock_tool_context)
        get_paper_info = next(t for t in tools if t.name == "get_paper_info")

        with (
            patch(
                "src.tools.research.papers._get_semantic_scholar_info",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "src.tools.research.papers._get_arxiv_info",
                new_callable=AsyncMock,
                return_value="Paper: Arxiv Paper\narXiv: 1706.03762",
            ),
        ):
            result = await get_paper_info.ainvoke({"identifier": "1706.03762"})

        assert "Arxiv Paper" in result

    @pytest.mark.asyncio
    async def test_get_info_not_found(self, mock_tool_context):
        tools = create_paper_tools(mock_tool_context)
        get_paper_info = next(t for t in tools if t.name == "get_paper_info")

        with patch(
            "src.tools.research.papers._get_semantic_scholar_info",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await get_paper_info.ainvoke({"identifier": "10.9999/nonexistent"})

        assert "Could not find" in result


class TestPaperToolsMetadata:
    """Tests for paper tools metadata."""

    def test_metadata_has_all_tools(self):
        from src.tools.research.papers import PAPER_TOOLS_METADATA

        assert "search_papers" in PAPER_TOOLS_METADATA
        assert "download_paper" in PAPER_TOOLS_METADATA
        assert "get_paper_info" in PAPER_TOOLS_METADATA

    def test_metadata_category_is_research(self):
        from src.tools.research.papers import PAPER_TOOLS_METADATA

        for name, meta in PAPER_TOOLS_METADATA.items():
            assert meta["category"] == "research", f"{name} has wrong category"

    def test_metadata_has_phases(self):
        from src.tools.research.papers import PAPER_TOOLS_METADATA

        for name, meta in PAPER_TOOLS_METADATA.items():
            assert "phases" in meta, f"{name} missing phases"
            assert "tactical" in meta["phases"], f"{name} not in tactical phase"


# ── Remote workspace awareness tests ─────────────────────────────────


class TestIsRemoteWorkspace:
    """Tests for _is_remote_workspace detection."""

    def test_remote_when_backend_has_host(self, mock_remote_tool_context):
        assert _is_remote_workspace(mock_remote_tool_context) is True

    def test_local_when_backend_has_no_host(self, mock_tool_context):
        assert _is_remote_workspace(mock_tool_context) is False

    def test_local_when_no_workspace(self):
        ctx = MagicMock()
        ctx.has_workspace.return_value = False
        assert _is_remote_workspace(ctx) is False


class TestGetLocalDocumentsDir:
    """Tests for _get_local_documents_dir — always returns a local path."""

    def test_local_workspace_returns_workspace_path(
        self, mock_tool_context, temp_docs_dir
    ):
        mock_tool_context.workspace_manager.get_path.return_value = (
            temp_docs_dir / "documents"
        )
        result = _get_local_documents_dir(mock_tool_context)
        assert str(temp_docs_dir) in str(result)

    def test_remote_workspace_returns_temp_dir(self, mock_remote_tool_context):
        result = _get_local_documents_dir(mock_remote_tool_context)
        # Must be a real local path, not the remote path
        assert result.exists()
        assert "agent-host" not in str(result)
        # Clean up
        result.rmdir()

    def test_no_workspace_returns_fallback(self):
        ctx = MagicMock()
        ctx.has_workspace.return_value = False
        result = _get_local_documents_dir(ctx)
        assert result == Path("./downloads")


class TestTransferToWorkspace:
    """Tests for _transfer_to_workspace."""

    def test_transfers_file_via_backend(self, mock_remote_tool_context):
        backend = mock_remote_tool_context.workspace_manager.backend

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(b"%PDF-1.4 test content")
            local_path = Path(f.name)

        try:
            result = _transfer_to_workspace(
                mock_remote_tool_context, local_path, "documents/test.pdf"
            )
            assert result == "documents/test.pdf"
            backend.write_file.assert_called_once_with(
                "documents/test.pdf", b"%PDF-1.4 test content"
            )
        finally:
            local_path.unlink(missing_ok=True)


class TestMaybeTransfer:
    """Tests for _maybe_transfer — conditional transfer."""

    def test_local_mode_returns_local_path(self, mock_tool_context, temp_docs_dir):
        local_file = temp_docs_dir / "test.pdf"
        local_file.write_bytes(b"test")
        result = _maybe_transfer(mock_tool_context, False, local_file)
        assert result == str(local_file)

    def test_remote_mode_transfers_and_returns_relative(self, mock_remote_tool_context):
        backend = mock_remote_tool_context.workspace_manager.backend

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(b"test pdf")
            local_path = Path(f.name)

        try:
            result = _maybe_transfer(mock_remote_tool_context, True, local_path)
            assert result == f"documents/{local_path.name}"
            backend.write_file.assert_called_once()
        finally:
            local_path.unlink(missing_ok=True)

    def test_none_path_returns_empty(self, mock_tool_context):
        assert _maybe_transfer(mock_tool_context, False, None) == ""


class TestDownloadPaperRemote:
    """Tests for download_paper with remote workspace."""

    @pytest.mark.asyncio
    async def test_download_arxiv_transfers_to_workspace(
        self, mock_remote_tool_context, sample_paper
    ):
        """On remote workspace, downloaded file should be transferred via backend."""
        tools = create_paper_tools(mock_remote_tool_context)
        download_paper = next(t for t in tools if t.name == "download_paper")
        backend = mock_remote_tool_context.workspace_manager.backend

        # Create a real temp file that the mock will "download"
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(b"%PDF-1.4 test content")
            temp_pdf = Path(f.name)

        mock_result = DownloadResult(
            success=True,
            path=temp_pdf,
            source=PaperSource.ARXIV,
            paper=sample_paper,
        )

        try:
            with patch(
                "src.tools.research.papers._try_arxiv_download",
                new_callable=AsyncMock,
                return_value=mock_result,
            ):
                result = await download_paper.ainvoke(
                    {"identifier": "1706.03762", "identifier_type": "arxiv"}
                )

            assert "Downloaded" in result
            assert "documents/" in result
            # Verify the backend.write_file was called (transfer happened)
            backend.write_file.assert_called_once()
        finally:
            temp_pdf.unlink(missing_ok=True)
