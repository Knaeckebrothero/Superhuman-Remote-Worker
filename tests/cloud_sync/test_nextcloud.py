"""Tests for NextcloudWorkspaceSync — transport delegation."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.services.cloud_sync.nextcloud import NextcloudWorkspaceSync


class _FakeWebDavClient:
    """Records calls webdav3 would otherwise make to a real server."""

    def __init__(self):
        self.uploads: list[dict] = []
        self.mkdirs: list[str] = []
        self.downloads: list[dict] = []
        self.list_returns: list = []

    def upload_sync(self, **kwargs):
        self.uploads.append(kwargs)

    def mkdir(self, path):
        self.mkdirs.append(path)

    def download_sync(self, **kwargs):
        self.downloads.append(kwargs)

    def list(self, _path, get_info=False):
        return self.list_returns


@pytest.fixture
def fake_client_factory(monkeypatch):
    fake = _FakeWebDavClient()
    monkeypatch.setattr(
        "webdav3.client.Client",
        MagicMock(return_value=fake),
    )
    return fake


@pytest.mark.asyncio
async def test_upload_delegates(tmp_path: Path, fake_client_factory):
    sync = NextcloudWorkspaceSync(
        tmp_path,
        webdav_url="http://nc/remote.php/dav/files/agent/sess/",
        webdav_user="agent",
        webdav_password="pw",
    )
    await sync._upload_file("foo/bar.txt", "/tmp/local.txt")
    assert fake_client_factory.uploads == [
        {"remote_path": "foo/bar.txt", "local_path": "/tmp/local.txt"}
    ]


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
