"""Shared fixtures for research tool tests."""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.tools.research.utils.paper_types import (
    AccessStatus,
    DownloadResult,
    Paper,
    PaperSource,
)


@pytest.fixture
def temp_docs_dir():
    """Create a temporary directory for document downloads."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def mock_workspace_manager(temp_docs_dir):
    """Create a mock WorkspaceManager with temp directory."""
    manager = MagicMock()
    manager.is_initialized = True
    manager.workspace_dir = str(temp_docs_dir)
    manager.job_id = "test-job-123"
    return manager


@pytest.fixture
def mock_tool_context(mock_workspace_manager):
    """Create a mock ToolContext with workspace support."""
    from src.tools.context import ToolContext

    ctx = MagicMock(spec=ToolContext)
    ctx.workspace_manager = mock_workspace_manager
    ctx.config = {}
    ctx.has_workspace.return_value = True
    ctx.get_or_register_doc_source.return_value = 1
    return ctx


@pytest.fixture
def sample_paper():
    """Create a sample Paper for testing."""
    return Paper(
        title="Attention Is All You Need",
        authors=["Ashish Vaswani", "Noam Shazeer", "Niki Parmar"],
        abstract="The dominant sequence transduction models...",
        doi="10.48550/arXiv.1706.03762",
        arxiv_id="1706.03762",
        url="http://arxiv.org/abs/1706.03762v7",
        pdf_url="http://arxiv.org/pdf/1706.03762v7",
        source=PaperSource.ARXIV,
        access_status=AccessStatus.OPEN_ACCESS,
        citation_count=100000,
        year=2017,
        venue="NeurIPS",
    )


@pytest.fixture
def sample_paper_s2():
    """Create a sample Semantic Scholar Paper for testing."""
    return Paper(
        title="BERT: Pre-training of Deep Bidirectional Transformers",
        authors=["Jacob Devlin", "Ming-Wei Chang"],
        abstract="We introduce a new language representation model...",
        doi="10.18653/v1/N19-1423",
        arxiv_id="1810.04805",
        url="https://api.semanticscholar.org/graph/v1/paper/abc123",
        pdf_url="https://aclanthology.org/N19-1423.pdf",
        source=PaperSource.SEMANTIC_SCHOLAR,
        access_status=AccessStatus.OPEN_ACCESS,
        citation_count=50000,
        year=2019,
        venue="NAACL",
    )


@pytest.fixture
def sample_download_result(temp_docs_dir, sample_paper):
    """Create a sample successful DownloadResult."""
    pdf_path = temp_docs_dir / "1706.03762.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 fake content")
    return DownloadResult(
        success=True,
        path=pdf_path,
        source=PaperSource.ARXIV,
        paper=sample_paper,
    )


@pytest.fixture
def mock_arxiv_result():
    """Create a mock arxiv.Result object."""
    result = MagicMock()
    result.title = "Attention Is All You Need"
    result.authors = [MagicMock(name="Ashish Vaswani"), MagicMock(name="Noam Shazeer")]
    # MagicMock.name is special - override with property
    result.authors[0].name = "Ashish Vaswani"
    result.authors[1].name = "Noam Shazeer"
    result.summary = "The dominant sequence transduction models..."
    result.doi = "10.48550/arXiv.1706.03762"
    result.entry_id = "http://arxiv.org/abs/1706.03762v7"
    result.pdf_url = "http://arxiv.org/pdf/1706.03762v7"
    result.published = MagicMock()
    result.published.year = 2017
    result.download_pdf = MagicMock()
    return result
