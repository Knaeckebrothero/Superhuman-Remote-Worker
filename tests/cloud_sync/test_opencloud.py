"""Tests for OpenCloudWorkspaceSync — token handling, retries, hygiene."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from src.services.cloud_sync.opencloud import (
    OpenCloudWorkspaceSync,
    _looks_like_401,
)


SECRET = "super-secret-client-secret"


class _FakeDav:
    def __init__(self):
        self.uploads: list = []
        self.mkdirs: list = []
        self.list_returns: list = []
        self.should_raise = None

    def upload_sync(self, **kwargs):
        if self.should_raise:
            raise self.should_raise
        self.uploads.append(kwargs)

    def mkdir(self, path):
        if self.should_raise:
            raise self.should_raise
        self.mkdirs.append(path)

    def download_sync(self, **kwargs):
        pass

    def list(self, _path, get_info=False):
        return self.list_returns


def _token_response(token="abc123", expires_in=300):
    req = httpx.Request("POST", "http://kc/protocol/openid-connect/token")
    return httpx.Response(
        200,
        json={"access_token": token, "expires_in": expires_in, "token_type": "Bearer"},
        request=req,
    )


@pytest.fixture
def sync_with_mocks(tmp_path: Path, monkeypatch):
    """OpenCloud sync with mocked httpx token fetch and webdav3 client."""
    fake_dav = _FakeDav()
    monkeypatch.setattr(
        "webdav3.client.Client",
        MagicMock(return_value=fake_dav),
    )

    fake_httpx = MagicMock()
    fake_httpx.post = AsyncMock(return_value=_token_response())
    fake_httpx.aclose = AsyncMock()

    sync = OpenCloudWorkspaceSync(
        tmp_path,
        webdav_base_url="http://oc/dav/spaces/abc/sessions/xyz/",
        keycloak_issuer="http://kc/realms/srw",
        client_id="srw-orch",
        client_secret=SECRET,
    )
    sync._httpx = fake_httpx
    return sync, fake_dav, fake_httpx


def test_looks_like_401():
    class E(Exception):
        code = 401

    assert _looks_like_401(E("x"))
    assert _looks_like_401(Exception("HTTP 401 Unauthorized"))
    assert not _looks_like_401(Exception("404 Not Found"))


@pytest.mark.asyncio
async def test_token_fetch_body_shape(sync_with_mocks):
    sync, _fake_dav, fake_httpx = sync_with_mocks
    tok = await sync._get_token()
    assert tok == "abc123"
    fake_httpx.post.assert_called_once()
    args, kwargs = fake_httpx.post.call_args
    assert args[0] == "http://kc/realms/srw/protocol/openid-connect/token"
    data = kwargs["data"]
    assert data["grant_type"] == "client_credentials"
    assert data["client_id"] == "srw-orch"
    assert data["client_secret"] == SECRET
    assert data["scope"] == "openid"


@pytest.mark.asyncio
async def test_token_cache_hit(sync_with_mocks):
    sync, _fake_dav, fake_httpx = sync_with_mocks
    await sync._get_token()
    await sync._get_token()
    assert fake_httpx.post.call_count == 1


@pytest.mark.asyncio
async def test_token_refresh_after_expiry(sync_with_mocks, monkeypatch):
    sync, _fake_dav, fake_httpx = sync_with_mocks
    await sync._get_token()
    # Jump past the stored expiry
    sync._token_expires_at = 0.0
    await sync._get_token()
    assert fake_httpx.post.call_count == 2


@pytest.mark.asyncio
async def test_401_retry_forces_refresh(sync_with_mocks):
    sync, fake_dav, fake_httpx = sync_with_mocks

    class UnauthorizedError(Exception):
        code = 401

    first_call = {"fired": False}

    original_upload = fake_dav.upload_sync

    def upload_once_401(**kwargs):
        if not first_call["fired"]:
            first_call["fired"] = True
            raise UnauthorizedError("401")
        return original_upload(**kwargs)

    fake_dav.upload_sync = upload_once_401

    await sync._upload_file("a.txt", "/tmp/x")
    # Token fetched twice: once initial, once after 401 force-refresh
    assert fake_httpx.post.call_count == 2
    assert fake_dav.uploads  # eventual success


@pytest.mark.asyncio
async def test_repr_masks_secret(sync_with_mocks):
    sync, _fake_dav, _fake_httpx = sync_with_mocks
    text = repr(sync)
    assert SECRET not in text
    assert "secret=***" in text


@pytest.mark.asyncio
async def test_aclose_drops_secrets(sync_with_mocks):
    sync, _fake_dav, fake_httpx = sync_with_mocks
    await sync._get_token()
    assert sync._access_token == "abc123"
    await sync.aclose()
    assert sync._access_token is None
    assert sync._client_secret == ""
    assert sync._dav_client is None
    fake_httpx.aclose.assert_called_once()
