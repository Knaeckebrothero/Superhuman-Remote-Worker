"""Unit tests for the Phase 3 (D7) cloud citation snapshot-anchor.

Covers the agent-side capture + threading that makes a cloud-document citation
record its drift fingerprint and best-effort live pointer:

- ``_build_cloud_anchor`` / ``_webdav_base_url`` (src/tools/webdav/tools.py) —
  best-effort metadata capture for a downloaded cloud file (never raises).
- ``ToolContext.record_cloud_anchor`` / ``get_cloud_anchor`` — the stash keyed
  by resolved local path that bridges ``webdav_read`` → ``cite_document``.
- ``ToolContext.get_or_register_doc_source`` — forwards the anchor to
  ``engine.add_doc_source(metadata={"cloud": ...})``, explicitly or via the
  stash.

These are pure-unit (no Postgres / no model); the metadata DB round-trip is
asserted in tests/citation_engine/test_integration_postgres.py.
"""

from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.tools.context import ToolContext
from src.tools.webdav.tools import (
    _build_cloud_anchor,
    _file_sha256,
    _webdav_base_url,
)


class _FakeWebDavSettings:
    hostname = "https://cloud.example/remote.php/dav"


class _FakeClient:
    """Minimal stand-in for a webdav3 client."""

    webdav = _FakeWebDavSettings()

    def __init__(self, info=None, info_raises=False):
        self._info = info
        self._info_raises = info_raises

    def info(self, path):
        if self._info_raises:
            raise RuntimeError("PROPFIND failed")
        return self._info


# ---------------------------------------------------------------------------
# _webdav_base_url
# ---------------------------------------------------------------------------


def test_webdav_base_url_reads_hostname():
    assert _webdav_base_url(_FakeClient()) == "https://cloud.example/remote.php/dav"


def test_webdav_base_url_missing_attr_returns_empty():
    # An object without a `.webdav` settings attribute must degrade, not raise.
    assert _webdav_base_url(object()) == ""


# ---------------------------------------------------------------------------
# _file_sha256
# ---------------------------------------------------------------------------


def test_file_sha256_matches_hashlib(tmp_path):
    import hashlib

    f = tmp_path / "doc.txt"
    f.write_bytes(b"cloud bytes")
    assert _file_sha256(str(f)) == hashlib.sha256(b"cloud bytes").hexdigest()


def test_file_sha256_missing_file_returns_none():
    assert _file_sha256("/no/such/file") is None


# ---------------------------------------------------------------------------
# _build_cloud_anchor
# ---------------------------------------------------------------------------


def test_build_cloud_anchor_full(tmp_path):
    f = tmp_path / "report.pdf"
    f.write_bytes(b"%PDF-1.7 fake")
    client = _FakeClient(
        info={
            "etag": '"abc123"',
            "modified": "Wed, 01 Jan 2026 00:00:00 GMT",
            "content_type": "application/pdf",
            "size": "13",
        }
    )

    anchor = _build_cloud_anchor(client, "/Documents/report.pdf", str(f))

    assert anchor["backend"] == "webdav"
    assert anchor["path"] == "/Documents/report.pdf"
    assert anchor["webdav_url"] == (
        "https://cloud.example/remote.php/dav/Documents/report.pdf"
    )
    assert anchor["etag"] == '"abc123"'
    assert anchor["content_type"] == "application/pdf"
    assert anchor["file_sha256"]  # raw-bytes fingerprint present
    assert anchor["captured_at"]  # timestamped


def test_build_cloud_anchor_survives_info_failure(tmp_path):
    """A PROPFIND failure must not break capture — we still get a pointer + hash."""
    f = tmp_path / "report.pdf"
    f.write_bytes(b"data")
    client = _FakeClient(info_raises=True)

    anchor = _build_cloud_anchor(client, "/Documents/report.pdf", str(f))

    assert anchor["backend"] == "webdav"
    assert anchor["path"] == "/Documents/report.pdf"
    assert anchor["file_sha256"]  # still hashed the downloaded copy
    assert "etag" not in anchor  # no metadata, but no crash


# ---------------------------------------------------------------------------
# ToolContext stash + threading
# ---------------------------------------------------------------------------


def test_record_and_get_cloud_anchor_normalizes_path(tmp_path):
    ctx = ToolContext()
    f = tmp_path / "a.txt"
    f.write_text("x")
    anchor = {"etag": '"e1"', "backend": "webdav"}

    ctx.record_cloud_anchor(str(f), anchor)

    # Lookup via a non-normalized but equivalent path resolves to the same key.
    messy = str(tmp_path / "." / "a.txt")
    assert ctx.get_cloud_anchor(messy) == anchor


def test_record_cloud_anchor_ignores_empty():
    ctx = ToolContext()
    ctx.record_cloud_anchor("", {"etag": "e"})
    ctx.record_cloud_anchor("/x", {})
    assert ctx.get_cloud_anchor("/x") is None


@pytest.mark.asyncio
async def test_get_or_register_doc_source_threads_explicit_anchor():
    ctx = ToolContext()
    mock_engine = MagicMock()
    mock_source = MagicMock()
    mock_source.id = 7
    mock_engine.add_doc_source = AsyncMock(return_value=mock_source)
    ctx.citation_engine = mock_engine  # short-circuit lazy construction

    anchor = {"backend": "webdav", "etag": '"e9"'}
    sid = await ctx.get_or_register_doc_source(
        "/ws/documents/x.pdf", name="x.pdf", cloud_metadata=anchor
    )

    assert sid == 7
    mock_engine.add_doc_source.assert_awaited_once_with(
        "/ws/documents/x.pdf", name="x.pdf", metadata={"cloud": anchor}
    )


@pytest.mark.asyncio
async def test_get_or_register_doc_source_uses_stashed_anchor(tmp_path):
    """When cloud_metadata is omitted, a stashed anchor for the path is used."""
    ctx = ToolContext()
    mock_engine = MagicMock()
    mock_source = MagicMock()
    mock_source.id = 11
    mock_engine.add_doc_source = AsyncMock(return_value=mock_source)
    ctx.citation_engine = mock_engine

    f = tmp_path / "doc.txt"
    f.write_text("y")
    anchor = {"backend": "webdav", "file_sha256": "deadbeef"}
    ctx.record_cloud_anchor(str(f), anchor)

    await ctx.get_or_register_doc_source(str(f))

    _, kwargs = mock_engine.add_doc_source.call_args
    assert kwargs["metadata"] == {"cloud": anchor}


@pytest.mark.asyncio
async def test_get_or_register_doc_source_no_anchor_passes_none():
    ctx = ToolContext()
    mock_engine = MagicMock()
    mock_source = MagicMock()
    mock_source.id = 3
    mock_engine.add_doc_source = AsyncMock(return_value=mock_source)
    ctx.citation_engine = mock_engine

    await ctx.get_or_register_doc_source("/ws/documents/plain.txt")

    mock_engine.add_doc_source.assert_awaited_once_with(
        "/ws/documents/plain.txt", name=None, metadata=None
    )


# ---------------------------------------------------------------------------
# Remote-workspace materialization (regression: "stub mode" on sessions)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_or_register_doc_source_materializes_remote_file(tmp_path):
    """A workspace path that is not present on the agent's local fs (remote
    backend) must be downloaded via ``workspace.local_copy`` before the engine —
    which does local ``os.path.exists``/``fitz.open`` I/O — registers it.

    Regression for the persistent-session bug where every ``cite_document`` fell
    back to stub mode because the file lived on a separate workspace pod.
    """
    local_pdf = tmp_path / "x.pdf"
    local_pdf.write_bytes(b"%PDF-1.7 fake")

    @contextmanager
    def fake_local_copy(rel):
        # Must receive a workspace-RELATIVE path (leading slash stripped), the
        # only form the backend's resolve_path accepts.
        assert rel == "cloud/compliance/x.pdf"
        yield local_pdf

    ws = MagicMock()
    ws.workspace_relative_path.side_effect = lambda path: path.removeprefix(
        "/home/agent-host/workspace/"
    )
    ws.local_copy = MagicMock(side_effect=fake_local_copy)

    ctx = ToolContext()
    ctx.workspace_manager = ws
    mock_engine = MagicMock()
    mock_source = MagicMock()
    mock_source.id = 3
    mock_engine.add_doc_source = AsyncMock(return_value=mock_source)
    ctx.citation_engine = mock_engine

    sid = await ctx.get_or_register_doc_source(
        "/home/agent-host/workspace/cloud/compliance/x.pdf",
        name="x.pdf",
    )

    assert sid == 3
    ws.local_copy.assert_called_once_with("cloud/compliance/x.pdf")
    # The engine must receive the LOCAL materialized path, not the
    # remote/escaping one it cannot open.
    called_path = mock_engine.add_doc_source.call_args.args[0]
    assert called_path == str(local_pdf)


@pytest.mark.asyncio
async def test_get_or_register_doc_source_without_workspace_uses_local_path(tmp_path):
    """A deliberately local caller without a workspace can pass a local path."""
    local_doc = tmp_path / "report.pdf"
    local_doc.write_bytes(b"%PDF-1.7 fake")

    ctx = ToolContext()
    mock_engine = MagicMock()
    mock_source = MagicMock()
    mock_source.id = 5
    mock_engine.add_doc_source = AsyncMock(return_value=mock_source)
    ctx.citation_engine = mock_engine

    sid = await ctx.get_or_register_doc_source(str(local_doc), name="report.pdf")

    assert sid == 5
    mock_engine.add_doc_source.assert_awaited_once_with(
        str(local_doc), name="report.pdf", metadata=None
    )


@pytest.mark.asyncio
async def test_get_or_register_doc_source_ignores_agent_local_collision(tmp_path):
    """A bound workspace is authoritative even when the host path exists."""
    host_decoy = tmp_path / "documents" / "report.pdf"
    host_decoy.parent.mkdir()
    host_decoy.write_bytes(b"%PDF host decoy")
    workspace_copy = tmp_path / "materialized.pdf"
    workspace_copy.write_bytes(b"%PDF workspace authority")

    @contextmanager
    def fake_local_copy(rel):
        assert rel == "documents/report.pdf"
        yield workspace_copy

    ws = MagicMock()
    ws.workspace_relative_path.side_effect = lambda path: path
    ws.local_copy = MagicMock(side_effect=fake_local_copy)
    ctx = ToolContext(workspace_manager=ws)
    mock_engine = MagicMock()
    mock_source = MagicMock(id=9)
    mock_engine.add_doc_source = AsyncMock(return_value=mock_source)
    ctx.citation_engine = mock_engine

    sid = await ctx.get_or_register_doc_source(
        "documents/report.pdf", name="report.pdf"
    )

    assert sid == 9
    ws.local_copy.assert_called_once_with("documents/report.pdf")
    assert mock_engine.add_doc_source.await_args.args[0] == str(workspace_copy)
