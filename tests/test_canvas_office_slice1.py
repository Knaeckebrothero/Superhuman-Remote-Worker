"""Canvas Office Documents Slice 1: discovery, WOPI, and view-session contracts."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from orchestrator.services.canvas import (
    CanvasRecord,
    WorkspaceFileSource,
    canonical_source_fingerprint,
)
from orchestrator.services.canvas_files import ValidatedCanvasFile
from orchestrator.services.canvas_office import (
    CanvasOfficeError,
    CollaboraConfig,
    CollaboraDiscoveryService,
    WopiAccess,
    WopiTokenService,
    wopi_file_id,
)

THREAD_ID = "a3333333-3333-3333-3333-333333333333"
USER_ID = "b4444444-4444-4444-8444-444444444444"
GENERATION = UUID("11111111-aaaa-4aaa-8aaa-111111111111")
NOW = datetime(2026, 7, 24, 12, 30, 45, 123456, tzinfo=timezone.utc)
OFFICE_BYTES = b"PK\x03\x04office-document"
SOURCE_VERSION = "sha256:" + hashlib.sha256(OFFICE_BYTES).hexdigest()
FILE_ID = wopi_file_id(THREAD_ID, "output/quarterly report.docx")


def _config(**overrides: Any) -> CollaboraConfig:
    values: dict[str, Any] = {
        "enabled": True,
        "internal_url": "http://srw-collabora:9980",
        "public_origin": "https://office.example.test",
        "wopi_base_url": "http://srw-orchestrator:8085",
        "cockpit_origin": "https://cockpit.example.test",
        "token_ttl_seconds": 36_000,
        "discovery_cache_ttl_seconds": 60,
        "request_timeout_seconds": 2.0,
    }
    values.update(overrides)
    return CollaboraConfig(**values)


def _record(path: str = "output/quarterly report.docx") -> CanvasRecord:
    source = WorkspaceFileSource(path=path, workspace_generation=GENERATION)
    return CanvasRecord(
        thread_id=THREAD_ID,
        canvas_id="main",
        source=source,
        title="Quarterly report",
        renderer="office",
        editable=False,
        alt_text=None,
        presentation_revision=4,
        source_fingerprint=canonical_source_fingerprint(source),
        source_version=SOURCE_VERSION,
        origin_generation=None,
        created_at=NOW,
        updated_at=NOW,
    )


def _thread() -> dict[str, Any]:
    return {
        "id": THREAD_ID,
        "user_id": USER_ID,
        "metadata": {
            "_workspace_binding": {
                "generation": str(GENERATION),
                "kind": "remote",
            }
        },
    }


def _user() -> dict[str, Any]:
    return {
        "id": USER_ID,
        "display_name": "Ada Lovelace",
        "is_admin": False,
        "is_approved": True,
    }


def _file() -> ValidatedCanvasFile:
    return ValidatedCanvasFile(
        path="output/quarterly report.docx",
        data=OFFICE_BYTES,
        media_type=(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ),
        renderer="office",
        source_version=SOURCE_VERSION,
        last_modified=NOW,
    )


def _discovery_xml(urlsrc: str) -> str:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<wopi-discovery>
  <net-zone name="external-https">
    <app name="writer">
      <action ext="docx" name="view" urlsrc="{urlsrc.replace("&", "&amp;")}" />
    </app>
  </net-zone>
</wopi-discovery>
"""


@pytest.mark.asyncio
async def test_discovery_cache_uses_hashed_urlsrc_and_stale_on_error() -> None:
    now = [1_000.0]
    fail = [False]
    calls: list[str] = []
    urlsrc = "https://office.example.test/browser/abc123/cool.html?"

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if fail[0]:
            return httpx.Response(503, text="unavailable")
        if request.url.path == "/hosting/discovery":
            return httpx.Response(200, text=_discovery_xml(urlsrc))
        if request.url.path == "/hosting/capabilities":
            return httpx.Response(200, json={"hasMobileSupport": True})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        discovery = CollaboraDiscoveryService(
            _config(),
            client=client,
            clock=lambda: now[0],
        )
        assert await discovery.get_urlsrc(".docx") == urlsrc
        assert discovery.available is True
        assert calls == ["/hosting/discovery", "/hosting/capabilities"]

        now[0] += 30
        assert await discovery.get_urlsrc("docx") == urlsrc
        assert calls == ["/hosting/discovery", "/hosting/capabilities"]

        now[0] += 61
        fail[0] = True
        assert await discovery.get_urlsrc("docx") == urlsrc
        assert calls[-2:] == ["/hosting/discovery", "/hosting/capabilities"]
        assert discovery.available is True


@pytest.mark.asyncio
async def test_discovery_disabled_and_cold_failure_fail_closed() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        del request
        calls += 1
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        disabled = CollaboraDiscoveryService(
            _config(enabled=False),
            client=client,
        )
        with pytest.raises(CanvasOfficeError) as error:
            await disabled.get_urlsrc("docx")
        assert error.value.code == "canvas_office_unavailable"
        assert calls == 0

        cold = CollaboraDiscoveryService(_config(), client=client)
        with pytest.raises(CanvasOfficeError) as error:
            await cold.get_urlsrc("docx")
        assert error.value.code == "canvas_office_unavailable"
        assert cold.available is False


@pytest.mark.asyncio
async def test_wopi_token_mint_validate_expiry_scope_and_live_state_recheck() -> None:
    now = [1_721_822_400]
    state = {
        "user": _user(),
        "thread": _thread(),
        "record": _record(),
    }

    async def user_loader(user_id: str):
        return state["user"] if user_id == USER_ID else None

    async def thread_loader(thread_id: str):
        return state["thread"] if thread_id == THREAD_ID else None

    async def canvas_loader(thread_id: str):
        return state["record"] if thread_id == THREAD_ID else None

    service = WopiTokenService(
        "office-token-secret-with-at-least-32-bytes",
        ttl_seconds=600,
        user_loader=user_loader,
        thread_loader=thread_loader,
        canvas_loader=canvas_loader,
        clock=lambda: now[0],
    )
    grant = service.mint(
        user_id=USER_ID,
        thread_id=THREAD_ID,
        path="output/quarterly report.docx",
        write_flag=False,
    )

    assert grant.file_id == FILE_ID
    assert grant.expires_at_ms == (now[0] + 600) * 1000
    claims = service.validate(grant.access_token)
    assert {
        key: claims[key] for key in ("sub", "tid", "path", "write_flag", "exp", "jti")
    } == {
        "sub": USER_ID,
        "tid": THREAD_ID,
        "path": "output/quarterly report.docx",
        "write_flag": False,
        "exp": now[0] + 600,
        "jti": claims["jti"],
    }

    access = await service.authenticate(
        grant.access_token,
        file_id=grant.file_id,
        require_write=False,
    )
    assert access.record == state["record"]
    assert access.user["display_name"] == "Ada Lovelace"

    with pytest.raises(CanvasOfficeError):
        await service.authenticate(
            grant.access_token,
            file_id="0" * 64,
            require_write=False,
        )

    state["record"] = replace(
        _record(),
        source=WorkspaceFileSource(
            path="output/replaced.docx",
            workspace_generation=GENERATION,
        ),
    )
    with pytest.raises(CanvasOfficeError) as replaced:
        await service.authenticate(
            grant.access_token,
            file_id=grant.file_id,
            require_write=False,
        )
    assert replaced.value.code == "wopi_access_denied"

    state["record"] = _record()
    state["thread"] = {**_thread(), "user_id": "someone-else"}
    with pytest.raises(CanvasOfficeError) as membership:
        await service.authenticate(
            grant.access_token,
            file_id=grant.file_id,
            require_write=False,
        )
    assert membership.value.code == "wopi_access_denied"

    state["thread"] = _thread()
    now[0] += 601
    with pytest.raises(CanvasOfficeError) as expired:
        service.validate(grant.access_token)
    assert expired.value.code == "wopi_token_invalid"


class _RouteTokenService:
    def __init__(self) -> None:
        self.calls = 0
        self.access = WopiAccess(
            user=_user(),
            thread=_thread(),
            record=_record(),
            claims={
                "sub": USER_ID,
                "tid": THREAD_ID,
                "path": "output/quarterly report.docx",
                "write_flag": False,
            },
        )

    async def authenticate(self, token: str, *, file_id: str, require_write: bool):
        assert token == "wopi-token"
        assert file_id == FILE_ID
        assert require_write is False
        self.calls += 1
        return self.access


class _RouteGateway:
    def __init__(self) -> None:
        self.binary_calls = 0

    async def materialize_binary(self, thread, record):
        assert thread == _thread()
        assert record == _record()
        self.binary_calls += 1
        return _file()

    async def materialize_current(self, thread, record):  # pragma: no cover
        del thread, record
        raise AssertionError("WOPI must not use the text content materializer")


def _wopi_client(monkeypatch):
    from orchestrator.routers import wopi

    tokens = _RouteTokenService()
    gateway = _RouteGateway()
    monkeypatch.setattr(wopi, "_get_token_service", lambda: tokens)
    monkeypatch.setattr(wopi, "_get_file_gateway", lambda: gateway)
    monkeypatch.setattr(wopi, "_get_collabora_config", _config)
    app = FastAPI()
    app.include_router(wopi.router)
    return TestClient(app), tokens, gateway


def test_check_file_info_shape_and_get_file_are_read_only(monkeypatch) -> None:
    client, tokens, gateway = _wopi_client(monkeypatch)
    url = f"/wopi/files/{FILE_ID}?access_token=wopi-token"

    info = client.get(url)
    assert info.status_code == 200
    assert info.headers["cache-control"] == "private, no-store"
    assert info.json() == {
        "BaseFileName": "quarterly report.docx",
        "OwnerId": USER_ID,
        "Size": len(OFFICE_BYTES),
        "UserId": USER_ID,
        "UserFriendlyName": "Ada Lovelace",
        "LastModifiedTime": "2026-07-24T12:30:45.123456Z",
        "PostMessageOrigin": "https://cockpit.example.test",
        "SupportsLocks": False,
        "UserCanNotWriteRelative": True,
    }
    assert "UserCanWrite" not in info.json()

    contents = client.get(f"{url.replace('?', '/contents?')}")
    assert contents.status_code == 200
    assert contents.content == OFFICE_BYTES
    assert contents.headers["etag"] == f'"{SOURCE_VERSION}"'
    assert contents.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    assert contents.headers["x-content-type-options"] == "nosniff"
    assert tokens.calls == 4  # live state is re-checked after each materialization
    assert gateway.binary_calls == 2


def test_wopi_router_rejects_non_put_override(monkeypatch) -> None:
    client, _, _ = _wopi_client(monkeypatch)
    response = client.post(
        f"/wopi/files/{FILE_ID}/contents?access_token=wopi-token",
        headers={"X-WOPI-Override": "DELETE"},
        content=OFFICE_BYTES,
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "wopi_override_invalid"


def test_office_session_payload_uses_discovery_and_epoch_milliseconds() -> None:
    from orchestrator.routers.canvases import _build_office_session_payload
    from orchestrator.services.canvas_office import WopiTokenGrant

    grant = WopiTokenGrant(
        access_token="signed-token",
        file_id=FILE_ID,
        expires_at_ms=1_721_858_400_000,
    )
    payload = _build_office_session_payload(
        urlsrc="https://office.example.test/browser/version-hash/cool.html?",
        wopi_base_url="http://srw-orchestrator:8085",
        grant=grant,
    )

    assert payload == {
        "urlsrc": "https://office.example.test/browser/version-hash/cool.html?",
        "WOPISrc": f"http://srw-orchestrator:8085/wopi/files/{FILE_ID}",
        "access_token": "signed-token",
        "access_token_ttl": 1_721_858_400_000,
    }
    assert isinstance(payload["access_token_ttl"], int)
    assert not str(payload["urlsrc"]).startswith(
        "https://office.example.test/browser/cool.html"
    )
