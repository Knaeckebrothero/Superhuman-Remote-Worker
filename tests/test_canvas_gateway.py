from __future__ import annotations

import asyncio
from collections import deque
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
import re
from uuid import uuid4

import httpx
import pytest

from canvas_gateway import CanvasGatewayApp
from services.canvas import CanvasRecord, WorkspaceAppSource
from services.canvas_session_notifications import (
    CanvasConnectionLease,
    CanvasConnectionRegistry,
)
from services.canvas_ssh import RemoteWorkspaceTarget
from services.canvas_viewer_config import CanvasViewerConfig
from services.canvas_viewer_sessions import (
    CanvasBootstrapExchange,
    CanvasBootstrapStart,
    CanvasOriginSession,
    canvas_bootstrap_cookie_name,
)


class _FakeReader:
    def __init__(self, response: bytes):
        self._chunks = deque([response])

    async def read(self, _: int) -> bytes:
        return self._chunks.popleft() if self._chunks else b""


class _FakeWriter:
    def __init__(self):
        self.data = bytearray()
        self.closed = False

    def write(self, value: bytes) -> None:
        self.data.extend(value)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


class _FakeTransportPool:
    def __init__(self, response: bytes):
        self.reader = _FakeReader(response)
        self.writer = _FakeWriter()
        self.calls = []

    @asynccontextmanager
    async def open_loopback_connection(self, **kwargs):
        self.calls.append(kwargs)
        yield self.reader, self.writer

    async def close_all(self) -> None:
        return None


class _FakeDB:
    def __init__(self, thread: dict):
        self.thread = thread
        self.thread_queries: list[tuple[str, object]] = []

    @asynccontextmanager
    async def acquire(self):
        yield self

    async def fetchrow(self, query: str, thread_id: object):
        normalized = " ".join(query.split())
        assert normalized == ("SELECT id, user_id, metadata FROM threads WHERE id = $1")
        assert str(thread_id) == str(self.thread["id"])
        self.thread_queries.append((normalized, thread_id))
        return self.thread


class _FakeSessions:
    def __init__(self, session: CanvasOriginSession):
        self.session = session
        self.attachment_id = uuid4()
        self.challenge = "c" * 43
        self.ready_receipt = "r" * 43
        self.browser_binding = "b" * 43
        self.exchange_code = "e" * 43
        self.cookie_expires_at = session.expires_at + timedelta(hours=1)
        self.bootstrap_calls = []
        self.exchange_calls = []
        self.authenticate_calls = []

    async def begin_bootstrap(self, **kwargs):
        self.bootstrap_calls.append(kwargs)
        return CanvasBootstrapStart(
            attachment_id=self.attachment_id,
            challenge=self.challenge,
            ready_receipt=self.ready_receipt,
            browser_binding=self.browser_binding,
            embedding_origin=self.session.embedding_origin,
            expires_at=datetime.now(UTC) + timedelta(seconds=60),
        )

    async def exchange_bootstrap(self, **kwargs):
        self.exchange_calls.append(kwargs)
        source = self.session.record.source
        assert isinstance(source, WorkspaceAppSource)
        return CanvasBootstrapExchange(
            session=self.session,
            entry_path=source.entry_path,
            session_secret="n" * 43,
            cookie_expires_at=self.cookie_expires_at,
        )

    async def authenticate(self, **kwargs):
        self.authenticate_calls.append(kwargs)
        return self.session


def _fixture():
    origin_generation = uuid4()
    workspace_generation = uuid4()
    thread_id = uuid4()
    user_id = uuid4()
    now = datetime.now(UTC)
    source = WorkspaceAppSource(
        entry_port=8501,
        entry_path="/demo",
        workspace_generation=workspace_generation,
    )
    record = CanvasRecord(
        thread_id=str(thread_id),
        canvas_id="main",
        source=source,
        title="Demo",
        renderer="auto",
        editable=False,
        alt_text=None,
        presentation_revision=1,
        source_fingerprint="sha256:" + "a" * 64,
        source_version=None,
        origin_generation=origin_generation,
        created_at=now,
        updated_at=now,
    )
    thread = {"id": str(thread_id), "user_id": str(user_id), "metadata": {}}
    target = RemoteWorkspaceTarget(
        thread_id=str(thread_id),
        generation=workspace_generation,
        host="workspace.internal",
        port=22,
        fingerprint="SHA256:" + "a" * 43,
    )
    session = CanvasOriginSession(
        id=uuid4(),
        user_id=user_id,
        thread_id=thread_id,
        canvas_id="main",
        parent_srw_session_id=uuid4(),
        source_fingerprint=record.source_fingerprint or "",
        workspace_generation=workspace_generation,
        origin_generation=origin_generation,
        embedding_origin="https://cockpit.platform.test",
        cookie_mode="psl-isolated",
        expires_at=now + timedelta(minutes=15),
        record=record,
        thread=thread,
        remote_target=target,
    )
    config = CanvasViewerConfig(
        enabled=True,
        host_suffix=".canvas.user-content.test",
        cookie_mode="psl-isolated",
        deployment_profile="production",
        cockpit_origins=frozenset({"https://cockpit.platform.test"}),
        session_ttl_seconds=900,
        bootstrap_ttl_seconds=60,
        attachment_ttl_seconds=1200,
        revalidate_seconds=15,
    )
    sessions = _FakeSessions(session)
    return config, session, sessions, _FakeDB(thread)


def _client(app: CanvasGatewayApp, session: CanvasOriginSession):
    origin = f"https://{session.origin_generation}.canvas.user-content.test"
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=origin,
        headers={"Sec-Fetch-Site": "same-origin"},
    )


@pytest.mark.asyncio
async def test_bootstrap_requires_iframe_metadata_before_challenge_start() -> None:
    config, session, sessions, db = _fixture()
    app = CanvasGatewayApp(
        db=db,
        config=config,
        session_service=sessions,  # type: ignore[arg-type]
        registry=CanvasConnectionRegistry(max_connections=4),
    )
    async with _client(app, session) as client:
        response = await client.get(
            f"/_canvas/bootstrap?attachment_id={sessions.attachment_id}",
            headers={"Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate"},
        )
    assert response.status_code == 403
    assert sessions.bootstrap_calls == []
    assert response.headers["cache-control"] == "private, no-store"


@pytest.mark.asyncio
async def test_bootstrap_serves_bound_challenge_and_only_transient_cookie() -> None:
    config, session, sessions, db = _fixture()
    app = CanvasGatewayApp(
        db=db,
        config=config,
        session_service=sessions,  # type: ignore[arg-type]
        registry=CanvasConnectionRegistry(max_connections=4),
    )
    async with _client(app, session) as client:
        response = await client.get(
            f"/_canvas/bootstrap?attachment_id={sessions.attachment_id}",
            headers={
                "Sec-Fetch-Dest": "iframe",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "cross-site",
            },
            follow_redirects=False,
        )
    assert response.status_code == 200
    assert "location" not in response.headers
    assert str(sessions.attachment_id) in response.text
    assert sessions.challenge in response.text
    assert sessions.ready_receipt in response.text
    assert session.embedding_origin in response.text
    assert sessions.browser_binding not in response.text
    assert sessions.exchange_code not in response.text
    assert "bridge_nonce" not in response.text
    assert "token" not in response.text
    assert 'fetch("/_canvas/exchange"' in response.text
    assert "canvas_browser_storage_unavailable" in response.text
    assert "event.source!==parent" in response.text
    assert "event.origin!==bootstrap.parentOrigin" in response.text
    assert "bootstrap.parentOrigin" in response.text
    nonce = re.search(
        r"script-src 'nonce-([A-Za-z0-9_-]+)'",
        response.headers["content-security-policy"],
    )
    assert nonce is not None
    assert f'nonce="{nonce.group(1)}"' in response.text
    assert "connect-src 'self'" in response.headers["content-security-policy"]
    cookies = response.headers.get_list("set-cookie")
    assert len(cookies) == 1
    cookie = cookies[0]
    bootstrap_cookie = canvas_bootstrap_cookie_name(sessions.attachment_id)
    assert cookie.startswith(f"{bootstrap_cookie}={sessions.browser_binding}")
    assert "__Host-canvas_session=" not in cookie
    assert "Secure" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=None" in cookie
    assert "Partitioned" in cookie
    max_age = int(cookie.split("Max-Age=", 1)[1].split(";", 1)[0])
    assert 0 < max_age <= config.bootstrap_ttl_seconds
    assert len(sessions.bootstrap_calls) == 1
    assert sessions.bootstrap_calls[0] == {
        "attachment_id": sessions.attachment_id,
        "host_generation": session.origin_generation,
    }


@pytest.mark.asyncio
async def test_same_origin_exchange_sets_viewer_and_clears_transient_cookie() -> None:
    config, session, sessions, db = _fixture()
    app = CanvasGatewayApp(
        db=db,
        config=config,
        session_service=sessions,  # type: ignore[arg-type]
        registry=CanvasConnectionRegistry(max_connections=4),
    )
    origin = f"https://{session.origin_generation}.canvas.user-content.test"
    bootstrap_cookie = canvas_bootstrap_cookie_name(sessions.attachment_id)
    async with _client(app, session) as client:
        response = await client.post(
            "/_canvas/exchange",
            json={
                "attachment_id": str(sessions.attachment_id),
                "challenge": sessions.challenge,
                "exchange_code": sessions.exchange_code,
            },
            headers={
                "Cookie": (
                    f"{bootstrap_cookie}={sessions.browser_binding}; "
                    f"__Host-canvas_session={'s' * 43}"
                ),
                "Origin": origin,
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-origin",
            },
        )

    assert response.status_code == 200
    assert response.json() == {"entry_path": "/demo"}
    assert sessions.challenge not in response.text
    assert sessions.exchange_code not in response.text
    assert len(sessions.exchange_calls) == 1
    assert sessions.exchange_calls[0] == {
        "attachment_id": sessions.attachment_id,
        "challenge": sessions.challenge,
        "exchange_code": sessions.exchange_code,
        "browser_binding": sessions.browser_binding,
        "host_generation": session.origin_generation,
        "existing_session_secret": "s" * 43,
    }
    cookies = response.headers.get_list("set-cookie")
    assert len(cookies) == 2
    assert any(
        cookie.startswith(f"{bootstrap_cookie}=;") and "Max-Age=0" in cookie
        for cookie in cookies
    )
    viewer = next(
        cookie for cookie in cookies if cookie.startswith("__Host-canvas_session=")
    )
    assert viewer.startswith("__Host-canvas_session=" + "n" * 43)
    for attribute in ("Path=/", "Secure", "HttpOnly", "SameSite=None", "Partitioned"):
        assert attribute in viewer


@pytest.mark.asyncio
async def test_exchange_reports_browser_storage_when_partitioned_binding_is_missing() -> (
    None
):
    config, session, sessions, db = _fixture()
    app = CanvasGatewayApp(
        db=db,
        config=config,
        session_service=sessions,  # type: ignore[arg-type]
        registry=CanvasConnectionRegistry(max_connections=4),
    )
    origin = f"https://{session.origin_generation}.canvas.user-content.test"
    async with _client(app, session) as client:
        response = await client.post(
            "/_canvas/exchange",
            json={
                "attachment_id": str(sessions.attachment_id),
                "challenge": sessions.challenge,
                "exchange_code": sessions.exchange_code,
            },
            headers={
                "Origin": origin,
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-origin",
            },
        )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == ("canvas_browser_storage_unavailable")
    assert sessions.exchange_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("headers", "expected_status"),
    [
        (
            {
                "Origin": "https://attacker.test",
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-origin",
            },
            403,
        ),
        (
            {
                "Origin": "https://placeholder.invalid",
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "cross-site",
            },
            403,
        ),
    ],
)
async def test_exchange_rejects_non_same_origin_context_before_authorization(
    headers: dict[str, str], expected_status: int
) -> None:
    config, session, sessions, db = _fixture()
    app = CanvasGatewayApp(
        db=db,
        config=config,
        session_service=sessions,  # type: ignore[arg-type]
        registry=CanvasConnectionRegistry(max_connections=4),
    )
    origin = f"https://{session.origin_generation}.canvas.user-content.test"
    request_headers = dict(headers)
    if request_headers["Origin"] == "https://placeholder.invalid":
        request_headers["Origin"] = origin
    async with _client(app, session) as client:
        response = await client.post(
            "/_canvas/exchange",
            json={
                "attachment_id": str(sessions.attachment_id),
                "challenge": sessions.challenge,
                "exchange_code": sessions.exchange_code,
            },
            headers=request_headers,
        )

    assert response.status_code == expected_status
    assert sessions.exchange_calls == []
    assert "set-cookie" not in response.headers


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["extra_field", "wrong_attachment_cookie"])
async def test_exchange_requires_exact_schema_and_attachment_browser_binding(
    failure: str,
) -> None:
    config, session, sessions, db = _fixture()
    app = CanvasGatewayApp(
        db=db,
        config=config,
        session_service=sessions,  # type: ignore[arg-type]
        registry=CanvasConnectionRegistry(max_connections=4),
    )
    origin = f"https://{session.origin_generation}.canvas.user-content.test"
    payload = {
        "attachment_id": str(sessions.attachment_id),
        "challenge": sessions.challenge,
        "exchange_code": sessions.exchange_code,
    }
    if failure == "extra_field":
        payload["origin"] = origin
    cookie_attachment = (
        uuid4() if failure == "wrong_attachment_cookie" else sessions.attachment_id
    )
    async with _client(app, session) as client:
        response = await client.post(
            "/_canvas/exchange",
            json=payload,
            headers={
                "Cookie": (
                    f"{canvas_bootstrap_cookie_name(cookie_attachment)}="
                    f"{sessions.browser_binding}"
                ),
                "Origin": origin,
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-origin",
            },
        )

    assert response.status_code == (400 if failure == "extra_field" else 409)
    assert sessions.exchange_calls == []
    assert "set-cookie" not in response.headers


@pytest.mark.asyncio
async def test_bootstrap_rejects_duplicate_fetch_metadata() -> None:
    config, session, sessions, db = _fixture()
    app = CanvasGatewayApp(
        db=db,
        config=config,
        session_service=sessions,  # type: ignore[arg-type]
        registry=CanvasConnectionRegistry(max_connections=4),
    )
    async with _client(app, session) as client:
        response = await client.get(
            f"/_canvas/bootstrap?attachment_id={sessions.attachment_id}",
            headers=[
                ("Sec-Fetch-Dest", "iframe"),
                ("Sec-Fetch-Dest", "iframe"),
                ("Sec-Fetch-Mode", "navigate"),
                ("Sec-Fetch-Site", "cross-site"),
            ],
        )

    assert response.status_code == 400
    assert sessions.bootstrap_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    [
        "token=" + "t" * 43,
        "attachment_id=7F2640CB-8584-4AB1-A68E-95B2C9274419",
        "attachment_id=7f2640cb-8584-4ab1-a68e-95b2c9274419&token=secret",
    ],
)
async def test_bootstrap_rejects_credentials_and_noncanonical_locator(
    query: str,
) -> None:
    config, session, sessions, db = _fixture()
    app = CanvasGatewayApp(
        db=db,
        config=config,
        session_service=sessions,  # type: ignore[arg-type]
        registry=CanvasConnectionRegistry(max_connections=4),
    )
    async with _client(app, session) as client:
        response = await client.get(
            f"/_canvas/bootstrap?{query}",
            headers={
                "Sec-Fetch-Dest": "iframe",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "cross-site",
            },
        )

    assert response.status_code == 400
    assert sessions.bootstrap_calls == []
    assert "set-cookie" not in response.headers


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("destination", "mode"),
    [("image", "no-cors"), ("iframe", "navigate")],
)
async def test_legacy_unpartitioned_cookie_cannot_authorize_cross_site_requests(
    destination: str, mode: str
) -> None:
    config, session, sessions, db = _fixture()
    app = CanvasGatewayApp(
        db=db,
        config=config,
        session_service=sessions,  # type: ignore[arg-type]
        registry=CanvasConnectionRegistry(max_connections=4),
    )
    async with _client(app, session) as client:
        response = await client.get(
            "/side-effecting-get",
            headers={
                "Cookie": "__Host-canvas_session=" + "s" * 43,
                "Sec-Fetch-Dest": destination,
                "Sec-Fetch-Mode": mode,
                "Sec-Fetch-Site": "cross-site",
            },
        )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "canvas_embedding_required"
    assert sessions.authenticate_calls == []


@pytest.mark.asyncio
async def test_canvas_gateway_has_no_platform_route_fallback() -> None:
    config, session, sessions, db = _fixture()
    app = CanvasGatewayApp(
        db=db,
        config=config,
        session_service=sessions,  # type: ignore[arg-type]
        registry=CanvasConnectionRegistry(max_connections=4),
    )
    async with _client(app, session) as client:
        response = await client.get("/api/auth/me")
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "canvas_session_required"
    assert sessions.authenticate_calls == []


@pytest.mark.asyncio
async def test_ordinary_top_level_navigation_is_rejected_before_authentication() -> (
    None
):
    config, session, sessions, db = _fixture()
    app = CanvasGatewayApp(
        db=db,
        config=config,
        session_service=sessions,  # type: ignore[arg-type]
        registry=CanvasConnectionRegistry(max_connections=4),
    )
    async with _client(app, session) as client:
        response = await client.get(
            "/demo",
            headers={
                "Cookie": "__Host-canvas_session=" + "s" * 43,
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
            },
        )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "canvas_embedding_required"
    assert sessions.authenticate_calls == []


@pytest.mark.asyncio
async def test_post_auth_capacity_error_does_not_clear_shared_cookie() -> None:
    config, session, sessions, db = _fixture()
    registry = CanvasConnectionRegistry(max_connections=1)
    app = CanvasGatewayApp(
        db=db,
        config=config,
        session_service=sessions,  # type: ignore[arg-type]
        registry=registry,
    )
    async with registry.register(uuid4()):
        async with _client(app, session) as client:
            response = await client.get(
                "/demo",
                headers={"Cookie": "__Host-canvas_session=" + "s" * 43},
            )
    assert response.status_code == 503
    assert "set-cookie" not in response.headers


@pytest.mark.asyncio
async def test_revalidation_and_disconnect_guards_fail_closed() -> None:
    config, session, sessions, db = _fixture()
    config = CanvasViewerConfig(
        enabled=True,
        host_suffix=config.host_suffix,
        cookie_mode=config.cookie_mode,
        deployment_profile=config.deployment_profile,
        cockpit_origins=config.cockpit_origins,
        session_ttl_seconds=config.session_ttl_seconds,
        bootstrap_ttl_seconds=config.bootstrap_ttl_seconds,
        attachment_ttl_seconds=config.attachment_ttl_seconds,
        revalidate_seconds=0.001,  # type: ignore[arg-type]
    )

    async def failed_authenticate(**kwargs):
        del kwargs
        raise RuntimeError("database unavailable")

    sessions.authenticate = failed_authenticate  # type: ignore[method-assign]
    app = CanvasGatewayApp(
        db=db,
        config=config,
        session_service=sessions,  # type: ignore[arg-type]
        registry=CanvasConnectionRegistry(max_connections=4),
    )
    handler = asyncio.create_task(asyncio.Event().wait())
    await app._revalidation_guard(  # noqa: SLF001
        lease=CanvasConnectionLease(session_id=session.id),
        handler=handler,
        session=session,
        session_secret="s" * 43,
    )
    with pytest.raises(asyncio.CancelledError):
        await handler

    disconnected = asyncio.create_task(asyncio.Event().wait())
    await app._disconnect_guard(  # noqa: SLF001
        receive=lambda: _message({"type": "http.disconnect"}),
        handler=disconnected,
        exchange_complete=asyncio.Event(),
    )
    with pytest.raises(asyncio.CancelledError):
        await disconnected


async def _message(value):
    return value


@pytest.mark.asyncio
async def test_ordinary_http_proxies_over_one_direct_channel_without_identity_leak() -> (
    None
):
    config, session, sessions, db = _fixture()
    pool = _FakeTransportPool(
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: text/plain\r\n"
        b"Set-Cookie: app=secret\r\n"
        b"Content-Length: 5\r\n\r\nhello"
    )
    app = CanvasGatewayApp(
        db=db,
        config=config,
        session_service=sessions,  # type: ignore[arg-type]
        registry=CanvasConnectionRegistry(max_connections=4),
        transport_pool=pool,  # type: ignore[arg-type]
        key_resolver=lambda: "/tmp/test-key",
    )
    async with _client(app, session) as client:
        response = await client.get(
            "/page?q=one",
            headers={
                "Cookie": (
                    "__Host-canvas_session=" + "s" * 43 + "; srw_session=platform"
                ),
                "Authorization": "Bearer platform",
                "X-Forwarded-For": "203.0.113.10",
            },
        )
    assert response.status_code == 200
    assert response.content == b"hello"
    assert response.headers["cache-control"] == "private, no-store"
    assert "set-cookie" not in response.headers
    upstream = bytes(pool.writer.data).lower()
    assert b"get /page?q=one http/1.1" in upstream
    assert b"host: " + session.origin_generation.hex.encode() not in upstream
    assert str(session.origin_generation).encode() in upstream
    assert b"srw_session" not in upstream
    assert b"authorization" not in upstream
    assert b"x-forwarded-for" not in upstream
    assert pool.writer.closed
    assert len(pool.calls) == 1
    generation = await pool.calls[0]["generation_resolver"]()
    assert generation == db.thread
    assert db.thread_queries == [
        (
            "SELECT id, user_id, metadata FROM threads WHERE id = $1",
            session.thread_id,
        )
    ]
