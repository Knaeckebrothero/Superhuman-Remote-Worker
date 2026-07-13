"""Dedicated isolated-origin ASGI gateway for Dynamic Canvas live apps.

This process intentionally contains no platform API, BFF, authentication, or
Cockpit routes. Wildcard Canvas DNS must target only this application/service;
an untrusted host can therefore never fall through to the normal orchestrator.
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, suppress
from datetime import UTC, datetime
import inspect
import json
import logging
import re
import secrets
from typing import Any, Awaitable, Callable
from uuid import UUID

from database.postgres import PostgresDB
from services import resolve_ssh_key_path
from services.canvas import WorkspaceAppSource
from services.canvas_http import open_canvas_http_exchange, spool_canvas_request_body
from services.canvas_proxy_policy import (
    CanvasProxyError,
    CanvasProxyLimits,
    CanvasPublicOrigin,
    canvas_proxy_limits_from_env,
    validate_canvas_request,
)
from services.canvas_session_notifications import (
    CANVAS_CONNECTION_REGISTRY,
    CanvasConnectionLease,
    CanvasConnectionRegistry,
    CanvasSessionNotificationListener,
)
from services.canvas_ssh import (
    PINNED_SSH_TRANSPORT_POOL,
    CanvasDirectChannelUnavailable,
    CanvasSSHError,
    PinnedSSHTransportPool,
)
from services.canvas_viewer_config import (
    CanvasViewerConfig,
    CanvasViewerConfigurationError,
    canvas_viewer_config,
)
from services.canvas_viewer_sessions import (
    CANVAS_VIEWER_COOKIE,
    CanvasOriginSession,
    CanvasViewerError,
    CanvasViewerSessionService,
)

logger = logging.getLogger(__name__)

_BOOTSTRAP_PATH = b"/_canvas/bootstrap"
_BOOTSTRAP_TOKEN = re.compile(rb"^[A-Za-z0-9_-]{32,128}$")
_COOKIE_NAME = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_NO_STORE_HEADERS = (
    (b"cache-control", b"private, no-store"),
    (b"referrer-policy", b"no-referrer"),
    (b"x-content-type-options", b"nosniff"),
)

SessionFactory = Callable[[Any, CanvasViewerConfig], CanvasViewerSessionService]
KeyResolver = Callable[[], str | Awaitable[str]]
InitialAuthorization = Callable[[], Awaitable[Any]]
_MAX_PENDING_AUTHORIZATIONS = 32


def _headers(scope: dict[str, Any]) -> list[tuple[bytes, bytes]]:
    result: list[tuple[bytes, bytes]] = []
    for name, value in scope.get("headers") or []:
        if not isinstance(name, bytes) or not isinstance(value, bytes):
            raise CanvasProxyError(
                400, "canvas_headers_invalid", "Canvas request headers are invalid"
            )
        result.append((name.lower(), value))
    return result


def _single_header(
    headers: list[tuple[bytes, bytes]], name: bytes, *, required: bool = False
) -> bytes | None:
    values = [value for candidate, value in headers if candidate == name]
    if len(values) > 1 or (required and not values):
        raise CanvasProxyError(
            400,
            "canvas_headers_invalid",
            f"Canvas request requires exactly one {name.decode('ascii')} header",
        )
    return values[0] if values else None


def _host_generation(
    headers: list[tuple[bytes, bytes]], config: CanvasViewerConfig
) -> UUID:
    host = _single_header(headers, b"host", required=True)
    assert host is not None
    try:
        value = host.decode("ascii")
        return config.generation_for_host(value)
    except (UnicodeDecodeError, CanvasViewerConfigurationError) as exc:
        raise CanvasProxyError(
            421, "canvas_host_mismatch", "Canvas host is not allocated"
        ) from exc


def _reserved_cookie(headers: list[tuple[bytes, bytes]]) -> str | None:
    raw = _single_header(headers, b"cookie")
    if raw is None:
        return None
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise CanvasProxyError(
            400, "canvas_cookie_invalid", "Canvas cookie header is invalid"
        ) from exc
    values: list[str] = []
    for item in text.split(";"):
        item = item.strip()
        if not item:
            continue
        name, separator, value = item.partition("=")
        if not separator or not _COOKIE_NAME.fullmatch(name):
            raise CanvasProxyError(
                400, "canvas_cookie_invalid", "Canvas cookie header is malformed"
            )
        if name == CANVAS_VIEWER_COOKIE:
            if not _BOOTSTRAP_TOKEN.fullmatch(value.encode("ascii")):
                raise CanvasProxyError(
                    400,
                    "canvas_cookie_invalid",
                    "Canvas viewer cookie is malformed",
                )
            values.append(value)
    if len(values) > 1:
        raise CanvasProxyError(
            400,
            "canvas_cookie_invalid",
            "Reserved Canvas viewer cookie is duplicated",
        )
    return values[0] if values else None


def _bootstrap_token(raw_query: bytes) -> str:
    if len(raw_query) > 256 or not raw_query.startswith(b"token=") or b"&" in raw_query:
        raise CanvasProxyError(
            400, "canvas_bootstrap_invalid", "Canvas bootstrap request is invalid"
        )
    token = raw_query[6:]
    if not _BOOTSTRAP_TOKEN.fullmatch(token):
        raise CanvasProxyError(
            400, "canvas_bootstrap_invalid", "Canvas bootstrap request is invalid"
        )
    return token.decode("ascii")


def _frame_ancestors(config: CanvasViewerConfig) -> bytes:
    return (
        "default-src 'none'; frame-ancestors "
        + " ".join(sorted(config.cockpit_origins))
        + "; base-uri 'none'; form-action 'none'"
    ).encode("ascii")


def _bootstrap_headers(
    config: CanvasViewerConfig, *, script_nonce: str
) -> list[tuple[bytes, bytes]]:
    policy = _frame_ancestors(config) + (f"; script-src 'nonce-{script_nonce}'").encode(
        "ascii"
    )
    return [
        *_NO_STORE_HEADERS,
        (b"content-security-policy", policy),
        (
            b"permissions-policy",
            b"camera=(), microphone=(), geolocation=(), payment=(), usb=(), "
            b"serial=(), bluetooth=(), hid=(), midi=(), display-capture=(), "
            b"clipboard-read=(), clipboard-write=(), fullscreen=()",
        ),
    ]


def _require_embedded_navigation(headers: list[tuple[bytes, bytes]]) -> None:
    """Admit only requests initiated by the already-loaded Canvas origin."""

    mode = _single_header(headers, b"sec-fetch-mode")
    destination = _single_header(headers, b"sec-fetch-dest")
    site = _single_header(headers, b"sec-fetch-site")
    if (
        site != b"same-origin"
        or destination == b"document"
        or (mode == b"navigate" and destination != b"iframe")
    ):
        raise CanvasProxyError(
            403,
            "canvas_embedding_required",
            "Canvas applications must remain inside the trusted Cockpit frame",
        )


def _session_cookie(secret: str, expires_at: datetime) -> bytes:
    seconds = max(
        0,
        int((expires_at.astimezone(UTC) - datetime.now(UTC)).total_seconds()),
    )
    return (
        f"{CANVAS_VIEWER_COOKIE}={secret}; Path=/; Max-Age={seconds}; "
        "Secure; HttpOnly; SameSite=None; Partitioned"
    ).encode("ascii")


async def _request_chunks(receive: Callable[[], Awaitable[dict[str, Any]]]):
    while True:
        message = await receive()
        message_type = message.get("type")
        if message_type == "http.disconnect":
            raise asyncio.CancelledError
        if message_type != "http.request":
            raise CanvasProxyError(
                400, "canvas_request_invalid", "Canvas request body is invalid"
            )
        body = message.get("body", b"")
        if not isinstance(body, bytes):
            raise CanvasProxyError(
                400, "canvas_request_invalid", "Canvas request body is invalid"
            )
        if body:
            yield body
        if not message.get("more_body", False):
            return


async def _send_response(
    send: Callable[[dict[str, Any]], Awaitable[None]],
    *,
    status: int,
    headers: list[tuple[bytes, bytes]] | tuple[tuple[bytes, bytes], ...],
    body: bytes = b"",
) -> None:
    complete_headers = list(headers)
    if not any(name == b"content-length" for name, _ in complete_headers):
        complete_headers.append((b"content-length", str(len(body)).encode("ascii")))
    await send(
        {"type": "http.response.start", "status": status, "headers": complete_headers}
    )
    await send({"type": "http.response.body", "body": body, "more_body": False})


async def _send_error(
    send: Callable[[dict[str, Any]], Awaitable[None]],
    error: CanvasProxyError | CanvasViewerError,
    config: CanvasViewerConfig | None,
    *,
    clear_cookie: bool = False,
) -> None:
    body = json.dumps(
        {"detail": {"code": error.code, "message": error.message}},
        separators=(",", ":"),
    ).encode("utf-8")
    headers = [
        (b"content-type", b"application/json"),
        *_NO_STORE_HEADERS,
    ]
    if config is not None and config.enabled:
        # Bootstrap errors must still be embeddable only by trusted Cockpit
        # parents. Ordinary errors inherit the same narrower response policy.
        headers.append((b"content-security-policy", _frame_ancestors(config)))
    if clear_cookie:
        headers.append(
            (
                b"set-cookie",
                (
                    f"{CANVAS_VIEWER_COOKIE}=; Path=/; Max-Age=0; Secure; "
                    "HttpOnly; SameSite=None; Partitioned"
                ).encode("ascii"),
            )
        )
    await _send_response(send, status=error.status_code, headers=headers, body=body)


class CanvasGatewayApp:
    """Minimal ASGI app serving only isolated Canvas hosts."""

    def __init__(
        self,
        *,
        db: Any | None = None,
        config: CanvasViewerConfig | None = None,
        session_service: CanvasViewerSessionService | None = None,
        registry: CanvasConnectionRegistry | None = None,
        transport_pool: PinnedSSHTransportPool | None = None,
        key_resolver: KeyResolver | None = None,
        limits: CanvasProxyLimits | None = None,
        authorization_limit: asyncio.Semaphore | None = None,
    ) -> None:
        self._db = db or PostgresDB()
        self._owns_db = db is None
        self._config = config
        self._session_service = session_service
        self._registry = registry or CANVAS_CONNECTION_REGISTRY
        self._transport_pool = transport_pool or PINNED_SSH_TRANSPORT_POOL
        self._key_resolver = key_resolver or resolve_ssh_key_path
        self._limits = limits or canvas_proxy_limits_from_env()
        self._authorization_limit = authorization_limit or asyncio.Semaphore(
            _MAX_PENDING_AUTHORIZATIONS
        )
        self._listener: CanvasSessionNotificationListener | None = None

    def _service(self) -> CanvasViewerSessionService:
        assert self._config is not None
        if self._session_service is None:
            self._session_service = CanvasViewerSessionService(
                self._db, config=self._config
            )
        return self._session_service

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        scope_type = scope.get("type")
        if scope_type == "lifespan":
            await self._lifespan(receive, send)
            return
        if scope_type == "websocket":
            await send({"type": "websocket.close", "code": 4404})
            return
        if scope_type != "http":
            return
        await self._http(scope, receive, send)

    async def _lifespan(self, receive: Any, send: Any) -> None:
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                try:
                    config = self._config or canvas_viewer_config()
                    if not config.enabled:
                        raise CanvasViewerConfigurationError(
                            "Canvas viewer gateway cannot start while disabled"
                        )
                    self._config = config
                    if self._owns_db:
                        await self._db.connect()
                    # Fail/retry until the authoritative orchestrator has
                    # applied the viewer-session migration.
                    async with self._db.acquire() as conn:
                        present = await conn.fetchval(
                            "SELECT to_regclass('public.canvas_origin_sessions')"
                        )
                    if present is None:
                        raise RuntimeError("Canvas viewer schema is not installed")
                    self._service()
                    self._listener = CanvasSessionNotificationListener(
                        self._db, self._registry
                    )
                    self._listener.start()
                except Exception as exc:
                    logger.exception("Canvas gateway startup failed")
                    await send(
                        {
                            "type": "lifespan.startup.failed",
                            "message": str(exc),
                        }
                    )
                    return
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await self._registry.close_all()
                if self._listener is not None:
                    await self._listener.stop()
                await self._transport_pool.close_all()
                if self._owns_db:
                    await self._db.disconnect()
                await send({"type": "lifespan.shutdown.complete"})
                return

    async def _http(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        config = self._config
        if config is None or not config.enabled:
            await _send_error(
                send,
                CanvasProxyError(
                    503, "canvas_viewer_disabled", "Canvas viewer is disabled"
                ),
                config,
            )
            return
        started = False
        clear_cookie = False

        def mark_started() -> None:
            nonlocal started
            started = True

        try:
            headers = _headers(scope)
            generation = _host_generation(headers, config)
            raw_path = scope.get("raw_path")
            if not isinstance(raw_path, bytes):
                raise CanvasProxyError(
                    503,
                    "canvas_raw_path_unavailable",
                    "The serving edge did not preserve the raw request path",
                )
            if raw_path == _BOOTSTRAP_PATH:
                await self._bootstrap(
                    scope, receive, send, headers=headers, generation=generation
                )
                return
            if raw_path.lower().startswith(b"/_canvas"):
                raise CanvasProxyError(
                    404, "canvas_control_not_found", "Canvas control path not found"
                )
            _require_embedded_navigation(headers)

            origin = CanvasPublicOrigin(config.host_for_origin(generation))
            validated = validate_canvas_request(
                method=str(scope.get("method") or ""),
                raw_path=raw_path,
                raw_query=scope.get("query_string") or b"",
                headers=headers,
                public_origin=origin,
                limits=self._limits,
                reserved_cookie_name=CANVAS_VIEWER_COOKIE,
            )
            if validated.viewer_cookie is None:
                raise CanvasViewerError(
                    401,
                    "canvas_session_required",
                    "Canvas viewer session is required",
                )
            try:
                session = await self._initial_authorization(
                    lambda: self._service().authenticate(
                        session_secret=validated.viewer_cookie,
                        host_generation=generation,
                    )
                )
            except CanvasViewerError as exc:
                # The cookie is shared by every frame/tab in this origin
                # partition. Clear it only when the credential itself is
                # invalid, never for capacity, framing, or upstream failures.
                clear_cookie = exc.status_code == 401
                raise
            async with self._registry.register(session.id) as lease:
                handler = asyncio.current_task()
                assert handler is not None
                guard = asyncio.create_task(
                    self._revalidation_guard(
                        lease=lease,
                        handler=handler,
                        session=session,
                        session_secret=validated.viewer_cookie,
                    ),
                    name=f"canvas-session-guard-{session.id}",
                )
                try:
                    body = await spool_canvas_request_body(
                        _request_chunks(receive),
                        declared_content_length=validated.declared_content_length,
                        limits=self._limits,
                    )
                    exchange_complete = asyncio.Event()
                    disconnect = asyncio.create_task(
                        self._disconnect_guard(
                            receive=receive,
                            handler=handler,
                            exchange_complete=exchange_complete,
                        ),
                        name=f"canvas-client-disconnect-{session.id}",
                    )
                    try:
                        try:
                            await self._proxy(
                                send=send,
                                validated=validated,
                                body=body,
                                origin=origin,
                                session=session,
                                mark_started=mark_started,
                                exchange_complete=exchange_complete,
                            )
                        finally:
                            disconnect.cancel()
                            with suppress(asyncio.CancelledError):
                                await disconnect
                    finally:
                        body.close()
                finally:
                    guard.cancel()
                    with suppress(asyncio.CancelledError):
                        await guard
        except asyncio.CancelledError:
            # Revocation and client disconnect both actively tear down the
            # ASGI/SSH exchange. Never convert either into a replayable body.
            raise
        except (CanvasProxyError, CanvasViewerError) as exc:
            if started:
                raise RuntimeError("Canvas stream failed after response start") from exc
            await _send_error(send, exc, config, clear_cookie=clear_cookie)
        except (CanvasSSHError, CanvasDirectChannelUnavailable) as exc:
            if started:
                raise RuntimeError(
                    "Canvas SSH stream failed after response start"
                ) from exc
            error = CanvasProxyError(
                503,
                "canvas_upstream_unavailable",
                "Workspace application is unavailable",
            )
            await _send_error(send, error, config)
        except Exception:
            logger.exception("Canvas gateway request failed (query redacted)")
            if started:
                raise
            await _send_error(
                send,
                CanvasProxyError(
                    500, "canvas_gateway_error", "Canvas gateway request failed"
                ),
                config,
            )

    async def _bootstrap(
        self,
        scope: dict[str, Any],
        receive: Any,
        send: Any,
        *,
        headers: list[tuple[bytes, bytes]],
        generation: UUID,
    ) -> None:
        assert self._config is not None
        if scope.get("method") != "GET":
            raise CanvasProxyError(
                405,
                "canvas_bootstrap_method_invalid",
                "Canvas bootstrap requires GET",
            )
        if (
            _single_header(headers, b"sec-fetch-dest") != b"iframe"
            or _single_header(headers, b"sec-fetch-mode") != b"navigate"
            or _single_header(headers, b"sec-fetch-site") != b"cross-site"
        ):
            raise CanvasProxyError(
                403,
                "canvas_bootstrap_embedding_required",
                "Canvas bootstrap requires an embedded Cockpit navigation",
            )
        raw_path = scope.get("raw_path")
        if raw_path != _BOOTSTRAP_PATH:
            raise CanvasProxyError(
                404, "canvas_control_not_found", "Canvas control path not found"
            )
        token = _bootstrap_token(scope.get("query_string") or b"")
        existing = _reserved_cookie(headers)

        # A GET bootstrap never accepts a request body. Check ASGI framing
        # without consuming the one-time token on a malformed exchange.
        content_length = _single_header(headers, b"content-length")
        if (
            content_length not in {None, b"0"}
            or _single_header(headers, b"transfer-encoding") is not None
        ):
            raise CanvasProxyError(
                400,
                "canvas_bootstrap_invalid",
                "Canvas bootstrap request body is not allowed",
            )
        exchange = await self._initial_authorization(
            lambda: self._service().consume_bootstrap(
                token=token,
                host_generation=generation,
                existing_session_secret=existing,
            )
        )
        script_nonce = secrets.token_urlsafe(18)
        target = (
            json.dumps(exchange.entry_path, ensure_ascii=True)
            .replace("<", r"\u003c")
            .replace(">", r"\u003e")
            .replace("&", r"\u0026")
        )
        body = (
            "<!doctype html><meta charset=utf-8>"
            "<title>Opening Canvas</title>"
            f'<script nonce="{script_nonce}">location.replace({target})</script>'
        ).encode("ascii")
        response_headers = [
            *_bootstrap_headers(self._config, script_nonce=script_nonce),
            (b"content-type", b"text/html; charset=utf-8"),
        ]
        if exchange.session_secret is not None:
            response_headers.append(
                (
                    b"set-cookie",
                    _session_cookie(
                        exchange.session_secret,
                        exchange.cookie_expires_at or exchange.session.expires_at,
                    ),
                )
            )
        # A trusted same-origin transition document is deliberate. Its
        # navigation makes subsequent app requests `Sec-Fetch-Site:
        # same-origin`; requiring that signal prevents browsers which silently
        # ignore `Partitioned` from sending an unpartitioned viewer cookie on
        # attacker-site GET/HEAD subrequests.
        await _send_response(send, status=200, headers=response_headers, body=body)

    async def _key_path(self) -> str:
        value = self._key_resolver()
        if inspect.isawaitable(value):
            value = await value
        if not isinstance(value, str) or not value.strip():
            raise CanvasSSHError(
                503, "workspace_unavailable", "Workspace SSH key is not configured"
            )
        return value.strip()

    async def _initial_authorization(self, operation: InitialAuthorization) -> Any:
        """Bound public bootstrap/cookie database work before registry admission."""

        if self._authorization_limit.locked():
            raise CanvasViewerError(
                503,
                "canvas_authorization_capacity_exhausted",
                "Canvas viewer authorization capacity is currently exhausted",
            )
        await self._authorization_limit.acquire()
        try:
            try:
                async with asyncio.timeout(self._limits.connect_timeout_seconds):
                    return await operation()
            except TimeoutError as exc:
                raise CanvasViewerError(
                    503,
                    "canvas_authorization_unavailable",
                    "Canvas viewer authorization is currently unavailable",
                ) from exc
        finally:
            self._authorization_limit.release()

    async def _proxy(
        self,
        *,
        send: Any,
        validated: Any,
        body: Any,
        origin: CanvasPublicOrigin,
        session: CanvasOriginSession,
        mark_started: Callable[[], None],
        exchange_complete: asyncio.Event,
    ) -> None:
        source = session.record.source
        if not isinstance(source, WorkspaceAppSource) or session.remote_target is None:
            raise CanvasViewerError(
                401,
                "canvas_session_invalid",
                "Canvas viewer session is no longer valid",
            )
        key_path = await self._key_path()

        async def generation_resolver() -> dict[str, Any]:
            current = await self._db.get_thread(str(session.thread_id))
            return current or {}

        async with AsyncExitStack() as stack:
            try:
                async with asyncio.timeout(self._limits.connect_timeout_seconds):
                    reader, writer = await stack.enter_async_context(
                        self._transport_pool.open_loopback_connection(
                            target=session.remote_target,
                            destination_port=source.entry_port,
                            key_path=key_path,
                            generation_resolver=generation_resolver,
                        )
                    )
            except TimeoutError as exc:
                raise CanvasProxyError(
                    504,
                    "canvas_upstream_timeout",
                    "Workspace application connection timed out",
                ) from exc
            async with open_canvas_http_exchange(
                reader=reader,
                writer=writer,
                request=validated.prepare_upstream(body.size),
                body=body,
                public_origin=origin,
                cockpit_origins=tuple(sorted(self._config.cockpit_origins)),
                entry_port=source.entry_port,
                limits=self._limits,
            ) as response:
                await send(
                    {
                        "type": "http.response.start",
                        "status": response.status_code,
                        "headers": list(response.headers),
                    }
                )
                mark_started()
                async for chunk in response.aiter_bytes():
                    await send(
                        {
                            "type": "http.response.body",
                            "body": chunk,
                            "more_body": True,
                        }
                    )
                # The upstream/direct channel has terminated. Mark this before
                # the final downstream send so an ASGI server which reports a
                # normal post-response disconnect cannot cancel the handler.
                exchange_complete.set()
                await send(
                    {"type": "http.response.body", "body": b"", "more_body": False}
                )

    async def _revalidation_guard(
        self,
        *,
        lease: CanvasConnectionLease,
        handler: asyncio.Task[Any],
        session: CanvasOriginSession,
        session_secret: str,
    ) -> None:
        assert self._config is not None
        expiry = session.expires_at
        interval = float(self._config.revalidate_seconds)
        auth_budget = min(
            interval,
            self._limits.connect_timeout_seconds,
            max(0.05, interval / 3),
        )
        wait_budget = max(0.0, interval - auth_budget)
        try:
            while True:
                if lease.cancelled.is_set():
                    handler.cancel()
                    return
                until_expiry = max(0.0, (expiry - datetime.now(UTC)).total_seconds())
                if until_expiry <= 0:
                    handler.cancel()
                    return
                wake_after = min(wait_budget, until_expiry)
                if wake_after > 0:
                    try:
                        await asyncio.wait_for(
                            lease.cancelled.wait(), timeout=wake_after
                        )
                    except TimeoutError:
                        pass
                    else:
                        handler.cancel()
                        return
                remaining_expiry = max(
                    0.0, (expiry - datetime.now(UTC)).total_seconds()
                )
                if remaining_expiry <= 0:
                    handler.cancel()
                    return
                try:
                    async with asyncio.timeout(min(auth_budget, remaining_expiry)):
                        current = await self._service().authenticate(
                            session_secret=session_secret,
                            host_generation=session.origin_generation,
                        )
                except CanvasViewerError:
                    handler.cancel()
                    return
                expiry = current.expires_at
        except asyncio.CancelledError:
            raise
        except Exception:
            # An authorization-store failure must never silently disable the
            # guard for an already-open upload/channel/response.
            logger.warning(
                "Canvas session revalidation failed closed for %s",
                session.id,
                exc_info=True,
            )
            handler.cancel()

    @staticmethod
    async def _disconnect_guard(
        *,
        receive: Any,
        handler: asyncio.Task[Any],
        exchange_complete: asyncio.Event,
    ) -> None:
        """Cancel upstream work when the browser leaves after body spooling."""

        try:
            message = await receive()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Canvas disconnect watcher failed closed", exc_info=True)
            handler.cancel()
            return
        if exchange_complete.is_set():
            return
        if message.get("type") != "http.disconnect":
            logger.warning("Canvas received unexpected post-body ASGI message")
        handler.cancel()


app = CanvasGatewayApp()


__all__ = ["CanvasGatewayApp", "app"]
