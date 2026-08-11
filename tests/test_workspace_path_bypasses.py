"""Virtual-tier regressions for services that require local staging paths."""

import asyncio
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


def test_workspace_relative_path_accepts_remote_canonical_path_only() -> None:
    root = "/home/agent-host/workspace"
    backend = MagicMock()

    def _resolve(path: str) -> str:
        if path.startswith("/") or path == ".." or path.startswith("../"):
            raise ValueError("outside workspace")
        return root if not path else f"{root}/{path}"

    backend.resolve_path.side_effect = _resolve
    workspace = WorkspaceManager.__new__(WorkspaceManager)
    workspace._backend = backend

    assert (
        workspace.workspace_relative_path(f"{root}/documents/report.pdf")
        == "documents/report.pdf"
    )
    assert workspace.workspace_relative_path("documents/./report.pdf") == (
        "documents/report.pdf"
    )
    with pytest.raises(ValueError, match="outside the workspace"):
        workspace.workspace_relative_path("/etc/passwd")


def test_workspace_relative_path_strips_virtual_object_prefix() -> None:
    workspace, backend = _virtual_workspace()
    backend.write_file("documents/report.pdf", b"top-level")

    assert (
        workspace.workspace_relative_path(backend.resolve_path("documents/report.pdf"))
        == "documents/report.pdf"
    )
    with pytest.raises(ValueError, match="escapes workspace"):
        workspace.workspace_relative_path("../other-prefix/report.pdf")


def test_workspace_relative_path_refuses_ambiguous_virtual_prefix() -> None:
    workspace, backend = _virtual_workspace()
    backend.write_file("documents/report.pdf", b"top-level")
    backend.write_file(
        "virtual-path-test/documents/report.pdf",
        b"legitimate nested path",
    )
    dual_form = backend.resolve_path("documents/report.pdf")

    with pytest.raises(ValueError, match="ambiguous workspace-relative identity"):
        workspace.workspace_relative_path(dual_form)

    # A caller using an unambiguous ordinary relative path still reaches the
    # nested object exactly; only the dual-form string is refused.
    assert (
        workspace.workspace_relative_path(
            "virtual-path-test/virtual-path-test/documents/report.pdf"
        )
        == "virtual-path-test/documents/report.pdf"
    )


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
async def test_parallel_webdav_reads_keep_target_bytes_and_anchor_in_order() -> None:
    workspace, backend = _virtual_workspace()

    class _ParallelWebDavClient:
        webdav = SimpleNamespace(hostname="https://cloud.example/dav")

        def download_sync(self, *, remote_path: str, local_path: str) -> None:
            Path(local_path).write_bytes(
                b"source-a" if remote_path.endswith("a.pdf") else b"source-b"
            )

        def info(self, path: str) -> dict[str, str]:
            return {
                "etag": '"a"' if path.endswith("a.pdf") else '"b"',
                "content_type": "application/pdf",
            }

    context = ToolContext(workspace_manager=workspace)
    context.datasources["webdav"] = _ParallelWebDavClient()
    first_persist_started = asyncio.Event()
    release_first_persist = asyncio.Event()
    persist_order: list[str] = []

    async def _persist(_path: str, anchor: dict[str, str]) -> None:
        etag = anchor["etag"]
        if etag == '"a"':
            first_persist_started.set()
            await release_first_persist.wait()
        persist_order.append(etag)

    context.cloud_anchor_persist_callback = _persist
    read = {tool.name: tool for tool in create_webdav_tools(context)}["webdav_read"]

    first = asyncio.create_task(
        read.ainvoke({"path": "/reports/a.pdf", "target": "shared.pdf"})
    )
    await asyncio.wait_for(first_persist_started.wait(), timeout=1)
    second = asyncio.create_task(
        read.ainvoke({"path": "/reports/b.pdf", "target": "shared.pdf"})
    )
    await asyncio.sleep(0.05)

    # The second write cannot overtake the first durable anchor update.
    assert backend.read_file("documents/shared.pdf", binary=True) == b"source-a"
    assert persist_order == []

    release_first_persist.set()
    await asyncio.gather(first, second)

    assert backend.read_file("documents/shared.pdf", binary=True) == b"source-b"
    assert context.get_cloud_anchor("documents/shared.pdf")["etag"] == '"b"'
    assert persist_order == ['"a"', '"b"']


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
async def test_citation_snapshot_materializes_virtual_source_bytes(
    tmp_path, monkeypatch
) -> None:
    workspace, backend = _virtual_workspace()
    backend.write_file("documents/cloud.pdf", b"%PDF anchored")
    # Reproduce the old path heuristic's collision: VirtualBackend.resolve_path
    # is an object key, and a same-named file under the agent CWD must not win.
    decoy = tmp_path / "virtual-path-test" / "documents" / "cloud.pdf"
    decoy.parent.mkdir(parents=True)
    decoy.write_bytes(b"%PDF agent-host decoy")
    monkeypatch.chdir(tmp_path)
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
    registered_bytes: list[bytes] = []

    async def _register_source(local_path: str, **_kwargs):
        registered_bytes.append(Path(local_path).read_bytes())
        return source

    citation_engine.add_doc_source = AsyncMock(side_effect=_register_source)
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

    canonical_path = backend.resolve_path("documents/cloud.pdf")
    assert context.get_cloud_anchor(canonical_path) is anchor

    result = await tools["cite_document"].ainvoke(
        {
            "text": "anchored",
            "document_path": canonical_path,
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
    assert registered_bytes == [b"%PDF anchored"]
    assert add_kwargs["metadata"] == {"cloud": anchor}
    assert decoy.read_bytes() == b"%PDF agent-host decoy"
