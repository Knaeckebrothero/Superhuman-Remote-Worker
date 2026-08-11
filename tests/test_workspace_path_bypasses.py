"""Virtual-tier regressions for services that require local staging paths."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.backends.object_store import InMemoryObjectStore
from src.core.backends.virtual import VirtualWorkspaceBackend
from src.core.workspace import WorkspaceManager
from src.tools.citation.sources import create_source_tools
from src.tools.context import ToolContext
from src.tools.research.papers import create_paper_tools
from src.tools.research.utils.paper_types import (
    AccessStatus,
    DownloadResult,
    Paper,
    PaperSource,
)
from src.tools.research.workflow import _download_available_papers
from src.tools.webdav.tools import create_webdav_tools


def _virtual_workspace() -> tuple[WorkspaceManager, VirtualWorkspaceBackend]:
    """Build the small real WorkspaceManager surface used by these tools."""
    backend = VirtualWorkspaceBackend(
        InMemoryObjectStore(),
        prefix="virtual-path-test/",
    )
    workspace = WorkspaceManager.__new__(WorkspaceManager)
    workspace._backend = backend
    workspace._initialized = True
    workspace._workspace_path = Path("/nonexistent-virtual-workspace")
    return workspace, backend


class _WebDavClient:
    def __init__(self, payload: bytes = b"cloud document") -> None:
        self.payload = payload
        self.uploaded: bytes | None = None
        self.webdav = SimpleNamespace(hostname="https://cloud.example/dav")

    def download_sync(self, *, remote_path: str, local_path: str) -> None:
        assert remote_path == "/reports/cloud.pdf"
        Path(local_path).write_bytes(self.payload)

    def upload_sync(self, *, remote_path: str, local_path: str) -> None:
        assert remote_path == "/out/report.pdf"
        self.uploaded = Path(local_path).read_bytes()

    def info(self, path: str) -> dict[str, str]:
        assert path == "/reports/cloud.pdf"
        return {"etag": '"v1"', "content_type": "application/pdf"}

    def mkdir(self, path: str) -> None:
        assert path == "/out"


@pytest.mark.asyncio
async def test_webdav_read_and_write_stage_through_virtual_backend() -> None:
    workspace, backend = _virtual_workspace()
    client = _WebDavClient(b"%PDF cloud")
    context = ToolContext(workspace_manager=workspace)
    context.datasources["webdav"] = client
    context.cloud_anchor_persist_callback = AsyncMock()
    tools = {tool.name: tool for tool in create_webdav_tools(context)}

    result = await tools["webdav_read"].ainvoke(
        {"path": "/reports/cloud.pdf", "target": "cloud.pdf"}
    )

    assert "documents/cloud.pdf" in result
    assert backend.read_file("documents/cloud.pdf", binary=True) == b"%PDF cloud"
    assert not Path(backend.resolve_path("documents/cloud.pdf")).exists()
    anchor = context.get_cloud_anchor("documents/cloud.pdf")
    assert anchor["etag"] == '"v1"'
    context.cloud_anchor_persist_callback.assert_awaited_once_with(
        "documents/cloud.pdf",
        anchor,
    )

    backend.write_file("output/report.pdf", b"workspace report")
    result = tools["webdav_write"].invoke(
        {"source": "output/report.pdf", "remote_path": "/out/report.pdf"}
    )

    assert "Uploaded output/report.pdf" in result
    assert client.uploaded == b"workspace report"


@pytest.mark.asyncio
async def test_research_workflow_download_writes_hostless_virtual_backend() -> None:
    workspace, backend = _virtual_workspace()
    context = ToolContext(workspace_manager=workspace)
    context.get_or_register_doc_source = AsyncMock(return_value=7)
    paper = Paper(
        title="Virtual Paper",
        authors=["Researcher"],
        arxiv_id="2608.00001",
        url="https://arxiv.org/abs/2608.00001",
        source=PaperSource.ARXIV,
        access_status=AccessStatus.OPEN_ACCESS,
    )

    async def _download(_arxiv_id: str, destination: Path) -> Path:
        assert destination.is_absolute()
        path = destination / "virtual-paper.pdf"
        path.write_bytes(b"%PDF workflow")
        return path

    with patch(
        "src.tools.research.workflow._download_single_arxiv",
        side_effect=_download,
    ):
        results = await _download_available_papers([paper], context)

    assert results == ["  Downloaded: Virtual Paper -> virtual-paper.pdf"]
    assert backend.read_file("documents/virtual-paper.pdf", binary=True) == (
        b"%PDF workflow"
    )
    context.get_or_register_doc_source.assert_awaited_once_with(
        "documents/virtual-paper.pdf",
        name="Virtual Paper",
    )


@pytest.mark.asyncio
async def test_download_paper_writes_virtual_backend_without_host_heuristic() -> None:
    workspace, backend = _virtual_workspace()
    context = ToolContext(workspace_manager=workspace)
    context.get_or_register_doc_source = AsyncMock(return_value=8)
    paper = Paper(
        title="Direct Paper",
        authors=["Researcher"],
        arxiv_id="2608.00002",
        url="https://arxiv.org/abs/2608.00002",
        source=PaperSource.ARXIV,
        access_status=AccessStatus.OPEN_ACCESS,
    )
    tools = {tool.name: tool for tool in create_paper_tools(context)}

    async def _download(_identifier: str, destination: Path) -> DownloadResult:
        assert destination.is_absolute()
        path = destination / "direct-paper.pdf"
        path.write_bytes(b"%PDF direct")
        return DownloadResult(
            success=True,
            path=path,
            source=PaperSource.ARXIV,
            paper=paper,
        )

    with patch(
        "src.tools.research.papers._try_arxiv_download",
        side_effect=_download,
    ):
        result = await tools["download_paper"].ainvoke(
            {"identifier": "2608.00002", "identifier_type": "arxiv"}
        )

    assert "Path: documents/direct-paper.pdf" in result
    assert backend.read_file("documents/direct-paper.pdf", binary=True) == (
        b"%PDF direct"
    )


@pytest.mark.asyncio
async def test_citation_snapshot_materializes_virtual_source_bytes() -> None:
    workspace, backend = _virtual_workspace()
    backend.write_file("documents/cloud.pdf", b"%PDF anchored")
    context = ToolContext(workspace_manager=workspace)
    anchor = {
        "backend": "webdav",
        "path": "/reports/cloud.pdf",
        "content_type": "application/pdf",
    }
    context.record_cloud_anchor("documents/cloud.pdf", anchor)

    orchestrator_client = MagicMock()
    orchestrator_client.save_citation_snapshot = AsyncMock(
        return_value="citations/aa/snapshot"
    )
    context.orchestrator_client = orchestrator_client

    source = MagicMock(id=12)
    citation_engine = MagicMock()
    citation_engine.add_doc_source = AsyncMock(return_value=source)
    citation_engine.cite_doc = AsyncMock(
        return_value=SimpleNamespace(
            verification_status=SimpleNamespace(value="verified"),
            citation_id=99,
            similarity_score=1.0,
            verification_notes=None,
        )
    )
    context.citation_engine = citation_engine
    tools = {tool.name: tool for tool in create_source_tools(context)}

    result = await tools["cite_document"].ainvoke(
        {
            "text": "anchored",
            "document_path": "documents/cloud.pdf",
        }
    )

    assert "Cloud-anchored: yes" in result
    orchestrator_client.save_citation_snapshot.assert_awaited_once_with(
        b"%PDF anchored",
        content_type="application/pdf",
    )
    assert anchor["snapshot_blob_key"] == "citations/aa/snapshot"
    add_args, add_kwargs = citation_engine.add_doc_source.await_args
    assert add_args[0] != backend.resolve_path("documents/cloud.pdf")
    assert add_kwargs["metadata"] == {"cloud": anchor}
    assert not Path(backend.resolve_path("documents/cloud.pdf")).exists()
