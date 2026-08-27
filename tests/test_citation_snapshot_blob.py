"""Unit tests for the Phase 3b (D7) original-bytes snapshot path.

The agent has no S3 credentials, so a cited cloud document's original bytes are
round-tripped through the orchestrator into the `srw-snapshots` blob store:

- ``SnapshotService.save_blob`` / ``get_blob`` (orchestrator) — content-addressed
  put (HEAD-dedup) + get.
- ``OrchestratorClient.save_citation_snapshot`` (agent) — POSTs the bytes to
  ``/api/citations/snapshot`` and returns the key.
- ``ToolContext.snapshot_cloud_source_bytes`` — reads the file, uploads via the
  client, and writes the key back onto the anchor in place (best-effort).

The live endpoint + real MinIO round-trip is exercised on k3d (see the
integration verification in the citation-engine integration doc).
"""

import hashlib
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from src.api.orchestrator_client import OrchestratorClient
from src.tools.context import ToolContext


# ---------------------------------------------------------------------------
# SnapshotService.save_blob / get_blob (orchestrator side)
# ---------------------------------------------------------------------------


def _make_service():
    from orchestrator.services.snapshot_service import SnapshotService

    svc = SnapshotService()
    svc._s3 = MagicMock()
    svc._bucket = "srw-snapshots"
    svc._available = True
    return svc


@pytest.mark.asyncio
async def test_save_blob_puts_when_absent():
    from botocore.exceptions import ClientError

    svc = _make_service()
    # HEAD says "missing" → the PUT must happen.
    svc._s3.head_object.side_effect = ClientError(
        {"Error": {"Code": "404"}}, "HeadObject"
    )

    data = b"%PDF-1.7 original bytes"
    sha = hashlib.sha256(data).hexdigest()
    key = await svc.save_blob(data, content_type="application/pdf")

    assert key == f"citations/{sha[:2]}/{sha}"
    svc._s3.put_object.assert_called_once()
    _, kwargs = svc._s3.put_object.call_args
    assert kwargs["Key"] == key
    assert kwargs["Body"] == data
    assert kwargs["ContentType"] == "application/pdf"


@pytest.mark.asyncio
async def test_save_blob_skips_put_when_present():
    svc = _make_service()
    svc._s3.head_object.return_value = {"ContentLength": 5}  # exists → no PUT

    data = b"already there"
    sha = hashlib.sha256(data).hexdigest()
    key = await svc.save_blob(data)

    assert key == f"citations/{sha[:2]}/{sha}"
    svc._s3.put_object.assert_not_called()


@pytest.mark.asyncio
async def test_save_blob_unavailable_or_empty_returns_none():
    svc = _make_service()
    svc._available = False
    assert await svc.save_blob(b"x") is None

    svc._available = True
    assert await svc.save_blob(b"") is None


@pytest.mark.asyncio
async def test_get_blob_round_trips_bytes():
    svc = _make_service()
    body = MagicMock()
    body.read.return_value = b"the original"
    svc._s3.get_object.return_value = {"Body": body}

    assert await svc.get_blob("citations/ab/abcd") == b"the original"
    svc._s3.get_object.assert_called_once_with(
        Bucket="srw-snapshots", Key="citations/ab/abcd"
    )


@pytest.mark.asyncio
async def test_get_blob_missing_returns_none():
    svc = _make_service()
    svc._s3.get_object.side_effect = Exception("NoSuchKey")
    assert await svc.get_blob("citations/zz/none") is None


# ---------------------------------------------------------------------------
# OrchestratorClient.save_citation_snapshot (agent side)
# ---------------------------------------------------------------------------


def _make_client():
    return OrchestratorClient(
        orchestrator_url="http://orch:8085",
        pod_ip="",
        pod_port=0,
        hostname="",
        config_name="",
    )


@pytest.mark.asyncio
async def test_save_citation_snapshot_returns_key_on_200():
    client = _make_client()
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"snapshot_blob_key": "citations/ab/abcd", "size_bytes": 9}
    client._client = MagicMock()
    client._client.post = AsyncMock(return_value=resp)

    key = await client.save_citation_snapshot(
        b"some data", content_type="application/pdf"
    )

    assert key == "citations/ab/abcd"
    _, kwargs = client._client.post.call_args
    assert kwargs["content"] == b"some data"
    assert kwargs["params"] == {"content_type": "application/pdf"}


@pytest.mark.asyncio
async def test_save_citation_snapshot_none_on_error_status():
    client = _make_client()
    resp = MagicMock()
    resp.status_code = 503
    resp.text = "Snapshot store unavailable"
    client._client = MagicMock()
    client._client.post = AsyncMock(return_value=resp)

    assert await client.save_citation_snapshot(b"x") is None


@pytest.mark.asyncio
async def test_save_citation_snapshot_none_on_network_error():
    client = _make_client()
    client._client = MagicMock()
    client._client.post = AsyncMock(side_effect=httpx.RequestError("boom"))

    assert await client.save_citation_snapshot(b"x") is None


@pytest.mark.asyncio
async def test_save_citation_snapshot_none_on_empty():
    client = _make_client()
    client._client = MagicMock()
    client._client.post = AsyncMock()

    assert await client.save_citation_snapshot(b"") is None
    client._client.post.assert_not_called()


# ---------------------------------------------------------------------------
# ToolContext.snapshot_cloud_source_bytes (cite-time wiring)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_snapshot_cloud_source_bytes_uploads_and_mutates(tmp_path):
    ctx = ToolContext()
    mock_client = MagicMock()
    mock_client.save_citation_snapshot = AsyncMock(return_value="citations/de/deadbeef")
    ctx.orchestrator_client = mock_client

    f = tmp_path / "doc.pdf"
    f.write_bytes(b"%PDF original")
    anchor = {"backend": "webdav", "content_type": "application/pdf"}

    key = await ctx.snapshot_cloud_source_bytes(str(f), anchor)

    assert key == "citations/de/deadbeef"
    assert anchor["snapshot_blob_key"] == "citations/de/deadbeef"
    args, kwargs = mock_client.save_citation_snapshot.call_args
    assert args[0] == b"%PDF original"
    assert kwargs["content_type"] == "application/pdf"


@pytest.mark.asyncio
async def test_snapshot_cloud_source_bytes_short_circuits_when_keyed():
    ctx = ToolContext()
    mock_client = MagicMock()
    mock_client.save_citation_snapshot = AsyncMock()
    ctx.orchestrator_client = mock_client

    anchor = {"snapshot_blob_key": "citations/ex/existing"}
    key = await ctx.snapshot_cloud_source_bytes("/irrelevant", anchor)

    assert key == "citations/ex/existing"
    mock_client.save_citation_snapshot.assert_not_awaited()


@pytest.mark.asyncio
async def test_snapshot_cloud_source_bytes_no_client_returns_none(tmp_path):
    ctx = ToolContext()  # orchestrator_client defaults to None
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"data")

    assert await ctx.snapshot_cloud_source_bytes(str(f), {"backend": "webdav"}) is None


@pytest.mark.asyncio
async def test_snapshot_cloud_source_bytes_missing_file_returns_none():
    ctx = ToolContext()
    mock_client = MagicMock()
    mock_client.save_citation_snapshot = AsyncMock(return_value="k")
    ctx.orchestrator_client = mock_client

    key = await ctx.snapshot_cloud_source_bytes(
        "/no/such/file.pdf", {"backend": "webdav"}
    )

    assert key is None
    mock_client.save_citation_snapshot.assert_not_awaited()
