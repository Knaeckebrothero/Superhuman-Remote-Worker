"""Tests for NextcloudWorkspaceSync — transport delegation."""

from __future__ import annotations

from pathlib import Path
import pytest

from agent.services.cloud_sync.nextcloud_sync import NextcloudWorkspaceSync


class _FakeWebDavClient:
    """Records calls webdav3 would otherwise make to a real server."""

    def __init__(self):
        self.uploads: list[dict] = []
        self.mkdirs: list[str] = []
        self.downloads: list[dict] = []
        self.deletes: list[str] = []
        self.delete_error: Exception | None = None
        self.list_returns: list = []

    def upload_sync(self, **kwargs):
        self.uploads.append(kwargs)

    # Commit-then-effects (step 4a): uploads/deletes go through
    # execute_request so RFC 4918 preconditions can be sent and the new ETag
    # read back. `requests` records (action, path, headers_ext).
    requests: list[tuple] = []
    request_error: Exception | None = None
    etag: str = '"etag-1"'

    def execute_request(self, action, path, data=None, headers_ext=None):
        self.requests.append((action, path, headers_ext))
        if action == "upload":
            self.uploads.append({"path": path, "bytes": data.read() if data else b""})
        if action == "clean":
            self.deletes.append(path)
            if self.delete_error is not None:
                raise self.delete_error
        if self.request_error is not None:
            raise self.request_error

        class _Response:
            headers = {"ETag": self.etag}

        return _Response()

    def info(self, path):
        return {"etag": self.etag}

    def mkdir(self, path):
        self.mkdirs.append(path)

    def download_sync(self, **kwargs):
        self.downloads.append(kwargs)

    def clean(self, path):
        self.deletes.append(path)
        if self.delete_error is not None:
            raise self.delete_error

    def list(self, _path, get_info=False):
        return self.list_returns


@pytest.fixture
def fake_client_factory(monkeypatch):
    fake = _FakeWebDavClient()
    monkeypatch.setattr(NextcloudWorkspaceSync, "_get_client", lambda _self: fake)
    return fake


@pytest.mark.asyncio
async def test_upload_delegates(tmp_path: Path, fake_client_factory):
    sync = NextcloudWorkspaceSync(
        tmp_path,
        webdav_url="http://nc/remote.php/dav/files/agent/sess/",
        webdav_user="agent",
        webdav_password="pw",
    )
    local = tmp_path / "local.txt"
    local.write_bytes(b"payload")
    etag = await sync._upload_file("foo/bar.txt", str(local))
    assert etag == '"etag-1"'
    assert fake_client_factory.uploads == [
        {"path": "/foo/bar.txt", "bytes": b"payload"}
    ]
    assert fake_client_factory.requests == [("upload", "/foo/bar.txt", None)]


@pytest.mark.asyncio
async def test_upload_sends_preconditions_and_maps_412_to_fence_lost(
    tmp_path: Path, fake_client_factory
):
    from agent.services.cloud_sync.base import CloudSyncFenceLost

    sync = NextcloudWorkspaceSync(
        tmp_path,
        webdav_url="http://nc/remote.php/dav/files/agent/sess/",
        webdav_user="agent",
        webdav_password="pw",
    )
    local = tmp_path / "local.txt"
    local.write_bytes(b"payload")
    await sync._upload_file("a.txt", str(local), if_match='"e0"')
    await sync._upload_file("b.txt", str(local), if_none_match=True)
    assert fake_client_factory.requests[-2:] == [
        ("upload", "/a.txt", ['If-Match: "e0"']),
        ("upload", "/b.txt", ["If-None-Match: *"]),
    ]

    class ResponseErrorCode(Exception):
        def __init__(self, code):
            self.code = code

    fake_client_factory.request_error = ResponseErrorCode(412)
    with pytest.raises(CloudSyncFenceLost):
        await sync._upload_file("c.txt", str(local), if_match='"stale"')
    with pytest.raises(CloudSyncFenceLost):
        await sync._delete_remote_file("c.txt", if_match='"stale"')
    fake_client_factory.request_error = ResponseErrorCode(500)
    with pytest.raises(ResponseErrorCode):
        await sync._upload_file("d.txt", str(local))
    assert await sync._remote_etag("a.txt") == '"etag-1"'


@pytest.mark.asyncio
async def test_mkdir_delegates(tmp_path: Path, fake_client_factory):
    sync = NextcloudWorkspaceSync(
        tmp_path,
        webdav_url="http://nc/remote.php/dav/files/agent/sess/",
        webdav_user="agent",
        webdav_password="pw",
    )
    await sync._ensure_remote_dir("a/b")
    assert fake_client_factory.mkdirs == ["a/b"]


@pytest.mark.asyncio
async def test_list_strips_base_path(tmp_path: Path, fake_client_factory):
    fake_client_factory.list_returns = [
        {
            "path": "/remote.php/dav/files/agent/sess/a.txt",
            "etag": "v1",
            "isdir": False,
        },
        {
            "path": "/remote.php/dav/files/agent/sess/dir/",
            "etag": "",
            "isdir": True,
        },
    ]
    sync = NextcloudWorkspaceSync(
        tmp_path,
        webdav_url="http://nc/remote.php/dav/files/agent/sess/",
        webdav_user="agent",
        webdav_password="pw",
    )
    out = await sync._list_remote_files()
    rels = [item["path"] for item in out]
    assert "a.txt" in rels
    assert "dir/" in rels or "dir" in rels


@pytest.mark.asyncio
async def test_download_delegates(tmp_path: Path, fake_client_factory):
    sync = NextcloudWorkspaceSync(
        tmp_path,
        webdav_url="http://nc/remote.php/dav/files/agent/sess/",
        webdav_user="agent",
        webdav_password="pw",
    )
    await sync._download_file("a.txt", "/tmp/out.txt")
    assert fake_client_factory.downloads == [
        {"remote_path": "a.txt", "local_path": "/tmp/out.txt"}
    ]


@pytest.mark.asyncio
async def test_generation_delete_is_idempotent_when_resource_already_missing(
    tmp_path: Path, fake_client_factory
):
    class RemoteResourceNotFound(Exception):
        pass

    fake_client_factory.delete_error = RemoteResourceNotFound("already gone")
    sync = NextcloudWorkspaceSync(
        tmp_path,
        webdav_url="http://nc/remote.php/dav/files/agent/sess/",
        webdav_user="agent",
        webdav_password="pw",
    )
    before_write_calls = 0

    async def before_write():
        nonlocal before_write_calls
        before_write_calls += 1

    await sync._delete_remote_file("deleted.txt", before_write=before_write)

    assert fake_client_factory.deletes == ["/deleted.txt"]
    assert before_write_calls == 1
