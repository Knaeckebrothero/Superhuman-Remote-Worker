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


# ---------------------------------------------------------------------------
# RFC 8693 token-exchange (Phase 2 user-home impersonation mode)
# ---------------------------------------------------------------------------

TARGET_SUB = "67acc521-97eb-4238-bc05-e4e047f6ce59"


def _exchange_response(token="impersonated-user-token", expires_in=300):
    req = httpx.Request("POST", "http://kc/protocol/openid-connect/token")
    return httpx.Response(
        200,
        json={"access_token": token, "expires_in": expires_in, "token_type": "Bearer"},
        request=req,
    )


def _routed_token_post():
    """Return an AsyncMock that returns different responses per grant_type.

    The token-exchange flow hits the same endpoint twice — first with
    ``grant_type=client_credentials`` (service token), then with
    ``grant_type=urn:ietf:params:oauth:grant-type:token-exchange`` (user
    token). We route by body so a single mock covers both cases.
    """

    async def _post(_url, *, data, headers):
        grant = data.get("grant_type", "")
        if grant == "client_credentials":
            return _token_response(token="service-acct-token", expires_in=600)
        if grant == "urn:ietf:params:oauth:grant-type:token-exchange":
            return _exchange_response()
        raise AssertionError(f"unexpected grant_type={grant!r}")

    mock = AsyncMock(side_effect=_post)
    return mock


@pytest.fixture
def impersonation_sync(tmp_path: Path, monkeypatch):
    """OpenCloud sync configured for user-impersonation mode."""
    fake_dav = _FakeDav()
    monkeypatch.setattr(
        "webdav3.client.Client",
        MagicMock(return_value=fake_dav),
    )

    fake_httpx = MagicMock()
    fake_httpx.post = _routed_token_post()
    fake_httpx.aclose = AsyncMock()

    sync = OpenCloudWorkspaceSync(
        tmp_path,
        webdav_base_url="http://oc/dav/spaces/drive$home-of-target-user/",
        keycloak_issuer="http://kc/realms/srw",
        client_id="srw-orch",
        client_secret=SECRET,
        target_user_sub=TARGET_SUB,
    )
    sync._httpx = fake_httpx
    return sync, fake_dav, fake_httpx


@pytest.mark.asyncio
async def test_impersonation_token_returned_after_exchange(impersonation_sync):
    """In impersonation mode the cached token is the EXCHANGED user token,
    not the service-account one.
    """
    sync, _fake_dav, fake_httpx = impersonation_sync
    token = await sync._get_token()
    assert token == "impersonated-user-token"
    # Two POSTs on first fetch: client_credentials + token-exchange.
    assert fake_httpx.post.call_count == 2


@pytest.mark.asyncio
async def test_impersonation_exchange_request_shape(impersonation_sync):
    """Verify the token-exchange POST carries the right RFC 8693 fields."""
    sync, _fake_dav, fake_httpx = impersonation_sync
    await sync._get_token()
    exchange_call = fake_httpx.post.call_args_list[1]
    data = exchange_call.kwargs["data"]
    assert data["grant_type"] == "urn:ietf:params:oauth:grant-type:token-exchange"
    assert data["client_id"] == "srw-orch"
    assert data["client_secret"] == SECRET
    assert data["subject_token"] == "service-acct-token"
    assert data["subject_token_type"] == "urn:ietf:params:oauth:token-type:access_token"
    assert data["requested_subject"] == TARGET_SUB
    assert data["scope"] == "openid"


@pytest.mark.asyncio
async def test_impersonation_token_cached(impersonation_sync):
    """Second call within TTL returns cached token without re-exchanging."""
    sync, _fake_dav, fake_httpx = impersonation_sync
    await sync._get_token()
    await sync._get_token()
    # Still 2 total POSTs — no additional exchange.
    assert fake_httpx.post.call_count == 2


@pytest.mark.asyncio
async def test_impersonation_refresh_redoes_exchange(impersonation_sync):
    """Expired token triggers BOTH a fresh service token AND a new exchange."""
    sync, _fake_dav, fake_httpx = impersonation_sync
    await sync._get_token()
    sync._token_expires_at = 0.0  # force expiry
    await sync._get_token()
    # 4 POSTs total: (svc, exchange) × 2.
    assert fake_httpx.post.call_count == 4
    grants = [c.kwargs["data"]["grant_type"] for c in fake_httpx.post.call_args_list]
    assert grants.count("client_credentials") == 2
    assert grants.count("urn:ietf:params:oauth:grant-type:token-exchange") == 2


@pytest.mark.asyncio
async def test_impersonation_repr_marks_mode(impersonation_sync):
    sync, _fake_dav, _fake_httpx = impersonation_sync
    text = repr(sync)
    assert SECRET not in text
    assert f"impersonate_sub={TARGET_SUB}" in text


@pytest.mark.asyncio
async def test_service_account_repr_marks_mode(sync_with_mocks):
    sync, _fake_dav, _fake_httpx = sync_with_mocks
    text = repr(sync)
    assert "service-account" in text


@pytest.mark.asyncio
async def test_impersonation_401_retry_redoes_full_chain(impersonation_sync):
    """A 401 during WebDAV forces a fresh service token AND a fresh exchange."""
    sync, fake_dav, fake_httpx = impersonation_sync

    class UnauthorizedError(Exception):
        code = 401

    first = {"fired": False}
    original_upload = fake_dav.upload_sync

    def upload_once_401(**kwargs):
        if not first["fired"]:
            first["fired"] = True
            raise UnauthorizedError("401")
        return original_upload(**kwargs)

    fake_dav.upload_sync = upload_once_401
    await sync._upload_file("a.txt", "/tmp/x")
    # After 401: re-fetch service token + re-exchange. Total 4 POSTs.
    assert fake_httpx.post.call_count == 4
    assert fake_dav.uploads  # eventual success
